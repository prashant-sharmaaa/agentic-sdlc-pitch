#!/usr/bin/env python3
"""
Flow 2: KaneAI Test Cases → Test Manager → HyperExecute (auto)

Steps:
  1. Create a test run with the 7 SC test case IDs
  2. Trigger execution via HE API — LT routes to HyperExecute automatically
  3. Print job link
"""
import base64
import json
import os
import sys
import urllib.request
from datetime import datetime

# ── Credentials ───────────────────────────────────────────────────────────────
LT_USERNAME   = os.environ.get("LT_USERNAME", "gagandeepb")
LT_ACCESS_KEY = os.environ.get("LT_ACCESS_KEY")

if not LT_ACCESS_KEY:
    print("ERROR: LT_ACCESS_KEY env var not set", file=sys.stderr)
    sys.exit(1)

AUTH = "Basic " + base64.b64encode(f"{LT_USERNAME}:{LT_ACCESS_KEY}".encode()).decode()

# ── Config ────────────────────────────────────────────────────────────────────
TM_API    = "https://test-manager-api.lambdatest.com/api/v1"
HE_API    = "https://test-manager-api.lambdatest.com/api/atm/v1/hyperexecute"
PROJECT_ID        = "01KVXJ82AKT83GWJNFZTQVMNRQ"  # kane-agentic
TM_ENVIRONMENT_ID = 282603                         # "Windows Config" — Win10, Firefox 150, desktop web

BUILD_NAME = f"Agentic SDLC | KaneAI Flow2 | {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"

# ── SC Test Case IDs — from TM project kane-agentic (01KVXJ82AKT83GWJNFZTQVMNRQ)
# NOTE: these are TM test_case_id ULIDs, NOT kane-cli session testcase_ids
TEST_CASES = [
    {"id": "01KW1T9WTR2C22NDSWVHJAY86H", "name": "SC-001: Add to cart updates counter instantly"},      # TC-41427
    {"id": "01KW1T7RGT4KZVVEWAEAD2D7M9", "name": "SC-002: Cart dropdown shows item names and prices"},   # TC-41425
    {"id": "01KW1NS775V5Q7102744G7PT03", "name": "SC-003: Remove item recalculates cart total"},          # TC-41389
    {"id": "01KW1TBFDPNK3FEFMVJE4N7E6B", "name": "SC-004: Search returns relevant product results"},     # TC-41428
    {"id": "01KW1TE579VJNZMTSCFGQNNFW6", "name": "SC-005: Catalog displays product tiles with pricing"}, # TC-41430
    {"id": "01KW1TE572K6HBVVZ9A0DHENDK", "name": "SC-006: Product tile opens detail page"},              # TC-41429
    {"id": "01KW1Q2ACTK4N175XGZS64NKSX", "name": "SC-007: Category filter narrows product list"},        # TC-41402
]


def api_call(url, payload, method="POST"):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": AUTH, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP {e.code} {e.reason}: {body}", file=sys.stderr)
        raise


def main():
    # ── Step 1: Create test run ────────────────────────────────────────────────
    print(f"[1] Creating test run: {BUILD_NAME}")
    instances = [
        {"test_case_id": tc["id"], "name": tc["name"], "priority": "Medium", "serial_no": i + 1}
        for i, tc in enumerate(TEST_CASES)
    ]
    run_payload = {
        "title":               BUILD_NAME,
        "objective":           "Agentic SDLC pitch — KaneAI Flow 2 automated trigger",
        "project_id":          PROJECT_ID,
        "is_auteur_generated": True,
        "tags":                ["agentic-sdlc", "kaneai", "flow2"],
        "test_run_instances":  instances,
    }

    run_resp = api_call(f"{TM_API}/test-run", run_payload)
    test_run_id = run_resp.get("id")
    if not test_run_id:
        print(f"ERROR: failed to create test run: {run_resp}", file=sys.stderr)
        sys.exit(1)
    print(f"    test_run_id: {test_run_id}")

    # ── Step 2: Link instances via PUT /test-run/{id} ─────────────────────────
    # Each instance gets environment_id so HE trigger doesn't fail with "empty configurations"
    print("[2] Linking test case instances to run...")
    instances_with_env = [
        {**inst, "environment_id": TM_ENVIRONMENT_ID}
        for inst in instances
    ]
    link_payload = {
        "id":                  test_run_id,
        "title":               BUILD_NAME,
        "project_id":          PROJECT_ID,
        "objective":           "Agentic SDLC pitch — KaneAI Flow 2 automated trigger",
        "is_auteur_generated": True,
        "tags":                ["agentic-sdlc", "kaneai", "flow2"],
        "test_run_instances":  instances_with_env,
    }
    link_resp = api_call(f"{TM_API}/test-run/{test_run_id}", link_payload, method="PUT")
    print(f"    linked: {link_resp.get('message', link_resp)}")

    # ── Step 3: Trigger HyperExecute ──────────────────────────────────────────
    print("[3] Triggering HyperExecute execution...")
    he_payload = {
        "test_run_id":      test_run_id,
        "concurrency":      5,
        "title":            BUILD_NAME,
        "retry_on_failure": True,
        "max_retries":      1,
        "report_enabled":   True,
        "console_log":      True,
        "network_logs":     True,
    }

    he_resp = api_call(HE_API, he_payload)
    job_id   = he_resp.get("job_id")
    job_link = he_resp.get("job_link")

    print(f"\n{'='*60}")
    print(f"  Job ID  : {job_id}")
    print(f"  Job Link: {job_link}")
    print(f"{'='*60}")
    print("\nMonitor at: https://hyperexecute.lambdatest.com/")


if __name__ == "__main__":
    main()
