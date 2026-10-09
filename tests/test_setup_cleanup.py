"""Actual process cleanup and damaged-cache repair, never a model download."""

import json
import signal
import subprocess
import sys
import time
import types
import model_setup as ms
from test_setup_lifecycle import tiny as tiny


def test_shutdown_reaps_an_owned_child_which_ignores_term(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    obj = ms.ModelSetup(tmp_path / "state")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)',
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert child.stdout.readline() == b"ready\n"
        obj._child = child
        obj._acquire_lock()
        started = time.monotonic()
        obj.close()
        assert child.poll() == -signal.SIGKILL
        assert time.monotonic() - started < 8
        assert obj._child is None and obj._lock_fd is None
        assert json.loads(obj.status_path.read_text())["state"] == "cancelled"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        child.stdout.close()


def test_same_size_damaged_files_are_explicitly_refetched(tiny, monkeypatch):
    obj, folder, entry = tiny
    path = folder / entry["path"]
    obj._write_receipt()  # explicit consent solely for the synthetic test snapshot
    path.write_bytes(b"X" * entry["size"])
    monkeypatch.setenv("AUDIOBOOK_SETUP_DIR", str(obj.state_dir))
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        path.write_bytes(b"pinned synthetic content")
        return str(path.parent)

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=download))
    assert ms._download() == 0
    assert calls[0]["force_download"] is True
    assert calls[0]["allow_patterns"] == [entry["path"]]
    assert calls[0]["revision"] == ms.MODEL_REVISION
    assert obj.status()["ready"] is True


def test_wrong_shaped_local_state_is_not_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    obj = ms.ModelSetup(tmp_path / "state")
    obj.receipt_path.write_text("[1,2,3]")
    obj.status_path.write_text("[]")
    assert obj.status()["accepted"] is False
    assert obj.status()["ready"] is False
