#!/usr/bin/env python3
"""
Agentic SDLC Pitch — Demo Mode

Instantly shows real results from the last pipeline run without re-running anything.
Results are pulled from reports/demo_cache.json — written by the pipelines after
every run. Zero fake data.

Usage:
    python3 ci/demo.py                   # show traceability matrix + HE links
    python3 ci/demo.py --flow1           # re-trigger Flow 1 HE job link only
    python3 ci/demo.py --flow2           # re-trigger Flow 2 HE job link only
    python3 ci/demo.py --rebuild         # rebuild matrix from latest data, then show
"""
import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

CI_DIR     = Path(__file__).parent
DEMO_CACHE = CI_DIR.parent / "reports" / "demo_cache.json"


def _load_cache() -> dict:
    if not DEMO_CACHE.exists():
        print("No demo cache found. Run a pipeline first:")
        print("  LT_ACCESS_KEY=<key> python3 ci/flow1_pipeline.py")
        print("  LT_ACCESS_KEY=<key> python3 ci/flow2_pipeline.py --skip-phase1")
        sys.exit(1)
    return json.loads(DEMO_CACHE.read_text())


def print_matrix(cache: dict):
    rows    = cache["rows"]
    summary = cache["summary"]
    he_jobs = cache.get("he_jobs", {})
    ts      = cache.get("generated_at", "unknown")

    # Header
    w = 72
    print("\n" + "=" * w)
    print("  AGENTIC SDLC — LIVE TRACEABILITY MATRIX")
    print(f"  Last run: {ts[:19]}")
    print("=" * w)

    # Table
    print(f"\n{'AC':<8} {'Scenario':<46} {'TC':<12} {'Result'}")
    print("-" * w)
    for r in rows:
        sc_name = r.get("sc_name", r["sc_id"])[:44]
        tc = r.get("tc_internal", "pending")
        print(f"{r['ac_id']:<8} {sc_name:<46} {tc:<12} {r['overall']} {r['status']}")

    # Summary bar
    s = summary
    bar_len  = 40
    filled   = round(s["pass_rate"] / 100 * bar_len)
    bar      = "█" * filled + "░" * (bar_len - filled)
    print(f"\n  [{bar}] {s['pass_rate']}%")
    print(f"  {s['passed']} passed  {s['failed']} failed  {s['not_run']} not run  / {s['total']} total ACs\n")

    # HE Job links
    if he_jobs:
        print("  HyperExecute Jobs:")
        for flow, info in he_jobs.items():
            link = info.get("job_link", "")
            ts2  = info.get("ts", "")
            print(f"    {flow.upper()}: {link}  [{ts2}]")
        print()

    # Failed detail + RCA
    failed = [r for r in rows if r["status"] == "failed"]
    if failed:
        print("  Failed scenarios — AI Root Cause Analysis:")
        for r in failed:
            sc_name = r.get("sc_name", r["sc_id"])
            print(f"\n    ❌ {r['sc_id']} ({r['ac_id']}): {sc_name}")
            print(f"       {r['criterion'][:70]}")
            if r.get("rca"):
                for line in r["rca"].splitlines():
                    print(f"       {line}")
            elif r.get("failure_detail"):
                snippet = r["failure_detail"].replace("\n", " ")[:120]
                print(f"       → {snippet}")
            if r.get("session_link"):
                print(f"       Session: {r['session_link']}")
        print()

    print("=" * w)

    # Chain summary
    print("\n  Traceability chain:")
    print("  Requirements → ACs → [Claude] → kane-cli Objectives → KaneAI Test Cases")
    print("  → HyperExecute (Flow 1: code export / Flow 2: TM API) → Results")
    print()


def open_he_links(cache: dict, flow: str = None):
    he_jobs = cache.get("he_jobs", {})
    targets = {flow: he_jobs[flow]} if flow and flow in he_jobs else he_jobs
    for f, info in targets.items():
        link = info.get("job_link")
        if link:
            print(f"  Opening {f.upper()} job: {link}")
            webbrowser.open(link)


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--rebuild" in args:
        print("Rebuilding matrix from latest pipeline data...")
        sys.path.insert(0, str(CI_DIR))
        from traceability import run_traceability
        run_traceability()

    cache = _load_cache()
    print_matrix(cache)

    if "--flow1" in args:
        open_he_links(cache, "flow1")
    elif "--flow2" in args:
        open_he_links(cache, "flow2")
    elif "--open" in args:
        open_he_links(cache)
