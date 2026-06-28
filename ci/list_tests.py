#!/usr/bin/env python3
"""HyperExecute test discovery — pure stdlib, no installed packages required.

Priority:
  1. Kane Python exports: tests/playwright/kane/*/test.py  (exact Kane CLI output)
     + SC-008 JS entry (no Kane export for SC-008)
  2. JS spec (all tests, including SC-008)
  3. Python fallback

Kane tests run without browser parametrisation — testmu handles LT connection.
JS/Python tests are parametrized by BROWSERS env var.

Outputs one line per test to stdout for HyperExecute testDiscovery.
"""
import ast
import os
import re
import sys
from pathlib import Path

KANE_DIR  = Path("tests/playwright/kane")
JS_FILE   = Path("tests/playwright/test_scenarios.spec.js")
PY_FILE   = Path("tests/playwright/test_scenarios.py")
BROWSERS  = [b.strip() for b in os.environ.get("BROWSERS", "chrome").split(",")]

SC008_JS  = "tests/playwright/test_scenarios.spec.js::test_sc_008_success_message_appears_on_cart_add[chrome]"

count = 0

# ── 1. Kane Python exports (preferred) ───────────────────────────────────────
if KANE_DIR.exists():
    kane_tests = sorted(KANE_DIR.glob("*/test.py"))
    for test_py in kane_tests:
        print(str(test_py))
        count += 1

# ── 2. JS spec fallback ───────────────────────────────────────────────────────
if count == 0 and JS_FILE.exists():
    content = JS_FILE.read_text(encoding="utf-8")
    test_names = re.findall(r"^test\(['\"](\w+)['\"]", content, re.MULTILINE)
    for name in test_names:
        for browser in BROWSERS:
            print(f"{JS_FILE}::{name}[{browser}]")
            count += 1

# ── 3. Python fallback ────────────────────────────────────────────────────────
elif count == 0 and PY_FILE.exists():
    tree = ast.parse(PY_FILE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            for browser in BROWSERS:
                print(f"{PY_FILE}::{node.name}[{browser}]")
                count += 1

if count == 0:
    print("ERROR: no test files found", file=sys.stderr)
    sys.exit(1)
