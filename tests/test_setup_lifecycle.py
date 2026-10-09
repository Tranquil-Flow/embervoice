"""Adversarial setup lifecycle checks, with tiny synthetic files and no network."""

import hashlib
import io
import sys
import types
import pytest
from fastapi.testclient import TestClient
import model_setup as ms
import studio


class Child:
    def __init__(self, *args, **kwargs):
        self.returncode = None
        self.stderr = io.BytesIO(b"fixture download failed\n")
        self.pid = 9999999

    def poll(self):
        return self.returncode


@pytest.fixture
def tiny(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.delenv("AUDIOBOOK_MODEL_DIR", raising=False)
    monkeypatch.setenv("AUDIOBOOK_SETUP_DIR", str(tmp_path / "state"))
    content = b"pinned synthetic content"
    entry = {
        "path": "model.safetensors",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "blob_id": "fixture",
    }
    monkeypatch.setattr(ms, "manifest_files", lambda: [entry])
    monkeypatch.setattr(ms, "total_bytes", lambda: len(content))
    path = ms.resolve_model_path()
    path.mkdir(parents=True)
    (path / entry["path"]).write_bytes(content)
    obj = ms.ModelSetup(tmp_path / "state")
    monkeypatch.setattr(ms.subprocess, "Popen", Child)
    monkeypatch.setattr(ms, "free_bytes", lambda path: 100 * 2**30)
    return obj, path, entry


def test_sizes_and_acceptance_are_not_integrity_verification(tiny):
    obj, path, entry = tiny
    obj._write_receipt()  # test-only consent, never touches the user's state
    assert obj.status()["accepted"]
    assert not obj.status()["ready"], "Matching sizes must not admit an unverified model"
    obj._mark_verified()
    assert obj.status()["ready"]
    (path / entry["path"]).write_bytes(b"X" * entry["size"])
    assert not obj.status()["ready"], "Same-sized corruption must retire verification"


def test_other_revision_partials_do_not_inflate_progress(tiny):
    obj, path, entry = tiny
    (path / entry["path"]).unlink()
    blobs = ms._incomplete_root()
    blobs.mkdir(parents=True)
    (blobs / "unrelated.incomplete").write_bytes(b"Z" * entry["size"])
    assert ms.stored_bytes(path) == 0
    (blobs / (entry["sha256"] + ".incomplete")).write_bytes(b"pin")
    assert ms.stored_bytes(path) == 3


def test_active_setup_never_ready_and_cannot_spawn_twice(tiny):
    obj, path, entry = tiny
    first = obj.start(True, ms.licence_digest())
    assert first["state"] == "downloading" and not first["ready"]
    child = obj._child
    with pytest.raises(RuntimeError, match="already|progress|running"):
        obj.start(True, ms.licence_digest())
    assert obj._child is child
    child.returncode = 1
    obj.poll()


def test_different_stores_cannot_share_two_download_owners(tiny, tmp_path):
    obj, path, entry = tiny
    second = ms.ModelSetup(tmp_path / "second-state")
    obj.start(True, ms.licence_digest())
    try:
        with pytest.raises(RuntimeError, match="already|progress|running"):
            second.start(True, ms.licence_digest())
        assert second._child is None
    finally:
        obj._child.returncode = 1
        obj.poll()
        if second._child is not None:
            second._child.returncode = 1
            second.poll()
        second.close()


def test_download_entrypoint_requires_current_acceptance_before_network(tiny, monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=lambda **kw: calls.append(kw))
    )
    with pytest.raises(RuntimeError, match="accept|licence"):
        ms.main(["--download"])
    assert calls == []


def test_api_status_reaps_a_completed_child_and_preserves_hash_error(tiny, tmp_path):
    obj, path, entry = tiny
    obj.start(True, ms.licence_digest())
    obj._child.returncode = 1
    app = studio.create_app(tmp_path / "app", setup=obj, readiness=lambda: {"blockers": []})
    with TestClient(app) as client:
        status = client.get("/api/setup").json()
        assert obj._child is None, "Polling must reap the exited owned process"
        assert status["state"] == "error" and not status["ready"]
        assert "fixture download failed" in status["error"]
