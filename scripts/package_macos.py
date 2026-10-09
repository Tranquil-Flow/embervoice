"""Package the verified app as a drag-to-Applications DMG; never changes OS trust."""

from pathlib import Path
import hashlib
import json
import plistlib
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent.parent


def version(root=ROOT):
    return tomllib.loads((Path(root) / "pyproject.toml").read_text())["project"]["version"]


def stage(app, destination, guide):
    """Copy only the app and shared opening guide, with an Applications shortcut."""
    app, destination, guide = Path(app), Path(destination), Path(guide)
    if app.is_symlink() or guide.is_symlink():
        raise ValueError("Refusing symlinked distribution input")
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if any(info.get(key) != version() for key in ("CFBundleShortVersionString", "CFBundleVersion")):
        raise ValueError("App version does not match project version; rebuild before packaging")
    if not guide.is_file() or not (app / "Contents/MacOS/Embervoice").is_file():
        raise ValueError("App or opening guide missing")
    destination.mkdir()
    subprocess.run(["/usr/bin/ditto", str(app), str(destination / "Embervoice.app")], check=True, timeout=120)
    (destination / "START HERE.txt").write_bytes(guide.read_bytes())
    (destination / "Applications").symlink_to("/Applications")


def build():
    if sys.platform != "darwin":
        raise RuntimeError("Package on macOS with the built Embervoice.app")
    app = ROOT / "dist/Embervoice.app"
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True, capture_output=True)
    scratch = ROOT / ".local"
    scratch.mkdir(exist_ok=True)
    destination = ROOT / "dist/Embervoice-mac.dmg"
    with tempfile.TemporaryDirectory(prefix="mac-package-", dir=scratch) as directory:
        contents = Path(directory) / "contents"
        stage(app, contents, ROOT / "OPEN_FIRST.txt")
        subprocess.run(
            [
                "hdiutil",
                "create",
                "-quiet",
                "-ov",
                "-fs",
                "HFS+",
                "-format",
                "UDZO",
                "-volname",
                "Embervoice - Drag to Applications",
                "-srcfolder",
                str(contents),
                str(destination),
            ],
            check=True,
            timeout=180,
        )
    subprocess.run(["hdiutil", "verify", "-quiet", str(destination)], check=True, timeout=60)
    print(
        json.dumps(
            {
                "artifact": str(destination),
                "version": version(),
                "bytes": destination.stat().st_size,
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "signature": "ad-hoc only; NOT Developer ID signed or notarized",
            }
        )
    )
    return destination


if __name__ == "__main__":
    build()
