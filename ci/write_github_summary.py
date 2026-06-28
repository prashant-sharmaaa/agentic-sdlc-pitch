#!/usr/bin/env python3
"""Write rich GitHub Actions Step Summary for the Agentic SDLC pipeline."""
import json
import os
from pathlib import Path

SUMMARY_FILE = os.environ.get("GITHUB_STEP_SUMMARY", "reports/github_summary.md")


def main() -> None:
    requirements = json.loads(Path("requirements/analyzed_requirements.json").read_text()) \
        if Path("requirements/analyzed_requirements.json").exists() else []
    matrix = json.loads(Path("reports/traceability_matrix.json").read_text()) \
        if Path("reports/traceability_matrix.json").exists() else []
    verdict_md = Path("reports/release_recommendation.md").read_text(encoding="utf-8") \
        if Path("reports/release_recommendation.md").exists() else "No verdict generated."
    api = json.loads(Path("reports/api_details.json").read_text()) \
        if Path("reports/api_details.json").exists() else {}

    total = len(matrix)
    passed = sum(1 for r in matrix if r.get("result") == "passed")
    rate   = round(passed / total * 100, 1) if total > 0 else 0
    kane_passed = sum(1 for r in requirements if r.get("kane_status") == "passed")
    he_sum = api.get("he_summary", {})
    job_link = he_sum.get("job_link", "")

    lines = [
        "# 🤖 Agentic SDLC Pipeline — LambdaTest",
        "",
        "## Pipeline Overview",
        "| Stage | Tool | Status |",
        "| --- | --- | --- |",
        f"| 1 · Requirements Verification | KaneAI | {kane_passed}/{len(requirements)} passed |",
        f"| 2-4 · Scenario Sync + Test Gen | Python Orchestrator | {total} scenarios |",
        f"| 5 · Parallel Execution | HyperExecute | {he_sum.get('task_pass_count',0)}✅ {he_sum.get('task_fail_count',0)}❌ |",
        f"| 6 · Result Fetch | LambdaTest MCP Server | {len(api.get('he_tasks',[]))} sessions |",
        f"| 7 · Verdict | Traceability Engine | {rate}% pass rate |",
        "",
    ]

    if job_link:
        lines += [f"🔗 [View HyperExecute Job]({job_link})", ""]

    lines += ["## Traceability Matrix",
              "| Req | Criterion | Scenario | KaneAI | Test Result | RCA |",
              "| --- | --- | --- | --- | --- | --- |"]
    for r in matrix:
        link = r.get('link', '')
        result_cell = f"[{r['result']}]({link})" if link else r['result']
        rca_raw = r.get('rca', '')
        rca_cell = (rca_raw[:80] + "…") if len(rca_raw) > 80 else rca_raw
        lines.append(f"| {r['req_id']} | {r['criterion'][:60]} | {r['sc_id']} | {r['kane']} | {r['overall']} {result_cell} | {rca_cell} |")

    lines += ["", "## Release Verdict", "", verdict_md]

    content = "\n".join(lines)
    Path("reports").mkdir(exist_ok=True)
    Path(SUMMARY_FILE).write_text(content, encoding="utf-8")
    print(f"[summary] written → {SUMMARY_FILE}")

if __name__ == "__main__":
    main()
