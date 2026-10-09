"""Generation feedback regressions; controlled worker events, no model inference."""

import io
import json
import threading
import time

from fastapi.testclient import TestClient

import studio
from setup_fakes import ReadySetup, ready_environment
from test_audiobook import sample_epub


def test_first_measured_passage_is_enough_for_provisional_eta():
    assert studio.estimate_narration([(100, 10)], 1000, 100) == 90
    assert studio.estimate_narration([], 1000, 0) is None


def test_full_start_reports_totals_and_cached_progress_before_worker_events(tmp_path):
    release = threading.Event()

    def worker(*args, **kwargs):
        assert release.wait(5)

    store = tmp_path / "store"
    with TestClient(studio.create_app(store, runner=worker)) as client:
        source = sample_epub(tmp_path / "book.epub")
        book = client.post("/api/books", files={"file": ("book.epub", source.read_bytes())}).json()
        version = studio.hashlib.sha256(b"Warm narrator").hexdigest()[:16]
        folder = store / "books" / book["id"] / "versions" / version / "passages"
        folder.mkdir(parents=True)
        (folder / "001-0001.wav").write_bytes(b"cached fixture")
        (folder / "001-0002.partial.wav").write_bytes(b"not saved")
        try:
            job = client.post(
                "/api/jobs", json={"book_id": book["id"], "style": "Warm narrator", "mode": "full"}
            ).json()
            assert job["passages_total"] == sum(ch["passages"] for ch in book["chapters"])
            assert job["passages_done"] == 1
            assert job["current_chapter"] == 1
            assert job["eta_narration_seconds"] is None
        finally:
            release.set()


def test_preview_measurement_provides_eta_at_full_start_even_after_restart(tmp_path, monkeypatch):
    clock = [1.0]
    release, saved = threading.Event(), threading.Event()
    calls = [0]

    def worker(source, output, style, preview_chapter=None, *, on_process, on_line):
        calls[0] += 1
        if preview_chapter is None:
            assert release.wait(5)
            return

        def event(stage, **fields):
            on_line("STUDIO_EVENT " + json.dumps({"stage": stage, "message": stage, **fields}))

        output.mkdir(parents=True, exist_ok=True)
        event("synthesizing", chapter=1, passage=1, characters=100)
        clock[0] += 10
        event("saved", chapter=1, passage=1, characters=100)
        saved.set()

    monkeypatch.setattr(studio, "default_runner", worker)
    monkeypatch.setattr(studio, "elapsed_clock", lambda: clock[0])
    store = tmp_path / "store"

    def app():
        return studio.create_app(store, setup=ReadySetup(), readiness=ready_environment)

    with TestClient(app()) as client:
        source = sample_epub(tmp_path / "book.epub")
        book = client.post("/api/books", files={"file": ("book.epub", source.read_bytes())}).json()
        body = {"book_id": book["id"], "style": "Warm narrator", "mode": "preview"}
        preview = client.post("/api/jobs", json=body).json()
        assert saved.wait(3)
        for _ in range(100):
            if client.get(f"/api/jobs/{preview['id']}").json()["state"] == "complete":
                break
            time.sleep(0.01)
    with TestClient(app()) as client:
        try:
            job = client.post("/api/jobs", json={**body, "mode": "full"}).json()
            assert job["state"] == "running"
            assert job["eta_narration_seconds"] > 0
            assert job["eta_total_seconds"] >= job["eta_narration_seconds"]
            assert job["eta_basis"] == "previous"
            assert job["eta_samples"] == 1
        finally:
            release.set()
    assert calls[0] == 2


def test_current_chapter_is_distinct_from_saved_passage_count(tmp_path, monkeypatch):
    ready, release = threading.Event(), threading.Event()

    def worker(source, output, style, preview_chapter=None, *, on_process, on_line):
        folder = output / "passages"
        folder.mkdir(parents=True)
        (folder / "001-0001.wav").write_bytes(b"saved fixture")
        on_line(
            'STUDIO_EVENT {"stage":"synthesizing","message":"Narrating section 2","chapter":2,"passage":1,"characters":100}'
        )
        ready.set()
        assert release.wait(5)

    monkeypatch.setattr(studio, "default_runner", worker)
    with TestClient(studio.create_app(tmp_path / "store", setup=ReadySetup(), readiness=ready_environment)) as client:
        source = sample_epub(tmp_path / "book.epub")
        book = client.post("/api/books", files={"file": ("book.epub", source.read_bytes())}).json()
        try:
            job = client.post(
                "/api/jobs", json={"book_id": book["id"], "style": "Warm narrator", "mode": "full"}
            ).json()
            assert ready.wait(3)
            state = client.get(f"/api/jobs/{job['id']}").json()
            assert state["current_chapter"] == 2
            assert state["current_passage_index"] == 1
            assert state["passages_done"] == 1
        finally:
            release.set()


def test_timing_history_is_bounded_private_and_corrupt_data_is_ignored(tmp_path):
    folder = tmp_path / "version"
    folder.mkdir()
    studio.save_narration_timings(folder, [(100, float(i)) for i in range(1, 30)])
    path = folder / "narration-timings.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(studio.load_narration_timings(folder)) == 20
    assert studio.load_narration_timings(folder)[-1] == (100, 29.0)
    for corrupt in ("bad json", "{}", "[null]", "[[100, NaN]]", "[[100, -1]]", "[[-1, 10]]"):
        path.write_text(corrupt)
        assert studio.load_narration_timings(folder) == []
    path.write_text(" " * 16385)
    assert studio.load_narration_timings(folder) == []


def test_actual_conversion_events_reach_eta_and_saved_counter(tmp_path, monkeypatch):
    """Real conversion and audio encoding, with a synthetic tone instead of Breeze."""
    from audiobook import convert
    from test_audiobook import tone

    release, second = threading.Event(), threading.Event()
    observations = []

    # Keep the real producer's event bytes, replacing only its stdout sink.
    def worker(source, output, style, preview_chapter=None, *, on_process, on_line):
        import contextlib

        class Sink(io.StringIO):
            def write(self, text):
                if text.startswith("STUDIO_EVENT "):
                    on_line(text.strip())
                return len(text)

        def synth(path, text, direction, seed):
            if len(observations) == 1:
                second.set()
                assert release.wait(5)
            observations.append(text)
            tone(path, text, direction, seed)

        with contextlib.redirect_stdout(Sink()):
            return convert(source, output, style, synth=synth)

    monkeypatch.setattr(studio, "default_runner", worker)
    with TestClient(studio.create_app(tmp_path / "store", setup=ReadySetup(), readiness=ready_environment)) as client:
        source = sample_epub(tmp_path / "book.epub")
        book = client.post("/api/books", files={"file": ("book.epub", source.read_bytes())}).json()
        try:
            job = client.post(
                "/api/jobs", json={"book_id": book["id"], "style": "Warm, measured narrator", "mode": "full"}
            ).json()
            assert second.wait(4), client.get(f"/api/jobs/{job['id']}").json().get("error")
            state = client.get(f"/api/jobs/{job['id']}").json()
            assert state["passages_done"] == 1
            assert state["current_chapter"] == 2
            assert state["eta_samples"] == 1
            assert state["eta_narration_seconds"] > 0
            assert state["eta_basis"] == "current"
        finally:
            release.set()
        for _ in range(150):
            state = client.get(f"/api/jobs/{job['id']}").json()
            if state["state"] != "running":
                break
            time.sleep(0.02)
        assert state["state"] == "complete", state.get("error")
        assert state["passages_done"] == 2 and state["book_url"]
