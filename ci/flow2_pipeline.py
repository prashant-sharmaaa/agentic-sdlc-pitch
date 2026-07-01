#!/usr/bin/env python3
"""
Flow 2 — Full End-to-End Pipeline
===================================
Phase 1 : Run kane-cli for each SC objective → creates test cases in TM
Phase 2 : Poll TM project for newly created test cases with code generated
Phase 3 : Create test run, link instances with environment, trigger HyperExecute

Usage:
    python3 ci/flow2_pipeline.py
    LT_ACCESS_KEY=<key> python3 ci/flow2_pipeline.py

Requirements:
    - kane-cli installed and authenticated (kane-cli config show)
    - kane-cli project set to kane-agentic (01KVXJ82AKT83GWJNFZTQVMNRQ)
    - LT_ACCESS_KEY env var set
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# Add ci/ to path for local imports
sys.path.insert(0, str(Path(__file__).parent))
from pipeline_logger import get_logger
from self_heal import load_history, save_history, heal_objectives, heal_single_objective
from traceability import record_he_job, record_tm_test_cases_with_sc, run_traceability
from rca import run_rca, update_history_from_he

log = get_logger("flow2")

# ── Credentials ───────────────────────────────────────────────────────────────
LT_USERNAME   = os.environ.get("LT_USERNAME", "gagandeepb")
LT_ACCESS_KEY = os.environ.get("LT_ACCESS_KEY")
if not LT_ACCESS_KEY:
    print("ERROR: LT_ACCESS_KEY env var not set", file=sys.stderr)
    sys.exit(1)

AUTH = "Basic " + base64.b64encode(f"{LT_USERNAME}:{LT_ACCESS_KEY}".encode()).decode()

# ── Config ────────────────────────────────────────────────────────────────────
TM_API        = "https://test-manager-api.lambdatest.com/api/v1"
HE_API        = "https://test-manager-api.lambdatest.com/api/atm/v1/hyperexecute"
PROJECT_ID     = os.environ.get("TM_PROJECT_ID", "").strip() or "01KVXJ82AKT83GWJNFZTQVMNRQ"
ENVIRONMENT_ID = int(os.environ.get("TM_ENVIRONMENT_ID", "").strip() or "282603")
# BASE_URL: read from analyzed_requirements.json if available, else extract from first objective
def _resolve_base_url() -> str:
    analyzed = Path(__file__).parent.parent / "requirements" / "analyzed_requirements.json"
    if analyzed.exists():
        try:
            data = json.loads(analyzed.read_text())
            url = data.get("base_url", "")
            if url:
                return url.rstrip("/") + "/"
        except Exception:
            pass
    # Fall back: extract URL from first objective string
    obj_file = Path(__file__).parent / "objectives.json"
    if obj_file.exists():
        try:
            objs = json.loads(obj_file.read_text())
            if objs:
                import re
                m = re.search(r'https?://[^\s,]+', objs[0].get("objective", ""))
                if m:
                    return m.group(0).rstrip("/.") + "/"
        except Exception:
            pass
    return "https://www.saucedemo.com/"

BASE_URL = _resolve_base_url()
KANE_TIMEOUT  = 600   # seconds per kane-cli run (10 min — let kane-cli use its own default)
BUILD_NAME    = f"Agentic SDLC | KaneAI Flow2 | {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
# Sessions in LT dashboard use the BUILD env var from hyperexecute.yaml — must match for polling
SESSION_BUILD_NAME = os.environ.get("BUILD", "Agentic SDLC | KaneAI Export")

# ── SC Objectives — loaded from Claude-generated objectives.json if present ───
_OBJECTIVES_FILE = Path(__file__).parent / "objectives.json"
if _OBJECTIVES_FILE.exists():
    SC_OBJECTIVES = json.loads(_OBJECTIVES_FILE.read_text())
    print(f"[config] Loaded {len(SC_OBJECTIVES)} objectives from {_OBJECTIVES_FILE.name}")
else:
    print("[config] Using hardcoded objectives (run ci/generate_objectives.py to generate from requirements)")
    SC_OBJECTIVES = [
        {
            "id":   "SC-001",
            "name": "SC-001: Add to cart changes button to Remove",
            "objective": (
                "Navigate to https://www.saucedemo.com/, type 'standard_user' into the username input, "
                "type 'secret_sauce' into the password input, click the Login button, "
                "click the button with text 'Add to cart' that is directly below the price '$29.99' "
                "under the 'Sauce Labs Backpack' product name, and assert that the same button now reads 'Remove'."
            ),
        },
        {
            "id":   "SC-002",
            "name": "SC-002: Cart page shows item name and price",
            "objective": (
                "Navigate to https://www.saucedemo.com/, type 'standard_user' into the username input, "
                "type 'secret_sauce' into the password input, click the Login button, "
                "click the button with text 'Add to cart' below the price '$29.99' under the 'Sauce Labs Backpack' product name, "
                "then navigate to https://www.saucedemo.com/cart.html and assert that the text 'Sauce Labs Backpack' is visible."
            ),
        },
        {
            "id":   "SC-003",
            "name": "SC-003: Remove item restores Add to cart button",
            "objective": (
                "Navigate to https://www.saucedemo.com/, type 'standard_user' into the username input, "
                "type 'secret_sauce' into the password input, click the Login button, "
                "click the button with text 'Add to cart' below the price '$29.99' under the 'Sauce Labs Backpack' product name, "
                "click the 'Remove' button that replaced it, and assert the button now reads 'Add to cart'."
            ),
        },
        {
            "id":   "SC-004",
            "name": "SC-004: Sort by price shows cheapest product first",
            "objective": (
                "Navigate to https://www.saucedemo.com/, type 'standard_user' into the username input, "
                "type 'secret_sauce' into the password input, click the Login button, "
                "click the sort dropdown and select 'Price (low to high)', "
                "and assert that the first product tile shows the price $7.99."
            ),
        },
        {
            "id":   "SC-005",
            "name": "SC-005: Catalog page loads with product listings",
            "objective": (
                "Navigate to https://www.saucedemo.com/, type 'standard_user' into the username input, "
                "type 'secret_sauce' into the password input, click the Login button, "
                "and assert that the text 'Sauce Labs Backpack' is visible on the inventory page."
            ),
        },
    ]


# ── Helpers ───────────────────────────────────────────────────────────────────

def api_request(method, url, payload=None):
    data = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(
        url, data=data,
        headers={"Authorization": AUTH, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  HTTP {e.code} on {method} {url}: {body}", file=sys.stderr)
        raise


def tm_request(method, path, payload=None):
    return api_request(method, f"{TM_API}{path}", payload)


def run_kane(sc):
    """Run a single kane-cli objective. Streams NDJSON events in real-time. Returns (status, session_id, failure_detail)."""
    import threading

    sc_id  = sc["id"]
    obj    = sc["objective"]
    log.info(f"[{sc_id}] Objective: {obj}")

    cmd = [
        "kane-cli", "run", obj,
        "--url", BASE_URL,
        "--agent",
        "--headless",
        "--code-export",
        "--code-language", "python",
        "--skip-code-validation",
        "--timeout", str(KANE_TIMEOUT),
    ]

    # ── Streaming helpers ─────────────────────────────────────────────────────
    # Event types to log in real-time (step-level authoring progress)
    _STEP_TYPES = {
        "step_start", "step_end", "action_start", "action_end",
        "navigate", "click", "fill", "select", "type", "scroll",
        "assertion", "verify", "check", "hover", "wait",
        "screenshot", "log", "error", "warning",
    }
    _SKIP_TYPES = {"debug", "trace", "heartbeat", "ping"}

    def _log_event(ev):
        ev_type = ev.get("type", "")
        if ev_type in _SKIP_TYPES or not ev_type:
            return
        if ev_type == "run_end":
            return  # logged after the loop

        # Build a concise one-liner from the event
        parts = []
        for key in ("description", "message", "text", "action", "selector", "url", "value", "error"):
            val = ev.get(key, "")
            if val and isinstance(val, str):
                parts.append(val[:120])
                break
        status_ev = ev.get("status", "")
        step_no   = ev.get("step_no") or ev.get("step") or ev.get("index", "")
        prefix    = f"step {step_no}: " if step_no else ""
        suffix    = f" [{status_ev}]" if status_ev else ""
        detail    = parts[0] if parts else str(ev)[:100]
        log.info(f"  [{sc_id}] {ev_type}: {prefix}{detail}{suffix}")

    # ── Launch process with timeout guard ────────────────────────────────────
    all_lines = []
    timed_out = False

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception as exc:
        log.failure(sc_id, detail=str(exc))
        return None, None, str(exc)

    def _kill_on_timeout():
        nonlocal timed_out
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(KANE_TIMEOUT, _kill_on_timeout)
    timer.start()

    try:
        log.info(f"  [{sc_id}] --- authoring started ---")
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            all_lines.append(line)
            try:
                ev = json.loads(line)
                _log_event(ev)
            except (json.JSONDecodeError, Exception):
                if line.strip():
                    log.info(f"  [{sc_id}] {line[:120]}")
    finally:
        timer.cancel()

    proc.wait()
    returncode = proc.returncode

    if timed_out:
        detail = f"Timeout after {KANE_TIMEOUT}s"
        log.failure(sc_id, "TIMEOUT", detail=detail)
        return None, None, detail

    # ── Parse final state from collected lines ────────────────────────────────
    status = session_id = session_dir = failure_detail = None
    run_end_ev = None
    events_seen = []

    for line in all_lines:
        try:
            ev = json.loads(line)
            events_seen.append(ev.get("type", "?"))
            if ev.get("type") == "run_end":
                run_end_ev  = ev
                status      = ev.get("status")
                session_dir = ev.get("session_dir", "")
                if session_dir:
                    session_id = Path(session_dir).name
                # kane-cli sometimes exits 1 after assertion passes (code-export finalization)
                if status == "failed" and session_dir:
                    s = ev.get("summary", "").lower()
                    if "passed" in s and any(w in s for w in ["check", "verified", "confirmed", "assert"]):
                        log.info(f"[{sc_id}] assertion passed but kane-cli errored post-check — treating as passed")
                        status = "passed"
                break
        except Exception:
            continue

    log.info(f"  [{sc_id}] --- authoring ended --- exit={returncode} events=[{', '.join(events_seen)}]")

    if status == "passed":
        log.success(sc_id, f"session: {session_id}")
    else:
        if run_end_ev:
            log.info(f"[{sc_id}] run_end: {json.dumps(run_end_ev)[:400]}")

        combined = "\n".join(all_lines).strip()
        tail = combined[-800:] if len(combined) > 800 else combined
        run_end_summary = run_end_ev.get("summary", "") if run_end_ev else ""
        if run_end_summary:
            failure_detail = f"[run summary]: {run_end_summary}\n[raw tail]: {tail}"
        else:
            failure_detail = tail if combined else f"No output (exit code {returncode})"
        log.failure(sc_id, detail=failure_detail)

    return status, session_id, failure_detail


def get_testcase_id_from_session(session_id):
    """Read session.json and return testcase_id."""
    if not session_id:
        return None
    sessions_root = Path.home() / ".testmuai" / "kaneai" / "sessions"
    sf = sessions_root / session_id / "session.json"
    if sf.exists():
        d = json.loads(sf.read_text())
        return d.get("testcase_id")
    return None


def fetch_tm_test_cases_by_ids(testcase_ids: list) -> list:
    """
    Fetch TM test cases that match exactly the testcase_ids generated by this pipeline run.
    Uses session.json testcase_id == TM test_case_id (confirmed identical).
    Only includes cases with automation_status=Automated (code generated).
    Returns list of dicts with test_case_id, title, internal_id.
    """
    if not testcase_ids:
        return []

    id_set = set(testcase_ids)
    found  = []
    page   = 1

    while True:
        data  = tm_request("GET", f"/projects/{PROJECT_ID}/test-cases?per_page=50&page={page}")
        cases = data.get("data", [])
        if not cases:
            break

        for tc in cases:
            tc_id = tc.get("test_case_id", "")
            if tc_id in id_set:
                if tc.get("automation_status") == "Automated":
                    found.append({
                        "test_case_id": tc_id,
                        "title":        tc.get("title", tc_id),
                        "internal_id":  tc.get("internal_id", ""),
                    })
                    id_set.discard(tc_id)  # avoid duplicates
                if not id_set:
                    return found

        pagination = data.get("pagination", {})
        if page >= pagination.get("last_page", 1):
            break
        page += 1

    return found


# ── Phase 1: Run kane-cli objectives ─────────────────────────────────────────

_TM_TC_FILE = Path(__file__).parent / "tm_test_cases.json"


def _can_reuse(sc_id: str, objective: str, tm_tcs: dict, history: dict):
    """
    Return the existing TM test case ULID if we can skip kane-cli for this SC.
    Conditions: valid TM test case exists + last authoring passed + objective unchanged.
    """
    tm_id = tm_tcs.get(sc_id, {}).get("tm_id", "")
    if not tm_id:
        return None
    hist = history.get(sc_id, {})
    if hist.get("authoring_status") != "passed":
        return None
    if hist.get("objective", "") != objective:
        return None
    return tm_id


def phase1_run_objectives(objectives=None):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    log.phase("PHASE 1 — Running kane-cli objectives (parallel, max_workers=3)")
    objs = list(objectives or SC_OBJECTIVES)
    results = [None] * len(objs)

    # Load existing TM test cases + history for reuse check
    tm_tcs  = json.loads(_TM_TC_FILE.read_text()) if _TM_TC_FILE.exists() else {}
    history = load_history()

    # Infrastructure failure keywords: only genuine transient browser/CDP issues.
    # Keep narrow — broad matches cause false positives that get stuck on bifurcation.
    _INFRA_KEYWORDS = (
        "screenshot failed",       # explicit screenshot capture crash
        "cdp disconnected",        # Chrome DevTools Protocol lost
        "browser crashed",
        "connection reset by peer",
        "socket hang up",
        "econnreset",
        "net::err_",               # Chrome network errors
        '"recording_state"',       # kane-cli exited before run_end — session rejected/crashed at start
    )

    def _is_infra_failure(detail: str) -> bool:
        d = (detail or "").lower()
        return any(kw in d for kw in _INFRA_KEYWORDS)

    def _run(idx, sc):
        sc_id     = sc["id"]
        objective = sc.get("objective", "")

        # ── Reuse check: skip kane-cli if objective unchanged + TM test case exists ──
        tm_id = _can_reuse(sc_id, objective, tm_tcs, history)
        if tm_id:
            tc_internal = tm_tcs.get(sc_id, {}).get("internal_id", "")
            log.info(f"[{sc_id}] ⏭ Reusing {tc_internal or tm_id} — objective unchanged, skipping kane-cli")
            return idx, {
                "sc_id":          sc_id,
                "objective":      objective,
                "status":         "passed",
                "session_id":     None,
                "testcase_id":    tm_id,
                "failure_detail": "",
                "healed":         False,
                "reused":         True,
            }

        # ── Author with kane-cli ──────────────────────────────────────────────
        log.info(f"[{sc_id}] Authoring with kane-cli (objective changed or no existing test case)")
        status, session_id, failure_detail = run_kane(sc)
        healed = False

        if status != "passed" and failure_detail:
            # Tier 1: transient infra failure (screenshot crash, CDP disconnect, etc.)
            # Retry with the SAME objective — no healing needed
            if _is_infra_failure(failure_detail):
                log.warning(f"[{sc['id']}] transient infra failure detected — retrying with same objective (no heal)")
                status, session_id, failure_detail = run_kane(sc)

            # Tier 2: still failing after infra-retry → logic failure → heal + retry (up to 2 attempts)
            for heal_attempt in range(1, 3):
                if status == "passed" or not failure_detail:
                    break
                log.warning(f"[{sc['id']}] authoring failed — inline heal attempt {heal_attempt}/2")
                original_obj = sc.get("objective", "")
                healed_obj = heal_single_objective(sc, failure_detail, log)
                if not healed_obj:
                    break
                log.info(f"[{sc['id']}] ── inline heal objective comparison (attempt {heal_attempt}) ──")
                log.info(f"[{sc['id']}]   BEFORE: {original_obj}")
                log.info(f"[{sc['id']}]   AFTER : {healed_obj}")
                sc_retry = {**sc, "objective": healed_obj}
                s2, sid2, fd2 = run_kane(sc_retry)
                if s2 == "passed":
                    log.info(f"[{sc['id']}] retry PASSED with healed objective (attempt {heal_attempt}) ✓")
                    status, session_id, failure_detail = s2, sid2, fd2
                    sc = sc_retry
                    healed = True
                    break
                else:
                    log.warning(f"[{sc['id']}] heal attempt {heal_attempt} also failed — {'trying one more heal' if heal_attempt == 1 else 'giving up'}")
                    # Use fresh failure detail from this attempt for the next heal iteration
                    failure_detail = fd2 or failure_detail
                    sc = sc_retry
        tc_id = get_testcase_id_from_session(session_id)
        return idx, {
            "sc_id":          sc_id,
            "objective":      objective,
            "status":         status,
            "session_id":     session_id,
            "testcase_id":    tc_id,
            "failure_detail": failure_detail or "",
            "healed":         healed,
            "reused":         False,
        }

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {}
        for i, sc in enumerate(objs):
            if i > 0:
                time.sleep(3)   # stagger starts to avoid hitting LT concurrency limit simultaneously
            futures[ex.submit(_run, i, sc)] = i
        for fut in as_completed(futures):
            idx, entry = fut.result()
            results[idx] = entry

    passed  = sum(1 for r in results if r["status"] == "passed")
    reused  = sum(1 for r in results if r.get("reused"))
    authored = sum(1 for r in results if not r.get("reused"))
    log.info(f"Kane-cli: {passed}/{len(results)} passed — {reused} reused (skipped kane-cli), {authored} authored")
    for r in results:
        tag = "REUSED" if r.get("reused") else r["status"].upper()
        log.info(f"  {r['sc_id']}: {tag} | tc_id: {r['testcase_id']}")

    return results


# ── Phase 2: Fetch TM test cases with code generated ─────────────────────────

def phase2_fetch_test_cases(kane_results):
    print("\n" + "="*60)
    print("PHASE 2 — Fetching TM test cases by session IDs (code generated)")
    print("="*60)

    passed       = [r for r in kane_results if r.get("status") == "passed" and r.get("testcase_id")]
    failed_count = sum(1 for r in kane_results if r.get("status") != "passed")
    if failed_count:
        print(f"  Skipping {failed_count} failed authoring run(s) — only passed test cases go to HyperExecute")
    if not passed:
        print("  No passed testcase_ids — cannot proceed")
        return []

    # Split: reused SCs already have full TM data in tm_test_cases.json — no API lookup needed.
    # Only newly authored SCs need TM polling (give TM time to index).
    tm_tcs       = json.loads(_TM_TC_FILE.read_text()) if _TM_TC_FILE.exists() else {}
    reused_tcs   = []
    new_tc_ids   = []

    for r in passed:
        if r.get("reused"):
            tc_info = tm_tcs.get(r["sc_id"], {})
            reused_tcs.append({
                "test_case_id": r["testcase_id"],
                "title":        tc_info.get("title", r["sc_id"]),
                "internal_id":  tc_info.get("internal_id", ""),
            })
            print(f"  ⏭ {r['sc_id']}: reusing {tc_info.get('internal_id', r['testcase_id'])} — no TM lookup needed")
        else:
            new_tc_ids.append(r["testcase_id"])

    if not new_tc_ids:
        print(f"  All {len(reused_tcs)} test case(s) reused — skipping TM poll")
        return reused_tcs

    print(f"  Looking for {len(new_tc_ids)} newly authored test case(s) in TM project...")
    for tc_id in new_tc_ids:
        print(f"    {tc_id}")

    # Give TM time to index newly created test cases
    print("  Waiting 15s for TM to index...")
    time.sleep(15)

    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        print(f"  Attempt {attempt}/{max_attempts}...")
        new_tcs = fetch_tm_test_cases_by_ids(new_tc_ids)
        if len(new_tcs) == len(new_tc_ids):
            print(f"  Found all {len(new_tcs)} newly authored test case(s):")
            for tc in new_tcs:
                print(f"    {tc['internal_id']}: {tc['test_case_id']} | {tc['title'][:60]}")
            return reused_tcs + new_tcs
        elif new_tcs:
            print(f"  Found {len(new_tcs)}/{len(new_tc_ids)} so far — retrying in 15s...")
            if attempt == max_attempts:
                print("  Using partial results.")
                return reused_tcs + new_tcs
        else:
            print("  None found yet — retrying in 15s...")
        time.sleep(15)

    return reused_tcs


# ── Phase 3: Create test run + trigger HyperExecute ──────────────────────────

def poll_he_job(job_id: str, tc_internal_ids: set, timeout: int = 1800, log=None, start_time: str = None) -> str:
    """
    Poll automation sessions API until all sessions for this HE job reach a final
    status (passed/failed/cancelled/error) or timeout is reached.
    Returns 'completed' or 'timeout'.

    TM-triggered HE sessions are named "Web || gagandeepb || TC-41961" and each
    test case gets its own build UUID — there is no shared build name across the job.
    We match sessions by tc_internal_ids ({"TC-41961", "TC-41962", ...}).
    """
    from rca import _fetch_sessions_by_tc_ids
    _log = lambda m: (log.info(m) if log else print(m))
    FINAL = {"passed", "failed", "cancelled", "error", "skipped", "stopped", "completed"}

    if not tc_internal_ids:
        _log("[he-poll] No TC internal IDs available — skipping HE poll (Phase 2 may have failed)")
        return "skipped"

    _log(f"[he-poll] Waiting for HE job {job_id} to complete (timeout={timeout}s)...")
    _log(f"[he-poll] Tracking TC IDs: {tc_internal_ids}")
    time.sleep(60)  # warm-up — HE takes time to start sessions

    elapsed = 60
    while elapsed < timeout:
        sessions = _fetch_sessions_by_tc_ids(tc_internal_ids, start_time=start_time)
        if sessions:
            statuses = {s["status"] for s in sessions}
            pending  = statuses - FINAL
            seen_tcs = {s.get("_tc_id") for s in sessions}
            missing_tcs = tc_internal_ids - seen_tcs
            _log(f"[he-poll] {len(sessions)} session(s) — statuses: {dict((s, sum(1 for x in sessions if x['status']==s)) for s in statuses)}")
            if missing_tcs:
                _log(f"[he-poll] Waiting for sessions to appear for: {missing_tcs}")
            elif not pending:
                _log(f"[he-poll] All {len(tc_internal_ids)} TC(s) in final state after {elapsed}s")
                return "completed"
        else:
            _log(f"[he-poll] No sessions yet (elapsed={elapsed}s) — waiting...")
        time.sleep(30)
        elapsed += 30

    _log(f"[he-poll] Timeout after {timeout}s — proceeding with partial results")
    return "timeout"


def phase3_trigger_he(test_cases):
    print("\n" + "="*60)
    print("PHASE 3 — Creating test run + triggering HyperExecute")
    print("="*60)

    if not test_cases:
        print("  No test cases to run — aborting.", file=sys.stderr)
        sys.exit(1)

    # Step 3a: Create test run
    print(f"  [3a] Creating test run: {BUILD_NAME}")
    instances = [
        {
            "test_case_id": tc["test_case_id"],
            "name":         tc["title"],
            "priority":     "Medium",
            "serial_no":    i + 1,
        }
        for i, tc in enumerate(test_cases)
    ]
    run_resp = tm_request("POST", "/test-run", {
        "title":               BUILD_NAME,
        "objective":           "Agentic SDLC pitch — KaneAI Flow 2 end-to-end pipeline",
        "project_id":          PROJECT_ID,
        "is_auteur_generated": True,
        "tags":                ["agentic-sdlc", "kaneai", "flow2"],
        "test_run_instances":  instances,
    })
    test_run_id = run_resp.get("id")
    if not test_run_id:
        print(f"  ERROR: failed to create test run: {run_resp}", file=sys.stderr)
        sys.exit(1)
    tm_report_url = f"https://test-manager.lambdatest.com/projects/{PROJECT_ID}/test-run/{test_run_id}?type=report"
    print(f"       test_run_id: {test_run_id}")
    print(f"       TM Report  : {tm_report_url}")

    # Step 3b: Link instances with environment
    print(f"  [3b] Linking {len(instances)} instance(s) with environment {ENVIRONMENT_ID}...")
    instances_with_env = [{**inst, "environment_id": ENVIRONMENT_ID} for inst in instances]
    link_resp = tm_request("PUT", f"/test-run/{test_run_id}", {
        "id":                  test_run_id,
        "title":               BUILD_NAME,
        "project_id":          PROJECT_ID,
        "objective":           "Agentic SDLC pitch — KaneAI Flow 2 end-to-end pipeline",
        "is_auteur_generated": True,
        "tags":                ["agentic-sdlc", "kaneai", "flow2"],
        "test_run_instances":  instances_with_env,
    })
    print(f"       {link_resp.get('message', link_resp)}")

    # Step 3c: Trigger HyperExecute
    print("  [3c] Triggering HyperExecute...")
    he_resp = api_request("POST", HE_API, {
        "test_run_id":      test_run_id,
        "concurrency":      5,
        "title":            BUILD_NAME,
        "retry_on_failure": True,
        "max_retries":      1,
        "report_enabled":   True,
        "console_log":      True,
        "network_logs":     True,
    })

    job_id   = he_resp.get("job_id")
    job_link = he_resp.get("job_link")

    print(f"\n{'='*60}")
    print(f"  Job ID     : {job_id}")
    print(f"  Job Link   : {job_link}")
    print(f"  TM Report  : {tm_report_url}")
    print(f"{'='*60}")
    return job_id, job_link, tm_report_url


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--sc", help="Run only specific SC ID (e.g. SC-001) for testing")
    parser.add_argument("--skip-phase1", action="store_true",
                        help="Skip kane-cli runs — reuse sessions from ci/last_run.json (written by flow1_pipeline.py)")
    args = parser.parse_args()

    log.phase("FLOW 2 — KaneAI → Test Manager → HyperExecute API")
    log.info(f"Build: {BUILD_NAME}")
    log.info(f"Project: kane-agentic ({PROJECT_ID})")
    log.info(f"Environment: {ENVIRONMENT_ID} (Windows Config — Win10, Firefox 150)")

    # ── Self-heal: rewrite objectives that failed last run ────────────────────
    if not args.skip_phase1:
        history = load_history()
        if history:
            # Skip heal if history is from a different app's run.
            # Check 1: SC IDs must overlap.
            # Check 2: At least one objective text in history must match current objectives
            #          (prevents custom URL run history from corrupting saucedemo objectives).
            current_ids  = {o["id"] for o in SC_OBJECTIVES}
            current_objs = {o["id"]: o.get("objective", "") for o in SC_OBJECTIVES}
            history_ids  = set(history.keys())
            overlap      = current_ids & history_ids
            obj_match    = any(
                history[sc].get("objective", "") == current_objs.get(sc, "NOMATCH")
                for sc in overlap
            )
            if not overlap or not obj_match:
                log.info("[self-heal] History is from a different app/run — skipping cross-run heal")
            else:
                healed = heal_objectives(history, log)
                if healed:
                    if _OBJECTIVES_FILE.exists():
                        SC_OBJECTIVES = json.loads(_OBJECTIVES_FILE.read_text())
                        log.info("[self-heal] ── cross-run healed objectives (used this run) ──")
                        for obj in SC_OBJECTIVES:
                            if obj.get("healed_from"):
                                log.info(f"[self-heal] {obj['id']} BEFORE: {obj['healed_from']}")
                                log.info(f"[self-heal] {obj['id']} AFTER : {obj['objective']}")

    if args.skip_phase1:
        last_run_file = Path(__file__).parent / "last_run.json"
        if not last_run_file.exists():
            log.error("ci/last_run.json not found — run flow1_pipeline.py first")
            sys.exit(1)
        raw = json.loads(last_run_file.read_text())
        kane_results = []
        for r in raw:
            session_dir = r.get("session_dir")
            session_id  = Path(session_dir).name if session_dir else None
            tc_id       = get_testcase_id_from_session(session_id)
            kane_results.append({
                "sc_id":       r.get("id", r.get("sc_id")),
                "status":      r.get("status"),
                "session_id":  session_id,
                "testcase_id": tc_id,
            })
        log.info(f"[skip-phase1] Loaded {len(kane_results)} sessions from {last_run_file.name}")
    else:
        objectives = SC_OBJECTIVES
        if args.sc:
            objectives = [s for s in SC_OBJECTIVES if s["id"] == args.sc]
            if not objectives:
                log.error(f"{args.sc} not found")
                sys.exit(1)
        log.info(f"Running {len(objectives)} objective(s)")
        kane_results = phase1_run_objectives(objectives)
        save_history(kane_results, flow="flow2")

    test_cases = phase2_fetch_test_cases(kane_results)

    # Persist TM test case IDs for traceability (sc_id → TM ULID + TC number)
    # Only record passed SCs — for healed SCs, session_id already points to the retry session
    if not args.skip_phase1:
        passed_results = [r for r in kane_results if r.get("status") == "passed"]
        for r in passed_results:
            src = "self-heal retry" if r.get("healed") else "first pass"
            log.info(f"[tm] {r['sc_id']} → {r.get('testcase_id')} ({src})")
        record_tm_test_cases_with_sc(passed_results, test_cases)

    job_id, job_link, tm_report_url = phase3_trigger_he(test_cases)

    # Record the HE trigger time — used to filter out sessions from previous runs
    # when the same TC IDs are reused across runs (prevents session flood / stale results)
    he_start_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    # Build TC internal ID → SC-id mapping for session tracking
    # TM-triggered HE sessions are named "Web || gagandeepb || TC-41961" —
    # there is no shared build name, so we match sessions by TC internal IDs.
    ulid_to_sc = {r["testcase_id"]: r["sc_id"] for r in kane_results if r.get("testcase_id")}
    tc_to_sc: dict = {}       # {"TC-41961": "SC-001", ...}
    tc_internal_ids: set = set()
    for tc in test_cases:
        internal = tc.get("internal_id", "")
        ulid     = tc.get("test_case_id", "")
        if internal and ulid in ulid_to_sc:
            tc_to_sc[internal] = ulid_to_sc[ulid]
            tc_internal_ids.add(internal)
    log.info(f"[poll] TC→SC mapping: {tc_to_sc}")

    # Persist HE job
    if job_id:
        record_he_job("flow2", job_id, job_link, tm_report_url=tm_report_url)

        # Wait for HE job to finish before fetching results / running RCA
        poll_he_job(job_id, tc_internal_ids, timeout=1800, log=log, start_time=he_start_time)

        # Give HE 30s grace period — "completed" sessions may still have a retry
        # in flight; waiting ensures the retry result is visible in the sessions API
        log.info("[flow2] Waiting 30s for HE retry sessions to settle...")
        time.sleep(30)

        # Override run_history with actual HE pass/fail (not kane-cli Phase 1 status)
        update_history_from_he(SESSION_BUILD_NAME, flow="flow2", log=log, tc_to_sc=tc_to_sc, start_time=he_start_time)

        # Give LT insights engine time to index the completed HE sessions.
        # The automation sessions API indexes fast (used above), but the insights
        # RCA engine typically needs 2+ minutes after sessions reach final state.
        log.info("[flow2] Waiting 120s for LT insights engine to index HE sessions before triggering RCA...")
        time.sleep(120)

        # RCA only for failed test sessions (skips automatically if triggered=0)
        run_rca(job_id, build_name=SESSION_BUILD_NAME, log=log, tc_to_sc=tc_to_sc, start_time=he_start_time)

    # Build traceability matrix after HE results are in
    run_traceability(log)

    # ── Auto-improve: heal objectives from full run results ──────────────
    # Run heal_objectives again with final HE history + RCA so ALL failed SCs
    # get improved objectives — then commit back to repo for next run.
    final_history = load_history()
    rca_data = json.loads(Path(__file__).parent.joinpath("rca_results.json").read_text()) \
               if Path(__file__).parent.joinpath("rca_results.json").exists() else {}
    healed_count = heal_objectives(final_history, log, rca_results=rca_data)
    # Skip auto-commit when running with a custom requirements URL — the objectives
    # generated from that URL are one-off and must NOT overwrite the default saucedemo repo.
    custom_url_run = bool(os.environ.get("REQUIREMENTS_URL", "").strip())
    if custom_url_run and healed_count:
        log.info("[auto-improve] Custom URL run — skipping auto-commit to keep repo on default (saucedemo) objectives")
        healed_count = 0
    if healed_count:
        log.info(f"[auto-improve] {healed_count} objective(s) improved — committing objectives.json")
        import subprocess as _sp
        run_number = os.environ.get("GITHUB_RUN_NUMBER", "?")
        _sp.run(["git", "config", "user.email", "actions@github.com"], check=False)
        _sp.run(["git", "config", "user.name", "GitHub Actions"], check=False)
        # Stage objectives + analyzed_requirements together so they always stay in sync
        _sp.run(["git", "add", "ci/objectives.json"], check=False)
        _sp.run(["git", "add", "requirements/analyzed_requirements.json"], check=False)
        result = _sp.run(
            ["git", "commit", "-m",
             f"chore(objectives): auto-improve {healed_count} SC(s) from run #{run_number}"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            _sp.run(["git", "push"], check=False)
            log.info("[auto-improve] objectives.json committed and pushed")
        else:
            log.info("[auto-improve] nothing to commit (objectives unchanged)")
    else:
        log.info("[auto-improve] all SCs passed — no objective improvements needed")

    log.info("Done — monitor at: https://hyperexecute.lambdatest.com/")
