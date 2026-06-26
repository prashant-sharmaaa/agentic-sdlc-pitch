#!/usr/bin/env python3
"""
HyperExecute test discovery — pure stdlib, no installed packages required.
Reads test_scenarios.py via AST and emits parametrized pytest node IDs.
Runs on the HE coordinator VM before workers start (no pip install runs there).
"""
import ast
import os
import sys
from pathlib import Path

TEST_FILE = Path("tests/playwright/test_scenarios.py")
BROWSERS = [b.strip() for b in os.environ.get("BROWSERS", "chrome").split(",")]

if not TEST_FILE.exists():
    print(f"ERROR: {TEST_FILE} not found", file=sys.stderr)
    sys.exit(1)

tree = ast.parse(TEST_FILE.read_text(encoding="utf-8"))

count = 0
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
        for browser in BROWSERS:
            print(f"{TEST_FILE}::{node.name}[{browser}]")
            count += 1

if count == 0:
    print("ERROR: no test functions found in test_scenarios.py", file=sys.stderr)
    sys.exit(1)
