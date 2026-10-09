"""Installer preflight tests: refusals for unsupported Macs and missing tools.

install.sh reads AUDIOBOOK_STUDIO_* overrides for OS, arch, RAM and PATH, so
every refusal is exercised without touching the real machine.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
INSTALL_COMMAND = ROOT / "Install.command"

MAC = "arm64"
RAM_OK = 16 * 1024**3
RAM_LOW = 8 * 1024**3


def _tool_dir(tmp_path: Path, names: tuple[str, ...]) -> Path:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for name in names:
        f = d / name
        f.write_text("#!/bin/sh\nexit 0\n")
        f.chmod(0o755)
    return d


def run_install(tmp_path: Path, env_extra: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AUDIOBOOK_STUDIO_", "PYTHONPATH", "PYTHONHOME"))}
    env["AUDIOBOOK_STUDIO_MACOS_VERSION"] = "26.5.1"
    env.update(env_extra)
    return subprocess.run(
        ["/bin/bash", str(INSTALL), *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_install_script_is_executable():
    assert os.access(INSTALL, os.X_OK), "install.sh must be executable"
    assert os.access(INSTALL_COMMAND, os.X_OK), "Install.command must be executable"


def test_refuses_non_darwin(tmp_path):
    tools = _tool_dir(tmp_path, ("ffmpeg", "ffprobe"))
    result = run_install(
        tmp_path,
        {
            "PATH": str(tools),
            "AUDIOBOOK_STUDIO_TOOL_PATHS": str(tools),
            "AUDIOBOOK_STUDIO_OS": "Linux",
            "AUDIOBOOK_STUDIO_ARCH": MAC,
            "AUDIOBOOK_STUDIO_RAM_BYTES": str(RAM_OK),
        },
        "--check",
    )
    assert result.returncode == 2
    assert "macOS only" in result.stdout + result.stderr


def test_refuses_intel_mac(tmp_path):
    tools = _tool_dir(tmp_path, ("ffmpeg", "ffprobe"))
    result = run_install(
        tmp_path,
        {
            "PATH": str(tools),
            "AUDIOBOOK_STUDIO_TOOL_PATHS": str(tools),
            "AUDIOBOOK_STUDIO_OS": "Darwin",
            "AUDIOBOOK_STUDIO_ARCH": "x86_64",
            "AUDIOBOOK_STUDIO_RAM_BYTES": str(RAM_OK),
        },
        "--check",
    )
    assert result.returncode == 2
    assert "Apple Silicon" in result.stdout + result.stderr


def test_refuses_8gb_mac(tmp_path):
    tools = _tool_dir(tmp_path, ("ffmpeg", "ffprobe"))
    result = run_install(
        tmp_path,
        {
            "PATH": str(tools),
            "AUDIOBOOK_STUDIO_TOOL_PATHS": str(tools),
            "AUDIOBOOK_STUDIO_OS": "Darwin",
            "AUDIOBOOK_STUDIO_ARCH": MAC,
            "AUDIOBOOK_STUDIO_RAM_BYTES": str(RAM_LOW),
        },
        "--check",
    )
    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert "12 GiB" in combined
    assert "8 GiB" in combined


def test_refuses_missing_ffmpeg(tmp_path):
    tools = _tool_dir(tmp_path, ("ffprobe",))
    result = run_install(
        tmp_path,
        {
            "PATH": str(tools),
            "AUDIOBOOK_STUDIO_TOOL_PATHS": str(tools),
            "AUDIOBOOK_STUDIO_OS": "Darwin",
            "AUDIOBOOK_STUDIO_ARCH": MAC,
            "AUDIOBOOK_STUDIO_RAM_BYTES": str(RAM_OK),
        },
        "--check",
    )
    assert result.returncode == 2
    assert "ffmpeg was not found" in result.stdout + result.stderr
    assert "brew install ffmpeg" in result.stdout + result.stderr


def test_refuses_missing_ffprobe(tmp_path):
    tools = _tool_dir(tmp_path, ("ffmpeg",))
    result = run_install(
        tmp_path,
        {
            "PATH": str(tools),
            "AUDIOBOOK_STUDIO_TOOL_PATHS": str(tools),
            "AUDIOBOOK_STUDIO_OS": "Darwin",
            "AUDIOBOOK_STUDIO_ARCH": MAC,
            "AUDIOBOOK_STUDIO_RAM_BYTES": str(RAM_OK),
        },
        "--check",
    )
    assert result.returncode == 2
    assert "ffprobe was not found" in result.stdout + result.stderr


def test_check_mode_installs_nothing_and_reports_missing_uv(tmp_path):
    tools = _tool_dir(tmp_path, ("ffmpeg", "ffprobe"))
    result = run_install(
        tmp_path,
        {
            "PATH": str(tools),
            "AUDIOBOOK_STUDIO_TOOL_PATHS": str(tools),
            "AUDIOBOOK_STUDIO_OS": "Darwin",
            "AUDIOBOOK_STUDIO_ARCH": MAC,
            "AUDIOBOOK_STUDIO_RAM_BYTES": str(RAM_OK),
        },
        "--check",
    )
    combined = result.stdout + result.stderr
    assert "NOT FOUND" in combined or "uv       :" in combined
    assert "does not install Homebrew or uv for you" in combined or "brew install uv" in combined
    assert not (ROOT / ".venv.new").exists()


def test_no_hardcoded_owner_paths_in_installer():
    for name in ("install.sh", "Install.command", "Start Embervoice.command"):
        text = (ROOT / name).read_text()
        assert "/Users/" not in text, f"{name} must not hardcode an owner path"


def test_installer_does_not_self_promote_licence_acceptance():
    text = INSTALL.read_text()
    assert "accept" not in text.lower().replace("licence acceptance happen", "")
