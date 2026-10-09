"""Isolated real Breeze ETA integration, generating a tiny synthetic EPUB locally."""

from e2e_support import URL as TEST_URL, require_real_test

require_real_test()
import json
import time
import urllib.request
from pathlib import Path

from ebooklib import epub
from playwright.sync_api import sync_playwright, expect

URL = TEST_URL
fixture = Path("samples/eta-fixture.epub")
book = epub.EpubBook()
book.set_identifier("eta-fixture-1")
book.set_title("A Patient Estimate")
book.set_language("en")
book.add_author("Studio Test")
chapters = []
for index in range(1, 5):
    title = f"Quiet Chapter {index}"
    chapter = epub.EpubHtml(title=title, file_name=f"chapter{index}.xhtml", lang="en")
    chapter.content = (
        f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>{title}</h1>'
        "<p>A reader turns a page and lets one gentle sentence settle.</p></body></html>"
    )
    book.add_item(chapter)
    chapters.append(chapter)
book.toc = tuple(epub.Link(ch.file_name, ch.title, f"ch{index}") for index, ch in enumerate(chapters, 1))
book.spine = ["nav", *chapters]
book.add_item(epub.EpubNcx())
book.add_item(epub.EpubNav())
epub.write_epub(str(fixture), book)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(URL)
    page.wait_for_function("() => (document.querySelector('#style').value.length > 0)")
    page.locator("#file-picker").set_input_files(str(fixture))
    expect(page.locator("#book-title")).to_have_text("A Patient Estimate")
    planned = page.locator("#book-stats").inner_text()
    assert "4 passages planned" in planned, planned
    page.locator("#style").fill(
        "A patient and clear narrator with measured pauses, even diction, and a consistent gentle voice."
    )
    page.get_by_role("button", name="Generate full book").click()
    expect(page.locator("#job-panel")).to_be_visible(timeout=5000)
    job_id = page.evaluate("localStorage.getItem('studio.job')")
    assert job_id, page.locator("#notice").inner_text()
    print("ISOLATED_JOB", job_id, flush=True)
    observations = []
    eta_seen = None
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f"{URL}/api/jobs/{job_id}", timeout=4) as response:
            state = json.load(response)
        mark = (
            state["state"],
            state["phase"],
            state["passages_done"],
            state["eta_narration_seconds"],
            state["eta_samples"],
        )
        if not observations or mark != observations[-1]:
            observations.append(mark)
        if state["eta_narration_seconds"] is not None and eta_seen is None:
            eta_seen = state["eta_narration_seconds"]
            expect(page.locator("#progress-caption")).to_contain_text("Rough narration ETA", timeout=4000)
            page.locator(".generate").screenshot(path="samples/eta-real.png")
        if state["state"] not in ("running", "cancelling"):
            break
        time.sleep(0.35)
    print("REAL_ETA_OBSERVATIONS", observations, "browser_errors", errors, flush=True)
    assert state["state"] == "complete", state.get("error")
    assert eta_seen is not None, "No measured ETA while passages remained"
    assert state["book_url"] and state["passages_done"] == 4
    assert not errors
    browser.close()
