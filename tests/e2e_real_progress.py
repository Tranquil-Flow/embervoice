"""One isolated-browser real Breeze full synthetic book; no audio playback."""

from e2e_support import URL as TEST_URL, require_real_test

require_real_test()
import json
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(TEST_URL)
    page.wait_for_function("() => (document.querySelector('#style').value.length > 0)")
    page.locator("#file-picker").set_input_files("tests/fixtures/synthetic.epub")
    expect(page.locator("#book-title")).to_have_text("A Free World")
    page.get_by_role("button", name="Generate full book").click()
    expect(page.locator("#work-signal")).to_be_visible(timeout=5000)
    page.locator(".generate").screenshot(path="samples/real-progress-loading.png")
    expect(page.locator("#job-log")).to_contain_text("Synthesizing chapter", timeout=180000)
    page.locator(".generate").screenshot(path="samples/real-progress-synthesizing.png")
    expect(page.locator("#job-state")).to_have_text("Audiobook complete", timeout=240000)
    log = page.locator("#job-log").inner_text()
    assert "Combining chapters" in log or "Chaptered audiobook saved" in log, log
    assert "Synthesizing chapter" in log, log
    assert "Saved chapter" in log, log
    expect(page.locator("#download-links a")).to_have_count(3)
    info = {"state": page.locator("#job-state").inner_text(), "log": log.splitlines(), "browser_errors": errors}
    print(json.dumps(info, indent=2))
    assert not errors
    browser.close()
