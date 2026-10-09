"""Consent-gated, resumable setup of the pinned Breeze TTS 2 speech model.

Embervoice downloads exactly one upstream Hugging Face revision, and only
after the user has accepted the bundled BreezeBlue licence. Nothing here talks
to the network except the owned child process started by ``ModelSetup.start``,
which runs this module as ``__main__`` and calls ``huggingface_hub``'s
``snapshot_download`` for the pinned revision.

Design rules (deliberate, do not relax):

* No network on import, ``status()``, ``resolve_model_path()`` or
  ``require_ready()``. Those are pure filesystem reads.
* Never auto-accept. A receipt is written only by ``start(accepted=True, ...)``.
* Progress is *bytes stored on disk*, never a percentage or an ETA.
* No background threads mutate global Hugging Face environment variables. The
  offline-disabling variables are set in the child process' environment only.
* Completeness is checked against the pinned manifest (sizes plus, where
  available, LFS sha256 / Git blob sha1) -- never by trusting a directory name.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from functools import wraps
from pathlib import Path
from typing import Any

MODEL_REPO = "mlx-community/Breeze-TTS-2-mlx-8bit"
MODEL_REVISION = "c6e4a2ff6ab9afba68b7853de802273ffe23fb49"

from runtime import app_root, worker_command

MANIFEST_PATH = app_root() / "model-files.json"
LICENSE_PATH = app_root() / "legal" / "BREEZE_LICENSE.txt"

#: Extra free space required on top of the missing bytes before we start.
DISK_HEADROOM_BYTES = 1 << 30  # 1 GiB

#: Receipt files inside the state directory.
RECEIPT_NAME = "model-acceptance.json"
LOCK_NAME = "model-setup.lock"
STATUS_NAME = "model-setup-status.json"

STATES = (
    "missing",
    "needs_acceptance",
    "ready",
    "downloading",
    "cancelling",
    "cancelled",
    "error",
)


# --------------------------------------------------------------------------
# manifest / licence
# --------------------------------------------------------------------------
def load_manifest() -> dict[str, Any]:
    """Return the pinned manifest shipped with the app."""
    return json.loads(MANIFEST_PATH.read_text())


def manifest_files() -> list[dict[str, Any]]:
    return load_manifest()["files"]


def total_bytes() -> int:
    return int(load_manifest()["total_bytes"])


def licence_text() -> str:
    """Exact bundled upstream licence text (never fetched, never edited)."""
    return LICENSE_PATH.read_text()


def licence_digest() -> str:
    return hashlib.sha256(licence_text().encode()).hexdigest()


def allow_patterns() -> list[str]:
    """Pinned file list, handed to snapshot_download as allow_patterns."""
    return [entry["path"] for entry in manifest_files()]


# --------------------------------------------------------------------------
# path resolution (filesystem only)
# --------------------------------------------------------------------------
def resolve_model_path() -> Path:
    """Directory that holds the pinned model snapshot.

    Order: ``AUDIOBOOK_MODEL_DIR`` override, ``HF_HUB_CACHE``/``HF_HOME``, then
    the default Hugging Face hub cache. Never touches the network.
    """
    override = os.environ.get("AUDIOBOOK_MODEL_DIR")
    if override:
        return _snapshot_dir(hub_root())
    hub_cache = os.environ.get("HF_HUB_CACHE")
    if not hub_cache:
        hf_home = os.environ.get("HF_HOME")
        hub_cache = str(Path(hf_home).expanduser() / "hub") if hf_home else None
    if hub_cache:
        return _snapshot_dir(Path(hub_cache).expanduser())
    return _snapshot_dir(Path.home() / ".cache" / "huggingface" / "hub")


def hub_root() -> Path:
    """The hub cache directory that holds ``models--<org>--<name>``."""
    override = os.environ.get("AUDIOBOOK_MODEL_DIR")
    if override:
        # An explicit override must be a valid pinned layout:
        #   <root>/models--<org>--<name>/snapshots/<revision>
        snapshot = Path(override).expanduser()
        root = snapshot.parent.parent.parent
        expected = _snapshot_dir(root)
        if snapshot != expected:
            raise RuntimeError(
                f"AUDIOBOOK_MODEL_DIR={snapshot} is not a pinned model layout. "
                f"Expected {expected} (i.e. <hub>/models--{MODEL_REPO.replace('/', '--')}"
                f"/snapshots/{MODEL_REVISION}), so downloads and this app cannot disagree "
                "about where the model lives."
            )
        return root
    hub_cache = os.environ.get("HF_HUB_CACHE")
    if not hub_cache:
        hf_home = os.environ.get("HF_HOME")
        hub_cache = str(Path(hf_home).expanduser() / "hub") if hf_home else None
    if hub_cache:
        return Path(hub_cache).expanduser()
    return Path.home() / ".cache" / "huggingface" / "hub"


def _hub_root() -> Path:
    return hub_root()


def _snapshot_dir(hub: Path) -> Path:
    folder = "models--" + MODEL_REPO.replace("/", "--")
    return hub / folder / "snapshots" / MODEL_REVISION


def _incomplete_root() -> Path:
    return _hub_root() / ("models--" + MODEL_REPO.replace("/", "--")) / "blobs"


def _index_missing_shards(model_dir: Path) -> list[str]:
    """Shard paths referenced by model.safetensors.index.json but absent on disk."""
    index = model_dir / "model.safetensors.index.json"
    if not index.is_file():
        return []
    try:
        data = json.loads(index.read_text())
    except (ValueError, OSError):
        # An unreadable index is caught by the size/hash check on the entry itself.
        return []
    if not isinstance(data, dict):
        return []
    weight_map = data.get("weight_map") or {}
    missing = []
    for shard in sorted(set(weight_map.values())):
        if not (model_dir / shard).is_file():
            missing.append(shard)
    return missing


# --------------------------------------------------------------------------
# completeness + progress
# --------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_entry(entry: dict[str, Any], model_dir: Path, deep: bool) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for one manifest entry."""
    path = model_dir / entry["path"]
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, f"missing {entry['path']} ({exc.strerror or exc})"
    if size != entry["size"]:
        return False, f"{entry['path']} is {size} bytes, expected {entry['size']}"
    if not deep:
        return True, ""
    if entry.get("sha256"):
        actual = _sha256(path)
        if actual != entry["sha256"]:
            return False, f"{entry['path']} sha256 mismatch"
        return True, ""
    blob = entry.get("blob_id")
    if blob:
        data = path.read_bytes()
        if hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != blob:
            return False, f"{entry['path']} git blob sha1 mismatch"
    return True, ""


def stored_bytes(model_dir: Path) -> int:
    """Bytes of pinned content actually present, including in-flight partials.

    Counts completed snapshot files by their real size, plus only partials
    named by this revision's hashes. Sparse/preallocated holes do not count.
    Stored bytes are not a claim that verification or setup has completed.
    """
    total = 0
    for entry in manifest_files():
        path = model_dir / entry["path"]
        try:
            total += min(path.stat().st_size, entry["size"])
            if path.stat().st_size == entry["size"]:
                continue
        except OSError:
            pass
        blob = _incomplete_root() / ((entry.get("sha256") or entry["blob_id"]) + ".incomplete")
        try:
            stat = blob.stat()
            # A sparse/preallocated partial has not transferred all logical bytes.
            allocated = getattr(stat, "st_blocks", (stat.st_size + 511) // 512) * 512
            total += min(stat.st_size, allocated, entry["size"])
        except OSError:
            pass
    return min(total, total_bytes())


def check_complete(model_dir: Path, deep: bool = False) -> tuple[bool, str]:
    """True only if every pinned file is present with the right size (and hash)."""
    model_dir = Path(model_dir)
    if not model_dir.is_dir():
        return False, "model directory does not exist"
    for entry in manifest_files():
        ok, reason = verify_entry(entry, model_dir, deep)
        if not ok:
            return False, reason
    missing = _index_missing_shards(model_dir)
    if missing:
        return False, "missing shards referenced by index: " + ", ".join(missing)
    return True, ""


# --------------------------------------------------------------------------
# disk space
# --------------------------------------------------------------------------
def free_bytes(path: Path) -> int:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


# --------------------------------------------------------------------------
# ModelSetup
# --------------------------------------------------------------------------
def serialized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)

    return guarded


class ModelSetup:
    """Consent gate + resumable download coordinator for one pinned revision."""

    def __init__(self, state_dir: Path):
        self._mutex = threading.RLock()
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._child: subprocess.Popen[bytes] | None = None
        self._lock_fd: int | None = None
        self._cancelling = False

    # -- paths -----------------------------------------------------------
    @property
    def receipt_path(self) -> Path:
        return self.state_dir / RECEIPT_NAME

    @property
    def lock_path(self) -> Path:
        # All app stores sharing this pinned cache share one download owner.
        folder = hub_root() / ".locks"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / ("audiobook-studio-" + MODEL_REVISION + ".lock")

    @property
    def verification_path(self) -> Path:
        return self.state_dir / "model-verification.json"

    def _fingerprint(self) -> dict[str, Any]:
        files = {}
        for entry in manifest_files():
            path = self.model_dir() / entry["path"]
            stat = path.stat()
            files[entry["path"]] = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino]
        manifest_hash = hashlib.sha256(json.dumps(manifest_files(), sort_keys=True).encode()).hexdigest()
        return {
            "model_dir": str(self.model_dir().resolve()),
            "revision": MODEL_REVISION,
            "manifest_sha256": manifest_hash,
            "files": files,
        }

    def _verified_current(self) -> bool:
        try:
            return json.loads(self.verification_path.read_text()) == self._fingerprint()
        except (OSError, ValueError):
            return False

    def _mark_verified(self) -> None:
        before = self._fingerprint()
        ok, reason = check_complete(self.model_dir(), deep=True)
        if not ok:
            raise RuntimeError("Pinned model verification failed: " + reason)
        if self._fingerprint() != before:
            raise RuntimeError("Model files changed during verification. Retry setup.")
        self._atomic_write(self.verification_path, json.dumps(before, sort_keys=True) + "\n")

    @property
    def status_path(self) -> Path:
        return self.state_dir / STATUS_NAME

    def model_dir(self) -> Path:
        return resolve_model_path()

    # -- receipt ---------------------------------------------------------
    def _read_receipt(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.receipt_path.read_text())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def _receipt_current(self) -> bool:
        receipt = self._read_receipt()
        if not receipt or not receipt.get("accepted"):
            return False
        if receipt.get("revision") != MODEL_REVISION:
            return False
        if receipt.get("license_sha256") != licence_digest():
            return False
        return True

    def _write_receipt(self) -> None:
        payload = {
            "accepted": True,
            "revision": MODEL_REVISION,
            "repo": MODEL_REPO,
            "license_sha256": licence_digest(),
            "accepted_at": time.time(),
        }
        self._atomic_write(self.receipt_path, json.dumps(payload, indent=2) + "\n", mode=0o600)

    def _clear_receipt(self) -> None:
        try:
            self.receipt_path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- status ----------------------------------------------------------
    def _stored_state(self) -> dict[str, Any]:
        """Live on-disk truth, plus any recorded error/cancel."""
        state = {
            "state": "missing",
            "accepted": self._receipt_current(),
            "ready": False,
            "completed_bytes": 0,
            "total_bytes": total_bytes(),
            "error": None,
        }
        model_dir = self.model_dir()
        complete, _reason = check_complete(model_dir, deep=False)
        state["completed_bytes"] = stored_bytes(model_dir)
        state["complete_by_size"] = complete
        if not state["accepted"]:
            state["state"] = "needs_acceptance"
        elif complete and self._verified_current():
            state["state"] = "ready"
            state["ready"] = True
        else:
            state["state"] = "missing"
        if state["state"] != "downloading" and self.status_path.is_file():
            try:
                saved = json.loads(self.status_path.read_text())
            except (OSError, ValueError):
                saved = {}
            if not isinstance(saved, dict):
                saved = {}
            if saved.get("state") in ("cancelled", "error") and not state["ready"]:
                state["state"] = saved["state"]
                state["error"] = saved.get("error")
        if self._child is not None and self._child.poll() is None:
            state["state"] = "cancelling" if self._cancelling else "downloading"
            state["ready"] = False
        return state

    @serialized
    def status(self) -> dict[str, Any]:
        state = self._stored_state()
        state.update({"repo": MODEL_REPO, "revision": MODEL_REVISION, "license_sha256": licence_digest()})
        return state

    # -- locking ---------------------------------------------------------
    def _acquire_lock(self) -> None:
        if self._lock_fd is not None:
            return
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RuntimeError(
                    "Another Embervoice process is already setting up the voice "
                    "model. Wait for it to finish, then try again."
                ) from exc
            raise
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(self._lock_fd)
        self._lock_fd = None

    # -- child process ---------------------------------------------------
    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Sanitise interpreter env inherited from the owner's shell.
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        # Scoped to this child only; the parent environment is untouched.
        env["HF_HUB_OFFLINE"] = "0"
        env["TRANSFORMERS_OFFLINE"] = "0"
        env["HF_HUB_DISABLE_XET"] = "1"
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        env["DO_NOT_TRACK"] = "1"
        env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        env["AUDIOBOOK_SETUP_DIR"] = str(self.state_dir.resolve())
        return env

    @serialized
    def start(self, accepted: bool, license_sha256: str) -> dict[str, Any]:
        """Begin setup. Requires an explicit acceptance of the current licence."""
        if not accepted:
            raise RuntimeError(
                "The model licence must be accepted before setup can start. "
                "Read the licence in the setup screen and tick the acceptance box."
            )
        if license_sha256 != licence_digest():
            self._clear_receipt()
            raise RuntimeError(
                "The licence text has changed since it was shown. Reload the licence and accept it again."
            )

        self.poll()
        if self._child is not None:
            raise RuntimeError("Model setup is already in progress.")
        self._acquire_lock()
        try:
            self.verification_path.unlink(missing_ok=True)
            model_dir = self.model_dir()
            model_dir.mkdir(parents=True, exist_ok=True)
            if not self._receipt_current():
                self._write_receipt()
            self._cancelling = False

            complete, _ = check_complete(model_dir, deep=False)
            if not complete:
                missing = total_bytes() - stored_bytes(model_dir)
                needed = max(0, missing) + DISK_HEADROOM_BYTES
                if free_bytes(model_dir) < needed:
                    self._save_status(
                        "error",
                        f"Not enough free disk space: {needed / 2**30:.1f} GB needed "
                        f"(about {missing / 2**30:.1f} GB for the model plus 1 GB spare), "
                        f"{free_bytes(model_dir) / 2**30:.1f} GB available.",
                    )
                    self._release_lock()
                    return self.status()

            self._save_status("downloading", None)
            fd = os.open(self.state_dir / "model-download.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            self._log = os.fdopen(fd, "wb")
            self._child = subprocess.Popen(
                worker_command("model_setup", ["--download"]),
                cwd=str(Path(__file__).resolve().parent),
                env=self._child_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self._log,
                start_new_session=True,
            )
        except BaseException:
            if getattr(self, "_log", None) is not None:
                self._log.close()
                self._log = None
            self._release_lock()
            raise
        return self.status()

    def _save_status(self, state: str, error: str | None) -> None:
        payload = {"state": state, "error": error, "updated_at": time.time()}
        try:
            self._atomic_write(self.status_path, json.dumps(payload, indent=2) + "\n")
        except OSError:
            pass

    @serialized
    def poll(self) -> dict[str, Any]:
        """Reap the child (if any) and fold its outcome into status."""
        if self._child is not None and self._cancelling and self._child.poll() is None:
            if time.monotonic() - self._cancelled_at >= 3:
                self._signal_child(9)
                try:
                    self._child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    raise RuntimeError("The owned download could not be stopped; do not start a replacement.")
        if self._child is not None and self._child.poll() is not None:
            child = self._child
            self._child = None
            stderr = b""
            if child.stderr is not None:
                stderr = child.stderr.read() or b""
                child.stderr.close()
            if getattr(self, "_log", None) is not None:
                self._log.close()
                self._log = None
            if not stderr:
                try:
                    with (self.state_dir / "model-download.log").open("rb") as log:
                        log.seek(0, 2)
                        log.seek(max(0, log.tell() - 8192))
                        stderr = log.read()
                except OSError:
                    pass
            code = child.returncode
            model_dir = self.model_dir()
            if self._cancelling:
                self._cancelling = False
                self._save_status("cancelled", None)
            elif code != 0:
                detail = stderr.decode(errors="replace").strip().splitlines()
                self._save_status("error", detail[-1] if detail else f"download exited {code}")
            else:
                if self._verified_current():
                    self._save_status("ready", None)
                else:
                    self._save_status("error", "Download exited without usable, verified pinned files. Retry setup.")
            self._release_lock()
        return self.status()

    @serialized
    def cancel(self) -> dict[str, Any]:
        """Terminate the owned child. HF partials are left for resume."""
        if self._child is None:
            return self.status()
        self._cancelling = True
        self._cancelled_at = time.monotonic()
        self._save_status("cancelling", None)
        if self._child.poll() is None:
            self._signal_child(15)
        return self.poll()

    def _signal_child(self, signal: int) -> None:
        try:
            os.killpg(os.getpgid(self._child.pid), signal)
        except (OSError, AttributeError):
            if signal == 9:
                self._child.kill()
            else:
                self._child.terminate()

    @serialized
    def close(self) -> None:
        if self._child is not None and self._child.poll() is None:
            self.cancel()
            if self._child is not None:
                try:
                    self._child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._signal_child(9)
                    try:
                        self._child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        raise RuntimeError("The owned download could not be stopped; cleanup must be retried.")
        self.poll()
        if getattr(self, "_log", None) is not None:
            self._log.close()
            self._log = None
        self._release_lock()

    def wait(self, timeout: float | None = None) -> dict[str, Any]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._child is not None:
            if deadline is not None and time.monotonic() > deadline:
                break
            time.sleep(0.2)
            self.poll()
        return self.status()

    # -- use -------------------------------------------------------------
    @serialized
    def require_ready(self) -> Path:
        """Return the model directory, or raise an actionable RuntimeError."""
        status = self.poll()
        model_dir = self.model_dir()
        if status["ready"]:
            return model_dir
        if not status["accepted"]:
            raise RuntimeError(
                "The voice model has not been set up yet. Open the model setup screen, "
                "read and accept the BreezeBlue licence, then start the download."
            )
        if status["state"] == "downloading":
            raise RuntimeError("The voice model is still downloading. Wait for it to finish.")
        if status["state"] == "error" and status.get("error"):
            raise RuntimeError(f"The voice model setup failed: {status['error']}")
        raise RuntimeError(
            "The voice model is incomplete in "
            f"{model_dir}. Re-run model setup to download the missing files "
            "(the download resumes where it stopped)."
        )


# --------------------------------------------------------------------------
# child entry point
# --------------------------------------------------------------------------
def _download() -> int:
    state = os.environ.get("AUDIOBOOK_SETUP_DIR")
    if not state:
        raise RuntimeError("Open setup and independently accept the model licence first.")
    setup = ModelSetup(Path(state))
    if not setup._receipt_current():
        raise RuntimeError("Read and accept the current model licence before downloading.")
    model_dir = setup.model_dir()
    missing, damaged = [], []
    needed = 0
    for entry in manifest_files():
        path = model_dir / entry["path"]
        ok, _ = verify_entry(entry, model_dir, deep=True)
        if ok:
            continue
        if path.is_file():
            damaged.append(entry["path"])
            needed += entry["size"]  # a forced repair needs a full replacement
        else:
            missing.append(entry["path"])
            partial = _incomplete_root() / ((entry.get("sha256") or entry["blob_id"]) + ".incomplete")
            try:
                stat = partial.stat()
                stored = min(stat.st_size, getattr(stat, "st_blocks", (stat.st_size + 511) // 512) * 512, entry["size"])
            except OSError:
                stored = 0
            needed += entry["size"] - stored
    if missing or damaged:
        if free_bytes(model_dir) < needed + DISK_HEADROOM_BYTES:
            raise RuntimeError("Not enough free disk space for missing or damaged model files plus 1 GiB headroom.")
        from huggingface_hub import snapshot_download  # imported only after acceptance

        kwargs = dict(
            repo_id=MODEL_REPO, revision=MODEL_REVISION, cache_dir=str(hub_root()), token=False, max_workers=2
        )
        if missing:
            snapshot_download(**kwargs, allow_patterns=missing)
        if damaged:
            snapshot_download(**kwargs, allow_patterns=damaged, force_download=True)
    setup._mark_verified()
    return 0


def main(argv: list[str]) -> int:
    if "--download" in argv:
        return _download()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
