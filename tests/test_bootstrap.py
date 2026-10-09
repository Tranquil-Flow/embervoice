"""Installer's default bootstrap pins and negative consent, without network."""

import os
import shutil
import subprocess
from test_installer import ROOT, run_install, _tool_dir, RAM_OK


def test_macos_below_runtime_minimum_refuses(tmp_path):
    tools = _tool_dir(tmp_path, ("ffmpeg", "ffprobe", "uv"))
    result = run_install(
        tmp_path,
        {
            "PATH": str(tools),
            "AUDIOBOOK_STUDIO_TOOL_PATHS": str(tools),
            "AUDIOBOOK_STUDIO_OS": "Darwin",
            "AUDIOBOOK_STUDIO_ARCH": "arm64",
            "AUDIOBOOK_STUDIO_RAM_BYTES": str(RAM_OK),
            "AUDIOBOOK_STUDIO_MACOS_VERSION": "15.6.1",
        },
        "--check",
    )
    assert result.returncode == 2 and "macOS 26 or later" in result.stderr


def test_default_bootstrap_has_pinned_official_archive_without_owner_variables():
    text = (ROOT / "install.sh").read_text()
    assert 'UV_VERSION="0.11.26"' in text
    assert "8f7fbf1708399b921857bce71e1d60f0d3ccf52a30caebc1c1a2f175dce13ab6" in text
    assert "uv-aarch64-apple-darwin.tar.gz" in text
    assert "Checksum" in text or "checksum" in text
    assert "AUDIOBOOK_STUDIO_UV_URL" not in text


def test_declining_bootstrap_download_creates_no_environment(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    shutil.copy2(ROOT / "install.sh", app / "install.sh")
    tools = _tool_dir(tmp_path, ("ffmpeg", "ffprobe"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("AUDIOBOOK_STUDIO_")}
    env.update(
        PATH="/usr/bin:/bin",
        AUDIOBOOK_STUDIO_TOOL_PATHS=str(tools),
        AUDIOBOOK_STUDIO_OS="Darwin",
        AUDIOBOOK_STUDIO_ARCH="arm64",
        AUDIOBOOK_STUDIO_RAM_BYTES=str(RAM_OK),
        AUDIOBOOK_STUDIO_MACOS_VERSION="26.5.1",
    )
    result = subprocess.run(
        ["/bin/bash", str(app / "install.sh")], input="n\n", text=True, capture_output=True, env=env, timeout=10
    )
    assert result.returncode == 2 and "Cancelled before downloading uv" in result.stderr
    assert not (app / ".local").exists() and not (app / ".venv").exists()
