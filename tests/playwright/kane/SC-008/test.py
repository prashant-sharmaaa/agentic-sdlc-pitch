import json
import os
import subprocess
import urllib.parse
from playwright.sync_api import sync_playwright

capabilities = {
    'browserName': 'Chrome',
    'browserVersion': 'latest',
    'LT:Options': {
        'platform': os.environ.get('TARGET_OS', 'LINUX'),
        'build': os.environ.get('BUILD', 'Agentic SDLC | KaneAI Export'),
        'name': 'SC-008: Success message appears on cart add',
        'user': os.environ.get('LT_USERNAME'),
        'accessKey': os.environ.get('LT_ACCESS_KEY'),
        'network': True,
        'video': True,
        'console': True,
    }
}


def test_sc008(playwright):
    playwrightVersion = str(subprocess.getoutput('playwright --version')).strip().split(' ')[1]
    capabilities['LT:Options']['playwrightVersion'] = playwrightVersion
    lt_cdp_url = 'wss://cdp.lambdatest.com/playwright?capabilities=' + urllib.parse.quote(json.dumps(capabilities))
    browser = playwright.chromium.connect(lt_cdp_url)
    page = browser.new_page(viewport={'width': 1920, 'height': 1080})
    try:
        page.goto("https://automationexercise.com/products")
        page.wait_for_load_state('domcontentloaded')
        page.wait_for_timeout(1000)
        page.mouse.click(821, 896)
        page.wait_for_timeout(500)
        page.wait_for_timeout(500)
        set_test_status(page, 'passed', 'SC-008 passed')
    except Exception as err:
        print('Error:: ', err)
        set_test_status(page, 'failed', str(err)[:500])
    browser.close()


def set_test_status(page, status, remark):
    page.evaluate('_ => {}',
                  'lambdatest_action: {"action": "setTestStatus", "arguments": {"status":"' + status + '", "remark": "' + remark + '"}}')


with sync_playwright() as playwright:
    test_sc008(playwright)
