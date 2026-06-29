#!/usr/bin/env python3
"""
Step 2 — Generate kane-cli run objectives from analyzed ACs (Claude-powered).

Reads requirements/analyzed_requirements.json (output of analyze_requirements.py)
and uses Claude to produce crisp, intent-based objectives for each AC.

Objective format (enforced):
  "Login to <url> as <user> with password <pass>, <one action>, and verify <one assertion>."

Each objective is short, high-level, and unambiguous — no micro-steps, no spatial
hints, no price coordinates. kane-cli authors the test steps itself.

Output: ci/objectives.json

Usage:
    ANTHROPIC_API_KEY=<key> python3 ci/generate_objectives.py
    python3 ci/generate_objectives.py --dry-run
"""
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT    = Path(__file__).parent.parent
ANALYZED_FILE   = PROJECT_ROOT / "requirements" / "analyzed_requirements.json"
OUTPUT_FILE     = Path(__file__).parent / "objectives.json"
MODEL           = "claude-sonnet-4-6"

OBJECTIVES_PROMPT = """\
You are a QA engineer writing test objectives for a browser automation tool called KaneAI.

KaneAI takes a plain-English objective and autonomously authors a browser test. The objective
must be short, intent-based, and unambiguous. KaneAI figures out the exact clicks — you must
NOT include step-by-step instructions, spatial hints, price coordinates, or UI element details.

FORMAT RULES (strict):
- One sentence per objective
- Start with login (include URL, username, password inline if the app requires login)
- State ONE action after login
- End with ONE "and verify ..." assertion
- No bullet points, no numbered steps, no "then", no "next", no "after that"
- Max 30 words

GOOD examples (follow this style exactly):
  "Login to https://www.saucedemo.com/ as standard_user with password secret_sauce, add Sauce Labs Backpack to the cart, and verify the button changes to 'Remove'."
  "Login to https://www.saucedemo.com/ as standard_user with password secret_sauce, add Sauce Labs Backpack to the cart, remove it, and verify the button returns to 'Add to cart'."
  "Login to https://www.saucedemo.com/ as standard_user with password secret_sauce, sort the product list by Price low to high, and verify the first product shows price $7.99."

BAD (never do this):
  "Navigate to the URL, type username into the username input, type password into the password input, click Login button, click Add to cart below $29.99 price..."

App URL: {base_url}
App credentials: username={username}, password={password}

Acceptance Criteria:
{ac_text}

Return ONLY a JSON array — no preamble, no explanation:
[
  {{"id": "SC-001", "ac_id": "AC-001", "name": "SC-001: <short name>", "objective": "<crisp objective>"}},
  ...
]
Number SCs sequentially starting from SC-001. Generate AT MOST 5 objectives — pick the most critical testable behaviours if there are more than 5 ACs.
"""


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate crisp objectives from analyzed ACs")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    args = parser.parse_args()

    # Load analyzed requirements
    if not ANALYZED_FILE.exists():
        print(f"ERROR: {ANALYZED_FILE} not found — run analyze_requirements.py first", file=sys.stderr)
        sys.exit(1)

    analyzed = json.loads(ANALYZED_FILE.read_text())
    base_url  = analyzed.get("base_url", "https://www.saucedemo.com/")
    acs       = analyzed.get("acceptance_criteria", [])

    if not acs:
        print("ERROR: no acceptance criteria found in analyzed_requirements.json", file=sys.stderr)
        sys.exit(1)

    # Extract credentials from ACs if present, else use saucedemo defaults
    ac_text = "\n".join(
        f"  {ac['id']}: {ac['description']}" for ac in acs
    )
    # Heuristic: look for credentials in analyzed text
    username = "standard_user"
    password = "secret_sauce"
    for ac in acs:
        desc = ac.get("description", "").lower()
        steps = " ".join(ac.get("kane_steps", [])).lower()
        combined = desc + " " + steps
        if "standard_user" in combined:
            username = "standard_user"
        if "secret_sauce" in combined:
            password = "secret_sauce"

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    try:
        import anthropic
    except ImportError:
        print("ERROR: anthropic package not installed. Run: pip install anthropic", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    prompt = OBJECTIVES_PROMPT.format(
        base_url=base_url,
        username=username,
        password=password,
        ac_text=ac_text,
    )

    # Skip if objectives already exist for this exact set of ACs
    import hashlib
    ac_hash = hashlib.sha256(json.dumps(acs, sort_keys=True).encode()).hexdigest()[:16]
    HASH_FILE = OUTPUT_FILE.parent / ".objectives_hash"
    cached = HASH_FILE.read_text().strip() if HASH_FILE.exists() else ""
    if ac_hash == cached and OUTPUT_FILE.exists() and "--force" not in __import__("sys").argv:
        print(f"[objectives] ACs unchanged (hash={ac_hash}) — skipping Claude generation")
        print(f"[objectives] Using existing {OUTPUT_FILE.name} (pass --force to regenerate)")
        return

    print(f"[objectives] Generating objectives for {len(acs)} ACs with Claude ({MODEL})...")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    try:
        objectives = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Claude returned invalid JSON: {e}\n{raw[:400]}", file=sys.stderr)
        sys.exit(1)

    print(f"\n[objectives] Generated {len(objectives)} objectives:")
    for o in objectives:
        print(f"  {o['id']}: {o['objective']}")

    if args.dry_run:
        print("\n--- objectives.json (dry run) ---")
        print(json.dumps(objectives, indent=2))
        return

    OUTPUT_FILE.write_text(json.dumps(objectives, indent=2))
    HASH_FILE.write_text(ac_hash)
    print(f"\n[objectives] Written to {OUTPUT_FILE.name}")


if __name__ == "__main__":
    main()
