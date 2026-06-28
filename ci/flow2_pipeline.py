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
from self_heal import load_history, save_history, heal_objectives
from traceability import record_he_job, record_tm_test_cases_with_sc, run_traceability
from rca import run_rca

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
PROJECT_ID    = "01KVXJ82AKT83GWJNFZTQVMNRQ"   # kane-agentic
ENVIRONMENT_ID = 282603                           # Windows Config — Win10, Firefox 150, desktop web
BASE_URL      = "https://automationexercise.com/"
KANE_TIMEOUT  = 180   # seconds per kane-cli run
BUILD_NAME    = f"Agentic SDLC | KaneAI Flow2 | {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"

# ── SC Objectives — loaded from Claude-generated objectives.json if present ───
_OBJECTIVES_FILE = Path(__file__).parent / "objectives.json"
if _OBJECTIVES_FILE.exists():
    SC_OBJECTIVES = json.loads(_OBJECTIVES_FILE.read_text())
    print(f"[config] Loaded {len(SC_OBJECTIVES)} objectives from {_OBJECTIVES_FILE.name}")
else:
    print("[config] Using hardcoded objectives (run ci/generate_objectives.py to generate from requirements)")
    SC_OBJECTIVES = [
        {
            "id":        "SC-001",
            "objective": (
                "Go to https://automationexercise.com/, search for 'Blue Top', "
                "click Add to Cart on the first result, and verify the cart count increases"
            ),
        },
        {
            "id":        "SC-002",
            "objective": (
                "Go to https://automationexercise.com/, hover over a product and click Add to Cart, "
                "then click the cart icon and verify it shows the item name and price"
            ),
        },
        {
            "id":        "SC-003",
            "objective": (
                "Go to https://automationexercise.com/, add an item to the cart, "
                "navigate to the cart page, remove the item using the X button, "
                "and verify the cart is now empty"
            ),
        },
        {
            "id":        "SC-004",
            "objective": (
                "Go to https://automationexercise.com/, type 'jeans' in the search bar and submit, "
                "and verify relevant product results appear on the search results page"
            ),
        },
        {
            "id":        "SC-005",
            "objective": (
                "Go to https://automationexercise.com/products, browse the product catalog, "
                "and verify product tiles display with names and prices"
            ),
        },
        {
            "id":        "SC-006",
            "objective": (
                "Go to https://automationexercise.com/products, click on the View Product link for the first product, "
                "and verify the product detail page shows the name, image, and price"
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
    """Run a single kane-cli objective. Returns (status, session_id, failure_detail)."""
    sc_id  = sc["id"]
    obj    = sc["objective"]
    log.info(f"[{sc_id}] {obj[:80]}...")

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

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=KANE_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        detail = f"Timeout after {KANE_TIMEOUT + 30}s"
        log.failure(sc_id, "TIMEOUT", detail=detail)
        return None, None, detail

    status = session_id = session_dir = failure_detail = None

    combined = result.stdout + "\n" + result.stderr
    for line in combined.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "run_end":
            status      = ev.get("status")
            session_dir = ev.get("session_dir", "")
            if session_dir:
                session_id = Path(session_dir).name
            break

    if status == "passed":
        log.success(sc_id, f"session: {session_id}")
    else:
        raw = (result.stdout + result.stderr).strip()
        failure_detail = raw[:500] if raw else f"No output (exit code {result.returncode})"
        log.failure(sc_id, detail=failure_detail)
        log.info(f"[{sc_id}] exit={result.returncode} stdout={len(result.stdout)}b stderr={len(result.stderr)}b")

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

def phase1_run_objectives(objectives=None):
    log.phase("PHASE 1 — Running kane-cli objectives")

    results = []
    for sc in (objectives or SC_OBJECTIVES):
        status, session_id, failure_detail = run_kane(sc)
        tc_id = get_testcase_id_from_session(session_id)
        results.append({
            "sc_id":          sc["id"],
            "objective":      sc.get("objective", ""),
            "status":         status,
            "session_id":     session_id,
            "testcase_id":    tc_id,
            "failure_detail": failure_detail or "",
        })

    passed = sum(1 for r in results if r["status"] == "passed")
    log.info(f"Kane-cli runs: {passed}/{len(results)} passed")
    for r in results:
        log.info(f"  {r['sc_id']}: {r['status']} | session: {r['session_id']} | tc_id: {r['testcase_id']}")

    return results


# ── Phase 2: Fetch TM test cases with code generated ─────────────────────────

def phase2_fetch_test_cases(kane_results):
    print("\n" + "="*60)
    print("PHASE 2 — Fetching TM test cases by session IDs (code generated)")
    print("="*60)

    # Collect testcase_ids from session.json (== TM test_case_id)
    testcase_ids = [r["testcase_id"] for r in kane_results if r.get("testcase_id")]
    if not testcase_ids:
        print("  No testcase_ids collected from sessions — cannot proceed")
        return []

    print(f"  Looking for {len(testcase_ids)} test case(s) in TM project...")
    for tc_id in testcase_ids:
        print(f"    {tc_id}")

    # Give TM a moment to index newly created test cases
    print("  Waiting 15s for TM to index...")
    time.sleep(15)

    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        print(f"  Attempt {attempt}/{max_attempts}...")
        test_cases = fetch_tm_test_cases_by_ids(testcase_ids)
        if len(test_cases) == len(testcase_ids):
            print(f"  Found all {len(test_cases)} test case(s) with code generated:")
            for tc in test_cases:
                print(f"    {tc['internal_id']}: {tc['test_case_id']} | {tc['title'][:60]}")
            return test_cases
        elif test_cases:
            print(f"  Found {len(test_cases)}/{len(testcase_ids)} so far — retrying in 15s...")
            if attempt == max_attempts:
                print("  Using partial results.")
                return test_cases
        else:
            print("  None found yet — retrying in 15s...")
        time.sleep(15)

    return []


# ── Phase 3: Create test run + trigger HyperExecute ──────────────────────────

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
    print(f"       test_run_id: {test_run_id}")

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
    print(f"  Job ID  : {job_id}")
    print(f"  Job Link: {job_link}")
    print(f"{'='*60}")
    return job_id, job_link


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
            healed = heal_objectives(history, log)
            if healed:
                if _OBJECTIVES_FILE.exists():
                    SC_OBJECTIVES = json.loads(_OBJECTIVES_FILE.read_text())

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
    if not args.skip_phase1:
        record_tm_test_cases_with_sc(kane_results, test_cases)

    job_id, job_link = phase3_trigger_he(test_cases)

    # Persist HE job
    if job_id:
        record_he_job("flow2", job_id, job_link)
        # Trigger + fetch LT AI RCA (job_ids scope — covers all failed tests in run)
        run_rca(job_id, build_name=BUILD_NAME, log=log)

    # Build live traceability matrix → reports/traceability_matrix.md + demo_cache.json
    run_traceability(log)

    log.info("Done — monitor at: https://hyperexecute.lambdatest.com/")
