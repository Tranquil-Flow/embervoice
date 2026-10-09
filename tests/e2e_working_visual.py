"""Browser visual regression with controlled stage events; no model or disk upload."""

from e2e_support import URL as TEST_URL, mock_ready
import time
from playwright.sync_api import sync_playwright, expect

URL = TEST_URL
BOOK_ID, JOB_ID = "a" * 32, "b" * 32
book = {
    "id": BOOK_ID,
    "title": "Working Visual Fixture",
    "author": "Test Reader",
    "language": "en",
    "chapters": [{"title": "Chapter One", "characters": 800, "passages": 2}],
}
start_time = time.time()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    polls = [0]

    def status(step):
        phase = "preparing" if step == 0 else "synthesizing" if step == 1 else "ready"
        message = (
            "Preparing local model and checking host resources…"
            if step == 0
            else "Synthesizing chapter 1/1, passage 1/2"
            if step == 1
            else "Audiobook ready"
        )
        return {
            "id": JOB_ID,
            "book_id": BOOK_ID,
            "mode": "full",
            "state": "complete" if step == 2 else "running",
            "phase": phase,
            "stage_message": message,
            "started_at": start_time,
            "error": None,
            "log": ["Preparing local model and checking host resources…"]
            + (["Synthesizing chapter 1/1, passage 1/2"] if step >= 1 else []),
            "passages_done": 1 if step == 2 else 0,
            "chapters_done": 0,
            "preview_url": None,
            "chapters": [],
            "book_url": None,
        }

    def route_api(route):
        req = route.request
        path = req.url.split("/api", 1)[-1]
        if path == "/books" and req.method == "POST":
            route.fulfill(json=book)
        elif path == "/jobs" and req.method == "POST":
            route.fulfill(json=status(0))
        elif path == f"/jobs/{JOB_ID}" and req.method == "GET":
            polls[0] += 1
            route.fulfill(json=status(0 if polls[0] <= 2 else 1 if polls[0] <= 4 else 2))
        else:
            route.continue_()

    page.route("**/api/**", route_api)
    mock_ready(page)
    page.goto(URL)
    page.wait_for_function("() => (document.querySelector('#style').value.length > 0)")
    page.locator("#file-picker").set_input_files(
        {"name": "fixture.epub", "mimeType": "application/epub+zip", "buffer": b"fixture"}
    )
    page.get_by_role("button", name="Generate full book").click()
    expect(page.locator("#work-signal")).to_be_visible()
    expect(page.locator("#work-stage")).to_contain_text("Preparing local model")
    expect(page.locator("#progress-fill")).to_have_attribute("style", "width: 0%;")
    before = page.locator("#work-elapsed").inner_text()
    assert before.startswith("Elapsed ") and "not an ETA" not in before
    page.wait_for_timeout(1250)
    after = page.locator("#work-elapsed").inner_text()
    assert before != after and "not an ETA" not in after, (before, after)
    expect(page.locator("#work-stage")).to_contain_text("Synthesizing", timeout=5000)
    expect(page.locator("#job-log")).to_contain_text("Synthesizing", timeout=5000)
    page.locator(".generate").screenshot(path="samples/working-visual.png")
    page.set_viewport_size({"width": 390, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    assert page.evaluate("getComputedStyle(document.querySelector('.candle .outer')).animationName") == "none"
    overflow = page.evaluate(
        """() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,items:[...document.querySelectorAll('*')].filter(el=>el.getBoundingClientRect().right>innerWidth+2).slice(0,12).map(el=>[el.tagName,el.id,el.className,Math.round(el.getBoundingClientRect().right)])})"""
    )
    print("mobile_overflow_probe", overflow)
    assert overflow["scroll"] <= overflow["width"]
    page.locator(".generate").screenshot(path="samples/working-visual-mobile.png")
    expect(page.locator("#work-signal")).to_be_hidden(timeout=6000)
    assert not errors, errors
    print(
        "WORKING_VISUAL_OK",
        "elapsed_before",
        before,
        "elapsed_after",
        after,
        "polls",
        polls[0],
        "browser_errors",
        errors,
    )
    browser.close()
