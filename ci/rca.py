#!/usr/bin/env python3
"""
RCA (Root Cause Analysis) integration for the Agentic SDLC pipeline.

Uses LambdaTest AI RCA API:
  POST https://api.lambdatest.com/insights/api/v3/public/rca/generate
    body: {"job_ids": ["<he_job_id>"]}   ← trigger by HE job (simplest, no session lookup needed)

  GET  https://api.lambdatest.com/automation/api/v1/sessions?build_name=<name>&limit=50
    → list sessions for a build → map to SC IDs → fetch per-session RCA

Flow:
  1. trigger_rca_for_job(job_id)   → LT AI generates RCA for all failed tests in job
  2. wait ~30s for async generation
  3. fetch_sessions_for_build(build_name) → session list with links
  4. fetch_rca_for_sessions(sessions) → sc_id → rca_text
  5. summarize with Claude (Haiku — fast + cheap)
  6. save ci/rca_results.json

Called by traceability.run_traceability() after each pipeline run.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

CI_DIR       = Path(__file__).parent
RCA_FILE     = CI_DIR / "rca_results.json"

LT_USERNAME   = os.environ.get("LT_USERNAME", "gagandeepb")
LT_ACCESS_KEY = os.environ.get("LT_ACCESS_KEY", "")

RCA_TRIGGER_URL = "https://api.lambdatest.com/insights/api/v3/public/rca/generate"
SESSIONS_URL    = "https://api.lambdatest.com/automation/api/v1/sessions"
SESSION_RCA_URL = "https://api.lambdatest.com/automation/api/v1/sessions/{sid}/rca"

# Build name used by Flow 1 HyperExecute yaml
FLOW1_BUILD_NAME = "Agentic SDLC | KaneAI Export"


def _auth_header() -> str:
    return "Basic " + base64.b64encode(f"{LT_USERNAME}:{LT_ACCESS_KEY}".encode()).decode()


def _request(method: str, url: str, payload: dict = None) -> dict:
    data = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(
        url, data=data,
        headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        print(f"[rca] HTTP {e.code} on {method} {url}: {body}", file=sys.stderr)
        # None signals permanent failure (404/403) so callers stop retrying
        if e.code in (404, 403):
            return None
        return {}
    except Exception as e:
        print(f"[rca] {method} {url} error: {e}", file=sys.stderr)
        return {}


# ── Step 1: Trigger RCA ───────────────────────────────────────────────────────

def trigger_rca_for_job(job_id: str, log=None) -> int:
    """
    POST /insights/api/v3/public/rca/generate with job_ids=[job_id].
    Returns number of tests triggered. Zero if already generated or error.
    """
    if not LT_ACCESS_KEY or not job_id:
        return 0

    msg = f"[rca] Triggering AI RCA for HE job {job_id}..."
    if log:
        log.info(msg)
    else:
        print(msg)

    resp = _request("POST", RCA_TRIGGER_URL, {"job_ids": [job_id]})
    data = resp.get("data", {})
    triggered = data.get("triggered_count", 0)
    skipped   = data.get("skipped_count", 0)

    msg = f"[rca] triggered={triggered}  skipped={skipped}"
    if log:
        log.info(msg)
    else:
        print(msg)

    return triggered


# ── Step 2: Fetch sessions for a build ───────────────────────────────────────

def fetch_sessions_for_build(build_name: str, log=None) -> list:
    """
    GET /automation/api/v1/sessions?build_name=<name>&limit=50
    Returns list of {session_id, name, status, session_link}.
    """
    if not LT_ACCESS_KEY:
        return []

    import urllib.parse
    url = f"{SESSIONS_URL}?build_name={urllib.parse.quote(build_name)}&limit=50"
    resp = _request("GET", url)
    raw  = resp.get("data", {})
    # API returns {"data": {"sessions": [...]}} or {"data": [...]}
    sessions = raw.get("sessions", raw) if isinstance(raw, dict) else raw
    if not isinstance(sessions, list):
        return []

    result = []
    for s in sessions:
        name   = s.get("name", "") or s.get("test_name", "")
        sid    = s.get("session_id", "") or s.get("id", "")
        status = s.get("status_ind", s.get("status", ""))
        link   = s.get("session_url", "") or s.get("public_url", "")
        # Client-side filter — API may return all account sessions regardless of build_name param
        s_build = s.get("build_name", "") or s.get("build", "")
        if build_name and s_build and build_name.lower() not in s_build.lower():
            continue
        result.append({"session_id": sid, "name": name, "status": status, "session_link": link})

    if log:
        log.info(f"[rca] Found {len(result)} sessions for build '{build_name}'")
    return result


# ── Step 3: Map session name → SC ID ─────────────────────────────────────────

def _session_to_sc_id(session_name: str) -> str:
    """
    Heuristic: extract SC-NNN from session name.
    e.g. "test_sc001_add_to_cart" → "SC-001"
         "SC-002: Cart shows..."  → "SC-002"
         "Add MacBook to Cart"    → "" (no match)
    """
    m = re.search(r'SC[-_]?0*(\d+)', session_name, re.IGNORECASE)
    if m:
        return f"SC-{int(m.group(1)):03d}"
    return ""


# ── Step 4: Fetch per-session RCA ────────────────────────────────────────────

def _fetch_session_rca(session_id: str):
    """GET /automation/api/v1/sessions/{id}/rca → RCA summary text, or None on 404."""
    if not session_id:
        return ""
    url  = SESSION_RCA_URL.format(sid=session_id)
    resp = _request("GET", url)
    if resp is None:
        return None  # 404/403 — session not found, stop polling
    d = resp.get("data", {})
    return (d.get("rca_summary") or d.get("summary") or d.get("message") or "")


def _summarize(raw: str, sc_id: str) -> str:
    """Condense raw RCA into 2-3 bullets using Claude Haiku."""
    if not raw:
        return ""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return raw[:300]
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": (
                f"Summarise this test failure RCA for {sc_id} into exactly 2-3 bullet points:\n"
                "• What failed (symptom)\n• Why it failed (root cause)\n• Suggested fix\n"
                f"Be concise — ≤20 words each. No preamble.\n\nRAW:\n{raw[:2000]}"
            )}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[rca] Claude summarize error: {e}", file=sys.stderr)
        return raw[:300]


# ── Flow 2: update run_history with actual HE results ────────────────────────

HISTORY_FILE = CI_DIR / "run_history.json"

def update_history_from_he(build_name: str, flow: str = "flow2", log=None) -> dict:
    """
    Fetch HE session results for a build and overwrite run_history.json with
    actual HE pass/fail — so the traceability matrix reflects execution truth,
    not just kane-cli authoring status.

    Called by flow1/flow2 pipelines after Phase 3 (HE job completes).
    Retries up to 3 times with 20s delay to allow LT API to index sessions.
    """
    if not LT_ACCESS_KEY:
        return {}

    # LT Automation API may take a few seconds to index sessions after HE finishes
    sessions = []
    for attempt in range(1, 4):
        sessions = fetch_sessions_for_build(build_name, log)
        if sessions:
            break
        msg = f"[rca] No sessions found for build '{build_name}' (attempt {attempt}/3) — waiting 20s..."
        print(msg) if not log else log.warning(msg)
        time.sleep(20)

    if not sessions:
        msg = f"[rca] No sessions found for build '{build_name}' after retries — run_history unchanged"
        print(msg) if not log else log.warning(msg)
        return {}

    # Log what the API returned so we can diagnose mapping failures
    _log = lambda m: print(m) if not log else log.info(m)
    _log(f"[rca] Found {len(sessions)} session(s) for build '{build_name}'")
    for s in sessions:
        sc_id = _session_to_sc_id(s["name"])
        _log(f"[rca]   session '{s['name']}' → sc_id='{sc_id}' status={s['status']}")

    history = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else {}

    updated = {}
    for s in sessions:
        sc_id = _session_to_sc_id(s["name"])
        if not sc_id:
            continue
        he_status = "passed" if s["status"] == "passed" else "failed"
        if sc_id in history:
            history[sc_id]["status"]       = he_status
            history[sc_id]["flow"]         = flow
            history[sc_id]["session_link"] = s.get("session_link", "")
            # Preserve Phase 1 failure_detail — HE failures have no detail of their own.
            # If authoring passed (detail empty) mark source so healing skips it.
            if he_status == "failed" and not history[sc_id].get("failure_detail", "").strip():
                history[sc_id]["failure_detail"] = f"[HE execution failed — session: {s.get('session_link', '')}]"
        updated[sc_id] = he_status

    if updated:
        HISTORY_FILE.write_text(json.dumps(history, indent=2))
        msg = f"[rca] Updated run_history with HE results: {updated}"
        print(msg) if not log else log.info(msg)
    else:
        msg = f"[rca] Sessions found but no SC IDs mapped — check session names above"
        print(msg) if not log else log.warning(msg)

    return updated


# ── Main public function ──────────────────────────────────────────────────────

def _poll_session_rca(session_id: str, sc_id: str, timeout: int = 120, interval: int = 10, log=None) -> str:
    """
    Poll per-session RCA endpoint until data arrives or timeout.
    Returns rca text (empty string if timeout reached without data).
    """
    _log = lambda m: (log.info(m) if log else print(m))
    elapsed = 0
    while elapsed < timeout:
        raw = _fetch_session_rca(session_id)
        if raw is None:
            _log(f"[rca] {sc_id} RCA session not found (404) — skipping")
            return ""
        if raw:
            _log(f"[rca] {sc_id} RCA received after {elapsed}s")
            return raw
        _log(f"[rca] {sc_id} RCA not ready (waited {elapsed}s) — retrying in {interval}s...")
        time.sleep(interval)
        elapsed += interval
    _log(f"[rca] {sc_id} RCA timed out after {timeout}s — no data available")
    return ""



HISTORY_FILE = CI_DIR / "run_history.json"


def _claude_rca_from_history(sc_ids: list, log=None) -> dict:
    """
    Generate RCA for failed SCs using Claude when LT AI RCA is unavailable.
    Reads failure_detail from run_history.json and summarises per SC.
    Returns {sc_id: rca_text}.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not HISTORY_FILE.exists():
        return {}

    history = json.loads(HISTORY_FILE.read_text())
    results = {}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except Exception:
        return {}

    for sc_id in sc_ids:
        info = history.get(sc_id, {})
        if info.get("status") == "passed":
            continue
        detail = info.get("failure_detail", "")
        objective = info.get("objective", "")
        if not detail:
            continue

        # Extract run_end narrative vs raw tail if embedded by run_kane()
        run_summary = ""
        raw_tail = detail
        if "[run summary]:" in detail:
            parts = detail.split("\n[raw tail]:", 1)
            run_summary = parts[0].replace("[run summary]:", "").strip()
            raw_tail = parts[1].strip() if len(parts) > 1 else ""

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": (
                    f"kane-cli failed to author a browser test for {sc_id}.\n\n"
                    f"**Objective given to kane-cli:**\n{objective[:300]}\n\n"
                    f"**What kane-cli did (run summary):**\n{run_summary or raw_tail[-600:]}\n\n"
                    "Respond in exactly this format — no other text:\n"
                    "**Objective:** <one line restating what was asked>\n"
                    "**What CLI did:** <one line — what kane-cli actually attempted or where it got stuck>\n"
                    "**What needs to be done:** <one line — concrete fix to the objective or test>"
                )}],
            )
            results[sc_id] = resp.content[0].text.strip()
            msg = f"[rca] {sc_id} Claude RCA → {results[sc_id][:80]}..."
            print(msg) if not log else log.info(msg)
        except Exception as e:
            msg = f"[rca] {sc_id} Claude RCA error: {e}"
            print(msg) if not log else log.warning(msg)

    return results

def run_rca(job_id: str, build_name: str = FLOW1_BUILD_NAME, log=None) -> dict:
    """
    Full RCA pipeline for one HE job.
    Returns {sc_id: rca_text} and saves ci/rca_results.json.

    For each failed session, polls the per-session RCA endpoint until
    data arrives (up to 120s) rather than using a fixed sleep.
    """
    if not LT_ACCESS_KEY or not job_id:
        if log:
            log.warning("[rca] Skipping — LT_ACCESS_KEY or job_id missing")
        return {}

    # Load existing results to preserve previous SCs
    existing = json.loads(RCA_FILE.read_text()) if RCA_FILE.exists() else {}

    # Trigger RCA generation
    triggered = trigger_rca_for_job(job_id, log)

    # Brief initial wait to let LT AI start processing
    if triggered > 0:
        wait_secs = 10
        msg = f"[rca] Waiting {wait_secs}s for AI RCA generation to start..."
        if log:
            log.info(msg)
        else:
            print(msg)
        time.sleep(wait_secs)

    # Fetch sessions for this build (retry up to 3x if not indexed yet)
    sessions = []
    for attempt in range(1, 4):
        sessions = fetch_sessions_for_build(build_name, log)
        if sessions:
            break
        msg = f"[rca] No sessions found for build '{build_name}' (attempt {attempt}/3) — waiting 15s..."
        print(msg) if not log else log.warning(msg)
        time.sleep(15)

    results = dict(existing)

    # If LT AI RCA trigger returned 0 (Flow 2 KaneAI TM sessions not indexed),
    # fall back to Claude-generated RCA from kane-cli failure_detail in run_history
    if triggered == 0:
        msg = "[rca] triggered=0 — LT AI RCA unavailable; generating Claude RCA from failure details"
        print(msg) if not log else log.info(msg)
        failed_sc_ids = [_session_to_sc_id(s["name"]) for s in sessions
                         if s["status"] in ("failed", "error") and _session_to_sc_id(s["name"])]
        # Also include any SC IDs from run_history that failed (in case sessions are filtered out)
        if HISTORY_FILE.exists():
            hist = json.loads(HISTORY_FILE.read_text())
            for sc_id, info in hist.items():
                if info.get("status") != "passed" and sc_id not in failed_sc_ids:
                    failed_sc_ids.append(sc_id)
        claude_rcas = _claude_rca_from_history(failed_sc_ids, log)
        for sc_id, rca_text in claude_rcas.items():
            results[sc_id] = {
                "rca":          rca_text,
                "raw":          "",
                "session_link": next((s["session_link"] for s in sessions
                                      if _session_to_sc_id(s["name"]) == sc_id), ""),
                "session_id":   "",
                "source":       "claude-fallback",
            }
        RCA_FILE.write_text(json.dumps(results, indent=2))
        msg = f"[rca] Saved {len(results)} Claude RCA entries to {RCA_FILE.name}"
        print(msg) if not log else log.info(msg)
        return results

    for s in sessions:
        if s["status"] not in ("failed", "error"):
            continue
        sc_id = _session_to_sc_id(s["name"])
        if not sc_id:
            continue

        # Poll until RCA data arrives for this failed session (up to 2 min)
        raw = _poll_session_rca(s["session_id"], sc_id, timeout=120, interval=10, log=log)
        if not raw:
            continue

        rca_text = _summarize(raw, sc_id)
        results[sc_id] = {
            "rca":          rca_text,
            "raw":          raw[:500],
            "session_link": s["session_link"],
            "session_id":   s["session_id"],
        }
        msg = f"[rca] {sc_id} RCA → {rca_text[:80]}..."
        if log:
            log.info(msg)
        else:
            print(msg)

    RCA_FILE.write_text(json.dumps(results, indent=2))
    msg = f"[rca] Saved {len(results)} RCA entries to {RCA_FILE.name}"
    if log:
        log.info(msg)
    else:
        print(msg)

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id", help="HyperExecute job ID")
    parser.add_argument("--build", default=FLOW1_BUILD_NAME, help="LT build name")
    args = parser.parse_args()
    run_rca(args.job_id, args.build)
