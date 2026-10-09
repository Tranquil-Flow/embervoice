"""Focused tests for the native Embervoice desktop shell.

These tests are offline and inert:

* they compile ``desktop/Embervoice.swift`` with ``swiftc`` into the ignored
  ``.local/desktop-shell-test`` directory (no repo writes, no git);
* they smoke the launcher against a *tiny synthetic* helper that prints its own
  startup line and serves ``/api/setup``. No real model, no inference, no
  weights, no ``~/Audiobooks`` access, no browser is ever opened;
* they never touch the user's real library or send any telemetry.

Not qualified here: physical Finder double-click, Gatekeeper/notarisation, code
signing. Those need a human on the machine.
"""

from __future__ import annotations

import json
import os
import plistlib
import platform
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DESKTOP = REPO / "desktop"
SWIFT_SOURCE = DESKTOP / "Embervoice.swift"
INFO_PLIST = DESKTOP / "Info.plist"
WORKDIR = REPO / ".local" / "desktop-shell-test"
BINARY = WORKDIR / "Embervoice"

STARTUP_PREFIX = "Embervoice is starting at "


# --------------------------------------------------------------------------
# helpers


def _swiftc_available() -> bool:
    if platform.system() != "Darwin" or platform.machine() != "arm64" or int(platform.mac_ver()[0].split(".")[0]) < 26:
        return False
    if shutil.which("swiftc") is None:
        return False
    probe = subprocess.run(
        ["swiftc", "-target", "arm64-apple-macosx26.0", "-version"],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


def _compile_shell() -> None:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["swiftc", "-target", "arm64-apple-macosx26.0", "-O", "-o", str(BINARY), str(SWIFT_SOURCE)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"swiftc failed:\n{result.stdout}\n{result.stderr}"
    assert BINARY.exists()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_synthetic_helper(path: Path, *, serve: bool = True) -> Path:
    """A helper stand-in: prints the startup line, optionally serves /api/setup.

    It records the argv it received so tests can assert the shell's launch
    contract without starting anything real.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, os, sys, time
            from http.server import BaseHTTPRequestHandler, HTTPServer

            argv = sys.argv[1:]
            with open({str(path.with_suffix(".argv.json"))!r}, "w") as handle:
                json.dump(argv, handle)
            with open({str(path.with_suffix(".pid"))!r}, "w") as handle:
                handle.write(str(os.getpid()))

            # tiny helper only; loops back an ephemeral port, exactly like the
            # real launcher does with --port 0
            server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path.startswith("/api/setup"):
                        payload = b'{{"configured": false}}'
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    payload = b"<html>synthetic</html>"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def log_message(self, *args):
                    pass

            server.RequestHandlerClass = Handler
            port = server.server_address[1]
            print({STARTUP_PREFIX!r} + f"http://127.0.0.1:{{port}}/\\n", flush=True)
            {"server.serve_forever()" if serve else "time.sleep(30)"}
            """
        )
    )
    path.chmod(0o755)
    return path


def _wait_for_status_file(path: Path, timeout: float = 25.0) -> str:
    """Return the most recent status transition the shell recorded."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        if path.exists():
            lines = [line for line in path.read_text().splitlines() if line.strip()]
            if lines and lines[-1] != last:
                last = lines[-1]
                if last.startswith(("running ", "failed ")):
                    return last
        time.sleep(0.1)
    raise AssertionError(f"shell never reached running/failed (last: {last!r})")


def _run_shell(*, helper: Path, status_file: Path, store: str | None = None, timeout: float = 40.0):
    """Launch the shell, wait for a terminal status, verify while still live.

    Returns (process, status, verify) where ``verify`` is a callable that must
    be invoked before teardown if it needs the helper's port still listening.
    """
    env = dict(os.environ)
    env["EMBERVOICE_HELPER"] = str(helper)
    argv = [str(BINARY), "--no-browser", "--status-file", str(status_file)]
    if store:
        argv += ["--store", store]
    process = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    verify = lambda: None
    try:
        status = _wait_for_status_file(status_file, timeout=timeout)
        if status.startswith("running "):
            url = status[len("running ") :]
            port = int(url.split(":")[2].split("/")[0])

            def verify() -> None:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/setup", timeout=5) as response:
                    assert response.status == 200

            verify()  # while the shell and its helper are still alive

    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=10)
    return process, status, verify


# --------------------------------------------------------------------------
# source + bundle contract


def test_sources_exist() -> None:
    assert SWIFT_SOURCE.is_file()
    assert INFO_PLIST.is_file()


def test_info_plist_contract() -> None:
    with INFO_PLIST.open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["CFBundleIdentifier"] == "org.tranquilflow.embervoice"
    assert plist["CFBundleName"] == "Embervoice"
    assert plist["CFBundleExecutable"] == "Embervoice"
    assert plist["CFBundleShortVersionString"] == "0.1.0"
    assert plist["CFBundleVersion"] == "0.1.0"
    assert plist["LSMinimumSystemVersion"] == "26.0"


def test_shell_uses_only_system_frameworks() -> None:
    source = SWIFT_SOURCE.read_text()
    for module in ("import AppKit", "import Foundation"):
        assert module in source
    # no third-party package managers or vendored dependencies
    for forbidden in ("import Vapor", "import Alamofire", "Package.swift", "CocoaPods", "cocoapods"):
        assert forbidden not in source


def test_shell_parses_startup_line_and_probes_setup() -> None:
    source = SWIFT_SOURCE.read_text()
    assert STARTUP_PREFIX in source
    assert "/api/setup" in source
    # browser is opened once only, guarded by a flag
    assert "didOpenBrowser" in source


def test_shell_uses_sigterm_then_sigkill_with_five_second_grace() -> None:
    source = SWIFT_SOURCE.read_text()
    assert "SIGTERM" in source
    assert "SIGKILL" in source
    assert "gracefulShutdownSeconds: Double = 5.0" in source


def test_shell_does_not_restart_helper_on_failure() -> None:
    source = SWIFT_SOURCE.read_text()
    assert "startHelper()" in source
    # only one launch call site; failures route to fail(), never to startHelper()
    assert source.count("helper.start(") == 1
    assert "Nothing was retried automatically." in source


# --------------------------------------------------------------------------
# compile + behavioural smoke


@pytest.fixture(scope="session")
def compiled_shell() -> Path:
    if not _swiftc_available():
        pytest.skip("swiftc with arm64-apple-macosx26.0 target unavailable")
    _compile_shell()
    return BINARY


def test_swiftc_compiles_shell(compiled_shell: Path) -> None:
    assert compiled_shell.exists() and os.access(compiled_shell, os.X_OK)
    assert os.path.getsize(compiled_shell) > 10_000


def test_shell_builds_no_unresolved_dependencies(compiled_shell: Path) -> None:
    # @rpath-free, system-only binary: no third-party runtime is embedded.
    result = subprocess.run(["otool", "-L", str(compiled_shell)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    libs = [line.strip().split(" ")[0] for line in result.stdout.splitlines()[1:] if line.strip()]
    assert libs, "expected at least one linked library"
    for lib in libs:
        assert lib.startswith("/usr/lib") or lib.startswith("/System"), f"unexpected link: {lib}"


@pytest.mark.skipif(
    not _swiftc_available() or os.environ.get("CI") == "true", reason="requires local macOS 26 arm64 Aqua session"
)
def test_shell_launches_helper_and_reaches_running(tmp_path: Path) -> None:
    helper = _write_synthetic_helper(WORKDIR / "synthetic-helper")
    status_file = tmp_path / "status.txt"
    process, status, _verify = _run_shell(helper=helper, status_file=status_file)
    assert status.startswith("running http://127.0.0.1:")
    assert status[len("running ") :].endswith("/")


@pytest.mark.skipif(
    not _swiftc_available() or os.environ.get("CI") == "true", reason="requires local macOS 26 arm64 Aqua session"
)
def test_shell_passes_expected_helper_arguments(tmp_path: Path) -> None:
    helper = _write_synthetic_helper(WORKDIR / "argv-helper")
    store = tmp_path / "isolated-store"
    status_file = tmp_path / "status.txt"
    _run_shell(helper=helper, status_file=status_file, store=str(store))
    argv = json.loads(helper.with_suffix(".argv.json").read_text())
    assert "--no-browser" in argv
    assert argv[argv.index("--port") + 1] == "0"
    assert argv[argv.index("--store") + 1] == str(store)
    # the isolated store is used; the real ~/Audiobooks is never touched
    assert not (store / "existing").exists()


@pytest.mark.skipif(
    not _swiftc_available() or os.environ.get("CI") == "true", reason="requires local macOS 26 arm64 Aqua session"
)
def test_missing_helper_yields_visible_error(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["EMBERVOICE_HELPER"] = str(tmp_path / "definitely-not-here")
    status_file = tmp_path / "status.txt"
    process = subprocess.Popen(
        [str(BINARY), "--no-browser", "--status-file", str(status_file)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        status = _wait_for_status_file(status_file, timeout=20)
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert status.startswith("failed ")
    assert "helper" in status.lower()
    # the shell itself stays alive so the error remains on screen
    assert "Nothing was retried automatically." in SWIFT_SOURCE.read_text()


@pytest.mark.skipif(
    not _swiftc_available() or os.environ.get("CI") == "true", reason="requires local macOS 26 arm64 Aqua session"
)
def test_quit_terminates_owned_helper(tmp_path: Path) -> None:
    helper = _write_synthetic_helper(WORKDIR / "quit-helper")
    pid_file = helper.with_suffix(".pid")
    pid_file.unlink(missing_ok=True)
    status_file = tmp_path / "status.txt"
    env = dict(os.environ)
    env["EMBERVOICE_HELPER"] = str(helper)
    process = subprocess.Popen(
        [str(BINARY), "--no-browser", "--status-file", str(status_file), "--quit-when-ready"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid = None
    deadline = time.monotonic() + 10
    while pid is None and time.monotonic() < deadline:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
            except ValueError:
                pass
        time.sleep(0.05)
    assert pid is not None, "synthetic helper never reported its pid"
    stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, f"{stdout}\n{stderr}"

    # helper is gone within the graceful window
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:  # pragma: no cover - defensive
        pytest.fail(f"owned helper pid {pid} still alive after quit")
    assert "running " in status_file.read_text()


@pytest.mark.skipif(
    not _swiftc_available() or os.environ.get("CI") == "true", reason="requires local macOS 26 arm64 Aqua session"
)
def test_sigterm_to_shell_also_reaps_helper(tmp_path: Path) -> None:
    """A SIGTERM to the shell must not orphan the helper it owns."""
    helper = _write_synthetic_helper(WORKDIR / "signal-helper")
    pid_file = helper.with_suffix(".pid")
    pid_file.unlink(missing_ok=True)
    status_file = tmp_path / "status.txt"
    env = dict(os.environ)
    env["EMBERVOICE_HELPER"] = str(helper)
    process = subprocess.Popen(
        [str(BINARY), "--no-browser", "--status-file", str(status_file)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid = None
    deadline = time.monotonic() + 15
    while pid is None and time.monotonic() < deadline:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
            except ValueError:
                pass
        time.sleep(0.05)
    assert pid is not None
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=15)

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    pytest.fail(f"owned helper pid {pid} survived SIGTERM to the shell")


def test_no_public_telemetry_or_book_text_in_shell() -> None:
    source = SWIFT_SOURCE.read_text()
    for forbidden in (
        "https://",  # no remote calls of any kind
        'URLSession.shared.dataTask(with: URL(string: "https',
        "analytics",
        "telemetry",
    ):
        assert forbidden not in source, forbidden
    # helper stderr is discarded rather than logged/uploaded
    assert "FileHandle.nullDevice" in source


@pytest.mark.skipif(not _swiftc_available() or os.environ.get("CI") == "true", reason="requires local Aqua session")
def test_fragmented_startup_stdout_is_buffered(compiled_shell, tmp_path):
    helper = _write_synthetic_helper(tmp_path / "fragment-helper")
    lines = helper.read_text().splitlines()
    for i, line in enumerate(lines):
        if "print(" in line and STARTUP_PREFIX in line:
            lines[i] = (
                "for c in 'Embervoice is starting at ' + f'http://127.0.0.1:{port}/\\n':\n    sys.stdout.write(c); sys.stdout.flush(); time.sleep(0.005)"
            )
    helper.write_text("\n".join(lines) + "\n")
    _, status, _ = _run_shell(helper=helper, status_file=tmp_path / "status", timeout=5)
    assert status.startswith("running ")


@pytest.mark.skipif(not _swiftc_available() or os.environ.get("CI") == "true", reason="requires local Aqua session")
def test_helper_exit_before_startup_is_visible(compiled_shell, tmp_path):
    helper = tmp_path / "failed-helper"
    helper.write_text("#!/bin/sh\nexit 7\n")
    helper.chmod(0o755)
    _, status, _ = _run_shell(helper=helper, status_file=tmp_path / "status", timeout=5)
    assert status.startswith("failed ")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
