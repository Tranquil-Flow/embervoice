"""Build small self-contained audio tools from checksum-pinned LGPL FFmpeg source."""

from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent.parent
VERSION = "9.0.2"
URL = f"https://ffmpeg.org/releases/ffmpeg-{VERSION}.tar.xz"
SHA256 = "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"
CONFIGURE = [
    "--disable-gpl",
    "--disable-nonfree",
    "--disable-shared",
    "--enable-static",
    "--disable-autodetect",
    "--disable-everything",
    "--disable-doc",
    "--disable-debug",
    "--enable-ffmpeg",
    "--enable-ffprobe",
    "--enable-avcodec",
    "--enable-avformat",
    "--enable-avutil",
    "--enable-swresample",
    "--enable-swscale",
    "--enable-avfilter",
    "--enable-encoder=aac,pcm_s16le",
    "--enable-decoder=aac,pcm_s16le,pcm_s24le,pcm_s32le,pcm_f32le,pcm_f64le,pcm_u8,alac,opus,vorbis,mp3,flac",
    "--enable-demuxer=mov,wav,matroska,mp3,flac,aac,ogg,concat,ffmetadata",
    "--enable-muxer=ipod,mp4,wav,ffmetadata",
    "--enable-parser=aac,mpegaudio,opus,vorbis,flac",
    "--enable-protocol=file,pipe",
    "--enable-filter=aresample,aformat,anull,atrim,asetpts",
    "--enable-bsf=aac_adtstoasc",
    "--arch=arm64",
    "--target-os=darwin",
    "--cc=clang",
]


def build():
    work = ROOT / ".local/ffmpeg-build"
    work.mkdir(parents=True, exist_ok=True)
    archive = work / f"ffmpeg-{VERSION}.tar.xz"
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "--proto",
                "=https",
                "--tlsv1.2",
                "-fL",
                "--connect-timeout",
                "15",
                "--max-time",
                "300",
                "--retry",
                "2",
                "-C",
                "-",
                URL,
                "-o",
                str(archive),
            ],
            timeout=930,
        )
        if result.returncode or hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
            raise RuntimeError("FFmpeg source download/checksum failed; no source was executed.")
    source = work / f"ffmpeg-{VERSION}"
    if not source.exists():
        with tarfile.open(archive) as tar:
            tar.extractall(work, filter="data")
    stamp = source / ".embervoice-config.json"
    if not (source / "config.h").exists() or not stamp.exists() or json.loads(stamp.read_text()) != CONFIGURE:
        if (source / "config.h").exists():
            subprocess.run(["make", "distclean"], cwd=source, check=True, capture_output=True, timeout=30)
        with (work / "configure.log").open("w") as out:
            subprocess.run(
                [str(source / "configure"), *CONFIGURE],
                cwd=source,
                stdout=out,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=1200,
            )
        stamp.write_text(json.dumps(CONFIGURE))
    with (work / "make.log").open("w") as out:
        subprocess.run(["make", "-j", "3"], cwd=source, stdout=out, stderr=subprocess.STDOUT, check=True, timeout=480)
    tools = ROOT / ".local/tools"
    tools.mkdir(exist_ok=True)
    for name in ["ffmpeg", "ffprobe"]:
        shutil.copy2(source / name, tools / name)
        subprocess.run(["codesign", "--force", "--sign", "-", str(tools / name)], check=True, capture_output=True)
    legal = ROOT / "legal/FFMPEG_LICENSE.txt"
    shutil.copy2(source / "COPYING.LGPLv2.1", legal)
    manifest = {
        "version": VERSION,
        "source_url": URL,
        "source_sha256": SHA256,
        "configure": CONFIGURE,
        "tools": {name: hashlib.sha256((tools / name).read_bytes()).hexdigest() for name in ["ffmpeg", "ffprobe"]},
    }
    (work / "build.json").write_text(json.dumps(manifest, indent=2))
    (ROOT / "legal/FFMPEG_BUILD.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (ROOT / "legal/FFMPEG_SOURCE.txt").write_text(
        f"FFmpeg {VERSION} is LGPL-2.1-or-later, built with GPL and nonfree components disabled.\n"
        f"Complete corresponding source is bundled as {archive.name} beside this notice.\n"
        f"Upstream source: {URL}\nSHA-256: {SHA256}\n"
        "Build configuration: FFMPEG_BUILD.json. Build/relink procedure: scripts/build_ffmpeg.py in the Embervoice source repository.\n"
        "You may modify and rebuild this separate executable and replace the bundled ffmpeg/ffprobe tools for your own use.\n"
        "The app has no code preventing replacement or debugging of these components. The surrounding app may require re-signing after modification.\n"
    )
    print(json.dumps(manifest), flush=True)


if __name__ == "__main__":
    build()
