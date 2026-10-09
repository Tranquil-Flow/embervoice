"""Real loopback upload + browser selection; job POST intercepted to avoid GPU use."""

from e2e_support import URL as TEST_URL, mock_ready
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from test_studio import reference_wav

root = Path(__file__).resolve().parent.parent
url = TEST_URL
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors, submissions = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def on_job(route):
        submissions.append(route.request.post_data_json)
        route.fulfill(
            json={
                "id": "d" * 32,
                "book_id": submissions[-1]["book_id"],
                "version": "test",
                "mode": "full",
                "state": "complete",
                "phase": "ready",
                "stage_message": "Ready",
                "started_at": 1,
                "error": None,
                "log": [],
                "passages_done": 0,
                "chapters_done": 0,
                "preview_url": None,
                "chapters": [],
                "book_url": None,
            }
        )

    page.route("**/api/jobs", lambda route: on_job(route) if route.request.method == "POST" else route.continue_())
    mock_ready(page)
    page.goto(url, wait_until="networkidle")
    page.locator("#file-picker").set_input_files(str(root / "tests/fixtures/synthetic.epub"))
    expect(page.locator("#book-title")).to_have_text("A Free World")
    assert page.locator("#presets input").count() == 12
    assert page.locator(".preset-item:not(.fun)").count() == 8
    page.locator("#presets input[value=documentary]").check(force=True)
    assert "documentary" in page.locator("#style").input_value()
    page.locator("#reference-audio").set_input_files(
        {"name": "sample.wav", "mimeType": "audio/wav", "buffer": reference_wav()}
    )
    expect(page.locator("#reference-name")).to_contain_text("sample.wav")
    assert page.locator("#full").is_disabled() and page.locator("#preview").is_disabled()
    page.locator("#reference-text").fill("These are the exact words spoken in the clip.")
    assert page.locator("#full").is_enabled() and page.locator("#preview").is_enabled()
    page.locator("#full").click()
    expect(page.locator("#job-state")).to_have_text("Audiobook complete")
    assert len(submissions) == 1 and len(submissions[0]["reference_id"]) == 64
    assert submissions[0]["reference_text"] == "These are the exact words spoken in the clip."
    assert submissions[0]["style"] == page.locator("#style").input_value()
    page.locator(".voice").screenshot(path=str(root / "samples/reference-ui.png"))
    page.reload(wait_until="networkidle")
    expect(page.locator("#reference-name")).to_contain_text("sample.wav")
    assert page.locator("#reference-text").input_value() == submissions[0]["reference_text"]
    assert page.locator("#full").is_enabled()
    page.set_viewport_size({"width": 390, "height": 844})
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), page.evaluate(
        '({width:innerWidth,scroll:document.documentElement.scrollWidth,items:[...document.querySelectorAll("*")].filter(e=>e.getBoundingClientRect().right>innerWidth+1).map(e=>[e.tagName,e.id,e.className,e.getBoundingClientRect().right]).slice(0,20)})'
    )
    page.locator(".voice").screenshot(path=str(root / "samples/reference-ui-mobile.png"))
    page.locator("#reference-clear").click()
    assert page.locator("#reference-selected").is_hidden()
    assert page.locator("#full").is_enabled()
    page.locator("#full").click()
    assert submissions[-1]["reference_id"] is None and submissions[-1]["reference_text"] is None
    assert not errors, errors
    print(
        "REFERENCE_UI_OK",
        "presets",
        page.locator("#presets input").count(),
        "submissions",
        len(submissions),
        "browser_errors",
        errors,
    )
    browser.close()
