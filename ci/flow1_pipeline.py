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
from pipeline_logger import get_logger
from self_heal import load_history, save_history, heal_objectives
from traceability import record_he_job, run_traceability
from rca import run_rca, FLOW1_BUILD_NAME

log = get_logger("flow1")

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
            "name": "SC-003: Remove item shows empty cart message",
            "objective": (
                "Go to https://automationexercise.com/, add an item to the cart, "
                "navigate to the cart page, remove the item using the X button, "
                "and verify the empty cart message text is displayed on the page"
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
    log.info(f"[{sc_id}] {sc['objective'][:80]}...")
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
        log.failure(sc_id, "TIMEOUT", detail=f"Exceeded {KANE_TIMEOUT + 30}s")
        return None, None, f"Timeout after {KANE_TIMEOUT + 30}s"

    # Parse both stdout and stderr for NDJSON events
    combined = result.stdout + "\n" + result.stderr
    status = session_dir = failure_detail = None
    for line in combined.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "run_end":
            status      = ev.get("status")
            session_dir = ev.get("session_dir", "")
            break

    if status == "passed":
        log.success(sc_id)
    else:
        # Log raw output to help diagnose CI failures
        raw = (result.stdout + result.stderr).strip()
        failure_detail = raw[:500] if raw else f"No output (exit code {result.returncode})"
        log.failure(sc_id, detail=failure_detail)
        log.info(f"[{sc_id}] exit={result.returncode} stdout={len(result.stdout)}b stderr={len(result.stderr)}b")

    return status, session_dir, failure_detail


def phase1_run_objectives(objectives=None):
    log.phase("PHASE 1 — Running kane-cli objectives")
    results = []
    for sc in (objectives or SC_OBJECTIVES):
        status, session_dir, failure_detail = run_kane(sc)
        results.append({**sc, "status": status, "session_dir": session_dir,
                        "failure_detail": failure_detail or ""})
    passed = sum(1 for r in results if r["status"] == "passed")
    log.info(f"{passed}/{len(results)} passed")
    return results


# ── Phase 2: Transform + write test.py ───────────────────────────────────────

def phase2_transform_and_write(results):
    log.phase("PHASE 2 — Transforming exports → SC-XXX/test.py")

    written = []
    for r in results:
        sc_id       = r["id"]
        session_dir = r.get("session_dir")
        if not session_dir:
            log.warning(f"[{sc_id}] SKIP — no session dir")
            continue

        export_file = Path(session_dir) / "code-export" / "test.py"
        if not export_file.exists():
            log.warning(f"[{sc_id}] SKIP — no code export at {export_file}")
            continue

        kane_code   = export_file.read_text(encoding="utf-8")
        transformed = transform(kane_code, sc_id, r["name"])
        dest_dir    = KANE_DIR / sc_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file   = dest_dir / "test.py"
        dest_file.write_text(transformed, encoding="utf-8")
        log.info(f"[{sc_id}] written → {dest_file}")
        written.append(sc_id)

    return written


# ── Phase 3: Trigger HyperExecute ────────────────────────────────────────────

def phase3_trigger_he():
    log.phase("PHASE 3 — Triggering HyperExecute")

    if not HE_BINARY.exists():
        log.error(f"{HE_BINARY} not found")
        sys.exit(1)
    if not LT_ACCESS_KEY:
        log.error("LT_ACCESS_KEY not set")
        sys.exit(1)

    cmd = [str(HE_BINARY), "--user", LT_USERNAME, "--key", LT_ACCESS_KEY,
           "--config", str(HE_CONFIG)]
    log.info(f"Running: hyperexecute --user {LT_USERNAME} --key *** --config {HE_CONFIG.name}")

    # Capture output to extract job ID while still streaming to console
    import re
    result = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(result.stdout)   # stream to console

    job_id = job_link = None
    for line in result.stdout.splitlines():
        m = re.search(r'jobId=([a-f0-9\-]{36})', line)
        if m:
            job_id = m.group(1)
            job_link = f"https://hyperexecute.lambdatest.com/hyperexecute/task?jobId={job_id}"

    if result.returncode != 0:
        log.error(f"HE job finished with exit code {result.returncode}")
    else:
        log.info("HE job completed successfully")
        if job_link:
            log.info(f"Job link: {job_link}")

    return job_id, job_link


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--sc", help="Run only specific SC ID e.g. SC-001")
    parser.add_argument("--skip-phase1", action="store_true",
                        help="Skip kane-cli runs, use existing code exports")
    args = parser.parse_args()

    log.phase("FLOW 1 — KaneAI → Code Export → HyperExecute")

    # ── Self-heal: rewrite objectives that failed last run ────────────────────
    history = load_history()
    if history:
        healed = heal_objectives(history, log)
        if healed:
            # Reload SC_OBJECTIVES after heal (objectives.json was updated)
            if _OBJECTIVES_FILE.exists():
                SC_OBJECTIVES = json.loads(_OBJECTIVES_FILE.read_text())

    objectives = SC_OBJECTIVES
    if args.sc:
        objectives = [s for s in SC_OBJECTIVES if s["id"] == args.sc]
        if not objectives:
            log.error(f"{args.sc} not found")
            sys.exit(1)

    log.info(f"Running {len(objectives)} SC objective(s)")

    if args.skip_phase1:
        results = []
        sessions_root = Path.home() / ".testmuai" / "kaneai" / "sessions"
        for sc in objectives:
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
            results.append({**sc, "status": "passed", "session_dir": matched, "failure_detail": ""})
    else:
        results = phase1_run_objectives(objectives)
        save_history(results, flow="flow1")
        last_run_file = Path(__file__).parent / "last_run.json"
        last_run_file.write_text(json.dumps(results, indent=2))
        log.info(f"Saved last_run.json — Flow 2 can use with --skip-phase1")

    written = phase2_transform_and_write(results)

    if not written:
        log.error("No test files written — aborting HE trigger")
        sys.exit(1)

    job_id, job_link = phase3_trigger_he()

    # Persist HE job
    if job_id:
        record_he_job("flow1", job_id, job_link)
        # Trigger + fetch LT AI RCA for failed sessions
        run_rca(job_id, build_name=FLOW1_BUILD_NAME, log=log)

    # Build live traceability matrix → reports/traceability_matrix.md + demo_cache.json
    run_traceability(log)
