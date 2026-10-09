"""Real loopback browser/server seam with synthetic setup states, no downloads."""

from contextlib import contextmanager
import socket
import threading
import time
import pytest
from playwright.sync_api import sync_playwright, expect
import uvicorn
from studio import create_app
from setup_fakes import ready_environment


class BrowserSetup:
    def __init__(self):
        self.state = "missing"
        self.accepted = False
        self.calls = []
        self.error = None

    def status(self):
        return {
            "state": self.state,
            "ready": self.state == "ready",
            "accepted": self.accepted,
            "completed_bytes": 50 if self.accepted else 0,
            "total_bytes": 100,
            "license_sha256": "test-only",
            "error": self.error,
        }

    def start(self, accepted, digest):
        assert accepted is True and digest == "test-only"
        self.calls.append((accepted, digest))
        self.accepted = True
        self.state = "downloading"
        self.error = None
        return self.status()

    def cancel(self):
        self.state = "cancelled"
        return self.status()

    def require_ready(self):
        if self.state != "ready":
            raise RuntimeError("Set up the voice model first")

    def close(self):
        pass


@contextmanager
def local_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    config = uvicorn.Config(app, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("Isolated test server failed to start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


@pytest.mark.browser
def test_first_run_consent_cancel_resume_failure_and_readiness(tmp_path):
    setup = BrowserSetup()
    app = create_app(tmp_path / "store", runner=lambda *a, **k: None, setup=setup, readiness=ready_environment)
    with local_server(app) as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        remote = []
        page.on("request", lambda req: remote.append(req.url) if not req.url.startswith(url) else None)
        page.goto(url, wait_until="networkidle")
        expect(page.locator("#setup-panel")).to_be_visible()
        expect(page.locator("#setup-start")).to_be_disabled()
        expect(page.locator("#setup-accept")).not_to_be_checked()
        assert not setup.calls and not list((tmp_path / "store").rglob("acceptance.json"))
        page.locator(".setup-legal summary").click()
        expect(page.locator("#setup-license")).to_contain_text(
            "BREEZEBLUE RESEARCH AND NON-COMMERCIAL LICENSE AGREEMENT"
        )
        page.locator("#setup-accept").check()
        expect(page.locator("#setup-start")).to_be_enabled()
        page.locator("#setup-start").click()
        expect(page.locator("#setup-status")).to_have_text("Downloading or verifying the voice model…")
        expect(page.locator("#setup-progress")).to_contain_text("0.00 GB of 0.00 GB")
        expect(page.locator("#setup-cancel")).to_be_visible()
        assert setup.calls == [(True, "test-only")]
        page.locator("#setup-cancel").click()
        expect(page.locator("#setup-status")).to_have_text("Download stopped; saved bytes are kept")
        expect(page.locator("#setup-start")).to_have_text("Resume download")
        page.locator("#setup-start").click()
        expect(page.locator("#setup-status")).to_have_text("Downloading or verifying the voice model…")
        assert len(setup.calls) == 2
        setup.state = "error"
        setup.error = "Network unavailable. Partial data retained."
        expect(page.locator("#setup-status")).to_have_text(setup.error, timeout=6000)
        expect(page.locator("#setup-start")).to_have_text("Retry setup")
        page.locator("#setup-start").click()
        expect(page.locator("#setup-status")).to_have_text("Downloading or verifying the voice model…")
        assert len(setup.calls) == 3
        setup.state = "ready"
        setup.error = None
        expect(page.locator("#setup-panel")).to_be_hidden(timeout=6000)
        page.set_viewport_size({"width": 390, "height": 844})
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert not errors and not remote, (errors, remote)
        browser.close()


@pytest.mark.browser
def test_platform_blocker_mobile_no_model_requests(tmp_path):
    setup = BrowserSetup()
    app = create_app(
        tmp_path / "store",
        setup=setup,
        readiness=lambda: {"blockers": ["Missing ffmpeg. Install Homebrew ffmpeg, then restart."]},
    )
    with local_server(app) as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(url, wait_until="networkidle")
        expect(page.locator("#setup-blockers")).to_contain_text("Missing ffmpeg")
        page.locator("#setup-accept").check()
        expect(page.locator("#setup-start")).to_be_disabled()
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert not setup.calls
        browser.close()
