"""Build a source ZIP from explicitly public paths, never from a whole working tree."""

from pathlib import Path
import hashlib
import json
import zipfile
import tomllib

ROOT = Path(__file__).resolve().parent.parent
TOP_LEVEL = (
    "audiobook.py",
    "studio.py",
    "model_setup.py",
    "launcher.py",
    "runtime.py",
    "embervoice_entry.py",
    "setup.py",
    "README.md",
    "HELP.md",
    "TECHNICAL.md",
    "OPEN_FIRST.txt",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "RELEASE_STATUS.md",
    "voice-style.example.txt",
    "LICENSE",
    "NOTICE",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    ".gitignore",
    "MANIFEST.in",
    "model-files.json",
    "install.sh",
    "Install.command",
    "Start Embervoice.command",
)


def public_files(root=ROOT):
    root = Path(root)
    files = [root / name for name in TOP_LEVEL]
    patterns = {
        "web": ["*.html", "*.css", "*.js", "voices/*.m4a"],
        "anchors": ["*.wav"],
        "legal": ["*.txt", "*.json"],
        "desktop": ["*.swift", "*.plist", "*.spec", "*.png", "*.icns"],
        "tests": ["*.py", "fixtures/*.epub", "fixtures/reference-*"],
        "scripts": ["*.py"],
        ".github/workflows": ["*.yml"],
    }
    for folder, globs in patterns.items():
        for pattern in globs:
            files.extend((root / folder).glob(pattern))
    files = sorted(set(files))
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or unsafe release input: {path.relative_to(root)}")
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError(f"Unexpectedly large release input: {path.relative_to(root)}")
    return files


def build(root=ROOT, destination=None):
    root = Path(root)
    files = public_files(root)
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    destination = Path(destination or root / f"dist/embervoice-{version}-source.zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, "embervoice/" + path.relative_to(root).as_posix())
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(
        json.dumps(
            {
                "artifact": str(destination.resolve()),
                "files": len(files),
                "bytes": destination.stat().st_size,
                "sha256": digest,
            }
        )
    )
    return destination


if __name__ == "__main__":
    build()
