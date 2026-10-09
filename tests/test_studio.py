import pytest
import io
import re
import time
import wave
import math
import struct
from pathlib import Path

from fastapi.testclient import TestClient
from studio import create_app
from test_audiobook import sample_epub
from setup_fakes import ReadySetup, ready_environment


def test_upload_inspect_and_reject_bad_inputs(tmp_path):
    app = create_app(tmp_path / "store", runner=lambda *a, **kw: None)
    with TestClient(app) as client:
        source = sample_epub(tmp_path / "read.epub")
        response = client.post(
            "/api/books", files={"file": ("reader.epub", source.read_bytes(), "application/epub+zip")}
        )
        assert response.status_code == 200, response.text
        book = response.json()
        assert [c["title"] for c in book["chapters"]] == ["Introduction", "Chapter One"]
        assert book["id"] and book["title"] == "A Free World"
        assert client.get("/api/books/" + book["id"]).json()["author"] == "A. Reader"
        assert client.post("/api/books", files={"file": ("bad.epub", b"not zip")}).status_code == 400
        assert client.post("/api/books", files={"file": ("bad.txt", source.read_bytes())}).status_code == 400
        assert client.get("/api/books/../etc").status_code in (404, 400)
        assert client.get("/", headers={"host": "evil.example"}).status_code == 403
        assert (
            client.post(
                "/api/books",
                headers={"origin": "https://evil.example"},
                files={"file": ("a.epub", source.read_bytes())},
            ).status_code
            == 403
        )


def test_preview_full_resume_status_files_and_cancel(tmp_path):
    import threading
    from audiobook import convert
    from test_audiobook import tone

    started = threading.Event()
    release = threading.Event()
    calls = []

    def runner(source, output, style, preview_chapter=None):
        calls.append((style, preview_chapter))
        if preview_chapter is None:
            started.set()
            release.wait(4)
        return convert(
            source,
            output,
            style,
            synth=tone,
            start_chapter=preview_chapter or 1,
            max_passages=1 if preview_chapter else None,
        )

    app = create_app(tmp_path / "store", runner=runner)
    with TestClient(app) as client:
        source = sample_epub(tmp_path / "book.epub")
        book = client.post("/api/books", files={"file": ("book.epub", source.read_bytes())}).json()
        # Preset is edited to the test voice to exercise the exact style/resume contract.
        payload = {"book_id": book["id"], "style": "Warm, measured narrator", "mode": "preview", "chapter": 2}
        response = client.post("/api/jobs", json=payload)
        assert response.status_code == 200, response.text
        job = response.json()
        for _ in range(100):
            status = client.get("/api/jobs/" + job["id"]).json()
            if status["state"] not in ("running", "queued"):
                break
            time.sleep(0.02)
        assert status["state"] == "complete", status
        assert status["preview_url"]
        audio = client.get(status["preview_url"])
        assert audio.status_code == 200 and audio.headers["content-type"].startswith("audio/")
        assert client.get("/api/jobs/" + job["id"] + "/files/passages/../../manifest.json").status_code != 200
        full = client.post("/api/jobs", json={**payload, "mode": "full"})
        assert full.status_code == 200, full.text
        assert started.wait(3)
        assert client.post("/api/jobs", json=payload).status_code == 409
        release.set()
        for _ in range(100):
            status = client.get("/api/jobs/" + full.json()["id"]).json()
            if status["state"] not in ("running", "queued"):
                break
            time.sleep(0.03)
        assert status["state"] == "complete", status
        assert status["book_url"]
        assert client.get(status["book_url"]).status_code == 200
        assert len(status["chapters"]) == 2
        assert calls == [("Warm, measured narrator", 2), ("Warm, measured narrator", None)]


def test_preview_start_chapter_does_not_synthesize_earlier_chapters(tmp_path):
    from audiobook import convert
    from test_audiobook import tone

    source = sample_epub(tmp_path / "book.epub")
    out = tmp_path / "output"
    result = convert(source, out, "Warm, measured narrator", synth=tone, start_chapter=2, max_passages=1)
    assert not result["complete"]
    assert sorted(p.name for p in (out / "passages").glob("*.wav")) == ["002-0001.wav"]
    convert(source, out, "Warm, measured narrator", synth=tone)
    assert (out / "A Free World.m4b").is_file()


def test_chapter_passage_picker_previews_only_requested_passage(tmp_path):
    from audiobook import convert
    from ebooklib import epub
    from test_audiobook import tone

    book = epub.EpubBook()
    book.set_identifier("preview-picker")
    book.set_title("Long Chapter")
    book.set_language("en")
    chapter = epub.EpubHtml(title="First chapter", file_name="one.xhtml", lang="en")
    chapter.content = (
        "<html><body><h1>First chapter</h1><p>" + ("Freedom begins with care. " * 55) + "</p></body></html>"
    )
    book.add_item(chapter)
    book.toc = (epub.Link("one.xhtml", "First chapter", "one"),)
    book.spine = ["nav", chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    source = tmp_path / "long.epub"
    epub.write_epub(str(source), book)

    def runner(source, output, style, preview_chapter=None, *, preview_passage=1):
        return convert(
            source,
            output,
            style,
            synth=tone,
            start_chapter=preview_chapter or 1,
            max_chapters=preview_chapter,
            max_passages=1 if preview_chapter else None,
            preview_passage=preview_passage if preview_chapter else None,
        )

    app = create_app(tmp_path / "store", runner=runner)
    with TestClient(app) as client:
        item = client.post("/api/books", files={"file": ("long.epub", source.read_bytes())}).json()
        passages = client.get(f"/api/books/{item['id']}/chapters/1/passages")
        assert passages.status_code == 200, passages.text
        parts = passages.json()["passages"]
        assert len(parts) >= 3 and parts[2]["index"] == 3 and "Freedom" in parts[2]["excerpt"]
        assert client.get(f"/api/books/{item['id']}/chapters/2/passages").status_code == 400
        invalid = {
            "book_id": item["id"],
            "style": "Warm, measured narrator",
            "mode": "preview",
            "chapter": 1,
            "passage": len(parts) + 1,
        }
        assert client.post("/api/jobs", json=invalid).status_code == 400
        request = {**invalid, "passage": 3}
        job = client.post("/api/jobs", json=request)
        assert job.status_code == 200, job.text
        for _ in range(100):
            status = client.get("/api/jobs/" + job.json()["id"]).json()
            if status["state"] != "running":
                break
            time.sleep(0.03)
        assert status["state"] == "complete", status
        assert status["preview_url"].endswith("001-0003.wav")
        assert client.get(status["preview_url"]).status_code == 200
        out = tmp_path / "store" / "books" / item["id"] / "versions" / status["version"]
        assert sorted(p.name for p in (out / "passages").glob("*.wav")) == ["001-0003.wav"]
        assert client.post("/api/jobs", json={**request, "mode": "full"}).status_code == 400


def test_library_survives_restart_and_exposes_only_completed_chapter_audio(tmp_path):
    from audiobook import convert
    from test_audiobook import tone

    store = tmp_path / "store"

    def runner(source, output, style, preview_chapter=None):
        return convert(source, output, style, synth=tone)

    app = create_app(store, runner=runner)
    with TestClient(app) as client:
        source = sample_epub(tmp_path / "book.epub")
        item = client.post("/api/books", files={"file": ("book.epub", source.read_bytes())}).json()
        assert client.get("/api/library").json()["books"] == []  # Uploaded is not an audiobook.
        assert client.get(f"/api/books/{item['id']}/versions").json()["versions"] == []
        job = client.post(
            "/api/jobs", json={"book_id": item["id"], "style": "Warm, measured narrator", "mode": "full", "chapter": 1}
        ).json()
        for _ in range(150):
            status = client.get("/api/jobs/" + job["id"]).json()
            if status["state"] != "running":
                break
            time.sleep(0.03)
        assert status["state"] == "complete", status
        version = status["version"]
    # A new process has no old job ID. The shelf and stable audio routes still work.
    with TestClient(create_app(store, runner=runner)) as client:
        shelf = client.get("/api/library")
        assert shelf.status_code == 200, shelf.text
        assert [(b["id"], b["title"]) for b in shelf.json()["books"]] == [(item["id"], "A Free World")]
        versions = client.get(f"/api/books/{item['id']}/versions")
        assert versions.status_code == 200, versions.text
        entry = versions.json()["versions"][0]
        assert entry["id"] == version and entry["complete"]
        assert len(entry["chapters"]) == 2 and entry["book_url"]
        assert client.get(entry["book_url"]).status_code == 200
        assert all(client.get(ch["url"]).status_code == 200 for ch in entry["chapters"])
        assert (
            client.get(f"/api/books/{item['id']}/versions/not-a-hash/files/book/A%20Free%20World.m4b").status_code
            == 404
        )
        assert client.get(f"/api/books/{item['id']}/versions/{version}/files/chapters/manifest.json").status_code == 404
        assert client.get(f"/api/books/{item['id']}/versions/{version}/files/passages/001-0001.wav").status_code == 404


def test_shelf_hides_incomplete_versions_and_uploaded_only_books(tmp_path):
    import shutil
    from audiobook import convert
    from test_audiobook import tone

    store = tmp_path / "store"
    with TestClient(create_app(store, runner=lambda *a, **kw: None)) as client:
        a = sample_epub(tmp_path / "a.epub")
        one = client.post("/api/books", files={"file": ("a.epub", a.read_bytes())}).json()
        two = client.post("/api/books", files={"file": ("b.epub", a.read_bytes())}).json()
    out = store / "books" / one["id"] / "versions" / ("a" * 16)
    convert(store / "books" / one["id"] / "book.epub", out, "Warm, measured narrator", synth=tone)
    incomplete = store / "books" / one["id"] / "versions" / ("b" * 16)
    shutil.copytree(out, incomplete)
    next(incomplete.glob("*.m4b")).unlink()
    with TestClient(create_app(store, runner=lambda *a, **kw: None)) as client:
        shelf = client.get("/api/library").json()["books"]
        assert len(shelf) == 1 and shelf[0]["id"] == one["id"] and shelf[0]["versions"] == 1
        assert two["id"] not in [item["id"] for item in shelf]
        entries = client.get(f"/api/books/{one['id']}/versions").json()["versions"]
        assert len(entries) == 2 and sum(entry["complete"] for entry in entries) == 1


def test_retry_one_passage_keeps_original_and_creates_a_new_complete_version(tmp_path):
    from audiobook import convert
    from test_audiobook import tone

    calls = []

    def runner(source, output, style, preview_chapter=None, **kwargs):
        def synth(path, text, style, seed):
            calls.append(text)
            tone(path, text, style, seed)

        return convert(source, output, style, synth=synth, **kwargs)

    with TestClient(create_app(tmp_path / "store", runner=runner)) as client:
        source = sample_epub(tmp_path / "source.epub")
        book = client.post("/api/books", files={"file": ("source.epub", source.read_bytes())}).json()
        request = {"book_id": book["id"], "style": "Warm, measured narrator", "mode": "full", "chapter": 1}
        started = client.post("/api/jobs", json=request).json()
        for _ in range(150):
            status = client.get("/api/jobs/" + started["id"]).json()
            if status["state"] != "running":
                break
            time.sleep(0.03)
        assert status["state"] == "complete", status
        old_version = status["version"]
        original_count = len(calls)
        body = {"chapter": 2, "passage": 1, "text": "Freedom begins with mutual care. We build it together."}
        invalid = client.post(f"/api/books/{book['id']}/versions/{old_version}/retry", json={**body, "passage": 99})
        assert invalid.status_code == 400
        started = client.post(f"/api/books/{book['id']}/versions/{old_version}/retry", json=body)
        assert started.status_code == 200, started.text
        job = started.json()
        for _ in range(150):
            status = client.get("/api/jobs/" + job["id"]).json()
            if status["state"] != "running":
                break
            time.sleep(0.03)
        assert status["state"] == "complete", status
        assert status["version"] != old_version and calls[original_count:] == [body["text"]]
        old = client.get(f"/api/books/{book['id']}/versions").json()["versions"]
        assert len(old) == 2 and all(v["complete"] for v in old)
        assert (
            client.get(f"/api/books/{book['id']}/chapters/2/passages?version={status['version']}").json()["passages"][
                0
            ]["text"]
            == body["text"]
        )
        assert (
            client.get(f"/api/books/{book['id']}/chapters/2/passages?version={old_version}").json()["passages"][0][
                "text"
            ]
            != body["text"]
        )


def test_failed_correction_resumes_same_version_then_new_take_gets_new_version(tmp_path):
    from audiobook import convert
    from test_audiobook import tone

    fail_once = [True]

    def runner(source, output, style, preview_chapter=None, **kwargs):
        def synth(path, text, style, seed):
            if kwargs.get("base_output") and fail_once[0]:
                fail_once[0] = False
                raise RuntimeError("Injected synthesis failure")
            tone(path, text, style, seed)

        return convert(source, output, style, synth=synth, **kwargs)

    with TestClient(create_app(tmp_path / "store", runner=runner)) as client:
        source = sample_epub(tmp_path / "source.epub")
        book = client.post("/api/books", files={"file": ("source.epub", source.read_bytes())}).json()
        started = client.post(
            "/api/jobs", json={"book_id": book["id"], "style": "Warm, measured narrator", "mode": "full", "chapter": 1}
        ).json()

        def terminal_status(job):
            for _ in range(150):
                status = client.get("/api/jobs/" + job["id"]).json()
                if status["state"] != "running":
                    return status
                time.sleep(0.03)
            raise AssertionError("Job did not finish")

        base = terminal_status(started)
        assert base["state"] == "complete"
        url = f"/api/books/{book['id']}/versions/{base['version']}/retry"
        body = {"chapter": 2, "passage": 1, "text": "A careful and clear new take."}
        first = terminal_status(client.post(url, json=body).json())
        assert first["state"] == "error" and "Injected synthesis failure" in first["error"]
        second = terminal_status(client.post(url, json=body).json())
        assert second["state"] == "complete" and second["version"] == first["version"]
        third = terminal_status(client.post(url, json=body).json())
        assert third["state"] == "complete" and third["version"] != first["version"]
        assert client.get(f"/api/books/{book['id']}/versions").json()["versions"][0]["retry"]["attempt"] == 1


def test_cancelling_keeps_lane_reserved_until_worker_exits(tmp_path):
    import threading

    started, release = threading.Event(), threading.Event()

    def slow_runner(source, output, style, preview_chapter=None):
        started.set()
        assert release.wait(3)

    app = create_app(tmp_path / "store", runner=slow_runner)
    with TestClient(app) as client:
        source = sample_epub(tmp_path / "book.epub")
        book = client.post("/api/books", files={"file": ("a.epub", source.read_bytes())}).json()
        payload = {"book_id": book["id"], "style": "Warm, measured narrator", "mode": "preview", "chapter": 1}
        first = client.post("/api/jobs", json=payload).json()
        assert started.wait(2)
        cancelling = client.post("/api/jobs/" + first["id"] + "/cancel").json()
        assert cancelling["state"] == "cancelling"
        assert client.post("/api/jobs", json=payload).status_code == 409
        release.set()
        for _ in range(100):
            status = client.get("/api/jobs/" + first["id"]).json()
            if status["state"] == "cancelled":
                break
            time.sleep(0.02)
        assert status["state"] == "cancelled"


def test_cancel_signals_only_own_subprocess_group(tmp_path, monkeypatch):
    import sys
    import subprocess
    import threading
    import studio

    ready = threading.Event()
    children = []

    def worker(source, output, style, preview_chapter=None, *, on_process, on_line):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        children.append(proc)
        on_process(proc)
        ready.set()
        assert proc.wait(timeout=5) != 0
        raise RuntimeError("cancelled")

    monkeypatch.setattr(studio, "default_runner", worker)
    app = studio.create_app(tmp_path / "store", setup=ReadySetup(), readiness=ready_environment)
    with TestClient(app) as client:
        source = sample_epub(tmp_path / "book.epub")
        book = client.post("/api/books", files={"file": ("book.epub", source.read_bytes())}).json()
        job = client.post(
            "/api/jobs", json={"book_id": book["id"], "style": "A calm narrator", "mode": "preview", "chapter": 1}
        ).json()
        assert ready.wait(2)
        cancelled = client.post("/api/jobs/" + job["id"] + "/cancel").json()
        assert cancelled["state"] in ("cancelling", "cancelled")
        for _ in range(100):
            status = client.get("/api/jobs/" + job["id"]).json()
            if status["state"] == "cancelled":
                break
            time.sleep(0.02)
        assert status["state"] == "cancelled"
        assert children[0].poll() is not None


def test_runner_streams_passage_log_before_child_exits(tmp_path, monkeypatch):
    import sys
    import subprocess
    import threading
    import studio

    real_popen = subprocess.Popen
    child = []

    def canary_popen(cmd, **kwargs):
        assert "--quiet-summary" in cmd
        proc = real_popen(
            [
                sys.executable,
                "-c",
                'import time; print("Generated chapter 1/2 passage 1/3: saved.wav"); time.sleep(1.5)',
            ],
            **kwargs,
        )
        child.append(proc)
        return proc

    monkeypatch.setattr(studio.subprocess, "Popen", canary_popen)
    seen = threading.Event()
    lines = []
    thread = threading.Thread(
        target=studio.default_runner,
        args=(tmp_path / "book.epub", tmp_path / "out", "Warm"),
        kwargs={"on_line": lambda line: (lines.append(line), seen.set())},
    )
    thread.start()
    try:
        assert seen.wait(0.6), "Child output was buffered until exit"
        assert child[0].poll() is None, "Line must arrive while worker is running"
        assert "Generated chapter" in lines[-1]
    finally:
        thread.join(4)


def test_swap_gate_error_is_actionable_without_traceback(tmp_path, monkeypatch):
    import sys
    import subprocess
    import studio

    real_popen = subprocess.Popen

    def failed_popen(cmd, **kwargs):
        return real_popen(
            [
                sys.executable,
                "-c",
                'print("RuntimeError: Swap pressure is too high for local inference"); raise SystemExit(1)',
            ],
            **kwargs,
        )

    monkeypatch.setattr(studio.subprocess, "Popen", failed_popen)
    try:
        studio.default_runner(tmp_path / "book.epub", tmp_path / "out", "Warm")
    except RuntimeError as exc:
        message = str(exc)
    else:
        assert False, "Expected a safe refusal"
    assert "swap" in message.lower() and "90%" in message
    assert "saved" in message.lower() and "retry" in message.lower()
    assert "RuntimeError:" not in message and 'File "' not in message


def test_every_preset_has_a_shipped_voice_sample_and_nothing_else_resolves(tmp_path):
    app = create_app(tmp_path / "store", runner=lambda *a, **kw: None)
    with TestClient(app) as client:
        for key in client.get("/api/presets").json():
            sample = client.get(f"/voices/{key}.m4a")
            assert sample.status_code == 200 and sample.headers["content-type"] == "audio/mp4"
        for bad in ("missing.m4a", "warm.wav", "warm", "..%2Fapp.js", "%2E%2E%2Fstyle.css"):
            assert client.get("/voices/" + bad).status_code == 404


def test_every_preset_is_on_the_page_and_fun_voices_sit_behind_the_toggle(tmp_path):
    app = create_app(tmp_path / "store", runner=lambda *a, **kw: None)
    with TestClient(app) as client:
        page = client.get("/").text
        styles = client.get("/api/presets").json()
    assert set(re.findall(r'name="voice" value="(\w+)"', page)) == set(styles)
    fun = re.findall(r'<div class="preset-item fun hidden">.*?value="(\w+)"', page)
    assert fun == ["wizard", "naturalist", "hype", "spy"]
    assert 'id="fun-toggle"' in page and 'aria-pressed="false"' in page


def test_preset_styles_and_activity_are_explicit_in_section_two(tmp_path):
    app = create_app(tmp_path / "store", runner=lambda *a, **kw: None)
    with TestClient(app) as client:
        page = client.get("/").text
        styles = client.get("/api/presets").json()
    assert "Preset narrator styles" in page
    assert {"warm", "literary", "bright", "calm"}.issubset(styles)
    assert all(f'value="{key}"' in page for key in styles)
    assert "not separate fixed voice models" in page
    assert '<details class="activity" open>' in page


def test_stage_event_reaches_live_status_before_worker_finishes(tmp_path, monkeypatch):
    import json
    import threading
    import studio

    stage_seen, release = threading.Event(), threading.Event()

    def staged_worker(source, output, style, preview_chapter=None, *, on_process, on_line):
        on_line(
            "STUDIO_EVENT "
            + json.dumps(
                {
                    "stage": "synthesizing",
                    "message": "Synthesizing chapter 1/2, passage 1/3",
                    "chapter": 1,
                    "passage": 1,
                }
            )
        )
        stage_seen.set()
        assert release.wait(3)

    monkeypatch.setattr(studio, "default_runner", staged_worker)
    app = studio.create_app(tmp_path / "store", setup=ReadySetup(), readiness=ready_environment)
    with TestClient(app) as client:
        source = sample_epub(tmp_path / "book.epub")
        book = client.post("/api/books", files={"file": ("book.epub", source.read_bytes())}).json()
        started = client.post(
            "/api/jobs", json={"book_id": book["id"], "style": "Warm storyteller", "mode": "preview", "chapter": 1}
        ).json()
        try:
            assert stage_seen.wait(2)
            status = client.get("/api/jobs/" + started["id"]).json()
            assert status["state"] == "running"
            assert status["phase"] == "synthesizing"
            assert status["stage_message"] == "Synthesizing chapter 1/2, passage 1/3"
            assert status["started_at"] > 0
            assert status["eta_narration_seconds"] is None  # A preview cannot be extrapolated.
            assert any("Synthesizing chapter" in line for line in status["log"])
        finally:
            release.set()


def test_narration_eta_needs_measured_passages_and_excludes_cached_work():
    from studio import estimate_narration

    assert estimate_narration([], 1000, 0) is None
    assert estimate_narration([(100, 10)], 1000, 100) is None
    assert estimate_narration([(100, 10), (100, 20)], 1000, 200) == 120
    # Cached work is already done, but its unknown compute time is not a sample.
    assert estimate_narration([(100, 10), (100, 20)], 1000, 400) == 90
    assert estimate_narration([(100, 10), (100, 20)], 1000, 1000) is None


def test_full_job_exposes_eta_only_after_two_new_saves(tmp_path, monkeypatch):
    import json
    import threading
    import studio

    first, continue_second, second, finish = (threading.Event() for _ in range(4))
    clock = [10]
    monkeypatch.setattr(studio, "elapsed_clock", lambda: clock[0], raising=False)

    def worker(source, output, style, preview_chapter=None, *, on_process, on_line):
        def event(stage, **fields):
            on_line("STUDIO_EVENT " + json.dumps({"stage": stage, "message": stage, **fields}))

        event("planning", total_characters=1000, total_passages=10)
        event("synthesizing", chapter=1, passage=1, characters=100)
        clock[0] = 30
        event("saved", chapter=1, passage=1, characters=100)
        first.set()
        assert continue_second.wait(3)
        clock[0] = 31
        event("synthesizing", chapter=1, passage=2, characters=100)
        clock[0] = 41
        event("saved", chapter=1, passage=2, characters=100)
        event("reused", chapter=1, passage=3, characters=200)
        second.set()
        assert finish.wait(3)

    monkeypatch.setattr(studio, "default_runner", worker)
    app = studio.create_app(tmp_path / "store", setup=ReadySetup(), readiness=ready_environment)
    with TestClient(app) as client:
        source = sample_epub(tmp_path / "book.epub")
        book = client.post("/api/books", files={"file": ("book.epub", source.read_bytes())}).json()
        job = client.post(
            "/api/jobs", json={"book_id": book["id"], "style": "Warm narrator", "mode": "full", "chapter": 1}
        ).json()
        try:
            assert first.wait(2)
            state = client.get("/api/jobs/" + job["id"]).json()
            assert state["eta_narration_seconds"] is None
            continue_second.set()
            assert second.wait(2)
            state = client.get("/api/jobs/" + job["id"]).json()
            assert state["state"] == "running"
            assert state["eta_narration_seconds"] == 90  # Cached work reduces remaining, not measured speed.
            assert state["eta_samples"] == 2
        finally:
            continue_second.set()
            finish.set()


def reference_wav(seconds=3, silent=False):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(
            b"".join(
                struct.pack("<h", 0 if silent else round(3500 * math.sin(i * 0.1)))
                for i in range(round(seconds * 24000))
            )
        )
    return output.getvalue()


def test_reference_upload_validation_version_and_runner(tmp_path):
    import threading
    from audiobook import convert
    from test_audiobook import tone

    calls = []
    ready = threading.Event()

    def runner(source, output, style, preview_chapter=None, *, ref_audio=None, ref_text=None):
        calls.append((output, ref_audio, ref_text))
        convert(
            source,
            output,
            style,
            synth=tone,
            start_chapter=preview_chapter or 1,
            max_passages=1 if preview_chapter else None,
            ref_audio=ref_audio,
            ref_text=ref_text,
        )
        ready.set()

    app = create_app(tmp_path / "store", runner=runner)
    with TestClient(app) as client:
        source = sample_epub(tmp_path / "book.epub")
        book = client.post("/api/books", files={"file": ("book.epub", source.read_bytes())}).json()
        url = f"/api/books/{book['id']}/references"
        assert client.post(url, files={"file": ("oops.txt", b"not audio")}).status_code == 400
        assert client.post(url, files={"file": ("bad.wav", b"not wav")}).status_code == 400
        assert client.post(url, files={"file": ("silent.wav", reference_wav(silent=True))}).status_code == 400
        assert client.post(url, files={"file": ("long.wav", reference_wav(seconds=31))}).status_code == 400
        assert client.post(url, files={"file": ("huge.wav", b"0" * (10 * 1024 * 1024 + 1))}).status_code == 400
        response = client.post(url, files={"file": ("voice.wav", reference_wav())})
        assert response.status_code == 200, response.text
        ref = response.json()
        assert ref["id"] and ref["name"] == "voice.wav" and ref["duration_seconds"] >= 2
        assert client.get(url + "/" + ref["id"]).json() == ref
        assert client.get(url + "/" + "0" * 64).status_code == 404
        assert client.post(url, files={"file": ("voice.wav", reference_wav())}).json()["id"] == ref["id"]
        base = {"book_id": book["id"], "style": "Warm, measured narrator", "mode": "preview", "chapter": 2}
        assert client.post("/api/jobs", json={**base, "reference_id": ref["id"]}).status_code == 400
        assert client.post("/api/jobs", json={**base, "reference_text": "Unmatched transcript."}).status_code == 400
        assert (
            client.post(
                "/api/jobs", json={**base, "reference_id": "0" * 64, "reference_text": "A sample line."}
            ).status_code
            == 404
        )
        first = client.post(
            "/api/jobs", json={**base, "reference_id": ref["id"], "reference_text": "Exactly what the speaker says."}
        )
        assert first.status_code == 200, first.text
        assert ready.wait(12), client.get("/api/jobs/" + first.json()["id"]).json()
        for _ in range(100):
            if client.get("/api/jobs/" + first.json()["id"]).json()["state"] == "complete":
                break
            time.sleep(0.02)
        assert calls[0][1].is_file() and calls[0][1].name == ref["id"] + ".wav"
        assert calls[0][2] == "Exactly what the speaker says."
        ready.clear()
        second = client.post(
            "/api/jobs", json={**base, "reference_id": ref["id"], "reference_text": "Exactly what the speaker says."}
        )
        assert ready.wait(3)
        assert calls[0][0] == calls[1][0]  # Stable resume version.
        assert second.json()["version"] == first.json()["version"]
        ready.clear()
        changed = client.post(
            "/api/jobs", json={**base, "reference_id": ref["id"], "reference_text": "Another exact transcript."}
        )
        assert ready.wait(3)
        assert calls[2][0] != calls[1][0]
        assert changed.json()["version"] != first.json()["version"]
        ready.clear()
        plain = client.post("/api/jobs", json=base)
        assert ready.wait(3)
        assert calls[3][1:] == (None, None) and plain.json()["version"] != changed.json()["version"]


def test_runner_passes_local_reference_pair_without_leaking_transcript_in_logs(tmp_path, monkeypatch):
    import studio
    import subprocess

    real_popen = subprocess.Popen
    seen = []

    def canary(cmd, **kwargs):
        seen.append(cmd)
        return real_popen([studio.sys.executable, "-c", 'print("ready")'], **kwargs)

    monkeypatch.setattr(studio.subprocess, "Popen", canary)
    clip = tmp_path / "voice.wav"
    clip.write_bytes(reference_wav())
    studio.default_runner(
        tmp_path / "book.epub", tmp_path / "out", "Warm", ref_audio=clip, ref_text="Exact spoken words."
    )
    assert seen and seen[0][-4:-2] == ["--reference-audio", str(clip)]
    assert seen[0][-2] == "--reference-text-file"
    assert Path(seen[0][-1]).read_text() == "Exact spoken words."
    assert "Exact spoken words." not in seen[0]


def test_runner_correction_cli_uses_private_text_file_and_preserves_resume(tmp_path, monkeypatch):
    import studio
    import subprocess
    import pytest

    real_popen = subprocess.Popen
    commands = []

    def canary(cmd, **kwargs):
        commands.append(cmd)
        return real_popen([studio.sys.executable, "-c", 'print("ready")'], **kwargs)

    monkeypatch.setattr(studio.subprocess, "Popen", canary)
    correction = {
        "base_output": tmp_path / "original",
        "retry_chapter": 2,
        "retry_passage": 3,
        "retry_text": "Private corrected spoken words.",
        "retry_attempt": 1,
        "max_chars": 320,
    }
    out = tmp_path / "version"
    studio.default_runner(tmp_path / "book.epub", out, "Warm, measured narrator", correction=correction)
    cmd = commands[-1]
    assert cmd[cmd.index("--base-output") + 1] == str(correction["base_output"])
    assert cmd[cmd.index("--retry-chapter") + 1] == "2"
    assert cmd[cmd.index("--retry-passage") + 1] == "3"
    assert cmd[cmd.index("--retry-attempt") + 1] == "1"
    textfile = Path(cmd[cmd.index("--retry-text-file") + 1])
    assert textfile.read_text() == correction["retry_text"]
    assert textfile.stat().st_mode & 0o777 == 0o600
    assert correction["retry_text"] not in cmd
    studio.default_runner(tmp_path / "book.epub", out, "Warm, measured narrator", correction=correction)
    with pytest.raises(ValueError, match="changed"):
        studio.default_runner(
            tmp_path / "book.epub",
            out,
            "Warm, measured narrator",
            correction={**correction, "retry_text": "Unexpected different text"},
        )


def test_more_voice_delivery_presets_are_in_section_two(tmp_path):
    app = create_app(tmp_path / "store", runner=lambda *a, **kw: None)
    with TestClient(app) as client:
        page = client.get("/").text
        styles = client.get("/api/presets").json()
    assert {"warm", "literary", "bright", "calm", "documentary", "dramatic", "reflective", "crisp"}.issubset(styles)
    assert all(f'value="{key}"' in page for key in styles)
    assert "reference-audio" in page and "reference-text" in page


def test_browser_recording_format_uploads_locally_as_bounded_voice_clip(tmp_path):

    app = create_app(tmp_path / "store", runner=lambda *a, **kw: None)
    with TestClient(app) as client:
        source = sample_epub(tmp_path / "book.epub")
        book = client.post("/api/books", files={"file": ("book.epub", source.read_bytes())}).json()
        # A checked-in synthetic browser-format fixture does not require the
        # shipping decoder to contain an unrelated Opus encoder/WebM muxer.
        webm = Path(__file__).parent / "fixtures/reference-opus.webm"
        result = client.post(
            f"/api/books/{book['id']}/references",
            files={"file": ("my-recording.webm", webm.read_bytes(), "audio/webm")},
        )
        assert result.status_code == 200, result.text
        ref = result.json()
        assert ref["name"] == "my-recording.webm" and 2 <= ref["duration_seconds"] <= 30
        assert (tmp_path / "store" / "books" / book["id"] / "references" / (ref["id"] + ".wav")).is_file()


_REAL_POPEN = __import__("subprocess").Popen


def _worker_printing(monkeypatch, text):
    import sys
    import studio

    real_popen = _REAL_POPEN
    commands = []

    def fake_popen(cmd, **kwargs):
        commands.append(cmd)
        return real_popen([sys.executable, "-c", f"print({text!r}); raise SystemExit(1)"], **kwargs)

    monkeypatch.setattr(studio.subprocess, "Popen", fake_popen)
    return commands


def test_low_memory_refusal_offers_opt_in_and_hard_limits_do_not(tmp_path, monkeypatch):
    import studio

    _worker_printing(monkeypatch, "RuntimeError: Low memory: 9.3 GiB free of 12.5 GiB recommended")
    with pytest.raises(studio.LowMemoryError) as low:
        studio.default_runner(tmp_path / "book.epub", tmp_path / "out", "Warm")
    assert "9.3 GiB" in str(low.value) and "low-memory mode" in str(low.value) and "slower" in str(low.value)
    _worker_printing(
        monkeypatch, "RuntimeError: Memory hard limit: this computer has 8 GiB of RAM; narration needs at least 12 GiB"
    )
    with pytest.raises(RuntimeError) as hard:
        studio.default_runner(tmp_path / "book.epub", tmp_path / "out", "Warm")
    assert not isinstance(hard.value, studio.LowMemoryError)
    assert "8 GiB of RAM" in str(hard.value) and "hard limit" in str(hard.value)
    _worker_printing(monkeypatch, "RuntimeError: Memory pressure critical: stopped before the next passage")
    with pytest.raises(RuntimeError, match="critical") as critical:
        studio.default_runner(tmp_path / "book.epub", tmp_path / "out", "Warm")
    assert "Saved passages are intact" in str(critical.value)


def test_low_memory_opt_in_is_passed_to_the_worker_only_when_chosen(tmp_path, monkeypatch):
    import studio

    commands = _worker_printing(monkeypatch, "ok")
    for allow in (False, True):
        with pytest.raises(RuntimeError):
            studio.default_runner(tmp_path / "book.epub", tmp_path / "out", "Warm", allow_low_memory=allow)
    assert "--allow-low-memory" not in commands[0] and "--allow-low-memory" in commands[1]


def test_job_reports_low_memory_code_and_mode(tmp_path):
    import studio

    def refuse(*args, **kwargs):
        if not kwargs.get("allow_low_memory"):
            raise studio.LowMemoryError("Only 9 GiB free")

    app = create_app(tmp_path / "store", runner=refuse)
    with TestClient(app) as client:
        book = client.post(
            "/api/books",
            files={"file": ("b.epub", sample_epub(tmp_path / "b.epub").read_bytes(), "application/epub+zip")},
        ).json()
        body = {"book_id": book["id"], "style": "Warm narrator", "mode": "full"}
        job = client.post("/api/jobs", json=body).json()
        for _ in range(50):
            job = client.get(f"/api/jobs/{job['id']}").json()
            if job["state"] != "running":
                break
            time.sleep(0.05)
        assert job["state"] == "error" and job["error_code"] == "low_memory" and not job["low_memory"]
        job = client.post("/api/jobs", json={**body, "allow_low_memory": True}).json()
        assert job["low_memory"] is True


def test_fun_styles_bring_their_own_clip_and_a_users_clip_wins(tmp_path, monkeypatch):
    import hashlib, studio

    anchors = tmp_path / "anchors"
    anchors.mkdir()
    (anchors / "wizard.wav").write_bytes(reference_wav(4))
    monkeypatch.setattr(studio, "ANCHORS", anchors)
    calls = []
    app = create_app(tmp_path / "store", runner=lambda source, out, style, chapter, **kw: calls.append((out.name, kw)))

    def run(client, body):
        job = client.post("/api/jobs", json=body).json()
        for _ in range(50):
            if client.get(f"/api/jobs/{job['id']}").json()["state"] != "running":
                break
            time.sleep(0.02)
        return calls[-1]

    with TestClient(app) as client:
        book = client.post(
            "/api/books",
            files={"file": ("b.epub", sample_epub(tmp_path / "b.epub").read_bytes(), "application/epub+zip")},
        ).json()
        body = {"book_id": book["id"], "style": "Grand old wizard", "mode": "full"}
        plain_version, plain = run(client, {**body, "preset": "warm"})
        assert plain == {} and run(client, body)[1] == {} and run(client, {**body, "preset": "nope"})[1] == {}
        anchor_version, anchored = run(client, {**body, "preset": "wizard"})
        assert anchored["ref_text"] == studio.PRESET_ANCHORS["wizard"] and anchor_version != plain_version
        assert hashlib.sha256(anchored["ref_audio"].read_bytes()).hexdigest() == anchored["ref_audio"].stem
        assert (
            client.get(f"/api/books/{book['id']}/references/{anchored['ref_audio'].stem}").json()["duration_seconds"]
            == 4.0
        )
        mine = client.post(
            f"/api/books/{book['id']}/references", files={"file": ("me.wav", reference_wav(3), "audio/wav")}
        ).json()
        _, own = run(client, {**body, "preset": "wizard", "reference_id": mine["id"], "reference_text": "My words"})
        assert own["ref_audio"].stem == mine["id"] and own["ref_text"] == "My words"


def test_every_built_in_clip_is_a_valid_reference():
    import studio

    for key, words in studio.PRESET_ANCHORS.items():
        assert key in studio.PRESETS and words.strip()
        assert 2 <= studio._reference_info(studio.ANCHORS / f"{key}.wav")["duration_seconds"] <= 30
