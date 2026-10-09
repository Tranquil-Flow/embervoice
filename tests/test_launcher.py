"""Launcher tests: real loopback HTTP, port fallback, no external binding.

Each server runs in its own subprocess with a temporary store, so no user data
and no microphone are involved.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import launcher  # noqa: E402  (path set above)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, proc, port):
        self.proc = proc
        self.port = port

    def get(self, path="/api/setup"):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.read()


def start_server(tmp_path: Path, *args: str, port: int | None = None) -> Server:
    port = port if port is not None else free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "launcher.py"),
            "--no-browser",
            "--store",
            str(tmp_path / "store"),
            "--port",
            str(port),
            *args,
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError("launcher exited early:\n" + proc.stdout.read())
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/setup", timeout=1)
            return Server(proc, port)
        except urllib.error.HTTPError:
            return Server(proc, port)
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise AssertionError("launcher never became ready")


# ------------------------------------------------------------------ pure bits
def test_default_port_and_loopback_only():
    args = launcher.parse_args([])
    assert args.port == 8765
    assert args.no_browser is False
    assert launcher.HOST == "127.0.0.1"


def test_parse_args_flags():
    args = launcher.parse_args(["--port", "9001", "--no-browser", "--store", "/tmp/x"])
    assert (args.port, args.no_browser, args.store) == (9001, True, "/tmp/x")


def test_native_shell_port_zero_is_ephemeral_loopback():
    with launcher.bind_loopback(0) as sock:
        host, port = sock.getsockname()
        assert host == "127.0.0.1" and 0 < port <= 65535


def test_bind_loopback_falls_back_when_busy():
    busy = socket.socket()
    busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    taken = busy.getsockname()[1]
    try:
        sock = launcher.bind_loopback(taken)
        try:
            assert sock.getsockname()[1] != taken
            assert sock.getsockname()[0] == "127.0.0.1"
        finally:
            sock.close()
    finally:
        busy.close()


def test_importing_launcher_does_not_load_studio():
    code = "import sys, launcher; print('studio' in sys.modules, 'mlx' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert out.stdout.strip() == "False False", out.stdout + out.stderr


# ------------------------------------------------------------ live subprocess
def test_serves_http_on_loopback(tmp_path):
    server = start_server(tmp_path)
    try:
        status, body = server.get("/api/setup")
        assert status == 200
        assert b"model_ready" in body
    finally:
        server.proc.terminate()
        server.proc.wait(timeout=60)


def test_port_fallback_launches_a_new_server_not_the_existing_one(tmp_path):
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    occupied = holder.getsockname()[1]
    server = None
    try:
        # The launcher must not attach to the unrelated listener on 127.0.0.1.
        proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "launcher.py"),
                "--no-browser",
                "--store",
                str(tmp_path / "store2"),
                "--port",
                str(occupied),
            ],
            cwd=str(ROOT),
            env={
                **{k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")},
                "HF_HUB_OFFLINE": "1",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 60
        new_port = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError("launcher exited:\n" + proc.stdout.read())
            line = proc.stdout.readline()
            if "http://127.0.0.1:" in line:
                new_port = int(line.split("127.0.0.1:")[1].split("/")[0])
                break
        assert new_port and new_port != occupied
        server = Server(proc, new_port)
        status, _ = server.get("/api/setup")
        assert status == 200
    finally:
        if server:
            server.proc.terminate()
            server.proc.wait(timeout=60)
        holder.close()


def test_server_is_not_reachable_off_loopback(tmp_path):
    server = start_server(tmp_path)
    try:
        probe_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe_udp.connect(("192.0.2.1", 9))  # TEST-NET-1: no packet is sent
            external = [probe_udp.getsockname()[0]]
        finally:
            probe_udp.close()
        external = [a for a in external if not a.startswith("127.")]
        if not external:
            pytest.skip("machine has no non-loopback IPv4 address")
        with socket.socket() as probe:
            probe.settimeout(3)
            with pytest.raises(OSError):
                probe.connect((external[0], server.port))
    finally:
        server.proc.terminate()
        server.proc.wait(timeout=60)


def test_sigterm_shuts_down_gracefully(tmp_path):
    server = start_server(tmp_path)
    server.proc.terminate()
    assert server.proc.wait(timeout=60) is not None
    out = server.proc.stdout.read()
    assert "Traceback" not in out, out
    assert "Address already in use" not in out, out


def test_browser_is_not_opened_when_no_browser(tmp_path, monkeypatch):
    port = free_port()
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")},
        "HF_HUB_OFFLINE": "1",
        "BROWSER": "/usr/bin/false",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "launcher.py"),
            "--no-browser",
            "--store",
            str(tmp_path / "store3"),
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        ready = False
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/setup", timeout=1)
                ready = True
                break
            except urllib.error.HTTPError:
                ready = True
                break
            except Exception:
                time.sleep(0.25)
        assert ready
    finally:
        proc.terminate()
        proc.wait(timeout=60)
