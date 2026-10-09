"""Focused tests for model_setup: licence gate, pinned manifest, resumable setup.

No network and no real weight transfer: the download child process is mocked.
The only real subprocess exercised is the module's own ``--download`` refusal
path (no huggingface_hub call), and flock behaviour across real processes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import model_setup as ms

ROOT = Path(ms.__file__).resolve().parent


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.delenv("AUDIOBOOK_MODEL_DIR", raising=False)
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    return tmp_path


@pytest.fixture
def state_dir(_isolated):
    return _isolated / "state"


def fake_snapshot(hub: Path, complete: bool = True, truncate: str | None = None) -> Path:
    """Create the pinned layout with real bytes (small files stay small)."""
    model_dir = hub / ("models--" + ms.MODEL_REPO.replace("/", "--")) / "snapshots" / ms.MODEL_REVISION
    model_dir.mkdir(parents=True, exist_ok=True)
    for entry in ms.manifest_files():
        path = model_dir / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        size = entry["size"]
        if size > 4096:  # keep tests small: sparse placeholder of exact size
            with path.open("wb") as fh:
                fh.truncate(size)
        else:
            path.write_bytes(b"x" * size)
        if truncate == entry["path"]:
            with path.open("r+b") as fh:
                fh.truncate(size // 2)
    if not complete:
        (model_dir / "config.json").unlink()
    return model_dir


# --------------------------------------------------------------------------
# pins, manifest, licence
# --------------------------------------------------------------------------
def test_pins_are_exact():
    assert ms.MODEL_REPO == "mlx-community/Breeze-TTS-2-mlx-8bit"
    assert ms.MODEL_REVISION == "c6e4a2ff6ab9afba68b7853de802273ffe23fb49"


def test_manifest_matches_upstream_sizes():
    manifest = ms.load_manifest()
    assert manifest["revision"] == ms.MODEL_REVISION
    assert manifest["gated"] is False, "repo is ungated; a token must not be required"
    assert manifest["total_bytes"] == sum(f["size"] for f in manifest["files"])
    by_path = {f["path"]: f for f in manifest["files"]}
    assert by_path["model.safetensors"]["size"] == 3_885_721_536
    assert by_path["audio_tokenizer/model.safetensors"]["size"] == 682_293_092
    assert by_path["tokenizer.json"]["size"] == 33_386_945
    assert by_path["model.safetensors"]["sha256"]
    # no local absolute paths leaked into a public manifest
    assert not any(str(f).startswith("/") for f in by_path)


def test_allow_patterns_is_the_pinned_list():
    assert ms.allow_patterns() == [f["path"] for f in ms.manifest_files()]
    assert "LICENSE" in ms.allow_patterns()


def test_licence_is_bundled_upstream_text():
    text = ms.licence_text()
    assert "BreezeBlue" in text
    assert len(text) > 1000
    assert ms.licence_digest() == ms.hashlib.sha256(text.encode()).hexdigest()


def test_licence_files_exist_and_notice_attribution():
    assert (ROOT / "legal" / "BREEZE_LICENSE.txt").is_file()
    notice = (ROOT / "legal" / "BREEZE_NOTICE.txt").read_text()
    assert "BreezeBlue Research and Non-Commercial License Agreement" in notice
    assert "Copyright (c) 2026 RESONIA, INC." in notice


# --------------------------------------------------------------------------
# path resolution
# --------------------------------------------------------------------------
def test_resolve_defaults_to_hub_cache(_isolated):
    expected = _isolated / "hub" / ("models--" + ms.MODEL_REPO.replace("/", "--")) / "snapshots" / ms.MODEL_REVISION
    assert ms.resolve_model_path() == expected


def test_resolve_honours_hf_home(_isolated, monkeypatch):
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(_isolated / "hfhome"))
    assert ms.resolve_model_path().is_relative_to(_isolated / "hfhome" / "hub")


def test_resolve_honours_explicit_override(_isolated, monkeypatch):
    override = _isolated / "custom" / ("models--" + ms.MODEL_REPO.replace("/", "--")) / "snapshots" / ms.MODEL_REVISION
    override.mkdir(parents=True)
    monkeypatch.setenv("AUDIOBOOK_MODEL_DIR", str(override))
    assert ms.resolve_model_path() == override
    assert ms.hub_root() == _isolated / "custom"


def test_wrong_override_layout_is_refused(_isolated, monkeypatch):
    monkeypatch.setenv("AUDIOBOOK_MODEL_DIR", str(_isolated / "somewhere-else"))
    with pytest.raises(RuntimeError, match="not a pinned model layout"):
        ms.resolve_model_path()


# --------------------------------------------------------------------------
# consent gate
# --------------------------------------------------------------------------
def test_missing_model_needs_acceptance(state_dir):
    status = ms.ModelSetup(state_dir).status()
    assert status["state"] == "needs_acceptance"
    assert status["accepted"] is False
    assert status["ready"] is False
    assert status["error"] is None
    assert status["repo"] == ms.MODEL_REPO
    assert status["revision"] == ms.MODEL_REVISION
    assert status["total_bytes"] == ms.total_bytes()
    assert status["completed_bytes"] == 0


def test_status_fields_always_present(state_dir):
    status = ms.ModelSetup(state_dir).status()
    for field in ("state", "accepted", "ready", "completed_bytes", "total_bytes", "error", "license_sha256"):
        assert field in status
    assert status["state"] in ms.STATES


def test_start_requires_explicit_acceptance(state_dir):
    setup = ms.ModelSetup(state_dir)
    with pytest.raises(RuntimeError, match="licence must be accepted"):
        setup.start(False, ms.licence_digest())
    assert not (state_dir / ms.RECEIPT_NAME).exists()
    assert setup.status()["accepted"] is False


def test_start_rejects_stale_licence_digest(state_dir):
    setup = ms.ModelSetup(state_dir)
    with pytest.raises(RuntimeError, match="licence text has changed"):
        setup.start(True, "0" * 64)
    assert not (state_dir / ms.RECEIPT_NAME).exists()


def test_existing_complete_model_is_never_autoaccepted(state_dir):
    fake_snapshot(Path(os.environ["HF_HUB_CACHE"]))
    setup = ms.ModelSetup(state_dir)
    status = setup.status()
    assert status["state"] == "needs_acceptance", "consent must come first"
    assert status["accepted"] is False
    with pytest.raises(RuntimeError, match="licence"):
        setup.require_ready()


def test_receipt_is_private_and_durable(state_dir):
    setup = ms.ModelSetup(state_dir)
    with pytest.raises(RuntimeError):
        setup.start(False, "")
    setup._write_receipt()
    receipt = state_dir / ms.RECEIPT_NAME
    assert oct(receipt.stat().st_mode)[-3:] == "600"
    payload = json.loads(receipt.read_text())
    assert payload["revision"] == ms.MODEL_REVISION
    assert payload["license_sha256"] == ms.licence_digest()
    assert setup.status()["accepted"] is True


def test_stale_receipt_is_not_accepted(state_dir):
    setup = ms.ModelSetup(state_dir)
    setup._write_receipt()
    data = json.loads((state_dir / ms.RECEIPT_NAME).read_text())
    data["revision"] = "deadbeef"
    (state_dir / ms.RECEIPT_NAME).write_text(json.dumps(data))
    assert ms.ModelSetup(state_dir).status()["accepted"] is False

    data = json.loads((state_dir / ms.RECEIPT_NAME).read_text())
    data["revision"] = ms.MODEL_REVISION
    data["license_sha256"] = "0" * 64
    (state_dir / ms.RECEIPT_NAME).write_text(json.dumps(data))
    assert ms.ModelSetup(state_dir).status()["accepted"] is False


def test_require_ready_message_actionable(state_dir):
    with pytest.raises(RuntimeError, match="accept the BreezeBlue licence"):
        ms.ModelSetup(state_dir).require_ready()


# --------------------------------------------------------------------------
# completeness
# --------------------------------------------------------------------------
def test_absent_model_is_incomplete(state_dir):
    ok, reason = ms.check_complete(ms.resolve_model_path())
    assert ok is False and reason


def test_present_model_still_needs_hash_verification_after_consent(state_dir):
    fake_snapshot(Path(os.environ["HF_HUB_CACHE"]))
    setup = ms.ModelSetup(state_dir)
    setup._write_receipt()
    status = setup.status()
    assert status["complete_by_size"] is True
    assert status["ready"] is False
    assert status["completed_bytes"] == ms.total_bytes()
    with pytest.raises(RuntimeError, match="incomplete"):
        setup.require_ready()


def test_truncated_file_is_not_ready(state_dir):
    fake_snapshot(Path(os.environ["HF_HUB_CACHE"]), truncate="model.safetensors")
    setup = ms.ModelSetup(state_dir)
    setup._write_receipt()
    status = setup.status()
    assert status["ready"] is False
    assert status["completed_bytes"] < ms.total_bytes()
    with pytest.raises(RuntimeError, match="incomplete"):
        setup.require_ready()


def test_missing_file_is_detected(state_dir):
    fake_snapshot(Path(os.environ["HF_HUB_CACHE"]), complete=False)
    setup = ms.ModelSetup(state_dir)
    setup._write_receipt()
    assert setup.status()["ready"] is False


def test_index_referenced_missing_shard_detected(state_dir):
    model_dir = fake_snapshot(Path(os.environ["HF_HUB_CACHE"]))
    index_path = model_dir / "model.safetensors.index.json"
    entry = next(e for e in ms.manifest_files() if e["path"] == index_path.name)
    index_path.write_bytes(
        json.dumps({"weight_map": {"a": "model-00001-of-00002.safetensors", "b": index_path.name}})
        .encode()
        .ljust(entry["size"], b" ")
    )
    assert index_path.stat().st_size == entry["size"]
    ok, reason = ms.check_complete(model_dir)
    assert ok is False
    assert "model-00001-of-00002.safetensors" in reason


def test_deep_verify_catches_wrong_content(state_dir):
    model_dir = fake_snapshot(Path(os.environ["HF_HUB_CACHE"]))
    entry = next(e for e in ms.manifest_files() if e["path"] == "tokenizer.json")
    path = model_dir / entry["path"]
    path.write_bytes(b"not the tokenizer" + b"\0" * (entry["size"] - len(b"not the tokenizer")))
    ok_size_only, _ = ms.check_complete(model_dir, deep=False)
    ok_deep, reason = ms.check_complete(model_dir, deep=True)
    assert ok_size_only is True
    assert ok_deep is False
    assert "mismatch" in reason
    # a size-correct but wrong file is caught by hash, never by directory name
    assert "git blob sha1" in reason or "sha256" in reason


def test_stored_bytes_counts_matching_allocated_partials(state_dir):
    hub = Path(os.environ["HF_HUB_CACHE"])
    blobs = hub / ("models--" + ms.MODEL_REPO.replace("/", "--")) / "blobs"
    blobs.mkdir(parents=True)
    entry = next(e for e in ms.manifest_files() if e["path"] == "model.safetensors")
    partial = blobs / (entry["sha256"] + ".incomplete")
    partial.write_bytes(b"x" * 12345)
    assert ms.stored_bytes(ms.resolve_model_path()) == 12345
    # Preallocated sparse holes are not transferred bytes.
    with partial.open("r+b") as fh:
        fh.truncate(123_456_789)
    assert ms.stored_bytes(ms.resolve_model_path()) < 123_456_789


def test_stored_bytes_clamped_to_entry_and_total(state_dir, monkeypatch):
    blobs = ms._incomplete_root()
    blobs.mkdir(parents=True)
    monkeypatch.setattr(ms, "manifest_files", lambda: [{"path": "fixture", "size": 3, "sha256": "pinned"}])
    monkeypatch.setattr(ms, "total_bytes", lambda: 3)
    (blobs / "pinned.incomplete").write_bytes(b"overlarge")
    (blobs / "unrelated.incomplete").write_bytes(b"also unrelated")
    assert ms.stored_bytes(ms.resolve_model_path()) == 3


# --------------------------------------------------------------------------
# no network on read paths
# --------------------------------------------------------------------------
def test_read_paths_do_not_spawn_processes_or_import_hub(state_dir, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        assert name != "huggingface_hub", "huggingface_hub must not be imported outside the download child"
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    setup = ms.ModelSetup(state_dir)
    setup.status()
    setup.poll()
    ms.resolve_model_path()
    with pytest.raises(RuntimeError):
        setup.require_ready()
    setup.close()


def _boom(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("a subprocess was spawned on a read path")


def test_acceptance_failure_spawns_no_subprocess(state_dir, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(ms.ModelSetup, "_acquire_lock", lambda self: None)
    setup = ms.ModelSetup(state_dir)
    with pytest.raises(RuntimeError):
        setup.start(False, ms.licence_digest())


# --------------------------------------------------------------------------
# download lifecycle (child mocked)
# --------------------------------------------------------------------------
@pytest.fixture
def fake_child(monkeypatch):
    calls = []

    class Child:
        def __init__(self, exit_code=0, stderr=b""):
            self.returncode = None
            self.stderr = None
            self._code = exit_code
            self.pid = 4242

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = self._code
            return self._code

        def terminate(self):
            calls.append("terminate")
            self.returncode = -15

        def kill(self):
            calls.append("kill")
            self.returncode = -9

    def factory(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        child = Child()
        child.factory_kwargs = kwargs
        child.args = args
        return child

    monkeypatch.setattr(subprocess, "Popen", factory)
    return calls


def test_start_launches_owned_child_with_scoped_env(state_dir, fake_child, monkeypatch):
    monkeypatch.setattr(ms, "free_bytes", lambda p: 99 * 2**30)
    setup = ms.ModelSetup(state_dir)
    status = setup.start(True, ms.licence_digest())
    assert status["state"] == "downloading"
    assert setup.status()["accepted"] is True
    argv, kwargs = fake_child[0]["args"][0], fake_child[0]["kwargs"]
    assert argv == [sys.executable, "-m", "model_setup", "--download"]
    env = kwargs["env"]
    assert env["HF_HUB_OFFLINE"] == "0" and env["TRANSFORMERS_OFFLINE"] == "0"
    assert env["HF_HUB_DISABLE_XET"] == "1" and env["DO_NOT_TRACK"] == "1"
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert os.environ.get("HF_HUB_OFFLINE") == "1", "parent env must not be mutated"


def test_start_refuses_when_disk_space_short(state_dir, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(ms, "free_bytes", lambda p: 1024)
    setup = ms.ModelSetup(state_dir)
    status = setup.start(True, ms.licence_digest())
    assert status["state"] == "error"
    assert "free disk space" in status["error"]
    assert setup._lock_fd is None, "lock released on refusal"


def test_disk_check_accounts_headroom_and_missing_bytes(state_dir, monkeypatch):
    """Needed free space == missing bytes + 1 GiB, checked before spawning."""
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(ms, "stored_bytes", lambda p: 0)
    monkeypatch.setattr(ms, "check_complete", lambda p, deep=False: (False, "missing"))
    monkeypatch.setattr(ms, "free_bytes", lambda p: (ms.total_bytes() + ms.DISK_HEADROOM_BYTES) - 1)
    setup = ms.ModelSetup(state_dir)
    status = setup.start(True, ms.licence_digest())
    assert status["state"] == "error", "one byte short of model + headroom must refuse"

    monkeypatch.setattr(ms, "free_bytes", lambda p: ms.total_bytes() + ms.DISK_HEADROOM_BYTES)
    setup2 = ms.ModelSetup(state_dir)
    with pytest.raises(AssertionError):
        setup2.start(True, ms.licence_digest())  # would spawn -> _boom


def test_existing_complete_cache_skips_the_disk_gate(state_dir, fake_child, monkeypatch):
    monkeypatch.setattr(ms, "free_bytes", lambda p: 0)  # refuse-if-needed would trigger
    fake_snapshot(Path(os.environ["HF_HUB_CACHE"]))
    setup = ms.ModelSetup(state_dir)
    status = setup.start(True, ms.licence_digest())
    assert status["state"] == "downloading", "nothing to fetch -> no disk requirement"
    assert len(fake_child) == 1


def test_failed_child_exit_records_error(state_dir, fake_child, monkeypatch):
    monkeypatch.setattr(ms, "free_bytes", lambda p: 99 * 2**30)
    setup = ms.ModelSetup(state_dir)
    setup.start(True, ms.licence_digest())
    child = setup._child
    child.returncode = 1

    class Err:
        def read(self):
            return b"ConnectionError: boom"

        def close(self):
            pass

    child.stderr = Err()
    status = setup.poll()
    assert status["state"] == "error"
    assert "ConnectionError" in status["error"]
    assert setup._lock_fd is None


def test_zero_exit_without_files_is_error_not_ready(state_dir, fake_child, monkeypatch):
    monkeypatch.setattr(ms, "free_bytes", lambda p: 99 * 2**30)
    setup = ms.ModelSetup(state_dir)
    setup.start(True, ms.licence_digest())
    setup._child.returncode = 0
    status = setup.poll()
    assert status["state"] == "error"
    assert "verified pinned files" in status["error"]


def test_successful_child_marks_ready_after_deep_verify(state_dir, fake_child, monkeypatch):
    import hashlib

    content = b"synthetic verified fixture"
    entry = {"path": "fixture.bin", "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    monkeypatch.setattr(ms, "manifest_files", lambda: [entry])
    monkeypatch.setattr(ms, "total_bytes", lambda: len(content))
    monkeypatch.setattr(ms, "free_bytes", lambda p: 99 * 2**30)
    setup = ms.ModelSetup(state_dir)
    setup.start(True, ms.licence_digest())
    (setup.model_dir() / entry["path"]).write_bytes(content)
    assert not setup.status()["ready"]
    setup._mark_verified()  # explicit simulated child completion, actual tiny hashes
    assert not setup.status()["ready"], "live child must still be reaped"
    setup._child.returncode = 0
    assert setup.poll()["ready"] is True
    assert ms.ModelSetup(state_dir).require_ready() == setup.model_dir()


def test_cancel_terminates_child_and_keeps_partials(state_dir, fake_child, monkeypatch):
    monkeypatch.setattr(ms, "free_bytes", lambda p: 99 * 2**30)
    hub = Path(os.environ["HF_HUB_CACHE"])
    blobs = hub / ("models--" + ms.MODEL_REPO.replace("/", "--")) / "blobs"
    blobs.mkdir(parents=True)
    partial = blobs / "deadbeef.incomplete"
    with partial.open("wb") as fh:
        fh.truncate(4242)

    setup = ms.ModelSetup(state_dir)
    setup.start(True, ms.licence_digest())
    status = setup.cancel()
    assert status["state"] == "cancelled"
    assert "terminate" in fake_child, "owned child must be terminated and reaped"
    assert partial.exists(), "HF partials preserved for resume"
    assert setup._lock_fd is None


def test_resume_starts_the_same_pinned_call(state_dir, fake_child, monkeypatch):
    monkeypatch.setattr(ms, "free_bytes", lambda p: 99 * 2**30)
    setup = ms.ModelSetup(state_dir)
    setup.start(True, ms.licence_digest())
    setup._child.returncode = -15
    setup._cancelling = True
    setup.poll()
    setup.start(True, ms.licence_digest())
    assert len(fake_child) == 2
    assert fake_child[0]["args"][0][1:] == fake_child[1]["args"][0][1:]
    assert fake_child[1]["args"][0][1:] == ["-m", "model_setup", "--download"]


def test_close_is_safe_without_child(state_dir):
    setup = ms.ModelSetup(state_dir)
    setup.close()
    setup.close()


def test_require_ready_while_downloading(state_dir, fake_child, monkeypatch):
    monkeypatch.setattr(ms, "free_bytes", lambda p: 99 * 2**30)
    setup = ms.ModelSetup(state_dir)
    setup.start(True, ms.licence_digest())
    with pytest.raises(RuntimeError, match="still downloading"):
        setup.require_ready()
    setup.close()


# --------------------------------------------------------------------------
# cross-process lock
# --------------------------------------------------------------------------
def test_flock_prevents_duplicate_setups(state_dir, monkeypatch):
    monkeypatch.setattr(ms, "free_bytes", lambda p: 99 * 2**30)
    setup = ms.ModelSetup(state_dir)
    setup.start(True, ms.licence_digest())
    script = textwrap.dedent(
        f"""
        import sys, pathlib
        sys.path.insert(0, {str(ROOT)!r})
        import model_setup as ms
        s = ms.ModelSetup(pathlib.Path({str(state_dir)!r}))
        try:
            s._acquire_lock()
        except RuntimeError as exc:
            print("LOCKED"); sys.exit(0)
        print("ACQUIRED"); sys.exit(1)
        """
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert "LOCKED" in proc.stdout, proc.stdout + proc.stderr
    setup.close()


def test_lock_released_after_close_allows_next_process(state_dir, monkeypatch):
    monkeypatch.setattr(ms, "free_bytes", lambda p: 99 * 2**30)
    setup = ms.ModelSetup(state_dir)
    setup.start(True, ms.licence_digest())
    setup.close()
    other = ms.ModelSetup(state_dir)
    other._acquire_lock()
    other._release_lock()


# --------------------------------------------------------------------------
# child entry point
# --------------------------------------------------------------------------
def test_download_child_calls_snapshot_download_pinned(tmp_path, monkeypatch):
    """Actual child entry point, simulated Hub and tiny hash-correct output."""
    out = tmp_path / "captured.json"
    script = textwrap.dedent(f"""
        import json, sys, types, pathlib, hashlib, os
        sys.path.insert(0, {str(ROOT)!r})
        import model_setup as ms
        content=b"synthetic test data"
        entry={{"path":"fixture.bin","size":len(content),"sha256":hashlib.sha256(content).hexdigest()}}
        ms.manifest_files=lambda:[entry]
        ms.total_bytes=lambda:len(content)
        captured={{}}
        def fake_snapshot_download(**kwargs):
            captured.update(kwargs)
            path=ms.resolve_model_path();path.mkdir(parents=True,exist_ok=True)
            (path/"fixture.bin").write_bytes(content)
            return str(path)
        stub=types.ModuleType("huggingface_hub")
        stub.snapshot_download=fake_snapshot_download
        sys.modules["huggingface_hub"]=stub
        setup=ms.ModelSetup(pathlib.Path(os.environ["AUDIOBOOK_SETUP_DIR"]))
        setup._write_receipt() # consent only for this synthetic test fixture
        code=ms.main(["--download"])
        json.dump({{"code":code,"captured":captured,"ready":setup.status()["ready"]}},open({str(out)!r},"w"))
    """)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["HF_HUB_CACHE"] = str(tmp_path / "hub")
    env["AUDIOBOOK_SETUP_DIR"] = str(tmp_path / "state")
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=20)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text())
    assert data["code"] == 0 and data["ready"] is True
    assert data["captured"]["repo_id"] == ms.MODEL_REPO
    assert data["captured"]["revision"] == ms.MODEL_REVISION
    assert data["captured"]["token"] is False
    assert data["captured"]["max_workers"] == 2
    assert data["captured"]["allow_patterns"] == ["fixture.bin"]
    assert data["captured"]["cache_dir"].endswith("hub")
