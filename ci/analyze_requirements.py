#!/usr/bin/env python3
"""
Step 1 — Requirements Analysis (Claude-powered)

Reads ALL files in requirements/ (.txt, .md — any format, any structure)
and uses Claude to extract structured Acceptance Criteria.

No manual formatting required. Drop any requirements doc → Claude extracts ACs.

Output: requirements/analyzed_requirements.json
[{
  "id": "AC-001",
  "description": "...",
  "kane_steps": ["...", "..."],
  "kane_one_liner": "..."
}]

Usage:
    ANTHROPIC_API_KEY=<key> python3 ci/analyze_requirements.py
    python3 ci/analyze_requirements.py --print   # also print extracted ACs
"""
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
REQUIREMENTS_DIR = PROJECT_ROOT / "requirements"
OUTPUT_FILE = REQUIREMENTS_DIR / "analyzed_requirements.json"

MODEL = "claude-sonnet-4-6"

EXTRACT_PROMPT = """\
You are a requirements analyst. Extract ALL acceptance criteria from the requirements text below.

For each acceptance criterion output a JSON object with:
- "id": sequential ID like "AC-001", "AC-002", etc.
- "description": clear, concise description of what the user should be able to do (1-2 sentences)
- "kane_steps": array of 2-4 test steps (strings) describing how to verify this criterion
- "kane_one_liner": a single short phrase summarizing the criterion (5-8 words)

Also extract the base URL of the application under test if mentioned in the requirements.

Rules:
- Extract EVERY distinct acceptance criterion, including implied ones
- If requirements use user stories (As a... I want... So that...), extract the testable criterion
- If requirements are free-form prose, identify each distinct testable behaviour
- Number ACs sequentially starting from AC-001
- Be precise — each AC should test ONE thing
- Do NOT include implementation details, only observable user-facing behaviour

Return ONLY a JSON object with two keys — no preamble, no explanation:
{{
  "base_url": "<extracted app URL or empty string>",
  "acceptance_criteria": [ ...AC objects... ]
}}

Requirements:
---
{requirements_text}
---
"""


def _load_requirements_text() -> str:
    """Read requirements files from requirements/.

    If any uploaded_* files exist (from a manual dispatch with a URL),
    use ONLY those — skip committed default files like saucedemo_requirements.md
    so new app requirements are not mixed with the defaults.
    """
    all_files = []
    for ext in ("*.txt", "*.md"):
        all_files.extend(sorted(REQUIREMENTS_DIR.glob(ext)))

    # Prefer uploaded files when present
    uploaded = [f for f in all_files if f.stem.startswith("uploaded_")]
    files_to_read = uploaded if uploaded else all_files

    texts = []
    for f in files_to_read:
        content = f.read_text(encoding="utf-8").strip()
        if content:
            texts.append(f"=== {f.name} ===\n{content}")
        print(f"[analyze] Reading: {f.name}" + (" (uploaded)" if f in uploaded else " (default)"))

    return "\n\n".join(texts)


def extract_acs_with_claude(raw_text: str) -> list:
    """Call Claude to extract structured ACs from any requirements text."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[analyze] ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    try:
        import anthropic
    except ImportError:
        print("[analyze] ERROR: anthropic package not installed. Run: pip install anthropic",
              file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    prompt = EXTRACT_PROMPT.format(requirements_text=raw_text)

    print(f"[analyze] Sending {len(raw_text)} chars to Claude ({MODEL})...")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_json = resp.content[0].text.strip()

    # Strip markdown code fences if present
    if raw_json.startswith("```"):
        lines = raw_json.splitlines()
        raw_json = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as e:
        print(f"[analyze] ERROR: Claude returned invalid JSON: {e}", file=sys.stderr)
        print(f"Raw response:\n{raw_json[:500]}", file=sys.stderr)
        sys.exit(1)

    # Support both new wrapper format and legacy array
    if isinstance(parsed, dict):
        return parsed.get("base_url", ""), parsed.get("acceptance_criteria", [])
    if isinstance(parsed, list):
        return "", parsed
    print("[analyze] ERROR: unexpected Claude response format", file=sys.stderr)
    sys.exit(1)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--print", action="store_true", dest="print_acs",
                        help="Print extracted ACs to stdout")
    args = parser.parse_args()

    import hashlib, sys as _sys
    REQUIREMENTS_DIR.mkdir(exist_ok=True)

    raw_text = _load_requirements_text()
    if not raw_text:
        print("[analyze] ERROR: No .txt or .md files found in requirements/", file=sys.stderr)
        sys.exit(1)

    # ── URL/content cache: skip Claude if same requirements as last run ────────
    CACHE_FILE = REQUIREMENTS_DIR / ".requirements_hash"
    content_hash = hashlib.sha256(raw_text.encode()).hexdigest()[:16]
    cached_hash  = CACHE_FILE.read_text().strip() if CACHE_FILE.exists() else ""

    if content_hash == cached_hash and OUTPUT_FILE.exists() and "--force" not in sys.argv:
        print(f"[analyze] Requirements unchanged (hash={content_hash}) — skipping Claude analysis")
        print(f"[analyze] Using existing {OUTPUT_FILE.name} (pass --force to re-analyse)")
        return

    print(f"[analyze] Found requirements text ({len(raw_text)} chars) — extracting ACs with Claude")

    base_url, acs = extract_acs_with_claude(raw_text)

    output = {"base_url": base_url, "acceptance_criteria": acs}
    OUTPUT_FILE.write_text(json.dumps(output, indent=2), encoding="utf-8")
    CACHE_FILE.write_text(content_hash)
    print(f"[analyze] Extracted {len(acs)} acceptance criteria → {OUTPUT_FILE.relative_to(PROJECT_ROOT)}")
    if base_url:
        print(f"[analyze] App URL: {base_url}")

    if args.print_acs:
        print()
        for ac in acs:
            print(f"  {ac['id']}: {ac['description']}")
            print(f"    One-liner: {ac.get('kane_one_liner', '')}")
            steps = ac.get("kane_steps", [])
            for s in steps:
                print(f"    - {s}")
            print()


if __name__ == "__main__":
    main()
