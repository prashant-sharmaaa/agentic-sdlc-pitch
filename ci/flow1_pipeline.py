#!/usr/bin/env python3
"""
Flow 1 — Full End-to-End Pipeline
===================================
Phase 1 : Run kane-cli for each SC objective → exports Python code
Phase 2 : Transform exports (testmu async → LT CDP sync) → write to SC-XXX/test.py
Phase 3 : Trigger HyperExecute via CLI

Usage:
    python3 ci/flow1_pipeline.py
    LT_ACCESS_KEY=<key> python3 ci/flow1_pipeline.py
    python3 ci/flow1_pipeline.py --sc SC-001   # single SC for testing
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# Add ci/ to path for local import
sys.path.insert(0, str(Path(__file__).parent))
from transform_kane_export import transform

# ── Config ────────────────────────────────────────────────────────────────────
LT_USERNAME   = os.environ.get("LT_USERNAME", "gagandeepb")
LT_ACCESS_KEY = os.environ.get("LT_ACCESS_KEY")
BASE_URL      = "https://automationexercise.com/"
KANE_TIMEOUT  = 180
KANE_DIR      = Path("tests/playwright/kane")
PROJECT_ROOT  = Path(__file__).parent.parent   # ci/ → project root
HE_BINARY     = PROJECT_ROOT / "hyperexecute"
HE_CONFIG     = PROJECT_ROOT / "hyperexecute.yaml"

# ── SC objectives — loaded from Claude-generated objectives.json if present ───
_OBJECTIVES_FILE = Path(__file__).parent / "objectives.json"
if _OBJECTIVES_FILE.exists():
    SC_OBJECTIVES = json.loads(_OBJECTIVES_FILE.read_text())
    print(f"[config] Loaded {len(SC_OBJECTIVES)} objectives from {_OBJECTIVES_FILE.name}")
else:
    print("[config] Using hardcoded objectives (run ci/generate_objectives.py to generate from requirements)")
    SC_OBJECTIVES = [
        {
            "id":   "SC-001",
            "name": "SC-001: Add to cart updates counter instantly",
            "objective": (
                "Go to https://automationexercise.com/, search for 'Blue Top', "
                "click Add to Cart on the first result, and verify the cart count increases"
            ),
        },
        {
            "id":   "SC-002",
            "name": "SC-002: Cart shows item names and prices",
            "objective": (
                "Go to https://automationexercise.com/, hover over a product and click Add to Cart, "
                "then click the cart icon and verify it shows the item name and price"
            ),
        },
        {
            "id":   "SC-003",
            "name": "SC-003: Remove item updates cart total",
            "objective": (
                "Go to https://automationexercise.com/, add an item to the cart, "
                "navigate to the cart page, remove the item using the X button, "
                "and verify the cart is now empty"
            ),
        },
        {
            "id":   "SC-004",
            "name": "SC-004: Search returns relevant product results",
            "objective": (
                "Go to https://automationexercise.com/, type 'jeans' in the search bar and submit, "
                "and verify relevant product results appear on the search results page"
            ),
        },
        {
            "id":   "SC-005",
            "name": "SC-005: Catalog displays product tiles with pricing",
            "objective": (
                "Go to https://automationexercise.com/products, browse the product catalog, "
                "and verify product tiles display with names and prices"
            ),
        },
        {
            "id":   "SC-006",
            "name": "SC-006: Product tile opens detail page with name, image, price",
            "objective": (
                "Go to https://automationexercise.com/products, click on the View Product link for the first product, "
                "and verify the product detail page shows the name, image, and price"
            ),
        },
    ]


# ── Phase 1: Run kane-cli ─────────────────────────────────────────────────────


def run_kane(sc):
    sc_id = sc["id"]
    print(f"\n  [{sc_id}] {sc['objective'][:80]}...")
    cmd = [
        "kane-cli", "run", sc["objective"],
        "--url", BASE_URL,
        "--agent",
        "--headless",
        "--code-export",
        "--code-language", "python",
        "--skip-code-validation",
        "--timeout", str(KANE_TIMEOUT),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=KANE_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        print(f"  [{sc_id}] TIMEOUT")
        return None, None

    status = session_dir = None
    for line in result.stdout.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "run_end":
            status      = ev.get("status")
            session_dir = ev.get("session_dir", "")
            break

    print(f"  [{sc_id}] {(status or 'no-status').upper()}")
    return status, session_dir


def phase1_run_objectives(objectives=None):
    print("\n" + "="*60)
    print("PHASE 1 — Running kane-cli objectives")
    print("="*60)
    results = []
    for sc in (objectives or SC_OBJECTIVES):
        status, session_dir = run_kane(sc)
        results.append({**sc, "status": status, "session_dir": session_dir})
    passed = sum(1 for r in results if r["status"] == "passed")
    print(f"\n  {passed}/{len(results)} passed")
    return results


# ── Phase 2: Transform + write test.py ───────────────────────────────────────

def phase2_transform_and_write(results):
    print("\n" + "="*60)
    print("PHASE 2 — Transforming exports → SC-XXX/test.py")
    print("="*60)

    written = []
    for r in results:
        sc_id       = r["id"]
        session_dir = r.get("session_dir")
        if not session_dir:
            print(f"  [{sc_id}] SKIP — no session dir")
            continue

        export_file = Path(session_dir) / "code-export" / "test.py"
        if not export_file.exists():
            print(f"  [{sc_id}] SKIP — no code export at {export_file}")
            continue

        kane_code     = export_file.read_text(encoding="utf-8")
        transformed   = transform(kane_code, sc_id, r["name"])
        dest_dir      = KANE_DIR / sc_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file     = dest_dir / "test.py"
        dest_file.write_text(transformed, encoding="utf-8")
        print(f"  [{sc_id}] ✓ written → {dest_file}")
        written.append(sc_id)

    return written


# ── Phase 3: Trigger HyperExecute ────────────────────────────────────────────

def phase3_trigger_he():
    print("\n" + "="*60)
    print("PHASE 3 — Triggering HyperExecute")
    print("="*60)

    if not HE_BINARY.exists():
        print(f"  ERROR: {HE_BINARY} not found", file=sys.stderr)
        sys.exit(1)
    if not LT_ACCESS_KEY:
        print("  ERROR: LT_ACCESS_KEY not set", file=sys.stderr)
        sys.exit(1)

    cmd = [
        str(HE_BINARY),
        "--user", LT_USERNAME,
        "--key", LT_ACCESS_KEY,
        "--config", str(HE_CONFIG),
    ]
    print(f"  Running: {' '.join(cmd[:4])} --key *** --config {HE_CONFIG}")
    result = subprocess.run(cmd, capture_output=False)  # stream output live

    if result.returncode != 0:
        print(f"\n  HE job finished with exit code {result.returncode}")
    else:
        print("\n  HE job completed successfully")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--sc", help="Run only specific SC ID e.g. SC-001")
    parser.add_argument("--skip-phase1", action="store_true",
                        help="Skip kane-cli runs, use existing code exports")
    args = parser.parse_args()

    objectives = SC_OBJECTIVES
    if args.sc:
        objectives = [s for s in SC_OBJECTIVES if s["id"] == args.sc]
        if not objectives:
            print(f"ERROR: {args.sc} not found", file=sys.stderr)
            sys.exit(1)

    print("=" * 60)
    print("FLOW 1 — KaneAI → Code Export → HyperExecute")
    print("=" * 60)
    print(f"Running {len(objectives)} SC objective(s)")

    if args.skip_phase1:
        # Build fake results from existing sessions
        results = []
        sessions_root = Path.home() / ".testmuai" / "kaneai" / "sessions"
        for sc in objectives:
            # Find latest session with code export for this SC
            matched = None
            for sf in sorted(sessions_root.glob("*/session.json"), reverse=True):
                try:
                    d = json.loads(sf.read_text())
                    runs = d.get("runs", [])
                    if runs and sc["objective"][:40].lower() in runs[0].get("objective", "").lower():
                        export = sf.parent / "code-export" / "test.py"
                        if export.exists():
                            matched = str(sf.parent)
                            break
                except Exception:
                    pass
            results.append({**sc, "status": "passed", "session_dir": matched})
    else:
        results = phase1_run_objectives(objectives)
        # Save session results so Flow 2 can reuse them with --skip-phase1
        last_run_file = Path(__file__).parent / "last_run.json"
        last_run_file.write_text(json.dumps(results, indent=2))
        print(f"\n  [saved] {last_run_file.name} — Flow 2 can use this with --skip-phase1")

    written = phase2_transform_and_write(results)

    if not written:
        print("\nERROR: No test files written — aborting HE trigger", file=sys.stderr)
        sys.exit(1)

    phase3_trigger_he()
