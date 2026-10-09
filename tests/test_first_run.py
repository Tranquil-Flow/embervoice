"""First-run API boundaries; no voice model, downloads or live user store."""

from pathlib import Path
from fastapi.testclient import TestClient
import studio
from test_audiobook import sample_epub


class Setup:
    def __init__(self, ready=False):
        self.ready = ready
        self.starts = []
        self.closed = False

    def status(self):
        return dict(
            state="ready" if self.ready else "missing",
            ready=self.ready,
            accepted=self.ready,
            completed_bytes=0,
            total_bytes=500,
            error=None,
            license_sha256="digest",
            repo="pinned",
            revision="revision",
        )

    def start(self, accepted, license_sha256):
        self.starts.append((accepted, license_sha256))
        if not accepted or license_sha256 != "digest":
            raise ValueError("Accept the current model licence first")
        return {**self.status(), "state": "downloading"}

    def cancel(self):
        return {**self.status(), "state": "cancelled"}

    def close(self):
        self.closed = True

    def require_ready(self):
        if not self.ready:
            raise RuntimeError("Set up the voice model first")
        return Path("/pinned/model")


def good():
    return {"supported": True, "blockers": [], "total_gib": 16, "ffmpeg": True, "ffprobe": True}


def test_readiness_platform_memory_tools(monkeypatch):
    monkeypatch.setattr(studio.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(studio.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(studio.platform, "mac_ver", lambda: ("26.5.1", (), "arm64"))
    monkeypatch.setattr(studio.shutil, "which", lambda tool: "/usr/bin/" + tool)
    monkeypatch.setattr(studio.subprocess, "check_output", lambda *a, **k: str(16 * 2**30))
    assert studio.startup_readiness()["blockers"] == []
    monkeypatch.setattr(studio.platform, "machine", lambda: "x86_64")
    assert "Apple Silicon" in " ".join(studio.startup_readiness()["blockers"])
    monkeypatch.setattr(studio.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(studio.subprocess, "check_output", lambda *a, **k: str(8 * 2**30))
    assert "RAM" in " ".join(studio.startup_readiness()["blockers"])
    monkeypatch.setattr(studio.shutil, "which", lambda _: None)
    assert "brew install ffmpeg" in " ".join(studio.startup_readiness()["blockers"])
    monkeypatch.setattr(studio.platform, "system", lambda: "Linux")
    assert not studio.startup_readiness()["supported"]


def test_setup_is_explicit_local_and_closes_owned_process(tmp_path):
    setup = Setup()
    with TestClient(studio.create_app(tmp_path / "store", setup=setup, readiness=good)) as client:
        status = client.get("/api/setup")
        assert status.status_code == 200
        assert not status.json()["ready"]
        assert setup.starts == []
        legal = client.get("/api/setup/license")
        assert legal.status_code == 200 and legal.json()["license_sha256"] == "digest"
        assert len(legal.json()["text"]) > 10000
        assert client.get("/setup.js").status_code == 200
        csp = status.headers["content-security-policy"]
        assert "connect-src 'self'" in csp and "script-src 'self'" in csp
        assert client.post("/api/setup/start", json={"accepted": False, "license_sha256": "digest"}).status_code == 400
        assert client.post("/api/setup/start", json={"accepted": True, "license_sha256": "stale"}).status_code == 400
        assert (
            client.post(
                "/api/setup/start",
                json={"accepted": True, "license_sha256": "digest"},
                headers={"origin": "https://outside.example"},
            ).status_code
            == 403
        )
        response = client.post("/api/setup/start", json={"accepted": True, "license_sha256": "digest"})
        assert response.status_code == 200 and response.json()["state"] == "downloading"
        assert client.post("/api/setup/cancel").json()["state"] == "cancelled"
    assert setup.closed


def test_model_gate_cannot_be_bypassed_with_direct_job_post(tmp_path):
    setup = Setup()
    calls = []
    with TestClient(
        studio.create_app(tmp_path / "store", runner=lambda *a, **k: calls.append(a), setup=setup, readiness=good)
    ) as client:
        source = sample_epub(tmp_path / "read.epub")
        book = client.post("/api/books", files={"file": ("read.epub", source.read_bytes())}).json()
        payload = dict(book_id=book["id"], style="Warm narrator", mode="preview", chapter=1)
        response = client.post("/api/jobs", json=payload)
        assert response.status_code == 409 and "voice model" in response.json()["detail"]
        assert not calls
        setup.ready = True
        assert client.post("/api/jobs", json=payload).status_code == 200


def test_missing_ffmpeg_is_friendly_and_blocks_worker_and_reference(tmp_path):
    bad = lambda: {**good(), "blockers": ["Install ffmpeg and ffprobe: brew install ffmpeg"]}
    with TestClient(
        studio.create_app(tmp_path / "store", runner=lambda *a, **k: None, setup=Setup(True), readiness=bad)
    ) as client:
        assert not client.get("/api/setup").json()["ready"]
        source = sample_epub(tmp_path / "read.epub")
        book = client.post("/api/books", files={"file": ("read.epub", source.read_bytes())}).json()
        assert (
            client.post("/api/jobs", json=dict(book_id=book["id"], style="Warm narrator", mode="full")).status_code
            == 409
        )
        result = client.post(f"/api/books/{book['id']}/references", files={"file": ("clip.wav", b"not audio")})
        assert result.status_code == 409 and "brew install ffmpeg" in result.json()["detail"]
        assert client.post("/api/setup/start", json={"accepted": True, "license_sha256": "digest"}).status_code == 409
