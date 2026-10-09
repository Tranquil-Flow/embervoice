"""Simple preview samples the first substantive section, then full includes all sections."""

from e2e_support import URL as TEST_URL, mock_ready
from pathlib import Path
from tempfile import TemporaryDirectory
from ebooklib import epub
from playwright.sync_api import sync_playwright, expect
from test_studio import reference_wav

with TemporaryDirectory() as directory:
    source = Path(directory) / "long.epub"
    book = epub.EpubBook()
    book.set_identifier("simple-preview-e2e")
    book.set_title("Long Reader")
    book.set_language("en")
    title = epub.EpubHtml(title="Title page", file_name="title.xhtml", lang="en")
    title.content = "<html><body><h1>Title page</h1><p>Long Reader</p></body></html>"
    chapter = epub.EpubHtml(title="Chapter One", file_name="one.xhtml", lang="en")
    chapter.content = (
        "<html><body><h1>Chapter One</h1><p>" + ("Freedom and care grow together. " * 50) + "</p></body></html>"
    )
    book.add_item(title)
    book.add_item(chapter)
    book.toc = (epub.Link("title.xhtml", "Title page", "title"), epub.Link("one.xhtml", "Chapter One", "one"))
    book.spine = ["nav", title, chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(source), book)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 850})
        errors = []
        submissions = []
        statuses = {}
        page.on("pageerror", lambda error: errors.append(str(error)))

        def on_job(route):
            data = route.request.post_data_json
            submissions.append(data)
            status = {
                "id": ("d" if data["mode"] == "preview" else "e") * 32,
                "book_id": data["book_id"],
                "version": "test",
                "mode": data["mode"],
                "state": "complete",
                "phase": "ready",
                "stage_message": "Ready",
                "started_at": 1,
                "error": None,
                "log": [],
                "passages_done": 1 if data["mode"] == "preview" else 0,
                "chapters_done": 0,
                "preview_url": "/api/jobs/" + "d" * 32 + "/files/passages/002-0001.wav"
                if data["mode"] == "preview"
                else None,
                "chapters": [],
                "book_url": None,
            }
            statuses[status["id"]] = status
            route.fulfill(json=status)

        page.route(
            "**/api/jobs/*/files/passages/*",
            lambda route: route.fulfill(body=reference_wav(), content_type="audio/wav"),
        )
        page.route(
            "**/api/jobs/*",
            lambda route: (
                route.fulfill(json=statuses[route.request.url.rsplit("/", 1)[-1]])
                if route.request.method == "GET" and route.request.url.rsplit("/", 1)[-1] in statuses
                else route.continue_()
            ),
        )
        page.route("**/api/jobs", lambda route: on_job(route) if route.request.method == "POST" else route.continue_())
        mock_ready(page)
        page.goto(TEST_URL, wait_until="networkidle")
        page.locator("#file-picker").set_input_files(str(source))
        expect(page.locator("#book-title")).to_have_text("Long Reader")
        assert page.locator("#chapter-select,#passage-select,#correction-panel").count() == 0
        expect(page.locator("#preview")).to_be_enabled()
        assert page.locator(".shelf-book").filter(has_text="Long Reader").count() == 0
        page.locator("#preview").click()
        expect(page.locator("#job-state")).to_have_text("Your preview is ready")
        assert (
            submissions[0]["mode"] == "preview"
            and submissions[0]["chapter"] == 2
            and submissions[0].get("passage", 1) == 1
        )
        expect(page.locator("#player-panel")).to_be_visible()
        page.locator("#audio-player").evaluate(
            "(audio)=>audio.load()"
        )  # preload=none: user-initiated metadata fetch, no autoplay
        page.wait_for_function("() => document.querySelector('#audio-player').readyState >= 1", timeout=5000)
        assert page.locator("#audio-player").evaluate("(audio)=>audio.paused && audio.error === null")
        page.locator("#full").click()
        expect(page.locator("#job-state")).to_have_text("Audiobook complete")
        assert len(submissions) == 2 and submissions[1]["mode"] == "full" and submissions[1]["chapter"] == 1
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.set_viewport_size({"width": 390, "height": 844})
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert not errors, errors
        print("SIMPLE_PREVIEW_UI_OK", submissions, "errors", errors)
        browser.close()
