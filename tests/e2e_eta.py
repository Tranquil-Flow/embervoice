"""Browser proof: indeterminate-between-saves bar, then measured ETA."""

from e2e_support import URL as TEST_URL, mock_ready
import time
from playwright.sync_api import sync_playwright, expect

BOOK, JOB = "c" * 32, "d" * 32
book = {
    "id": BOOK,
    "title": "Progress Fixture",
    "author": "Test Reader",
    "language": "en",
    "chapters": [{"title": "Chapter One", "characters": 1200, "passages": 4}],
}
start = time.time()


def status(step):
    saved = [0, 1, 2, 4][step]
    return {
        "id": JOB,
        "book_id": BOOK,
        "mode": "full",
        "state": "complete" if step == 3 else "running",
        "phase": "ready" if step == 3 else "synthesizing",
        "stage_message": "Narrating chapter 1/1",
        "started_at": start,
        "error": None,
        "eta_narration_seconds": 120 if step == 2 else None,
        "eta_samples": 2 if step >= 2 else step,
        "log": ["Narrating chapter 1/1"],
        "passages_done": saved,
        "chapters_done": 1 if step == 3 else 0,
        "preview_url": f"/api/jobs/{JOB}/files/passages/001-0001.wav" if saved else None,
        "chapters": [],
        "book_url": None,
    }


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    polls = [0]
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route(route):
        path = route.request.url.split("/api", 1)[-1]
        if path == "/books" and route.request.method == "POST":
            route.fulfill(json=book)
        elif path == "/jobs" and route.request.method == "POST":
            route.fulfill(json=status(0))
        elif path == f"/jobs/{JOB}" and route.request.method == "GET":
            polls[0] += 1
            step = 0 if polls[0] == 1 else 1 if polls[0] == 2 else 2 if polls[0] <= 5 else 3
            route.fulfill(json=status(step))
        else:
            route.continue_()

    page.route("**/api/**", route)
    mock_ready(page)
    page.goto(TEST_URL)
    page.wait_for_function("() => (document.querySelector('#style').value.length > 0)")
    page.locator("#file-picker").set_input_files(
        {"name": "fixture.epub", "mimeType": "application/epub+zip", "buffer": b"fixture"}
    )
    page.get_by_role("button", name="Generate full book").click()
    expect(page.locator("#progress-caption")).to_contain_text("Estimating after two new passages")
    expect(page.locator(".progress-track")).to_have_class("progress-track is-working")
    assert (
        page.evaluate("getComputedStyle(document.querySelector('.progress-track'),'::after').animationName") != "none"
    )
    expect(page.locator("#progress-caption")).to_contain_text("~2 min", timeout=7000)
    expect(page.locator("#job-count")).to_contain_text("2 / 4")
    expect(page.locator("#progress-fill")).to_have_attribute("style", "width: 50%;")
    expect(page.locator("#player-panel")).to_be_hidden()
    page.locator(".generate").screenshot(path="samples/eta-active.png")
    page.set_viewport_size({"width": 390, "height": 900})
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.locator(".generate").screenshot(path="samples/eta-active-mobile.png")
    page.emulate_media(reduced_motion="reduce")
    assert (
        page.evaluate("getComputedStyle(document.querySelector('.progress-track'),'::after').animationName") == "none"
    )
    expect(page.locator("#job-state")).to_have_text("Audiobook complete", timeout=9000)
    expect(page.locator(".progress-track")).not_to_have_class("progress-track is-working")
    assert not errors, errors
    print("ETA_BROWSER_OK", polls[0], "browser_errors", errors)
    browser.close()
