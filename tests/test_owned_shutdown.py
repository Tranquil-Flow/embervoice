"""Shutdown only the narration processes owned by this app, including stalled children."""

import os
import signal
import subprocess
import sys
import threading
from fastapi.testclient import TestClient
import studio
from studio import create_app
from setup_fakes import ReadySetup, ready_environment
from test_audiobook import sample_epub


def test_lifespan_reaps_owned_worker_that_ignores_sigterm(tmp_path, monkeypatch):
    children = []
    started = threading.Event()

    def runner(source, output, style, preview_chapter=None, *, on_process=None, **kwargs):
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)',
            ],
            start_new_session=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        children.append(proc)
        assert proc.stdout.readline().strip() == "ready"
        on_process(proc)
        started.set()
        proc.wait(timeout=15)
        return {}

    monkeypatch.setattr(studio, "default_runner", runner)
    app = create_app(tmp_path / "store", setup=ReadySetup(), readiness=ready_environment)
    try:
        with TestClient(app) as client:
            source = sample_epub(tmp_path / "book.epub")
            book = client.post("/api/books", files={"file": ("book.epub", source.read_bytes())}).json()
            response = client.post(
                "/api/jobs",
                json={"book_id": book["id"], "style": "Warm, measured narrator", "mode": "preview", "chapter": 1},
            )
            assert response.status_code == 200, response.text
            assert started.wait(5)
        assert children[0].poll() is not None, "owned SIGTERM-resistant worker survived app shutdown"
    finally:
        for proc in children:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)
            proc.stdout.close()
