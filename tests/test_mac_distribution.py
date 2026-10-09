"""Mac distribution structure with synthetic bundles; no narration or OS trust bypass."""

import importlib.util
from pathlib import Path
import plistlib
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def distribution():
    spec = importlib.util.spec_from_file_location("mac_distribution", ROOT / "scripts/package_macos.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bundle(tmp_path):
    app = tmp_path / "Embervoice.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleShortVersionString": version, "CFBundleVersion": version}, stream)
    (contents / "MacOS/Embervoice").write_text("synthetic bundle; never executed")
    return app


@pytest.mark.skipif(sys.platform != "darwin", reason="ditto is a macOS distribution tool")
def test_dmg_stage_is_one_app_applications_shortcut_and_shared_guide(distribution, bundle, tmp_path):
    stage = tmp_path / "stage"
    distribution.stage(bundle, stage, ROOT / "OPEN_FIRST.txt")
    assert {p.name for p in stage.iterdir()} == {"Embervoice.app", "Applications", "START HERE.txt"}
    assert (stage / "Applications").is_symlink()
    assert (stage / "Applications").readlink() == Path("/Applications")
    assert (stage / "START HERE.txt").read_bytes() == (ROOT / "OPEN_FIRST.txt").read_bytes()
    assert (stage / "Embervoice.app/Contents/MacOS/Embervoice").read_bytes() == (
        bundle / "Contents/MacOS/Embervoice"
    ).read_bytes()


def test_mismatched_bundle_version_refused_before_staging(distribution, bundle, tmp_path):
    plist = bundle / "Contents/Info.plist"
    with plist.open("wb") as stream:
        plistlib.dump({"CFBundleShortVersionString": "0.0.0", "CFBundleVersion": "0.0.0"}, stream)
    stage = tmp_path / "stage"
    with pytest.raises(ValueError, match="version"):
        distribution.stage(bundle, stage, ROOT / "OPEN_FIRST.txt")
    assert not stage.exists()


def test_symlinked_app_refused_before_staging(distribution, bundle, tmp_path):
    link = tmp_path / "unsafe.app"
    link.symlink_to(bundle)
    stage = tmp_path / "stage"
    with pytest.raises(ValueError, match="symlink"):
        distribution.stage(link, stage, ROOT / "OPEN_FIRST.txt")
    assert not stage.exists()
