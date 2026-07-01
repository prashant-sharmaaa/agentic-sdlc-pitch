#!/usr/bin/env python3
"""
Autonomous self-healing for the agentic SDLC pipeline.

How it works:
  1. After every pipeline run, results are written to ci/run_history.json
     (keyed by SC ID → last status + failure detail + objective used)
  2. At the START of the next pipeline run, call heal_objectives():
     - For each SC that failed last time, ask Claude to rewrite the objective
       using the failure detail as context
     - Updated objectives are written back to ci/objectives.json
     - The pipeline then runs with the healed objectives automatically

Usage (called from flow1/flow2 pipelines):
    from self_heal import load_history, save_history, heal_objectives

    # At start of run:
    history = load_history()
    healed = heal_objectives(history, log)   # rewrites objectives.json if needed

    # After Phase 1:
    save_history(kane_results)
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HISTORY_FILE   = Path(__file__).parent / "run_history.json"
RCA_FILE       = Path(__file__).parent / "rca_results.json"
OBJECTIVES_FILE = Path(__file__).parent / "objectives.json"
MODEL          = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You are a QA automation expert fixing failing browser test objectives for kane-cli.

kane-cli executes natural-language objectives as headless browser tests on a real browser.
A previous objective failed — you must rewrite it to be more robust.

RULES for rewriting (no exceptions):
1. Keep the same app URL, credentials, and intent as the original objective.
2. Be high-level and intent-based — state WHAT to verify, not HOW to click each element.
   kane-cli figures out the exact interactions itself.
3. EXACTLY one sentence: login (if required) + ONE physical interaction + ONE "and verify ..." assertion.
   - ONE interaction means one thing (one click, one selection, one submit).
   - The assertion must verify something IMMEDIATELY VISIBLE on the same page after that action.
   - NEVER chain two interactions — no "and then", "and navigate", "and click" after the first action.
   - NEVER add a second "and verify" or "also verify".
4. NEVER use "changes to" — button/element state transitions are timing-sensitive and unreliable.
5. Do not add spatial hints, price coordinates, or element positions.
6. Do not add step counts or micro-instructions.
7. If the failure suggests a timing issue, simplify — verify something static (a heading, a count, visible text).
8. Credentials must stay inline in the objective if the original had them.
9. ASSERTION QUALITY — the assertion is the most common failure point. Follow these rules:
   a. NEVER assert on long description sentences or body copy — they are hard to match exactly.
      BAD: "verify the description text 'It's not every day that you find a store...'"
      GOOD: "verify the text $29.99 is visible" or "verify the Add to cart button is visible"
   b. NEVER use text that contains special characters like parentheses (), brackets [], dots in method-call
      format (e.g. carry.allTheThings()), or code-style strings — vision models match these inconsistently.
      BAD: "verify the text carry.allTheThings() is visible"
      GOOD: "verify the Back to products button is visible"
   c. For tests that navigate to a new page, assert on a landmark UI element unique to that page
      (a navigation button, a section heading, a price, a badge count) — NOT on body paragraph text.
      BAD: "verify the product detail page shows the description text '...'"
      GOOD: "verify the Back to products button is visible" or "verify the price $29.99 is visible"
   d. Prefer short, exact labels that appear as buttons, headings, links, or badges — these are
      the most reliably readable elements for vision-based assertion.

GOOD — one click, assertion visible immediately:
  "Login to https://app.com/ as user with password pass, click the Add to cart button for Item X, and verify the cart badge shows 1."
  "Login to https://app.com/ as user with password pass, click the cart icon, and verify the heading Your Cart is visible."
  "Login to https://app.com/ as user with password pass, select Price low to high from the sort dropdown, and verify the price $7.99 is visible."
  "Login to https://app.com/ as user with password pass, click the Item X product name, and verify the Back to products button is visible."

BAD (NEVER write these):
  "...add Item X to the cart and navigate to the cart page, and verify..." — navigation is a second action
  "...add Item X to the cart and click Remove, and verify..." — TWO interactions
  "...verify the button changes to Remove" — state transition, timing-sensitive
  "...add to cart and verify badge, then click Remove and verify..." — TWO verifications
  "...verify the description text 'It's not every day...'" — long body copy, fragile exact match
  "...verify the text carry.allTheThings() is visible" — special characters, vision model unreliable
"""


def heal_single_objective(sc: dict, failure_detail: str, log=None):
    """
    Ask Claude to rewrite one failed objective inline (during the same run).
    Returns new objective string, or None if heal is not possible.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    # Extract run_end narrative summary if embedded by run_kane()
    run_summary = ""
    if "[run summary]:" in failure_detail:
        run_summary = failure_detail.split("\n[raw tail]:")[0].replace("[run summary]:", "").strip()

    prompt = (
        f"SC ID: {sc['id']}\n"
        f"Failed objective:\n{sc.get('objective', '')}\n\n"
        f"What kane-cli actually did (run summary):\n{run_summary or '(not available)'}\n\n"
        f"Raw failure detail (last 400 chars):\n{failure_detail[-400:]}\n\n"
        "Rewrite the objective to fix the issue. Apply all critical rules from your system prompt.\n"
        "Return ONLY the new objective string — no quotes, no explanation."
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=MODEL,
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        new_obj = msg.content[0].text.strip()
        if log:
            log.info(f"[self-heal] {sc['id']} inline → {new_obj[:100]}...")
        return new_obj
    except Exception as e:
        if log:
            log.warning(f"[self-heal] {sc['id']} inline heal failed: {e}")
        return None


def load_history() -> dict:
    """Load run history. Returns {} if no history exists."""
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {}


def save_history(kane_results: list, flow: str = "flow"):
    """Persist Phase 1 results to run_history.json."""
    history = load_history()
    for r in kane_results:
        sc_id = r.get("sc_id") or r.get("id", "unknown")
        history[sc_id] = {
            "flow":             flow,
            "status":           r.get("status"),
            "authoring_status": r.get("status"),   # preserved even after HE overwrites status
            "objective":        r.get("objective", ""),
            "failure_detail":   r.get("failure_detail", ""),
            "updated_at":       datetime.now().isoformat(),
        }
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def heal_objectives(history: dict, log=None, rca_results: dict = None) -> int:
    """
    For each SC that failed last run, ask Claude to rewrite the objective.
    Updates ci/objectives.json in place.
    Returns number of objectives healed.
    """
    # Load rca_results for HE failure context if not passed in
    if rca_results is None:
        rca_results = json.loads(RCA_FILE.read_text()) if RCA_FILE.exists() else {}

    # Heal all failed SCs — Phase 1 failures use failure_detail,
    # HE-only failures use RCA text as context
    failed = {}
    for sc_id, info in history.items():
        if info.get("status") in ("passed", None):
            continue
        detail = info.get("failure_detail", "").strip()
        rca_text = rca_results.get(sc_id, {}).get("rca", "") if rca_results else ""
        # Include if we have any context — authoring detail OR RCA
        if detail or rca_text:
            failed[sc_id] = {**info, "_rca": rca_text}

    if not failed:
        if log:
            log.info("[self-heal] No failures in history — nothing to heal")
        return 0

    if not OBJECTIVES_FILE.exists():
        if log:
            log.warning("[self-heal] objectives.json not found — skipping heal")
        return 0

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        if log:
            log.warning("[self-heal] ANTHROPIC_API_KEY not set — skipping autonomous heal")
        return 0

    try:
        import anthropic
    except ImportError:
        if log:
            log.warning("[self-heal] anthropic package not installed — skipping heal")
        return 0

    client   = anthropic.Anthropic(api_key=api_key)
    objectives = json.loads(OBJECTIVES_FILE.read_text())
    obj_map  = {o["id"]: o for o in objectives}
    healed   = 0

    for sc_id, info in failed.items():
        if sc_id not in obj_map:
            continue

        old_objective  = obj_map[sc_id].get("objective", "")
        failure_detail = info.get("failure_detail", "No detail captured")

        # Extract the run_end narrative summary if it was embedded by run_kane()
        run_summary = ""
        if "[run summary]:" in failure_detail:
            parts = failure_detail.split("\n[raw tail]:", 1)
            run_summary = parts[0].replace("[run summary]:", "").strip()

        if log:
            log.warning(f"[self-heal] {sc_id} failed last run — asking Claude to rewrite objective")

        rca_text = info.get("_rca", "")
        # If failure_detail is just an HE session link (not a meaningful authoring error),
        # prefer RCA text as healing context — it has the actual root cause.
        he_session_failure = failure_detail.startswith("[HE execution failed")
        if rca_text and (not run_summary or he_session_failure):
            context = f"[HE execution failure — AI RCA]: {rca_text}"
        else:
            context = run_summary or (failure_detail[-600:] if failure_detail else "")

        prompt = f"""The following test objective failed on the previous pipeline run.

SC ID: {sc_id}
Failed objective:
{old_objective}

What happened:
{context or "(no detail available)"}

Rewrite the objective to fix the issue. Apply the critical rules from your system prompt.
Return ONLY the new objective string — no quotes, no explanation."""

        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=256,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            new_objective = msg.content[0].text.strip()
            obj_map[sc_id]["objective"] = new_objective
            obj_map[sc_id]["healed_from"] = old_objective
            healed += 1

            if log:
                log.info(f"[self-heal] {sc_id} → {new_objective[:100]}...")
        except Exception as e:
            if log:
                log.error(f"[self-heal] {sc_id} Claude call failed: {e}")

    if healed:
        OBJECTIVES_FILE.write_text(json.dumps(list(obj_map.values()), indent=2))
        if log:
            log.info(f"[self-heal] Healed {healed} objective(s) → objectives.json updated")

    return healed
