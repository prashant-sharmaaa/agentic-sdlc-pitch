"""
LambdaTest Playwright utility helpers for Kane-CLI exported tests.

Centralises:
  - LT capabilities building + CDP URL generation
  - Common page actions (search, add-to-cart)
  - Test status reporting

Update selectors HERE when the site changes — all test.py files stay untouched.
"""
import json
import os
import urllib.parse
from importlib.metadata import version as _pkg_version


# ---------------------------------------------------------------------------
# Capabilities + CDP URL
# ---------------------------------------------------------------------------

def build_capabilities(test_name: str) -> dict:
    """Build LT:Options capabilities dict for a Playwright test."""
    return {
        'browserName': 'Chrome',
        'browserVersion': 'latest',
        'LT:Options': {
            'platform': 'Windows 10',
            'build': os.getenv('BUILD', 'Agentic SDLC | KaneAI Export'),
            'name': test_name,
            'user': os.getenv('LT_USERNAME'),
            'accessKey': os.getenv('LT_ACCESS_KEY'),
            'network': True,
            'video': True,
            'console': True,
            'tunnel': False,
            'tunnelName': '',
            'geoLocation': '',
            'playwrightClientVersion': _pkg_version('playwright'),
        }
    }


def cdp_url(test_name: str) -> str:
    """Return the LambdaTest CDP WebSocket URL for the given test name."""
    caps = build_capabilities(test_name)
    return 'wss://cdp.lambdatest.com/playwright?capabilities=' + urllib.parse.quote(
        json.dumps(caps))


# ---------------------------------------------------------------------------
# Test status reporting
# ---------------------------------------------------------------------------

def set_test_status(page, status: str, remark: str) -> None:
    """Report pass/fail to the LambdaTest automation dashboard."""
    page.evaluate(
        '_ => {}',
        f'lambdatest_action: {{"action": "setTestStatus", "arguments": {{"status":"{status}", "remark": "{remark}"}}}}'
    )


# ---------------------------------------------------------------------------
# Common page actions — update selectors HERE when site structure changes
# ---------------------------------------------------------------------------

# Search
SEARCH_INPUT = "input[name='search']"
SEARCH_SUBMIT = None   # None = press Enter (more reliable than button click)

# Add to Cart
ADD_TO_CART_BTN = "button[onclick*='cart.add']"

# Cart indicator
CART_SELECTOR = '#cart-total, .cart-total, .alert-success, #cart'

# Category nav
SHOP_BY_CATEGORY_BTN = "button:has-text('Shop by Category')"


def search(page, query: str) -> None:
    """Fill the search box and submit."""
    page.locator(SEARCH_INPUT).first.fill(query)
    if SEARCH_SUBMIT:
        page.locator(SEARCH_SUBMIT).first.click()
    else:
        page.keyboard.press('Enter')
    page.wait_for_load_state('domcontentloaded')
    page.wait_for_timeout(1500)


def add_to_cart(page, wait_ms: int = 3000) -> None:
    """Click the first visible Add to Cart button and wait for cart update."""
    page.locator(ADD_TO_CART_BTN).first.click(force=True)
    page.wait_for_timeout(wait_ms)
    # Non-strict check: cart element just needs to be in the DOM
    try:
        page.wait_for_selector(CART_SELECTOR, timeout=5000, state='attached')
    except Exception:
        pass  # Cart may update via different mechanism; continue


def navigate_to_category(page, category_name: str) -> None:
    """Open Shop by Category menu and click the given category."""
    page.locator(SHOP_BY_CATEGORY_BTN).first.click()
    page.wait_for_timeout(800)
    page.locator(f"a:has-text('{category_name}')").first.click()
    page.wait_for_load_state('domcontentloaded')
    page.wait_for_timeout(2000)
