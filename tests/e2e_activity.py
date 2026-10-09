"""Browser-only UI check with mocked job progress; no model or book upload."""

from e2e_support import URL as TEST_URL, mock_ready
from playwright.sync_api import sync_playwright, expect

URL = TEST_URL
book = {
    "id": "a" * 32,
    "title": "Activity Fixture",
    "author": "Test Reader",
    "language": "en",
    "chapters": [{"title": "Chapter One", "characters": 800, "passages": 3}],
}
job_id = "b" * 32

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    count = [0]

    def route_api(route):
        req = route.request
        path = req.url.split("/api", 1)[-1]
        if path == "/books" and req.method == "POST":
            route.fulfill(json=book)
        elif path == "/jobs" and req.method == "POST":
            route.fulfill(json=status(0))
        elif path == f"/jobs/{job_id}" and req.method == "GET":
            count[0] += 1
            route.fulfill(json=status(min(count[0], 2)))
        else:
            route.continue_()

    def status(step):
        return {
            "id": job_id,
            "book_id": book["id"],
            "mode": "full",
            "state": "complete" if step == 2 else "running",
            "error": None,
            "log": ["Preparing local model and checking host resources…"]
            + (["Generated chapter 1/1 passage 1/3: saved.wav"] if step else []),
            "passages_done": step,
            "chapters_done": 0,
            "preview_url": None,
            "chapters": [],
            "book_url": None,
        }

    page.route("**/api/**", route_api)
    mock_ready(page)
    page.goto(URL)
    page.wait_for_function("() => (document.querySelector('#style').value.length > 0)")
    page.locator("#file-picker").set_input_files(
        {"name": "fixture.epub", "mimeType": "application/epub+zip", "buffer": b"fixture"}
    )
    expect(page.locator("#book-title")).to_have_text("Activity Fixture")
    page.get_by_role("button", name="Generate full book").click()
    expect(page.locator(".activity")).to_have_attribute("open", "")
    expect(page.locator("#job-log")).to_contain_text("Preparing local model")
    expect(page.locator("#job-log")).to_contain_text("Generated chapter", timeout=5000)
    expect(page.locator("#job-count")).to_contain_text("1 / 3", timeout=5000)
    assert not errors, errors
    page.locator(".generate").screenshot(path="samples/activity-after.png")
    print("ACTIVITY_VISIBLE_AND_UPDATING", count[0], "browser_errors", errors)
    browser.close()
