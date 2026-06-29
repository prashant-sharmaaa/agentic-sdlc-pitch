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
PROJECT_ID    = "01KVXJ82AKT83GWJNFZTQVMNRQ"   # kane-agentic
ENVIRONMENT_ID = 282603                           # Windows Config — Win10, Firefox 150, desktop web
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
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=KANE_TIMEOUT)
    except subprocess.TimeoutExpired:
        detail = f"Timeout after {KANE_TIMEOUT}s"
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
            # Override: kane-cli sometimes exits 1 after the assertion passes
            # (during code-export / session finalization). Detect via summary.
            if status == "failed" and session_dir:
                s = ev.get("summary", "").lower()
                if "passed" in s and any(w in s for w in ["check", "verified", "confirmed", "assert"]):
                    log.info(f"[{sc_id}] assertion passed but kane-cli errored post-check — treating as passed")
                    status = "passed"
            break

    if status == "passed":
        log.success(sc_id, f"session: {session_id}")
    else:
        # Collect all event types + the run_end payload for diagnosis
        events_seen = []
        run_end_ev  = None
        for line in combined.splitlines():
            try:
                ev = json.loads(line)
                events_seen.append(ev.get("type", "?"))
                if ev.get("type") == "run_end":
                    run_end_ev = ev
            except Exception:
                continue

        log.info(f"[{sc_id}] exit={result.returncode} stdout={len(result.stdout)}b stderr={len(result.stderr)}b")
        log.info(f"[{sc_id}] events: {events_seen}")
        if run_end_ev:
            log.info(f"[{sc_id}] run_end payload: {json.dumps(run_end_ev)[:400]}")

        # Build failure_detail: run_end summary (narrative) + raw tail
        # Self-heal uses this to understand what the agent actually did
        raw = (result.stdout + result.stderr).strip()
        tail = raw[-800:] if len(raw) > 800 else raw
        run_end_summary = run_end_ev.get("summary", "") if run_end_ev else ""
        if run_end_summary:
            failure_detail = f"[run summary]: {run_end_summary}\n[raw tail]: {tail}"
        else:
            failure_detail = tail if raw else f"No output (exit code {result.returncode})"
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

def phase1_run_objectives(objectives=None):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    log.phase("PHASE 1 — Running kane-cli objectives (parallel, max_workers=2)")
    objs = list(objectives or SC_OBJECTIVES)
    results = [None] * len(objs)

    def _run(idx, sc):
        status, session_id, failure_detail = run_kane(sc)
        healed = False
        # Inline retry: if failed, ask Claude to rewrite objective and try once more
        if status != "passed" and failure_detail:
            log.warning(f"[{sc['id']}] authoring failed — attempting inline heal + retry")
            healed_obj = heal_single_objective(sc, failure_detail, log)
            if healed_obj:
                sc_retry = {**sc, "objective": healed_obj}
                s2, sid2, fd2 = run_kane(sc_retry)
                if s2 == "passed":
                    log.info(f"[{sc['id']}] retry PASSED with healed objective")
                    status, session_id, failure_detail = s2, sid2, fd2
                    sc = sc_retry
                    healed = True
                else:
                    log.warning(f"[{sc['id']}] retry also failed")
        tc_id = get_testcase_id_from_session(session_id)
        return idx, {
            "sc_id":          sc["id"],
            "objective":      sc.get("objective", ""),
            "status":         status,
            "session_id":     session_id,
            "testcase_id":    tc_id,
            "failure_detail": failure_detail or "",
            "healed":         healed,
        }

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(_run, i, sc): i for i, sc in enumerate(objs)}
        for fut in as_completed(futures):
            idx, entry = fut.result()
            results[idx] = entry

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

def poll_he_job(job_id: str, build_name: str, timeout: int = 1800, log=None) -> str:
    """
    Poll automation sessions API until all sessions for the build reach a final
    status (passed/failed/cancelled/error) or timeout is reached.
    Returns 'completed' or 'timeout'.
    Final statuses are checked every 30s after an initial 60s warm-up.
    """
    from rca import fetch_sessions_for_build
    _log = lambda m: (log.info(m) if log else print(m))
    FINAL = {"passed", "failed", "cancelled", "error", "skipped", "stopped"}

    _log(f"[he-poll] Waiting for HE job {job_id} to complete (timeout={timeout}s)...")
    time.sleep(60)  # warm-up — HE takes time to start sessions

    elapsed = 60
    while elapsed < timeout:
        sessions = fetch_sessions_for_build(build_name, log=None)
        if sessions:
            statuses = {s["status"] for s in sessions}
            pending  = statuses - FINAL
            _log(f"[he-poll] {len(sessions)} session(s) — statuses: {dict((s, sum(1 for x in sessions if x['status']==s)) for s in statuses)}")
            if not pending:
                _log(f"[he-poll] All sessions in final state after {elapsed}s")
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
            # Skip heal if objectives are for a different app than last run
            # (new requirements URL was provided — old history is irrelevant)
            current_ids = {o["id"] for o in SC_OBJECTIVES}
            history_ids = set(history.keys())
            overlap = current_ids & history_ids
            if not overlap:
                log.info("[self-heal] New objectives detected — skipping cross-run heal (different app or fresh requirements)")
            else:
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
    # Only record passed SCs — for healed SCs, session_id already points to the retry session
    if not args.skip_phase1:
        passed_results = [r for r in kane_results if r.get("status") == "passed"]
        for r in passed_results:
            src = "self-heal retry" if r.get("healed") else "first pass"
            log.info(f"[tm] {r['sc_id']} → {r.get('testcase_id')} ({src})")
        record_tm_test_cases_with_sc(passed_results, test_cases)

    job_id, job_link, tm_report_url = phase3_trigger_he(test_cases)

    # Persist HE job
    if job_id:
        record_he_job("flow2", job_id, job_link, tm_report_url=tm_report_url)

        # Wait for HE job to finish before fetching results / running RCA
        poll_he_job(job_id, BUILD_NAME, timeout=1800, log=log)

        # Override run_history with actual HE pass/fail (not kane-cli Phase 1 status)
        update_history_from_he(BUILD_NAME, flow="flow2", log=log)

        # RCA only for failed test sessions (skips automatically if triggered=0)
        run_rca(job_id, build_name=BUILD_NAME, log=log)

    # Build traceability matrix after HE results are in
    run_traceability(log)

    # ── Auto-improve: heal objectives from full run results ──────────────
    # Run heal_objectives again with final HE history + RCA so ALL failed SCs
    # get improved objectives — then commit back to repo for next run.
    final_history = load_history()
    rca_data = json.loads(Path(__file__).parent.joinpath("rca_results.json").read_text()) \
               if Path(__file__).parent.joinpath("rca_results.json").exists() else {}
    healed_count = heal_objectives(final_history, log, rca_results=rca_data)
    if healed_count:
        log.info(f"[auto-improve] {healed_count} objective(s) improved — committing objectives.json")
        import subprocess as _sp
        run_number = os.environ.get("GITHUB_RUN_NUMBER", "?")
        _sp.run(["git", "config", "user.email", "actions@github.com"], check=False)
        _sp.run(["git", "config", "user.name", "GitHub Actions"], check=False)
        _sp.run(["git", "add", "ci/objectives.json"], check=False)
        result = _sp.run(
            ["git", "commit", "-m",
             f"chore(objectives): auto-improve {healed_count} SC(s) from run #{run_number} [skip ci]"],
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
