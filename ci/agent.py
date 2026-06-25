#!/usr/bin/env python3
"""
Agentic SDLC Orchestrator — Stages 2-7.
Stage 2: Sync scenarios
Stage 3: Generate Playwright tests
Stage 4: Select tests (incremental or full)
Stage 5: Submit to HyperExecute
Stage 6: Fetch results via LambdaTest MCP Server
Stage 7: Traceability + release verdict + GitHub summary
"""
import asyncio
import json
import os
import py_compile
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

sys.path.insert(0, str(Path(__file__).parent))
from stage_utils import print_stage_header, print_stage_result

# ── Config ──────────────────────────────────────────────────────────────────
MCP_URL       = "https://mcp.lambdatest.com/mcp"
LT_API_BASE   = "https://api.lambdatest.com/automation/api/v1"
LT_USERNAME   = os.environ.get("LT_USERNAME", "")
LT_ACCESS_KEY = os.environ.get("LT_ACCESS_KEY", "")
FULL_RUN      = os.environ.get("FULL_RUN", "true").lower() == "true"
TARGET_URL    = os.environ.get("TARGET_URL", "https://ecommerce-playground.lambdatest.io/")
TODAY         = datetime.now(timezone.utc).date().isoformat()
RUN_NUMBER    = os.environ.get("GITHUB_RUN_NUMBER", "")
BUILD_NAME    = f"Agentic SDLC #{RUN_NUMBER} | {TODAY}" if RUN_NUMBER else f"Agentic SDLC | {TODAY}"

_TERMINAL_HE  = {"completed", "passed", "failed", "error", "aborted", "cancelled"}
_JOB_ID_RE    = re.compile(r"jobId=([\w-]+)|job[_\s-]?id[:\s=]+([0-9a-f-]{36})", re.IGNORECASE)

# ── Playwright test bodies keyed by scenario ID ──────────────────────────────
_ECOM = "https://ecommerce-playground.lambdatest.io"

PLAYWRIGHT_BODIES: dict[str, str] = {
    "SC-001": (
        f'    page.goto("{_ECOM}/")\n'
        '    page.wait_for_load_state("load", timeout=30000)\n'
        '    page.locator(".product-thumb").first.hover()\n'
        '    page.wait_for_timeout(500)\n'
        '    page.locator("button.btn-cart:visible").first.click(force=True)\n'
        '    page.wait_for_timeout(2000)\n'
        '    badge = page.locator(".cart-item-total")\n'
        '    assert badge.count() > 0 and badge.first.inner_text().strip() != "0", "Cart counter not updated"'
    ),
    "SC-002": (
        f'    page.goto("{_ECOM}/")\n'
        '    page.wait_for_load_state("load", timeout=30000)\n'
        '    page.locator(".product-thumb").first.hover()\n'
        '    page.wait_for_timeout(500)\n'
        '    page.locator("button.btn-cart:visible").first.click(force=True)\n'
        '    page.wait_for_timeout(2000)\n'
        '    page.locator("#entry_217825 a.cart").click()\n'
        '    page.wait_for_timeout(1000)\n'
        '    assert page.locator("#cart-total-drawer a[href*=\'product_id\']").count() > 0, "Cart drawer items not visible"'
    ),
    "SC-003": (
        f'    page.goto("{_ECOM}/")\n'
        '    page.wait_for_load_state("load", timeout=30000)\n'
        '    page.locator(".product-thumb").first.hover()\n'
        '    page.wait_for_timeout(500)\n'
        '    page.locator("button.btn-cart:visible").first.click(force=True)\n'
        '    page.wait_for_timeout(2000)\n'
        f'    page.goto("{_ECOM}/index.php?route=checkout/cart")\n'
        '    page.wait_for_load_state("load", timeout=20000)\n'
        '    remove = page.locator("button.btn-danger").first\n'
        '    if remove.count() > 0:\n'
        '        remove.click(force=True)\n'
        '        page.wait_for_timeout(1500)\n'
        '    assert page.locator("#content").count() > 0, "Cart page not visible"'
    ),
    "SC-004": (
        f'    page.goto("{_ECOM}/")\n'
        '    page.wait_for_load_state("domcontentloaded", timeout=30000)\n'
        '    search = page.locator("input[name=\'search\']").first\n'
        '    search.wait_for(timeout=15000)\n'
        '    search.fill("iPhone")\n'
        '    search.press("Enter")\n'
        '    page.wait_for_load_state("domcontentloaded")\n'
        '    assert page.locator(".product-thumb").count() > 0, "No search results"'
    ),
    "SC-005": (
        f'    page.goto("{_ECOM}/index.php?route=product/category&path=18")\n'
        '    page.wait_for_load_state("domcontentloaded", timeout=30000)\n'
        '    assert page.locator(".product-thumb").first.is_visible(), "No product tiles"'
    ),
    "SC-006": (
        f'    page.goto("{_ECOM}/index.php?route=product/product&product_id=28")\n'
        '    page.wait_for_load_state("domcontentloaded", timeout=30000)\n'
        '    assert page.locator("h1").first.inner_text().strip() != ""\n'
        '    assert page.locator(".price-new, .price, h2.price").count() > 0'
    ),
    "SC-008": (
        f'    page.goto("{_ECOM}/")\n'
        '    page.wait_for_load_state("load", timeout=30000)\n'
        '    page.locator(".product-thumb").first.hover()\n'
        '    page.wait_for_timeout(500)\n'
        '    page.locator("button.btn-cart:visible").first.click(force=True)\n'
        '    page.wait_for_timeout(2000)\n'
        '    success = page.locator(".toast-body, .alert-success, .toast")\n'
        '    assert success.count() > 0, "No success notification after adding to cart"'
    ),
    "SC-007": (
        f'    page.goto("{_ECOM}/index.php?route=product/category&path=25")\n'
        '    page.wait_for_load_state("domcontentloaded", timeout=30000)\n'
        '    filter_link = page.locator("#column-left .list-group-item").filter(has_text="Apple")\n'
        '    if filter_link.count() == 0:\n'
        '        filter_link = page.locator("#column-left a").filter(has_text="Apple")\n'
        '    if filter_link.count() > 0:\n'
        '        filter_link.first.click()\n'
        '        page.wait_for_load_state("load", timeout=15000)\n'
        '    assert page.locator("#content, #column-right, .product-layout").count() > 0, "Content not visible after filter"'
    ),
}

_FALLBACK_BODY = (
    '    page.goto("{url}")\n'
    '    page.wait_for_load_state("domcontentloaded", timeout=30000)\n'
    '    assert page.title().strip() != "", "Page failed to load"'
)

TEST_HEADER = '''\
"""
Playwright tests — generated by Agentic SDLC pipeline. Do not edit manually.
"""
import pytest

'''


# ── Utilities ────────────────────────────────────────────────────────────────

def _extract_job_id(text: str) -> str:
    m = _JOB_ID_RE.search(text)
    return (m.group(1) or m.group(2)) if m else ""


def _derive_fn_name(sc_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:50]
    return f"test_{sc_id.lower().replace('-', '_')}_{slug}"


def _normalize_body(code: str) -> str:
    """Re-indent every non-empty line to exactly 4 spaces (flattens nested blocks)."""
    return "\n".join("    " + line.strip() for line in code.splitlines() if line.strip())


def _build_test_fn(scenario: dict) -> str:
    sc_id   = scenario["id"]
    fn_name = scenario.get("function_name") or _derive_fn_name(sc_id, scenario.get("title", sc_id))
    title   = scenario.get("title", sc_id).replace('"', "'")
    url     = scenario.get("kane_url", TARGET_URL)
    # Prefer Kane's AI-generated code export; fall back to hardcoded bodies
    kane_code = scenario.get("kane_code", "").strip()
    if kane_code:
        body = _normalize_body(kane_code)  # always normalize indent — cache may have inconsistent code
    else:
        body = PLAYWRIGHT_BODIES.get(sc_id, _FALLBACK_BODY).format(url=url)
    req_id  = scenario.get("requirement_id", "AC-000")
    return (
        f'@pytest.mark.scenario("{sc_id}")\n'
        f'@pytest.mark.requirement("{req_id}")\n'
        f'def {fn_name}(page):\n'
        f'    """{sc_id}: {title}."""\n'
        f'{body}\n'
    )


# ── Stage 2: Sync scenarios ───────────────────────────────────────────────────

def sync_scenarios(requirements: list, existing: list) -> list:
    existing_by_req = {s["requirement_id"]: s for s in existing}
    current_req_ids = {r["id"] for r in requirements}
    max_num = max((int(re.match(r"SC-(\d+)", s.get("id","SC-0")).group(1)) for s in existing if re.match(r"SC-\d+", s.get("id",""))), default=0)
    next_num = max_num + 1
    result = []
    for req in requirements:
        ex = existing_by_req.get(req["id"])
        if ex is None:
            sc_id = f"SC-{next_num:03d}"; tc_id = f"TC-{next_num:03d}"; next_num += 1; status = "new"
        else:
            sc_id = ex["id"]; tc_id = ex.get("test_case_id", sc_id.replace("SC-","TC-"))
            status = "updated" if ex.get("source_description") != req["description"] else "active"
        title   = req.get("kane_one_liner") or req.get("title", req["id"])
        fn_name = ex.get("function_name") if ex else _derive_fn_name(sc_id, title)
        result.append({
            "id": sc_id, "test_case_id": tc_id, "requirement_id": req["id"],
            "title": title, "function_name": fn_name, "status": status,
            "source_description": req["description"],
            "steps": req.get("kane_steps") or [f"Navigate to {TARGET_URL}", "Verify the criterion"],
            "expected_result": req.get("kane_summary") or req["description"],
            "kane_url": TARGET_URL, "last_verified": TODAY,
            "kane_code": req.get("kane_code", ""),
        })
    for sc in existing:
        if sc["requirement_id"] not in current_req_ids:
            result.append({**sc, "status": "deprecated"})
    return result


# ── Stage 3: Generate tests ───────────────────────────────────────────────────

def generate_tests(scenarios: list) -> None:
    active = [s for s in scenarios if s["status"] != "deprecated"]
    lines  = [TEST_HEADER.rstrip(), ""]
    for sc in active:
        lines.append(_build_test_fn(sc).rstrip())
        lines.append("")
    out = Path("tests/playwright/test_scenarios.py")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    for sc in active:
        if not sc.get("function_name"):
            sc["function_name"] = _derive_fn_name(sc["id"], sc.get("title", sc["id"]))
    print(f"[generate_tests] wrote {len(active)} test(s) → {out}")


def _validate_syntax(path: str) -> bool:
    try:
        source = Path(path).read_text(encoding="utf-8")
        compile(source, path, "exec")
        print(f"[validate] {path} — syntax OK", flush=True)
        return True
    except SyntaxError as e:
        lineno = e.lineno or 0
        print(f"[validate] SYNTAX ERROR at line {lineno}: {e.msg}", file=sys.stderr, flush=True)
        try:
            lines = source.splitlines()
            for i in range(max(0, lineno - 3), min(len(lines), lineno + 2)):
                marker = ">>>" if i + 1 == lineno else "   "
                print(f"  {marker} {i+1:3}: {repr(lines[i])}", file=sys.stderr, flush=True)
        except Exception:
            pass
        return False


# ── Stage 4: Test selection ───────────────────────────────────────────────────

def write_test_selection(scenarios: list) -> list:
    selected = [s for s in scenarios if s["status"] != "deprecated"] if FULL_RUN \
               else [s for s in scenarios if s["status"] in ("new", "updated")]
    lines = [f"tests/playwright/test_scenarios.py::{s.get('function_name') or _derive_fn_name(s['id'], s.get('title', s['id']))}"
             for s in selected]
    Path("reports").mkdir(exist_ok=True)
    Path("reports/pytest_selection.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path("reports/test_execution_manifest.json").write_text(json.dumps({
        "selected_scenarios": [s["id"] for s in selected],
        "run_type": "full" if FULL_RUN else "incremental",
    }, indent=2), encoding="utf-8")
    print(f"[test_selection] {len(lines)} test(s) — {'full' if FULL_RUN else 'incremental'} run")
    return selected


# ── Stage 5: HyperExecute ────────────────────────────────────────────────────

def run_hyperexecute() -> str:
    cli = "hyperexecute.exe" if sys.platform == "win32" else "./hyperexecute"
    if not Path(cli).exists():
        print("[hyperexecute] binary not found — skipping")
        return ""
    if not LT_USERNAME or not LT_ACCESS_KEY:
        print("[hyperexecute] credentials missing — skipping")
        return ""
    sel = Path("reports/pytest_selection.txt")
    if not sel.exists() or not sel.read_text().strip():
        print("[hyperexecute] no tests selected — skipping")
        return ""
    cmd = [cli, "--user", LT_USERNAME, "--key", LT_ACCESS_KEY, "--config", "hyperexecute.yaml"]
    t0 = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    combined = result.stdout + result.stderr
    Path("reports/hyperexecute-cli.log").write_text(combined, encoding="utf-8")
    elapsed = round(time.monotonic() - t0, 1)
    print(f"[hyperexecute] exit={result.returncode} duration={elapsed}s")
    for line in combined.splitlines()[:20]:
        print(f"  {line}")
    job_id = _extract_job_id(combined)
    print(f"[hyperexecute] job_id={job_id!r}")
    return job_id


# ── Stage 6: Fetch results via MCP ───────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"```json\s*([\s\S]*?)\s*```")


def _parse_mcp_text(text: str):
    m = _JSON_BLOCK_RE.search(text)
    return json.loads(m.group(1) if m else text.strip())


def _parse_session(s: dict) -> dict:
    name   = s.get("scenario_name") or s.get("name") or s.get("session_name", "")
    parts  = [p.strip() for p in name.split("|")]
    fn     = parts[-1] if len(parts) > 1 else name
    raw    = s.get("status") or s.get("status_ind", "unknown")
    status = "passed" if raw in ("passed","pass","completed") else ("failed" if raw in ("failed","fail","error") else raw)
    tid    = s.get("testID") or s.get("test_id","")
    return {"name": fn, "status": status, "session_link": f"https://automation.lambdatest.com/test?testID={tid}" if tid else ""}


async def _fetch_sessions_api(client: httpx.AsyncClient, job_id: str) -> list:
    tasks, cursor, page = [], None, 0
    while True:
        params = {"limit": 20}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get(f"https://api.hyperexecute.cloud/v2.0/job/{job_id}/sessions",
                                params=params, auth=(LT_USERNAME, LT_ACCESS_KEY))
        if resp.status_code != 200:
            print(f"[sessions_api] {resp.status_code}")
            return []
        data = resp.json()
        tasks.extend(_parse_session(s) for s in data.get("data", []))
        meta = data.get("metadata", {})
        if not meta.get("hasmore"):
            break
        cursor = meta.get("cursor")
        if not cursor:
            break
        page += 1
    return tasks


async def fetch_and_save_results(job_id: str) -> None:
    if not job_id:
        print("[results] no job_id — writing empty results")
        _write_results({}, [], job_id)
        return

    he_tasks: list = []
    job_inner: dict = {}

    if MCP_AVAILABLE:
        mcp_url = f"{MCP_URL}?username={LT_USERNAME}&accessKey={LT_ACCESS_KEY}"
        try:
            async with sse_client(mcp_url, headers={"x-lt-username": LT_USERNAME, "x-lt-access-key": LT_ACCESS_KEY}) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    for attempt in range(30):
                        raw = await session.call_tool("getHyperExecuteJobInfo", {"jobId": job_id})
                        text = raw.content[0].text if raw.content else "{}"
                        try:
                            data = _parse_mcp_text(text)
                            job_inner = data.get("jobInfo") or data.get("data") or data
                        except Exception:
                            job_inner = {}
                        status = job_inner.get("status", "unknown")
                        print(f"[mcp] attempt {attempt+1}/30 — status: {status}")
                        if status in _TERMINAL_HE:
                            break
                        if status not in ("running", "initiated", "queued"):
                            break
                        await asyncio.sleep(30)
        except Exception as exc:
            print(f"[mcp] failed: {exc} — using REST API")

    if not he_tasks and job_id:
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                he_tasks = await _fetch_sessions_api(client, job_id)
            except Exception as exc:
                print(f"[sessions_api] error: {exc}")

    _write_results(job_inner, he_tasks, job_id)


def _write_results(job_inner: dict, he_tasks: list, job_id: str) -> None:
    status = (job_inner.get("status") or "").strip()
    task_pass  = sum(1 for t in he_tasks if t["status"] == "passed")
    task_fail  = len(he_tasks) - task_pass
    Path("reports").mkdir(exist_ok=True)
    Path("reports/api_details.json").write_text(json.dumps({
        "he_summary": {
            "job_id":   job_inner.get("jobId") or job_id,
            "job_link": f"https://hyperexecute.lambdatest.com/hyperexecute/task?jobId={job_id}" if job_id else "",
            "status":   status or "NOT_EXECUTED",
            "task_pass_count": task_pass, "task_fail_count": task_fail,
        },
        "he_tasks": he_tasks,
    }, indent=2), encoding="utf-8")
    print(f"[results] saved {len(he_tasks)} task(s) — pass={task_pass} fail={task_fail}")


# ── Stage 7: Post-pipeline ────────────────────────────────────────────────────

_CRITICAL = ["ci/normalize_artifacts.py", "ci/build_traceability.py",
             "ci/release_recommendation.py", "ci/write_github_summary.py"]
_ADVISORY = ["ci/coverage_analysis.py", "ci/quality_gates.py", "ci/pipeline_metrics.py"]


def post_pipeline() -> None:
    failed = []
    py = sys.executable  # use the same interpreter that's running this script
    for script in _CRITICAL:
        r = subprocess.run([py, script], capture_output=True, text=True)
        if r.stdout: print(r.stdout.strip())
        if r.returncode != 0:
            print(f"[ERROR] {script} failed (exit {r.returncode})", file=sys.stderr)
            if r.stderr: print(r.stderr[:600], file=sys.stderr)
            failed.append(script)
        else:
            print(f"[post] {script} OK")
    for script in _ADVISORY:
        r = subprocess.run([py, script], capture_output=True, text=True)
        if r.stdout: print(r.stdout.strip())
        if r.returncode != 0:
            print(f"[warn] {script} failed (advisory)")
        else:
            print(f"[post] {script} OK")
    if failed:
        print(f"\n[PIPELINE] {len(failed)} critical script(s) failed", file=sys.stderr)
        sys.exit(1)


# ── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    # Force line-buffered stdout so stderr and stdout interleave correctly in CI logs
    sys.stdout.reconfigure(line_buffering=True)
    Path("reports").mkdir(exist_ok=True)
    t0 = time.monotonic()
    print(f"[pipeline] start — FULL_RUN={FULL_RUN}  BUILD={BUILD_NAME}")

    # Stage 2: Sync scenarios
    print_stage_header("2", "MANAGE_SCENARIOS", "Sync scenarios.json with analyzed requirements")
    req_path = Path("requirements/analyzed_requirements.json")
    if not req_path.exists():
        print(f"ERROR: {req_path} not found — run Stage 1 first", file=sys.stderr); sys.exit(1)
    requirements = json.loads(req_path.read_text())
    sc_path  = Path("scenarios/scenarios.json")
    existing = json.loads(sc_path.read_text()) if sc_path.exists() and sc_path.stat().st_size > 2 else []
    scenarios = sync_scenarios(requirements, existing)
    sc_path.write_text(json.dumps(scenarios, indent=2), encoding="utf-8")
    counts = {s: sum(1 for x in scenarios if x["status"] == s) for s in ("new","updated","active","deprecated")}
    print_stage_result("2", "MANAGE_SCENARIOS", counts | {"Total": len(scenarios)})

    # Stage 3: Generate tests
    print_stage_header("3", "GENERATE_TESTS", "Generate Playwright tests from scenarios")
    generate_tests(scenarios)
    sc_path.write_text(json.dumps(scenarios, indent=2), encoding="utf-8")
    if not _validate_syntax("tests/playwright/test_scenarios.py"):
        sys.exit(1)
    print_stage_result("3", "GENERATE_TESTS", {"Active tests": sum(1 for s in scenarios if s["status"] != "deprecated")})

    # Stage 4: Select tests
    print_stage_header("4", "SELECT_TESTS", "Build test execution manifest")
    selected = write_test_selection(scenarios)
    print_stage_result("4", "SELECT_TESTS", {"Selected": len(selected), "Run type": "full" if FULL_RUN else "incremental"})

    # Stage 5: HyperExecute
    print_stage_header("5", "HYPEREXECUTE", "Submit to LambdaTest HyperExecute")
    t5 = time.monotonic()
    job_id = run_hyperexecute()
    print_stage_result("5", "HYPEREXECUTE", {
        "Job ID": job_id or "N/A (skipped)",
        "Concurrency": "5 VMs",
        "Tests submitted": len(selected),
        "Duration": f"{round(time.monotonic()-t5,1)}s",
        "Dashboard": f"https://hyperexecute.lambdatest.com/hyperexecute/task?jobId={job_id}" if job_id else "N/A",
    }, success=bool(job_id) or not LT_USERNAME)

    # Stage 6: Fetch results via MCP
    print_stage_header("6", "FETCH_RESULTS", "Fetch session results via LambdaTest MCP Server")
    await fetch_and_save_results(job_id)
    api = json.loads(Path("reports/api_details.json").read_text())
    print_stage_result("6", "FETCH_RESULTS", {
        "Sessions found": len(api.get("he_tasks", [])),
        "Job status": api.get("he_summary", {}).get("status", "unknown"),
    })

    # Stage 7: Post-pipeline
    print_stage_header("7", "POST_PIPELINE", "Traceability matrix → release verdict → GitHub summary")
    post_pipeline()
    print_stage_result("7", "POST_PIPELINE", {
        "Total pipeline time": f"{round(time.monotonic()-t0,1)}s",
        "Traceability": "reports/traceability_matrix.md",
        "Verdict": "reports/release_recommendation.md",
    })


if __name__ == "__main__":
    asyncio.run(main())
