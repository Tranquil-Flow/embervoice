"""Optional live UI smoke: isolated Chrome, real local Breeze full synthetic book."""

from e2e_support import URL as TEST_URL, require_real_test

require_real_test()
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
import os
from audiobook import extract_book

EPUB = Path(os.environ.get("STUDIO_EPUB", str(ROOT / "tests/fixtures/synthetic.epub")))
if not EPUB.is_file():
    raise SystemExit("SKIP: STUDIO_EPUB is unavailable")
URL = TEST_URL


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True
        )
        page = browser.new_page(viewport={"width": 1440, "height": 950}, device_scale_factor=1)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(URL, wait_until="networkidle")
        page.screenshot(path=str(ROOT / "samples/ui-desktop.png"), full_page=True)
        assert page.get_by_text("Drop your EPUB here").is_visible()
        page.locator("#file-picker").set_input_files(str(EPUB))
        page.locator("#book-title").get_by_text(extract_book(EPUB).title).wait_for(timeout=15000)
        print("BOOK", page.locator("#book-stats").inner_text(), flush=True)
        assert page.locator("#chapter-select,#passage-select,#correction-panel").count() == 0
        assert page.locator("#preview").count() == 1
        page.locator("#presets input[value=calm]").check(force=True)
        assert "softly spoken" in page.locator("#style").input_value()
        page.screenshot(path=str(ROOT / "samples/ui-loaded.png"), full_page=True)
        page.locator("#file-picker").set_input_files(str(ROOT / "tests/fixtures/synthetic.epub"))
        page.locator("#book-title").get_by_text("A Free World").wait_for(timeout=15000)
        page.locator("#full").click()
        page.get_by_text("Audiobook complete").wait_for(timeout=240000)
        assert page.locator("#download-links a").count() == 3
        url = page.locator("#download-links a").first.get_attribute("href")
        response = page.request.get(URL.rstrip("/") + url)
        assert response.ok
        result = ROOT / "samples/gui-full-test.m4b"
        result.write_bytes(response.body())
        print("FULL", page.locator("#job-count").inner_text(), "m4b_bytes", result.stat().st_size, flush=True)
        mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
        mobile.goto(URL, wait_until="networkidle")
        mobile.screenshot(path=str(ROOT / "samples/ui-mobile.png"), full_page=True)
        print(
            "MOBILE",
            mobile.locator("h1").inner_text(),
            "horizontal_overflow",
            mobile.evaluate("document.documentElement.scrollWidth > innerWidth"),
        )
        print("JS_ERRORS", errors)
        assert not errors
        browser.close()


if __name__ == "__main__":
    main()
