"""Persistent shelf and direct chapter playback, with synthetic audio only."""

from e2e_support import URL as TEST_URL, STORE as TEST_STORE, mock_ready
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import httpx
from playwright.sync_api import sync_playwright, expect
from audiobook import convert
from test_audiobook import sample_epub, tone

root = Path(__file__).resolve().parent.parent
store = TEST_STORE
with TemporaryDirectory() as directory:
    source = sample_epub(Path(directory) / "shelf.epub")
    response = httpx.post(
        TEST_URL.rstrip("/") + "/api/books",
        files={"file": ("shelf.epub", source.read_bytes(), "application/epub+zip")},
        timeout=20,
    )
    response.raise_for_status()
    book = response.json()
    version = hashlib.sha256((book["id"] + "shelf-proof").encode()).hexdigest()[:16]
    saved = store / "books" / book["id"] / "versions" / version
    convert(store / "books" / book["id"] / "book.epub", saved, "Warm, measured narrator", synth=tone)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 850})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        mock_ready(page)
        page.goto(TEST_URL, wait_until="networkidle")
        page.evaluate("localStorage.clear()")
        page.reload(wait_until="networkidle")
        row = page.locator(".shelf-book").filter(has_text=book["title"]).first
        expect(row).to_be_visible()
        row.get_by_role("button", name="Open book").click()
        expect(page.locator("#book-title")).to_have_text(book["title"])
        details = page.locator(".version-entry").filter(has_text=version[:8])
        expect(details).to_be_visible()
        details.locator("summary").click()
        expect(details.locator(".saved-chapter")).to_have_count(2)
        assert details.locator('a[href$=".m4b"]').count() == 1
        details.locator(".saved-chapter").nth(1).get_by_role("button").click()
        expect(page.locator("#chapter-listen")).to_be_visible()
        page.wait_for_function("() => document.querySelector('#chapter-player').readyState >= 1", timeout=5000)
        assert page.locator("#chapter-player").evaluate("(audio)=>audio.paused && audio.error === null")
        assert page.locator("#playing-chapter").inner_text().startswith("Chapter One")
        assert page.locator("#chapter-select,#passage-select,#correction-panel,.retry-open,.retry-resume").count() == 0
        expect(page.locator("#preview")).to_be_enabled()
        page.locator(".import").screenshot(path=str(root / "samples/shelf-chapter-desktop.png"))
        page.set_viewport_size({"width": 390, "height": 844})
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.locator(".import").screenshot(path=str(root / "samples/shelf-chapter-mobile.png"))
        assert not errors, errors
        print("SHELF_CHAPTER_OK", version, "chapters", 2, "media_ready", True, "errors", errors)
        browser.close()
