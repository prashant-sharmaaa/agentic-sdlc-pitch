#!/usr/bin/env python3
"""
Agentic SDLC — Traceability Engine

Builds a live requirements → test case → result matrix from actual pipeline data.
No fake pre-seeded data — all results come from real kane-cli / HyperExecute runs.

Data sources (all written by the pipelines, never hand-crafted):
    ci/objectives.json       SC-id → AC-id mapping + objective text
    ci/run_history.json      SC-id → last status + failure detail (from self_heal.py)
    ci/tm_test_cases.json    SC-id → TM test case id + title + TC number (from flow2)
    ci/he_jobs.json          flow1/flow2 → HE job id + link + timestamp
    requirements/analyzed_requirements.json   AC descriptions

Outputs:
    reports/traceability_matrix.md    human-readable table (for GitHub Summary, pitch deck)
    reports/traceability_matrix.json  machine-readable
    reports/demo_cache.json           cached real results for --demo mode

Usage:
    python3 ci/traceability.py           # build matrix from latest pipeline data
    python3 ci/traceability.py --print   # also print the table to stdout
"""
import json
import sys
from datetime import datetime
from pathlib import Path

CI_DIR       = Path(__file__).parent
PROJECT_ROOT = CI_DIR.parent
REPORTS_DIR  = PROJECT_ROOT / "reports"

# Pipeline-written data files
OBJECTIVES_FILE   = CI_DIR / "objectives.json"
HISTORY_FILE      = CI_DIR / "run_history.json"
TM_TC_FILE        = CI_DIR / "tm_test_cases.json"
HE_JOBS_FILE      = CI_DIR / "he_jobs.json"
RCA_FILE          = CI_DIR / "rca_results.json"
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements" / "analyzed_requirements.json"

# Outputs
MATRIX_MD   = REPORTS_DIR / "traceability_matrix.md"
MATRIX_JSON = REPORTS_DIR / "traceability_matrix.json"
DEMO_CACHE  = REPORTS_DIR / "demo_cache.json"


def _load(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def build_matrix() -> dict:
    """Build and return the full traceability matrix from live pipeline data."""
    requirements = _load(REQUIREMENTS_FILE, [])
    objectives   = _load(OBJECTIVES_FILE, [])
    history      = _load(HISTORY_FILE, {})
    tm_tcs       = _load(TM_TC_FILE, {})      # sc_id → {tm_id, title, internal_id}
    he_jobs      = _load(HE_JOBS_FILE, {})     # "flow1"/"flow2" → {job_id, job_link, ts}
    rca_results  = _load(RCA_FILE, {})         # sc_id → {rca, raw, session_link}

    # Build lookup: ac_id → objective entry
    ac_to_sc = {o["ac_id"]: o for o in objectives if "ac_id" in o}

    rows = []
    for req in requirements:
        ac_id = req["id"]
        obj   = ac_to_sc.get(ac_id, {})
        sc_id = obj.get("id", "—")

        # Result from run_history (written by pipeline after every Phase 1)
        hist  = history.get(sc_id, {})
        status = hist.get("status") or "not_run"
        failure_detail = hist.get("failure_detail", "")
        last_run_ts    = hist.get("updated_at", "")
        flow           = hist.get("flow", "")

        # TM test case (written by flow2 after Phase 2)
        tm = tm_tcs.get(sc_id, {})
        tm_id       = tm.get("tm_id", "")
        tc_internal = tm.get("internal_id", "—")   # e.g. TC-41514
        # Build TM test case URL: link uses tm_id (UUID), label shows tc_internal (TC-NNNNN)
        TM_BASE = f"https://test-manager.lambdatest.com/projects/01KVXJ82AKT83GWJNFZTQVMNRQ/test-cases"
        tc_link = f"[{tc_internal}]({TM_BASE}/{tm_id})" if tm_id else "—"

        # RCA (written by rca.py after HE job)
        rca_entry   = rca_results.get(sc_id, {})
        rca_text    = rca_entry.get("rca", "")
        session_link = rca_entry.get("session_link", "")

        overall = "✅" if status == "passed" else ("❌" if status == "failed" else "⏭")
        rows.append({
            "ac_id":          ac_id,
            "criterion":      req["description"],
            "sc_id":          sc_id,
            "sc_name":        obj.get("name", sc_id),
            "objective":      obj.get("objective", ""),
            "tm_id":          tm_id,
            "tc_internal":    tc_internal,
            "tc_link":        tc_link,
            "status":         status,
            "overall":        overall,
            "failure_detail": failure_detail,
            "rca":            rca_text,
            "session_link":   session_link,
            "last_run_ts":    last_run_ts,
            "flow":           flow,
        })

    passed   = sum(1 for r in rows if r["status"] == "passed")
    failed   = sum(1 for r in rows if r["status"] == "failed")
    not_run  = sum(1 for r in rows if r["status"] == "not_run")
    total    = len(rows)
    pass_pct = round(passed / total * 100, 1) if total else 0

    return {
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total": total, "passed": passed, "failed": failed,
            "not_run": not_run, "pass_rate": pass_pct,
        },
        "he_jobs": he_jobs,
        "rows": rows,
    }


def write_markdown(matrix: dict) -> str:
    rows    = matrix["rows"]
    summary = matrix["summary"]
    he_jobs = matrix["he_jobs"]
    ts      = matrix["generated_at"]

    TM_PROJECT_URL = "https://test-manager.lambdatest.com/projects/01KVXJ82AKT83GWJNFZTQVMNRQ/test-cases"

    lines = [
        "# Agentic SDLC — Traceability Matrix",
        f"\n_Generated: {ts}_\n",
        "## Requirement → Scenario → Test Case → Result\n",
        "| AC | Acceptance Criterion | Scenario Name | TM Test Case | Status | Result | RCA |",
        "| -- | -------------------- | ------------- | ------------ | ------ | ------ | --- |",
    ]
    for r in rows:
        tm_id   = r.get("tm_id", "")
        sc_name = r.get("sc_name", r["sc_id"])
        # Scenario Name: hyperlink to TM test case page if available
        sc_cell = f"[{sc_name[:45]}]({TM_PROJECT_URL}/{tm_id})" if tm_id else sc_name[:45]
        # TM TC column: hyperlinked TC-NNNNN label or pending
        tc_cell = r.get("tc_link") or ("pending" if not tm_id else r["tc_internal"])
        # RCA: prefer LT AI RCA, fall back to kane-cli failure detail
        rca_val = r.get("rca") or (r.get("failure_detail", "")[:80] if r["status"] == "failed" else "")
        rca_snippet = (rca_val[:60] + "…") if len(rca_val) > 60 else (rca_val or "—")
        lines.append(
            f"| {r['ac_id']} | {r['criterion'][:55]} "
            f"| {sc_cell} | {tc_cell} | {r['status']} | {r['overall']} | {rca_snippet} |"
        )

    lines += [
        "",
        "## Summary",
        f"- **Total ACs:** {summary['total']}",
        f"- **Passed:** {summary['passed']}  |  "
        f"**Failed:** {summary['failed']}  |  "
        f"**Not run:** {summary['not_run']}",
        f"- **Pass rate:** {summary['pass_rate']}%",
    ]

    if he_jobs:
        lines += ["", "## HyperExecute Jobs"]
        for flow, info in he_jobs.items():
            link = info.get("job_link", "")
            jid  = info.get("job_id", "")
            ts2  = info.get("ts", "")
            lines.append(f"- **{flow}:** [{jid}]({link}) — {ts2}")

    failed_rows = [r for r in rows if r["status"] == "failed"]
    if failed_rows:
        lines += ["", "## Failed Scenarios — Root Cause Analysis"]
        for r in failed_rows:
            lines.append(f"\n### {r['sc_id']} — {r.get('sc_name', r['sc_id'])}")
            lines.append(f"**AC:** {r['ac_id']} — {r['criterion']}")
            lines.append(f"\n**Objective:** {r['objective']}")
            if r.get("session_link"):
                lines.append(f"\n**Session:** [{r['session_link']}]({r['session_link']})")
            if r.get("rca"):
                lines.append(f"\n**AI RCA (LambdaTest):**\n\n{r['rca']}")
            elif r.get("failure_detail"):
                snippet = r["failure_detail"][:300]
                lines.append(f"\n**Failure detail:**\n```\n{snippet}\n```")

    return "\n".join(lines)


def save(matrix: dict, *, print_table: bool = False):
    REPORTS_DIR.mkdir(exist_ok=True)

    md = write_markdown(matrix)
    MATRIX_MD.write_text(md, encoding="utf-8")
    MATRIX_JSON.write_text(json.dumps(matrix, indent=2), encoding="utf-8")
    DEMO_CACHE.write_text(json.dumps(matrix, indent=2), encoding="utf-8")

    s = matrix["summary"]
    print(f"[traceability] {s['total']} ACs — {s['passed']} passed "
          f"({s['pass_rate']}%) — written to {MATRIX_MD.name}")

    if print_table:
        print("\n" + md)


# ── Public API used by pipelines ──────────────────────────────────────────────

def record_he_job(flow: str, job_id: str, job_link: str):
    """Called by pipeline after Phase 3 to persist HE job info."""
    jobs = _load(HE_JOBS_FILE, {})
    jobs[flow] = {
        "job_id":   job_id,
        "job_link": job_link,
        "ts":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    HE_JOBS_FILE.write_text(json.dumps(jobs, indent=2))


def record_tm_test_cases(test_cases: list):
    """Called by flow2 after Phase 2 to persist TM test case IDs per SC."""
    # test_cases is [{test_case_id, title, internal_id}]
    # We need SC-id → TM data. Title encodes the SC name set during flow2.
    # Match by position in objectives.json (same order as kane_results)
    existing = _load(TM_TC_FILE, {})
    objectives = _load(OBJECTIVES_FILE, [])

    # Map TM test case back to SC via objectives order
    # flow2 preserves order: test_cases list aligns with the passed objectives
    for tc in test_cases:
        # Try to find SC by matching TM title against objective names
        matched_sc = None
        for obj in objectives:
            if tc.get("test_case_id") and obj.get("id"):
                # Can't match by name alone — store by tm_id and let history correlate
                pass
        existing[tc.get("test_case_id", "")] = {
            "tm_id":       tc.get("test_case_id", ""),
            "internal_id": tc.get("internal_id", ""),
            "title":       tc.get("title", ""),
        }

    TM_TC_FILE.write_text(json.dumps(existing, indent=2))


def record_tm_test_cases_with_sc(kane_results: list, test_cases: list):
    """
    Called by flow2 after Phase 2.
    kane_results has sc_id + testcase_id; test_cases has tm details.
    Builds sc_id → TM data mapping.
    """
    # Build testcase_id → TM details
    tm_by_tc_id = {tc["test_case_id"]: tc for tc in test_cases}

    existing = _load(TM_TC_FILE, {})
    for r in kane_results:
        sc_id = r.get("sc_id")
        tc_id = r.get("testcase_id")
        if sc_id and tc_id and tc_id in tm_by_tc_id:
            tm = tm_by_tc_id[tc_id]
            existing[sc_id] = {
                "tm_id":       tc_id,
                "internal_id": tm.get("internal_id", ""),
                "title":       tm.get("title", ""),
            }

    TM_TC_FILE.write_text(json.dumps(existing, indent=2))


def run_traceability(log=None):
    """Build matrix and save. Called by pipelines after each run."""
    matrix = build_matrix()
    save(matrix)
    if log:
        s = matrix["summary"]
        log.info(f"[traceability] {s['total']} ACs — {s['passed']} passed ({s['pass_rate']}%)")
        log.info(f"[traceability] Matrix → {MATRIX_MD}")
    return matrix


if __name__ == "__main__":
    print_flag = "--print" in sys.argv
    m = build_matrix()
    save(m, print_table=print_flag)
