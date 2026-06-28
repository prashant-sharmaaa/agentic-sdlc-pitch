#!/usr/bin/env python3
"""Normalise HyperExecute + Kane artifacts into reports/normalized_results.json."""
import json
import sys
from pathlib import Path

# Allow importing fetch_rca from the ci/ directory
sys.path.insert(0, str(Path(__file__).parent))
try:
    from fetch_rca import fetch_rca, trigger_rca, summarize_rca, _extract_session_id
except Exception:
    def fetch_rca(_: str) -> str:            # type: ignore
        return ""
    def trigger_rca(_: list) -> None:        # type: ignore
        pass
    def summarize_rca(raw: str, scenario_id: str = "") -> str:  # type: ignore
        return raw[:300]
    def _extract_session_id(_: str) -> str:  # type: ignore
        return ""


def _extract_fn(node_id: str) -> str:
    """Extract bare function name from pytest node ID.

    Input:  'tests/playwright/test_scenarios.py::test_sc_001_add_to_cart[chrome]'
    Output: 'test_sc_001_add_to_cart'
    """
    # Strip file path prefix (everything up to and including '::')
    fn = node_id.split("::")[-1]
    # Strip parametrize suffix '[browser]'
    fn = fn.split("[")[0]
    return fn.strip()


def main() -> None:
    api = json.loads(Path("reports/api_details.json").read_text()) if Path("reports/api_details.json").exists() else {}
    he_tasks = api.get("he_tasks", [])
    scenarios = json.loads(Path("scenarios/scenarios.json").read_text()) if Path("scenarios/scenarios.json").exists() else []
    sc_by_fn = {s.get("function_name", ""): s for s in scenarios}

    # Build per-scenario result map — prefer 'passed' over 'failed' when retries exist
    best: dict = {}
    for t in he_tasks:
        fn  = _extract_fn(t.get("name", ""))
        sc  = sc_by_fn.get(fn, {})
        sid = sc.get("id", fn)   # fall back to fn if scenario not found
        status = t.get("status", "unknown")
        prev = best.get(sid)
        # Keep 'passed' if either attempt passed; otherwise keep latest
        if prev is None or status == "passed" or prev["status"] == "unknown":
            best[sid] = {
                "scenario_id":    sid,
                "requirement_id": sc.get("requirement_id", ""),
                "test_case_id":   sc.get("test_case_id", ""),
                "function_name":  fn,
                "status":         status,
                "session_link":   t.get("session_link", ""),
                "rca":            "",
            }

    # Bulk-trigger TestMu AI RCA generation for all failed sessions
    failed_results = [r for r in best.values() if r["status"] == "failed" and r.get("session_link")]
    if failed_results:
        failed_sids = [_extract_session_id(r["session_link"]) for r in failed_results]
        failed_sids = [s for s in failed_sids if s]
        if failed_sids:
            print(f"[normalize] triggering TestMu AI RCA for {len(failed_sids)} failed session(s) …")
            trigger_rca(failed_sids)
            # Give the async RCA generation a moment to start
            import time
            time.sleep(5)

    # Fetch and summarize RCA for each failed session
    for result in failed_results:
        print(f"[normalize] fetching RCA for {result['scenario_id']} …")
        raw = fetch_rca(result["session_link"])
        result["rca"] = summarize_rca(raw, result["scenario_id"])

    normalized = list(best.values())
    Path("reports").mkdir(exist_ok=True)
    Path("reports/normalized_results.json").write_text(json.dumps(normalized, indent=2), encoding="utf-8")
    print(f"[normalize] {len(normalized)} unique scenario result(s) written")


if __name__ == "__main__":
    main()
