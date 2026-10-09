"""Portable release boundaries; no private inputs permitted in the source ZIP."""

from pathlib import Path
import importlib.util
import zipfile
import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release_builder", ROOT / "scripts/build_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_public_archive_contains_legal_ui_lock_and_synthetic_fixtures(tmp_path):
    target = release.build(destination=tmp_path / "candidate.zip")
    with zipfile.ZipFile(target) as archive:
        paths = archive.namelist()
        for name in [
            "LICENSE",
            "NOTICE",
            "legal/BREEZE_LICENSE.txt",
            "uv.lock",
            "Install.command",
            "Start Embervoice.command",
            "web/setup.js",
            "tests/fixtures/synthetic.epub",
        ]:
            assert "embervoice/" + name in paths
        assert not any(
            "/samples/" in name or "/.venv/" in name or "/docs/" in name or name.endswith(".safetensors")
            for name in paths
        )
        assert sum(name.startswith("embervoice/web/voices/") for name in paths) == 12
        mode = archive.getinfo("embervoice/Install.command").external_attr >> 16
        assert mode & 0o111, "Double-click launchers must survive ZIP extraction as executable files"


def test_release_rejects_symlinked_inputs(tmp_path):
    for name in release.TOP_LEVEL:
        (tmp_path / name).write_text("fixture")
    (tmp_path / "LICENSE").unlink()
    (tmp_path / "LICENSE").symlink_to(ROOT / "LICENSE")
    with pytest.raises(ValueError, match="unsafe"):
        release.public_files(tmp_path)
