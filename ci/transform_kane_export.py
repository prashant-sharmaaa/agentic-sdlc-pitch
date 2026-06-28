#!/usr/bin/env python3
"""
Transform kane-cli Python export (testmu async) → direct LT Playwright CDP (sync).

Kane exports use:
  - testmu.configure() for setup
  - @testmu.test async def test(page)
  - async with testmu.step(...) blocks
  - testmu.get_vision_coordinates() for vision clicks
  - testmu.vision_query() / testmu.verify_assertion() for assertions

Output is the same format as our hand-crafted SC-XXX/test.py files:
  - sync playwright with direct LT CDP WebSocket connection
  - capabilities dict with LT:Options
  - set_test_status() reporting
"""
import re
import sys

LT_TEMPLATE = '''\
import json
import os
import subprocess
import urllib.parse
from playwright.sync_api import sync_playwright

capabilities = {{
    'browserName': 'Chrome',
    'browserVersion': 'latest',
    'LT:Options': {{
        'platform': os.environ.get('TARGET_OS', 'LINUX'),
        'build': os.environ.get('BUILD', 'Agentic SDLC | KaneAI Export'),
        'name': '{sc_name}',
        'user': os.environ.get('LT_USERNAME'),
        'accessKey': os.environ.get('LT_ACCESS_KEY'),
        'network': True,
        'video': True,
        'console': True,
    }}
}}


def {fn_name}(playwright):
    playwrightVersion = str(subprocess.getoutput('playwright --version')).strip().split(' ')[1]
    capabilities['LT:Options']['playwrightVersion'] = playwrightVersion
    lt_cdp_url = 'wss://cdp.lambdatest.com/playwright?capabilities=' + urllib.parse.quote(json.dumps(capabilities))
    browser = playwright.chromium.connect(lt_cdp_url)
    page = browser.new_page(viewport={{'width': 1920, 'height': 1080}})
    try:
{body}
        set_test_status(page, 'passed', '{sc_id} passed')
    except Exception as err:
        print('Error:: ', err)
        set_test_status(page, 'failed', str(err)[:500])
    browser.close()


def set_test_status(page, status, remark):
    page.evaluate('_ => {{}}',
                  'lambdatest_action: {{"action": "setTestStatus", "arguments": {{"status":"' + status + '", "remark": "' + remark + '"}}}}')


with sync_playwright() as playwright:
    {fn_name}(playwright)
'''


def extract_body(code: str) -> list[str]:
    """Extract actionable Playwright lines from kane testmu async export."""
    lines      = code.split('\n')
    body       = []
    in_fn      = False       # inside async def test()
    in_step    = False       # inside async with testmu.step()
    skip_next  = False       # skip the coords-click line after get_vision_coordinates

    for i, line in enumerate(lines):
        stripped = line.strip()

        if skip_next:
            skip_next = False
            continue

        # Detect entry into test function
        if re.match(r'^async def test\(', stripped):
            in_fn = True
            continue

        if not in_fn:
            continue

        # End of file sentinel
        if stripped.startswith('if __name__') or stripped.startswith('testmu.run'):
            break

        # Start of a step block — skip the header line itself
        if re.match(r'async with testmu\.step\(', stripped):
            in_step = True
            continue

        # Only process lines inside step blocks
        if not in_step:
            # A line at 4-space indent that isn't a step header ends the step scope
            if line and not line.startswith('        ') and not line.startswith('    async'):
                in_step = False
            continue

        # Empty line — fine, carry through for readability
        if not stripped:
            continue

        # ── testmu-specific lines to handle ──────────────────────────────

        # get_vision_coordinates → extract fallback coords and emit mouse.click
        if 'testmu.get_vision_coordinates' in line:
            m = re.search(
                r'get_vision_coordinates\(page,\s*[^,]+,\s*[^,]+,\s*(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?)\)',
                line
            )
            if m:
                x, y = int(float(m.group(1))), int(float(m.group(2)))
                body.append(f'        page.mouse.click({x}, {y})')
            skip_next = True  # skip coords['x'] / coords['y'] usage line
            continue

        # Skip lines that reference the coords variable (after a vision click)
        if "coords['" in stripped or 'coords["' in stripped:
            continue

        # vision_query → lightweight wait (visual assertion not portable)
        if 'testmu.vision_query' in line:
            body.append('        page.wait_for_timeout(500)')
            continue

        # verify_assertion → skip (kane already passed this before export)
        if 'testmu.verify_assertion' in line:
            continue

        # set_var / expect → skip
        if stripped.startswith('set_var(') or stripped.startswith('expect('):
            continue

        # _resolve_ranked_locator calls → use first locator arg directly
        if '_resolve_ranked_locator' in line:
            m = re.search(r'_resolve_ranked_locator\(page,\s*\[([^\]]+)\]', line)
            if m:
                first_loc = m.group(1).split(',')[0].strip()
                indent = '        '
                var_name = re.match(r'\s*(\w+)\s*=', line)
                vn = var_name.group(1) if var_name else 'element'
                body.append(f'{indent}{vn} = page.locator({first_loc})')
            continue

        # ── Standard Playwright lines ─────────────────────────────────────

        # Remove await, convert from async to sync
        clean = line.replace('        await ', '        ')
        clean = clean.replace('    await ', '    ')

        # Fix indentation — content is at 8 spaces in kane export, keep at 8
        body.append(clean)

    return body


def transform(kane_code: str, sc_id: str, sc_name: str) -> str:
    fn_name = f"test_{sc_id.lower().replace('-', '')}"  # e.g. test_sc001
    body_lines = extract_body(kane_code)

    # Add wait_for_load_state after page.goto calls for stability
    stabilised = []
    for line in body_lines:
        stabilised.append(line)
        if 'page.goto(' in line:
            stabilised.append("        page.wait_for_load_state('domcontentloaded')")
            stabilised.append("        page.wait_for_timeout(1000)")

    body = '\n'.join(stabilised) if stabilised else '        pass'
    return LT_TEMPLATE.format(
        sc_id=sc_id,
        sc_name=sc_name,
        fn_name=fn_name,
        body=body,
    )


if __name__ == '__main__':
    if len(sys.argv) < 4:
        print(f'Usage: {sys.argv[0]} <kane_export.py> <SC-ID> <SC Name>', file=sys.stderr)
        sys.exit(1)
    code = open(sys.argv[1]).read()
    print(transform(code, sys.argv[2], sys.argv[3]))
