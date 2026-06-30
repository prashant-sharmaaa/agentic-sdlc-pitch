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
    # analyzed_requirements.json is {"base_url":..., "acceptance_criteria":[...]}
    if isinstance(requirements, dict):
        requirements = requirements.get("acceptance_criteria", [])
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

        # Skip ACs that have no corresponding SC (not in current test scope)
        if sc_id == "—":
            continue

        # Result from run_history (written by pipeline after every Phase 1 + HE)
        hist             = history.get(sc_id, {})
        status           = hist.get("status") or "not_run"          # overall = HE execution result
        authoring_status = hist.get("authoring_status") or "not_run"  # Phase 1 kane-cli result
        he_status        = hist.get("he_status") or ""               # explicit HE field (may be absent on old runs)
        failure_detail   = hist.get("failure_detail", "")
        last_run_ts      = hist.get("updated_at", "")
        flow             = hist.get("flow", "")

        # TM test case (written by flow2 after Phase 2)
        tm = tm_tcs.get(sc_id, {})
        tm_id       = tm.get("tm_id", "")
        tc_internal = tm.get("internal_id", "—")   # e.g. TC-41514
        # Build TM test case URL: link uses tm_id (UUID), label shows tc_internal (TC-NNNNN)
        TM_BASE = "https://test-manager.lambdatest.com/projects/01KVXJ82AKT83GWJNFZTQVMNRQ/test-cases"
        tc_link = f"[{tc_internal}]({TM_BASE}/{tm_id}/dashboard?type=summary)" if tm_id else "—"

        # RCA (written by rca.py after HE job)
        rca_entry   = rca_results.get(sc_id, {})
        rca_text    = rca_entry.get("rca", "")
        session_link = rca_entry.get("session_link", "")

        def _icon(s): return "✅" if s == "passed" else ("❌" if s == "failed" else "—")
        exec_st = he_status or status
        rows.append({
            "ac_id":            ac_id,
            "criterion":        req["description"],
            "sc_id":            sc_id,
            "sc_name":          obj.get("name", sc_id),
            "objective":        obj.get("objective", ""),
            "tm_id":            tm_id,
            "tc_internal":      tc_internal,
            "tc_link":          tc_link,
            "status":           status,
            "authoring_status": authoring_status,
            "he_status":        he_status or status,
            "overall":          _icon(exec_st),   # kept for demo.py backwards compat
            "authoring_icon":   _icon(authoring_status),
            "he_icon":          _icon(exec_st),
            "failure_detail":   failure_detail,
            "rca":              rca_text,
            "rca_source":       rca_entry.get("source", ""),
            "session_link":     session_link,
            "last_run_ts":      last_run_ts,
            "flow":             flow,
        })

    passed   = sum(1 for r in rows if r["he_status"] == "passed")
    failed   = sum(1 for r in rows if r["he_status"] == "failed")
    not_run  = sum(1 for r in rows if r["he_status"] not in ("passed", "failed"))
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


def _clean_failure_detail(text: str, max_len: int = 0) -> str:
    """
    Extract the human-readable part of failure_detail.
    Strips [run summary]: / [raw tail]: tags inserted by run_kane().
    If both sections exist, returns the run summary (the narrative) — it is
    more readable than the raw NDJSON tail.
    """
    if not text:
        return ""
    if "[run summary]:" in text:
        summary = text.split("\n[raw tail]:")[0].replace("[run summary]:", "").strip()
        result = summary if summary else text
    else:
        result = text
    return result[:max_len] if max_len else result


def write_markdown(matrix: dict) -> str:
    rows    = matrix["rows"]
    summary = matrix["summary"]
    he_jobs = matrix["he_jobs"]
    ts      = matrix["generated_at"]

    lines = [
        "# Agentic SDLC — Traceability Matrix",
        f"\n_Generated: {ts}_\n",
    ]

    # ── HyperExecute job link at the top ──────────────────────────────────────
    if he_jobs:
        for flow, info in he_jobs.items():
            link       = info.get("job_link", "")
            jid        = info.get("job_id", "")
            ts2        = info.get("ts", "")
            report_url = info.get("tm_report_url", "")
            he_text    = f"[{jid}]({link})" if link else (jid or "—")
            he_line    = f"**HyperExecute Job:** {he_text} — {ts2}"
            if report_url:
                he_line += f" &nbsp;|&nbsp; [📋 TM Test Run Report]({report_url})"
            elif link:
                he_line += f" &nbsp;|&nbsp; [🔗 HE Dashboard]({link})"
            lines.append(he_line)
        lines.append("")

    # ── Summary ───────────────────────────────────────────────────────────────
    last_ts = max((r["last_run_ts"] for r in rows if r.get("last_run_ts")), default="")
    last_ts_note = f"\n_Last run: {last_ts[:19].replace('T', ' ')}_\n" if last_ts else "\n"
    lines += [
        "## Summary\n",
        f"| Total ACs | ✅ Passed | ❌ Failed | Pass Rate |",
        f"| --------- | --------- | --------- | --------- |",
        f"| {summary['total']} | {summary['passed']} | {summary['failed']} | {summary['pass_rate']}% |",
        last_ts_note,
    ]

    # ── Traceability table with bifurcation ───────────────────────────────────
    lines += [
        "## Requirement → Scenario → Test Case → Result\n",
        "| AC | Acceptance Criterion | Scenario | TM Test Case | Authoring | Execution | RCA |",
        "| -- | -------------------- | -------- | ------------ | :-------: | :-------: | --- |",
    ]
    for r in rows:
        tm_id   = r.get("tm_id", "")
        sc_name = r.get("sc_name", r["sc_id"])
        tc_cell = r.get("tc_link") or ("pending" if not tm_id else r["tc_internal"])
        # Table cell: only show clean LT AI RCA (bullet points) — never failure_detail noise
        rca_val = r.get("rca", "") if r.get("rca_source") != "claude-fallback" else ""
        rca_snippet = (rca_val[:150] + "…") if len(rca_val) > 150 else (rca_val or "—")
        lines.append(
            f"| {r['ac_id']} | {r['criterion']} | {sc_name} | {tc_cell} "
            f"| {r['authoring_icon']} | {r['he_icon']} | {rca_snippet} |"
        )

    # ── Scenario Objectives ───────────────────────────────────────────────────
    lines += [
        "",
        "## Scenario Objectives\n",
        "| SC | Scenario Name | Objective |",
        "| -- | ------------- | --------- |",
    ]
    for r in rows:
        lines.append(f"| {r['sc_id']} | {r.get('sc_name', r['sc_id'])} | {r.get('objective', '—')} |")

    # ── Failed Scenarios RCA ──────────────────────────────────────────────────
    # Show authoring failures and execution failures in separate subsections
    authoring_failed = [r for r in rows if r["authoring_status"] == "failed"]
    exec_failed      = [r for r in rows if r["he_status"] == "failed" and r["authoring_status"] == "passed"]

    if authoring_failed or exec_failed:
        lines += ["", "## Root Cause Analysis\n"]

    if authoring_failed:
        lines.append("### Phase 1 — Authoring Failures (kane-cli could not author the test)\n")
        for r in authoring_failed:
            lines.append(f"#### {r['sc_id']} — {r.get('sc_name', r['sc_id'])}")
            lines.append(f"**AC:** {r['ac_id']} — {r['criterion']}")
            lines.append(f"\n**Objective given to kane-cli:**\n> {r['objective']}\n")
            if r.get("rca"):
                lines.append(f"**AI RCA:**\n\n{r['rca']}\n")
            elif r.get("failure_detail"):
                snippet = _clean_failure_detail(r["failure_detail"], 500)
                lines.append(f"**What kane-cli did:**\n```\n{snippet}\n```\n")

    if exec_failed:
        lines.append("### Phase 3 — Execution Failures (authored OK, failed on HyperExecute)\n")
        for r in exec_failed:
            lines.append(f"#### {r['sc_id']} — {r.get('sc_name', r['sc_id'])}")
            lines.append(f"**AC:** {r['ac_id']} — {r['criterion']}")
            lines.append(f"\n**Objective:** {r['objective']}\n")
            if r.get("session_link"):
                lines.append(f"**Session:** {r['session_link']}\n")
            if r.get("rca"):
                lines.append(f"**AI RCA:**\n\n{r['rca']}\n")

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

def record_he_job(flow: str, job_id: str, job_link: str, tm_report_url: str = ""):
    """Called by pipeline after Phase 3 to persist HE job info."""
    jobs = _load(HE_JOBS_FILE, {})
    jobs[flow] = {
        "job_id":        job_id,
        "job_link":      job_link,
        "tm_report_url": tm_report_url,
        "ts":            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
