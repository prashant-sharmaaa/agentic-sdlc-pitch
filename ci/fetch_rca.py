#!/usr/bin/env python3
"""Root Cause Analysis helpers for LambdaTest / TestMu AI.

Two public functions:
  trigger_rca(session_ids)  — POST to TestMu AI RCA generation API (bulk)
  fetch_rca(session_link)   — GET the generated RCA text for a single session
"""
import base64
import os
import re
from urllib.parse import urlparse, parse_qs

import httpx

try:
    import anthropic as _anthropic
    _ANTHROPIC_CLIENT = _anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
except Exception:
    _ANTHROPIC_CLIENT = None

LT_USERNAME   = os.environ.get("LT_USERNAME", "")
LT_ACCESS_KEY = os.environ.get("LT_ACCESS_KEY", "")
BASE_API      = "https://api.lambdatest.com/automation/api/v1/sessions"
RCA_TRIGGER   = "https://api.lambdatest.com/insights/api/v3/public/rca/generate"


def _basic_auth_header() -> str:
    token = base64.b64encode(f"{LT_USERNAME}:{LT_ACCESS_KEY}".encode()).decode()
    return f"Basic {token}"


def _extract_session_id(session_link: str) -> str:
    """Extract the LambdaTest test-session ID from an automation session URL."""
    if not session_link:
        return ""
    parsed = urlparse(session_link)
    qs = parse_qs(parsed.query)
    if "testID" in qs:
        return qs["testID"][0]
    # path-based: /test/<id>
    m = re.search(r"/test/([a-zA-Z0-9_-]+)", session_link)
    if m:
        return m.group(1)
    return ""


def trigger_rca(session_ids: list[str]) -> None:
    """Trigger TestMu AI RCA generation for a batch of failed session IDs.

    Calls POST https://api.lambdatest.com/insights/api/v3/public/rca/generate
    with test_ids = session_ids.  Fire-and-forget — errors are logged but not
    raised so the pipeline continues even if the trigger fails.
    """
    if not LT_USERNAME or not LT_ACCESS_KEY or not session_ids:
        return

    ids = [sid for sid in session_ids if sid]
    if not ids:
        return

    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type":  "application/json",
    }
    payload = {"test_ids": ids}

    try:
        r = httpx.post(RCA_TRIGGER, headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            d = r.json().get("data", {})
            triggered = d.get("triggered_count", "?")
            skipped   = d.get("skipped_count", 0)
            print(f"[rca] triggered={triggered}  skipped={skipped}  ids={ids}")
        else:
            print(f"[rca] trigger returned HTTP {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        print(f"[rca] trigger error: {exc}")


def fetch_rca(session_link: str) -> str:
    """Return RCA text for a failed session, or '' if unavailable.

    Tries, in order:
      1. LambdaTest AI RCA endpoint  (/sessions/{id}/rca)
      2. Session details              (/sessions/{id})  → reason_of_failure
      3. Session exceptions           (/sessions/{id}/exceptions)
    """
    if not LT_USERNAME or not LT_ACCESS_KEY or not session_link:
        return ""

    sid = _extract_session_id(session_link)
    if not sid:
        return ""

    auth = (LT_USERNAME, LT_ACCESS_KEY)

    # 1. AI RCA endpoint
    try:
        r = httpx.get(f"{BASE_API}/{sid}/rca", auth=auth, timeout=15)
        if r.status_code == 200:
            d = r.json().get("data", {})
            rca = d.get("rca_summary") or d.get("summary") or d.get("message") or ""
            if rca:
                return str(rca)
    except Exception:
        pass

    # 2. Session details → reason_of_failure
    try:
        r = httpx.get(f"{BASE_API}/{sid}", auth=auth, timeout=15)
        if r.status_code == 200:
            d = r.json().get("data", {})
            reason = d.get("reason_of_failure") or d.get("message") or ""
            if reason:
                return str(reason)
    except Exception:
        pass

    # 3. Exceptions list
    try:
        r = httpx.get(f"{BASE_API}/{sid}/exceptions", auth=auth, timeout=15)
        if r.status_code == 200:
            items = r.json().get("data", {}).get("exceptions", [])
            if items:
                first = items[0]
                msg = first.get("message") or first.get("errorMessage") or str(first)
                return str(msg)
    except Exception:
        pass

    return ""


def summarize_rca(raw_rca: str, scenario_id: str = "") -> str:
    """Distill raw RCA text into 2–3 crisp bullet points using Claude.

    Falls back to a truncated version of the raw text if Claude is unavailable.
    """
    if not raw_rca:
        return ""

    if not _ANTHROPIC_CLIENT:
        return raw_rca[:300] + ("…" if len(raw_rca) > 300 else "")

    context = f" for scenario {scenario_id}" if scenario_id else ""
    prompt = (
        f"You are a QA analyst summarising a test failure RCA{context}.\n"
        "Condense the following raw RCA into exactly 2–3 bullet points that cover:\n"
        "• What failed (symptom)\n"
        "• Why it failed (root cause)\n"
        "• Suggested fix or next step\n"
        "Be concise — each bullet ≤ 20 words. No preamble.\n\n"
        f"RAW RCA:\n{raw_rca[:2000]}"
    )

    try:
        resp = _ANTHROPIC_CLIENT.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        print(f"[rca] summarize error: {exc}")
        return raw_rca[:300] + ("…" if len(raw_rca) > 300 else "")
