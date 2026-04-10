"""Debug test: verify let _files is accessible from page.evaluate arrow functions."""
from playwright.sync_api import sync_playwright

FIXTURES = 'C:/VideoTools/VideoConverter/tests/fixtures'
BASE_URL = 'http://localhost:5001'

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(BASE_URL)
    page.evaluate(f'scanFolder("{FIXTURES}")')
    page.wait_for_function('!document.getElementById("startBtn").disabled', timeout=30000)

    # Test 1: read _files via plain expression
    count1 = page.evaluate('_files.length')
    print('1. _files.length via expression:', count1)

    # Test 2: read _files via arrow fn
    count2 = page.evaluate('() => _files.length')
    print('2. _files.length via arrow fn:', count2)

    # Test 3: filter _files via arrow fn (no assignment)
    count3 = page.evaluate('() => { return _files.filter(f => f.name === "h264_tiny.mkv").length; }')
    print('3. filter count (no assign) via arrow fn:', count3)

    # Test 4: assign to _files via arrow fn
    count4 = page.evaluate('() => { _files = _files.filter(f => f.name === "h264_tiny.mkv"); return _files.length; }')
    print('4. _files.length after arrow fn assign:', count4)

    # Test 5: check outer _files after the arrow fn assign
    count5 = page.evaluate('_files.length')
    print('5. _files.length after assign (expression check):', count5)

    browser.close()
