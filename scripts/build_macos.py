"""Build a relocatable macOS app; does not sign with an account or change OS trust."""

from pathlib import Path
import importlib.metadata as metadata
import json
import os
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parent.parent


def notices():
    parts = [
        "Embervoice third-party notices\n\nRuntime and build dependencies are listed below; not all are shipped in the app.\n"
    ]
    for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata.get("Name") or "").lower()):
        name = dist.metadata.get("Name", "")
        if name.lower().replace("_", "-") in ("embervoice", "audiobook-studio"):
            continue
        parts.append(f"\n=== {name} {dist.version} ===\n")
        parts.append(
            str(dist.metadata.get("License-Expression") or dist.metadata.get("License") or "See licence text below.")
        )
        for file in sorted(dist.files or [], key=str):
            if any(
                "license" in piece.lower() or "copying" in piece.lower() or "notice" in piece.lower()
                for piece in file.parts
            ):
                path = Path(str(dist.locate_file(file)))
                if (
                    path.is_file()
                    and path.stat().st_size < 1_000_000
                    and path.suffix not in (".py", ".pyc", ".so", ".dylib")
                ):
                    try:
                        parts.append(f"\n--- {file} ---\n" + path.read_text())
                    except UnicodeDecodeError:
                        pass
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python runtime licence missing; do not ship without it.")
    parts.append("\n=== CPython runtime ===\n" + python_license.read_text())
    notice = ROOT / "legal/THIRD_PARTY_NOTICES.txt"
    text = "\n".join(parts)
    if not notice.exists() or notice.read_text() != text:
        notice.write_text(text)


def build():
    if sys.platform != "darwin":
        raise RuntimeError("Build on an Apple Silicon Mac with macOS 26 SDK.")
    notices()
    work = ROOT / ".local/app-build"
    work.mkdir(parents=True, exist_ok=True)
    output = ROOT / ".local/frozen"
    output.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    with (work / "pyinstaller.log").open("w") as log:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--distpath",
                str(output),
                "--workpath",
                str(work),
                str(ROOT / "desktop/Embervoice.spec"),
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=600,
        )
    app = ROOT / "dist/Embervoice.app"
    if app.exists():
        shutil.rmtree(app)
    content = app / "Contents"
    (content / "MacOS").mkdir(parents=True)
    resources = content / "Resources"
    resources.mkdir()
    shutil.copy2(ROOT / "desktop/Info.plist", content / "Info.plist")
    subprocess.run(
        [
            "swiftc",
            "-target",
            "arm64-apple-macosx26.0",
            "-O",
            "-o",
            str(content / "MacOS/Embervoice"),
            str(ROOT / "desktop/Embervoice.swift"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    shutil.copytree(output / "embervoice-server", resources / "runtime", symlinks=True)
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True, capture_output=True, text=True
    )
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True, capture_output=True, text=True)
    print(
        json.dumps(
            {
                "app": str(app),
                "bytes": sum(p.stat().st_size for p in app.rglob("*") if p.is_file() and not p.is_symlink()),
                "signature": "ad-hoc only; NOT Developer ID signed or notarized",
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    build()
