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
OBJECTIVES_FILE = Path(__file__).parent / "objectives.json"
MODEL          = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You are a QA automation expert fixing failing browser test objectives for kane-cli.

kane-cli executes natural-language objectives as headless browser tests on www.saucedemo.com.
A previous objective failed — you must rewrite it to be more robust.

Key facts about www.saucedemo.com:
- Login page: https://www.saucedemo.com/ — username: 'standard_user', password: 'secret_sauce'
- After login, inventory page: https://www.saucedemo.com/inventory.html
- Each product tile has an 'Add to cart' button; after clicking it the button text changes to 'Remove'
- Cart badge appears in top-right navigation showing item count
- Cart page: https://www.saucedemo.com/cart.html — accessible by clicking the cart icon
- Sort dropdown (top-right of inventory): options include 'Name (A to Z)', 'Name (Z to A)',
  'Price (low to high)', 'Price (high to low)'
- Cheapest product: 'Sauce Labs Onesie' at $7.99; alphabetically first: 'Sauce Labs Backpack'
- Product detail page: click the product name or image from inventory
- NO search bar, NO category sidebar, NO modals after add-to-cart

CRITICAL RULES — these patterns cause known failures, do not reproduce them:
1. ALWAYS start from https://www.saucedemo.com/ and log in first — no page is accessible without login.
2. Login steps count toward the 5-action budget: type username (1), type password (2), click Login (3).
3. Maximum 5 UI actions before the final assertion — kane-cli has a strict step limit.
4. NEVER assert on cart badge count (e.g. "badge shows 1") — the agent cannot reliably count badge numbers.
   Instead assert the button text changed: after clicking Add to cart, assert button reads 'Remove'.
5. For cart page verification: after add to cart, navigate directly to https://www.saucedemo.com/cart.html
   and assert a specific product name (e.g. 'Sauce Labs Backpack') or price (e.g. '$29.99') is visible.
6. For remove verification: login(3) + add to cart(1) + click Remove(1) = 5 actions → assert button reads 'Add to cart'.
7. NEVER click the cart icon/badge to navigate — navigate directly to https://www.saucedemo.com/cart.html.
8. For sort verification: login(3) + click sort dropdown(1) + select option(1) = 5 → assert first product name or price.
9. Use specific product name 'Sauce Labs Backpack' and price '$29.99' in assertions — these are stable.
10. One sentence only, starts with the full URL (https://www.saucedemo.com/), ends with a specific assertion.
"""


def heal_single_objective(sc: dict, failure_detail: str, log=None) -> str | None:
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
            "flow":       flow,
            "status":     r.get("status"),
            "objective":  r.get("objective", ""),
            "failure_detail": r.get("failure_detail", ""),
            "updated_at": datetime.now().isoformat(),
        }
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def heal_objectives(history: dict, log=None) -> int:
    """
    For each SC that failed last run, ask Claude to rewrite the objective.
    Updates ci/objectives.json in place.
    Returns number of objectives healed.
    """
    failed = {sc_id: info for sc_id, info in history.items()
              if info.get("status") not in ("passed", None) or info.get("status") is None}

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

        prompt = f"""The following kane-cli objective failed on the previous pipeline run.

SC ID: {sc_id}
Failed objective:
{old_objective}

What kane-cli actually did (run summary):
{run_summary if run_summary else '(not available)'}

Raw failure detail:
{failure_detail[-600:] if len(failure_detail) > 600 else failure_detail}

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
