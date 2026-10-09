"""Real Chromium rendering with controlled API replies; never runs a model."""

from pathlib import Path
import time

import pytest
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
BOOK = {
    "id": "a" * 32,
    "title": "Moonlit Sanctuary",
    "author": "Reader",
    "language": "en",
    "chapters": [
        {"title": "Opening Freedom", "characters": 100, "passages": 1},
        {"title": "Mutual Care", "characters": 600, "passages": 2},
    ],
}


@pytest.mark.browser
def test_words_start_before_job_response_and_first_eta_is_visible(tmp_path):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-gpu"])
        page = browser.new_page(viewport={"width": 1280, "height": 950})
        errors, pending = [], []
        page.on("pageerror", lambda err: errors.append(str(err)))
        state = {
            "id": "b" * 32,
            "book_id": BOOK["id"],
            "mode": "full",
            "state": "running",
            "phase": "synthesizing",
            "stage_message": "Synthesizing chapter 2/2, passage 1/2",
            "current_chapter": 2,
            "current_passage_index": 1,
            "passages_total": 3,
            "started_at": time.time(),
            "passages_done": 1,
            "chapters_done": 1,
            "eta_narration_seconds": 120,
            "eta_total_seconds": 140,
            "eta_samples": 1,
            "eta_basis": "current",
            "log": [],
            "chapters": [],
            "book_url": None,
            "preview_url": None,
        }

        def route(r):
            path = r.request.url.split("127.0.0.1:18077", 1)[-1]
            if path == "/":
                r.fulfill(path=str(ROOT / "web/index.html"), content_type="text/html")
            elif path in ("/app.js", "/setup.js", "/style.css"):
                r.fulfill(
                    path=str(ROOT / "web" / path[1:]),
                    content_type="text/css" if path.endswith("css") else "text/javascript",
                )
            elif path == "/api/presets":
                r.fulfill(json={"warm": "Warm narrator"})
            elif path == "/api/setup":
                r.fulfill(json={"ready": True, "accepted": True, "state": "ready", "environment": {"blockers": []}})
            elif path == "/api/setup/license":
                r.fulfill(json={"text": "Test only", "license_sha256": "test"})
            elif path == "/api/books" and r.request.method == "POST":
                r.fulfill(json=BOOK)
            elif path == "/api/jobs" and r.request.method == "POST":
                pending.append(r)
            elif path.startswith("/api/jobs/"):
                r.fulfill(json=state)
            elif path.endswith("/passages"):
                r.fulfill(json={"passages": [{"text": "Freedom blossoms through mutual kindness.", "index": 1}]})
            elif path.endswith("/versions"):
                r.fulfill(json={"versions": []})
            elif path == "/api/library":
                r.fulfill(json={"books": []})
            else:
                r.fulfill(status=404, body="")

        page.route("**/*", route)
        try:
            page.goto("http://127.0.0.1:18077/")
            page.wait_for_function("document.querySelector('#style').value.length > 0")
            page.locator("#file-picker").set_input_files(
                {"name": "test.epub", "mimeType": "application/epub+zip", "buffer": b"fixture"}
            )
            expect(page.locator("#book-title")).to_have_text(BOOK["title"])
            page.locator("#full").click()
            # Hold the POST: feedback must not wait for model setup or even the HTTP response.
            expect(page.locator("#job-panel")).to_be_visible(timeout=1500)
            expect(page.locator(".smokeword").first).to_be_attached(timeout=1500)
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.smokeword')).some(el => Number(getComputedStyle(el).opacity) > .02)",
                timeout=1500,
            )
            page.wait_for_function("hearth.pool.includes('Freedom')", timeout=1500)
            expect(page.locator("#full")).to_be_disabled()
            assert pending
            pending.pop().fulfill(json=state)
            expect(page.locator("#job-count")).to_have_text("1 / 3 passages saved")
            expect(page.locator("#progress-caption")).to_contain_text("~3 min total")
            expect(page.locator("#progress-caption")).to_contain_text("~2 min remaining")
            expect(page.locator("#job-detail")).to_contain_text("Section 2/2 · passage 1/2")
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.smokeword')).some(el => Number(getComputedStyle(el).opacity) > .15 && el.getBoundingClientRect().right < document.querySelector('#book-panel').getBoundingClientRect().left)",
                timeout=3000,
            )
            page.screenshot(path=str(tmp_path / "feedback.png"), full_page=True)
            page.emulate_media(reduced_motion="reduce")
            page.evaluate("document.querySelectorAll('.smokeword').forEach(el => el.remove())")
            page.wait_for_timeout(1100)
            assert page.locator(".smokeword").count() == 0
            page.set_viewport_size({"width": 390, "height": 844})
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            state["state"] = "cancelled"
            expect(page.locator("#full")).to_be_enabled(timeout=4000)
            page.locator("#full").click()
            expect(page.locator("#full")).to_be_disabled()
            page.wait_for_timeout(100)
            assert pending
            pending.pop().fulfill(status=409, json={"detail": "Injected safe refusal"})
            expect(page.locator("#notice")).to_have_text("Injected safe refusal")
            expect(page.locator("#full")).to_be_enabled()
            expect(page.locator("#cancel")).to_be_hidden()
            assert not errors, errors
        finally:
            browser.close()
