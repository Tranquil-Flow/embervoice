"""Frozen executable composition without invoking models or desktop UI."""

import sys
import pytest
import runtime


def test_frozen_missing_tools_explains_reinstall(monkeypatch):
    import studio

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(studio.shutil, "which", lambda name: None)
    message = " ".join(studio.startup_readiness()["blockers"]).lower()
    assert "brew install" not in message
    assert "ffmpeg" in message and "download" in message


def test_memory_snapshot_does_not_double_count_reclaimable_pages(monkeypatch):
    import audiobook

    def output(cmd, **kwargs):
        if cmd[0].endswith("vm_stat"):
            return "Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 1.\nPages inactive: 1.\nPages speculative: 100.\nPages purgeable: 100.\n"
        if "vm.swapusage" in cmd:
            return "total = 100.00M  used = 50.00M  free = 50.00M"
        return str(16 * 2**30)

    monkeypatch.setattr(audiobook.subprocess, "check_output", output)
    assert audiobook.memory_snapshot()["available_gib"] == 2 * 4096 / 2**30


def test_source_worker_uses_python_module():
    assert runtime.worker_command("audiobook", ["fixture.epub"]) == [sys.executable, "-m", "audiobook", "fixture.epub"]


def test_frozen_workers_reenter_owned_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert runtime.app_root() == tmp_path
    assert runtime.worker_command("model_setup", ["--download"]) == [
        sys.executable,
        "--worker",
        "model_setup",
        "--download",
    ]


def test_unknown_worker_is_refused():
    with pytest.raises(ValueError, match="worker"):
        runtime.worker_command("arbitrary_module", [])
    with pytest.raises(SystemExit, match="worker"):
        runtime.worker_main(["--worker", "arbitrary_module"])


def test_normal_launcher_is_not_dispatched():
    assert runtime.worker_main(["--no-browser"]) is None


def test_bundled_tools_precede_host_path(monkeypatch, tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ["ffmpeg", "ffprobe"]:
        path = tools / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    monkeypatch.setattr(runtime, "app_root", lambda: tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    runtime.configure_bundled_tools()
    assert runtime.tool_path("ffmpeg") == str(tools / "ffmpeg")
    assert "/usr/sbin" in runtime.os.environ["PATH"].split(runtime.os.pathsep)


def test_audiobook_worker_accepts_its_own_arguments(capsys):
    with pytest.raises(SystemExit) as exit:
        runtime.worker_main(["--worker", "audiobook", "--help"])
    assert exit.value.code == 0
    assert "--output" in capsys.readouterr().out


def test_worker_none_return_is_success_not_launcher(monkeypatch):
    import types

    monkeypatch.setattr(runtime.importlib, "import_module", lambda name: types.SimpleNamespace(main=lambda args: None))
    assert runtime.worker_main(["--worker", "audiobook", "fixture.epub"]) == 0
