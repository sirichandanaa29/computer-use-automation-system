from playwright.sync_api import sync_playwright
print("step 1: starting playwright", flush=True)
with sync_playwright() as pw:
 print("step 2: launching browser", flush=True)
 browser = pw.chromium.launch(headless=True)
 print("step 3: browser launched", flush=True)
 page = browser.new_page()
 print("step 4: page created", flush=True)
 page.goto("http://localhost:5000/")
 print("step 5: page loaded, title is:", page.title(), flush=True)
 browser.close()
print("step 6: done", flush=True)
