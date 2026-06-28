#!/usr/bin/env python3
"""HyperExecute test runner.

Accepts test IDs from list_tests.py:

  Kane format:   tests/playwright/kane/SC-001/test.py
                 → python3 tests/playwright/kane/SC-001/test.py

  JS format:     tests/playwright/test_scenarios.spec.js::fn_name[chrome]
                 → BROWSER_NAME=chrome npx playwright test <spec> --grep fn_name

  Python format: tests/playwright/test_scenarios.py::fn_name[chrome]
                 → remapped to JS spec (legacy fallback)
"""
import os
import re
import subprocess
import sys
from pathlib import Path

JS_SPEC = "tests/playwright/test_scenarios.spec.js"


def _run_kane_test(test_py: str) -> int:
    """Run a Kane-exported test.py with the system Python (testmu installed)."""
    env = {**os.environ}
    # testmu reads LT_USERNAME / LT_ACCESS_KEY from env (already set by HE)
    # BUILD env var overrides testmu.configure(build=...) if needed
    build = os.environ.get("BUILD", os.environ.get("BUILD_NAME", ""))
    if build:
        env["BUILD"] = build
    cmd = [sys.executable, test_py]
    print(f"[run_pw_test] running Kane test: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, env=env).returncode


def _run_js_test(spec_file: str, fn_name: str, browser: str) -> int:
    env = {**os.environ, "BROWSER_NAME": browser}
    cmd = [
        "npx", "playwright", "test",
        spec_file,
        "--grep", fn_name,
        "--project", browser,
        "--reporter=junit,json",
    ]
    return subprocess.run(cmd, env=env).returncode


def main() -> None:
    test_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("test", "")
    if not test_id:
        print("ERROR: no test ID provided", file=sys.stderr)
        sys.exit(1)

    # Kane format: tests/playwright/kane/SC-001/test.py
    if re.match(r"tests/playwright/kane/[^/]+/test\.py$", test_id):
        sys.exit(_run_kane_test(test_id))

    # JS format: tests/playwright/test_scenarios.spec.js::fn_name[browser]
    m = re.match(r"(.+\.spec\.js)::(.+)\[(\w+)\]$", test_id)
    if m:
        spec_file, fn_name, browser = m.group(1), m.group(2), m.group(3)
        sys.exit(_run_js_test(spec_file, fn_name, browser))

    # Python fallback: tests/playwright/test_scenarios.py::fn_name[browser]
    m2 = re.match(r".+\.py::(\w+)(?:\[(\w+)\])?$", test_id)
    if m2:
        fn_name = m2.group(1)
        browser = m2.group(2) or os.environ.get("BROWSER_NAME", "chrome")
        print(f"[run_pw_test] remapped Python ID → JS spec: {fn_name}[{browser}]")
        sys.exit(_run_js_test(JS_SPEC, fn_name, browser))

    print(f"ERROR: unrecognised test ID: {test_id}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
