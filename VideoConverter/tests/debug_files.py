from playwright.sync_api import sync_playwright

FIXTURES = 'C:/VideoTools/VideoConverter/tests/fixtures'
BASE_URL = 'http://localhost:5001'

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(BASE_URL)
    page.evaluate(
        f"document.getElementById('folderPath').textContent = '{FIXTURES}';"
        f"scanFolder('{FIXTURES}');"
    )
    # Wait for start button to be enabled (scan done)
    page.wait_for_function("!document.getElementById('startBtn').disabled", timeout=30000)
    names = page.evaluate("_files.map(f => f.name)")
    print("_files names:", names)
    browser.close()
