"""Bounded offline real Breeze synthetic book using a local approved reference."""

from e2e_support import URL as TEST_URL, STORE as TEST_STORE, require_real_test

require_real_test()
from pathlib import Path
import array
import json
import time
import wave
from playwright.sync_api import sync_playwright, expect

root = Path(__file__).resolve().parent.parent
import os

reference = Path(os.environ.get("STUDIO_REFERENCE_AUDIO", "missing-reference"))
transcript_file = Path(os.environ.get("STUDIO_REFERENCE_TRANSCRIPT_FILE", "missing-transcript"))
if not reference.is_file() or not transcript_file.is_file():
    raise SystemExit("SKIP: provide STUDIO_REFERENCE_AUDIO and STUDIO_REFERENCE_TRANSCRIPT_FILE")
transcript = transcript_file.read_text(encoding="utf-8").strip()
url = TEST_URL
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url, wait_until="networkidle")
    page.locator("#file-picker").set_input_files(str(root / "tests/fixtures/synthetic.epub"))
    expect(page.locator("#book-title")).to_have_text("A Free World")
    page.locator("#reference-audio").set_input_files(str(reference))
    expect(page.locator("#reference-name")).to_contain_text("002-0001.wav", timeout=15000)
    page.locator("#reference-text").fill(transcript)

    page.locator("#presets input[value=reflective]").check(force=True)
    started = time.monotonic()
    page.locator("#full").click()
    expect(page.locator("#job-state")).to_have_text("Audiobook complete", timeout=300000)
    job = page.evaluate('localStorage.getItem("studio.job")')
    payload = page.request.get(url + "api/jobs/" + job).json()
    assert payload["mode"] == "full" and payload["book_url"] and payload["preview_url"] and not errors, (
        payload,
        errors,
    )
    response = page.request.get(url.rstrip("/") + payload["preview_url"])
    assert response.ok and len(response.body()) > 1000
    path = root / "samples/reference-real-full-passage.wav"
    path.write_bytes(response.body())
    with wave.open(str(path), "rb") as wav:
        values = array.array("h")
        values.frombytes(wav.readframes(wav.getnframes()))
        assert wav.getframerate() == 24000 and wav.getnchannels() == 1 and max(map(abs, values)) > 100
        duration = wav.getnframes() / wav.getframerate()
    manifest = TEST_STORE / "books" / payload["book_id"] / "versions" / payload["version"] / "manifest.json"
    config = json.loads(manifest.read_text())
    assert config["reference_text"] == transcript and config["reference_sha256"]
    print(
        "REAL_REFERENCE_OK",
        "seconds",
        round(time.monotonic() - started, 1),
        "audio_seconds",
        round(duration, 1),
        "wav_bytes",
        path.stat().st_size,
        "errors",
        errors,
        flush=True,
    )
    browser.close()
