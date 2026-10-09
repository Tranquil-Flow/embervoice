"""Explicit isolated-browser test configuration; never target the personal server."""

import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get("STUDIO_URL", "http://127.0.0.1:8767/").rstrip("/") + "/"
parsed = urlsplit(URL)
if parsed.hostname != "127.0.0.1" or parsed.port in (None, 8765, 8766):
    raise RuntimeError("Browser tests require an isolated loopback port, not the personal servers on 8765/8766")
STORE = Path(os.environ.get("STUDIO_TEST_STORE", str(ROOT / "samples/feature-test-store"))).expanduser().resolve()
if STORE == (Path.home() / "Audiobooks/Studio").resolve():
    raise RuntimeError("Browser tests must not use the personal audiobook store")
FIXTURE = ROOT / "tests/fixtures/synthetic.epub"


def mock_ready(page):
    """Only for non-model browser tests; does not write any real acceptance receipt."""
    page.route(
        "**/api/setup",
        lambda route: route.fulfill(
            json={
                "state": "ready",
                "ready": True,
                "accepted": True,
                "completed_bytes": 1,
                "total_bytes": 1,
                "error": None,
                "license_sha256": "test-only",
                "environment": {"blockers": []},
            }
        ),
    )
    page.route(
        "**/api/setup/license",
        lambda route: route.fulfill(
            json={"text": (ROOT / "legal/BREEZE_LICENSE.txt").read_text(), "license_sha256": "test-only"}
        ),
    )


def require_real_test():
    if os.environ.get("STUDIO_REAL_MODEL") != "1":
        raise SystemExit("SKIP: real-model test requires STUDIO_REAL_MODEL=1 and an explicitly accepted isolated setup")
