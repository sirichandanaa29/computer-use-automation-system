from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
 browser = pw.chromium.launch(headless=True)
 page = browser.new_page()
 page.goto("http://localhost:5000/")
 print("TITLE:", repr(page.title()))
 print("CONTENT LENGTH:", len(page.content()))
 print("FIRST 300 CHARS:", page.content()[:300])
 browser.close()
