"""Fake Chromium microphone end-to-end; never opens the physical microphone."""

from e2e_support import URL as TEST_URL, mock_ready
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

root = Path(__file__).resolve().parent.parent
url = os.environ.get("STUDIO_URL", TEST_URL)
with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True, args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
    )
    context = browser.new_context(viewport={"width": 1280, "height": 900}, permissions=["microphone"])
    context.add_init_script("""
      window.__micCalls=0;
      const real=Object.getPrototypeOf(navigator.mediaDevices).getUserMedia;
      navigator.mediaDevices.getUserMedia=async function(constraints){
        window.__micCalls++;if(!constraints.audio||constraints.video)throw Error('Unexpected capture');
        const stream=await real.call(this,constraints);
        window.__micTrack=stream.getAudioTracks()[0];return stream;
      };
    """)
    page = context.new_page()
    errors = []
    page.set_default_timeout(4000)
    page.on("pageerror", lambda error: errors.append(str(error)))
    mock_ready(page)
    page.goto(url, wait_until="networkidle")
    assert page.evaluate("window.__micCalls") == 0
    page.locator("#file-picker").set_input_files(str(root / "tests/fixtures/synthetic.epub"))
    expect(page.locator("#book-title")).to_have_text("A Free World")
    assert page.evaluate("window.__micCalls") == 0
    page.locator("#record-start").click()
    expect(page.locator("#record-stop")).to_be_visible(timeout=5000)
    expect(page.locator("#record-status")).to_contain_text("Recording")
    assert page.evaluate("window.__micCalls") == 1
    page.wait_for_timeout(2450)
    page.locator("#record-stop").click()
    expect(page.locator("#reference-name")).to_contain_text("My voice recording", timeout=15000)
    assert page.evaluate("window.__micTrack.readyState") == "ended"
    assert page.locator("#record-playback").is_visible()
    assert page.locator("#record-playback").evaluate("(audio)=>audio.paused")
    page.wait_for_function("() => document.querySelector('#record-playback').readyState >= 1", timeout=5000)
    assert page.locator("#record-playback").evaluate("(audio)=>audio.error === null")
    assert page.locator("#full").is_disabled() and page.locator("#preview").is_disabled()
    page.locator("#reference-text").fill("This is a sample of my own voice.")
    assert page.locator("#full").is_enabled() and page.locator("#preview").is_enabled()
    page.locator(".voice").screenshot(path=str(root / "samples/voice-recording-ui.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.locator(".voice").screenshot(path=str(root / "samples/voice-recording-mobile.png"))
    assert not errors, errors
    print(
        "FAKE_MIC_RECORD_OK",
        page.locator("#reference-name").inner_text(),
        "mic_calls",
        page.evaluate("window.__micCalls"),
        "track",
        page.evaluate("window.__micTrack.readyState"),
        "errors",
        errors,
    )
    context.close()

    denied = browser.new_context()
    denied.add_init_script(
        """navigator.mediaDevices.getUserMedia=async()=>{throw new DOMException('Denied','NotAllowedError')}"""
    )
    p2 = denied.new_page()
    p2.goto(url, wait_until="networkidle")
    p2.locator("#file-picker").set_input_files(str(root / "tests/fixtures/synthetic.epub"))
    expect(p2.locator("#book-title")).to_have_text("A Free World")
    p2.locator("#record-start").click()
    expect(p2.locator("#notice")).to_contain_text("Microphone access", timeout=5000)
    assert p2.locator("#record-start").is_enabled() and p2.locator("#record-stop").is_hidden()
    print("MIC_DENIED_OK")
    denied.close()

    leave = browser.new_context(permissions=["microphone"])
    leave.add_init_script("""const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia=async options=>{const s=await original(options);window.__track=s.getAudioTracks()[0];return s}""")
    p3 = leave.new_page()
    p3.goto(url, wait_until="networkidle")
    p3.locator("#file-picker").set_input_files(str(root / "tests/fixtures/synthetic.epub"))
    expect(p3.locator("#book-title")).to_have_text("A Free World")
    p3.locator("#record-start").click()
    expect(p3.locator("#record-stop")).to_be_visible()
    p3.evaluate('window.dispatchEvent(new Event("pagehide"))')
    assert p3.evaluate("window.__track.readyState") == "ended"
    expect(p3.locator("#record-stop")).to_be_hidden()
    expect(p3.locator("#reference-selected")).to_be_hidden()
    print("LEAVE_DISCARDS_OK")
    leave.close()

    fault = browser.new_context(permissions=["microphone"])
    fault.add_init_script("""const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia=async options=>{const s=await original(options);window.__track=s.getAudioTracks()[0];return s};
      const recorder=window.MediaRecorder;
      window.MediaRecorder=class {static isTypeSupported(type){return recorder.isTypeSupported(type)}
        constructor(){throw new Error('Simulated recorder failure')}};""")
    p4 = fault.new_page()
    p4.goto(url, wait_until="networkidle")
    p4.locator("#file-picker").set_input_files(str(root / "tests/fixtures/synthetic.epub"))
    expect(p4.locator("#book-title")).to_have_text("A Free World")
    p4.locator("#record-start").click()
    expect(p4.locator("#notice")).to_contain_text("Microphone access failed")
    assert p4.evaluate("window.__track.readyState") == "ended"
    expect(p4.locator("#record-start")).to_be_enabled()
    print("RECORDER_FAILURE_RELEASES_MIC_OK")
    fault.close()

    # Accelerate only the app's recording limit; keep real MediaRecorder capture intact.
    capped = browser.new_context(permissions=["microphone"])
    capped.add_init_script("""const realTimeout=window.setTimeout.bind(window);
      window.setTimeout=(fn,delay,...args)=>realTimeout(fn,delay===29500?2400:delay,...args);
      const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia=async options=>{const s=await original(options);window.__track=s.getAudioTracks()[0];return s}""")
    p5 = capped.new_page()
    p5.goto(url, wait_until="networkidle")
    p5.locator("#file-picker").set_input_files(str(root / "tests/fixtures/synthetic.epub"))
    expect(p5.locator("#book-title")).to_have_text("A Free World")
    p5.locator("#record-start").click()
    expect(p5.locator("#reference-name")).to_contain_text("My voice recording", timeout=15000)
    assert p5.evaluate("window.__track.readyState") == "ended"
    expect(p5.locator("#record-stop")).to_be_hidden()
    selected = p5.locator("#reference-name").inner_text()
    p5.locator("#record-start").click()
    expect(p5.locator("#record-stop")).to_be_visible()
    p5.locator("#record-stop").click()
    expect(p5.locator("#notice")).to_contain_text("at least 2 seconds")
    assert p5.evaluate("window.__track.readyState") == "ended"
    assert p5.locator("#reference-name").inner_text() == selected
    print("AUTO_LIMIT_AND_SHORT_CLIP_OK")
    capped.close()
    browser.close()
