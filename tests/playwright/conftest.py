"""
Playwright conftest — connects to LambdaTest CDP for cloud execution,
falls back to local Chromium when LT credentials are absent.
BROWSERS env var (comma-separated) drives multi-browser parametrization.

playwright.sync_api is imported lazily inside the fixture so that
pytest --collect-only succeeds even before playwright is installed
(e.g. on HyperExecute VMs during testDiscovery).
"""
import json
import os
import re
from urllib.parse import quote

import pytest

LT_USERNAME   = os.environ.get("LT_USERNAME", "")
LT_ACCESS_KEY = os.environ.get("LT_ACCESS_KEY", "")
BROWSERS      = [b.strip() for b in os.environ.get("BROWSERS", "chrome").split(",")]
BUILD_NAME    = f"Agentic SDLC | Run {os.environ.get('GITHUB_RUN_NUMBER','local')}"
TARGET_URL    = os.environ.get("TARGET_URL", "https://ecommerce-playground.lambdatest.io/")

_BROWSER_NAME_LT = {
    "chrome":  "Chrome",
    "firefox": "Firefox",
    "safari":  "Safari",
}

# Maps browser name → playwright browser type for cloud connect
_PW_BROWSER = {
    "chrome":  "chromium",
    "firefox": "firefox",
    "safari":  "webkit",
}


def _lt_cdp_url(browser_name: str, test_name: str) -> str:
    caps = {
        "browserName":    _BROWSER_NAME_LT.get(browser_name, "Chrome"),
        "browserVersion": "latest",
        "LT:Options": {
            "platform":  "Windows 11",
            "build":     BUILD_NAME,
            "name":      test_name,
            "video":     True,
            "network":   True,
            "console":   True,
            "user":      LT_USERNAME,
            "accessKey": LT_ACCESS_KEY,
        },
    }
    return f"wss://cdp.lambdatest.com/playwright?capabilities={quote(json.dumps(caps))}"


def pytest_generate_tests(metafunc):
    if "browser_name" in metafunc.fixturenames:
        metafunc.parametrize("browser_name", BROWSERS)


@pytest.fixture()
def page(request, browser_name):
    from playwright.sync_api import sync_playwright  # lazy — allows collection without playwright
    test_name = re.sub(r"[^\w\s-]", "", request.node.name)[:80]
    pw_type = _PW_BROWSER.get(browser_name, "chromium")
    with sync_playwright() as p:
        browser_engine = getattr(p, pw_type)
        if LT_USERNAME and LT_ACCESS_KEY:
            browser = browser_engine.connect(
                _lt_cdp_url(browser_name, test_name),
                timeout=120_000
            )
            ctx = browser.new_context()
            pg  = ctx.new_page()
            yield pg
            status = "failed" if hasattr(request.node, "rep_call") and request.node.rep_call.failed else "passed"
            pg.evaluate(f"_ => {{}}", f"lambdatest_action: {{\"action\": \"setTestStatus\", \"arguments\": {{\"status\": \"{status}\"}}}}")
            ctx.close()
            browser.close()
        else:
            browser = browser_engine.launch(headless=True)
            ctx = browser.new_context()
            pg  = ctx.new_page()
            yield pg
            ctx.close()
            browser.close()


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)
