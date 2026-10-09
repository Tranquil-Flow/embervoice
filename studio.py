"""Loopback-only web UI for the offline EPUB audiobook converter."""

from __future__ import annotations

import atexit
import array
import hashlib
import inspect
import json
import math
import os
import platform
import shutil
from contextlib import asynccontextmanager
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile
import wave
from pathlib import Path
from time import monotonic as elapsed_clock
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from audiobook import CLIP_CFG, Chapter, chapter_label, extract_book, split_passages

from runtime import app_root, worker_command

PROJECT = app_root()
MAX_UPLOAD = 50 * 1024 * 1024
MAX_REFERENCE = 10 * 1024 * 1024
MAX_UNPACKED = 300 * 1024 * 1024
ID = re.compile(r"^[0-9a-f]{32}$")
REF_ID = re.compile(r"^[0-9a-f]{64}$")
VERSION_ID = re.compile(r"^[0-9a-f]{16}$")
PASSAGE_FILE = re.compile(r"^[0-9]{3}-[0-9]{4}\.wav$")
PRESETS = {
    "warm": "A warm, grounded adult storyteller. Clear diction, gentle expression and an unhurried, natural cadence. Keep the same narrator voice across chapters; avoid theatrical exaggeration.",
    "literary": "A mature literary narrator with a smooth, resonant mid-range voice. Reflective, subtle, expressive on key lines, with natural pauses and measured pacing. Maintain a consistent voice.",
    "bright": "A clear, friendly adult narrator with a lightly buoyant tone. Engaged and curious, crisp articulation, conversational pacing. Keep a stable voice across chapters.",
    "calm": "A calm, softly spoken adult narrator. Intimate and reassuring, steady rhythm, gentle emphasis, clear articulation without whispering. Consistent vocal identity across the book.",
    "documentary": "A grounded documentary narrator. Observant and precise, steady pace, articulate without sounding detached; allow the facts to carry the weight.",
    "dramatic": "An expressive adult narrator with restrained dramatic range. Bring tension and contrast to key moments, while keeping dialogue natural and avoiding caricature.",
    "reflective": "A thoughtful, contemplative adult narrator. Quiet confidence, warm pauses and a reflective cadence; leave room for difficult ideas to land.",
    "crisp": "A crisp, direct adult narrator. Bright articulation, purposeful pace and clean phrasing; conversational rather than formal or hurried.",
    # Playful styles, shown behind the "Fun voices" toggle.
    "wizard": "A very old man with a deep, resonant bass voice, gravelly and weathered with age. Slow, grand and deliberate, with long dramatic pauses and commanding, kindly authority; rising now and then to thunderous emphasis.",
    "naturalist": "An elderly English gentleman with a soft, breathy, slightly husky low voice. A hushed near-whisper full of quiet wonder, gentle rising intonation and careful pauses, as if trying not to disturb a wild creature close by.",
    "hype": "A raspy, ferociously intense wrestling-promo narrator. Growling, drawn-out vowels, explosive emphasis and wild swings in volume, delivering every line like a championship challenge.",
    "spy": "A flamboyant 1960s swinging-London spy narrator. Cheeky, smooth and thoroughly pleased with himself, with a groovy lilt, playful innuendo in the delivery and bursts of giddy excitement.",
}

# Styles that bring their own speaker: a Breeze-made clip (anchors/<key>.wav) and its exact words.
# A clip the user adds takes precedence; the written direction still guides delivery.
ANCHORS = PROJECT / "anchors"
PRESET_ANCHORS = {
    "wizard": "Listen closely, for the old roads remember what the young have long forgotten, and the night is not yet over.",
    "naturalist": "Here, in the stillness of the forest floor, something quite extraordinary is about to happen.",
}


def estimate_narration(samples: list[tuple[int, float]], total: int, done: int) -> int | None:
    """Provisional remaining synthesis time from the first measurement; encoding extra."""
    remaining = total - done
    if not samples or remaining <= 0:
        return None
    measured_chars = sum(chars for chars, _ in samples)
    measured_seconds = sum(seconds for _, seconds in samples)
    if measured_chars <= 0 or measured_seconds <= 0:
        return None
    return max(1, round(measured_seconds * remaining / measured_chars))


def load_narration_timings(output: Path) -> list[tuple[int, float]]:
    """Small local, same-voice timing history; never infer timing from cached audio."""
    path = output / "narration-timings.json"
    try:
        if path.is_symlink() or path.stat().st_size > 16384:
            return []
        data = json.loads(path.read_text())
        if not isinstance(data, list):
            return []
        return [
            (chars, seconds)
            for chars, seconds in data[-20:]
            if type(chars) is int
            and chars > 0
            and type(seconds) in (int, float)
            and math.isfinite(seconds)
            and seconds > 0
        ]
    except (OSError, ValueError, TypeError):
        return []


def save_narration_timings(output: Path, samples: list[tuple[int, float]]) -> None:
    """Best-effort atomic metadata; a timing write must not abort saved narration."""
    path = output / "narration-timings.json"
    temp = output / ".narration-timings.tmp"
    try:
        if path.is_symlink() or temp.is_symlink():
            return
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(samples[-20:], stream)
        os.replace(temp, path)
    except OSError:
        pass


class JobRequest(BaseModel):
    book_id: str
    style: str = Field(min_length=3, max_length=800)
    mode: str
    chapter: int = 1
    passage: int = 1
    reference_id: str | None = None
    reference_text: str | None = Field(default=None, max_length=2000)
    preset: str | None = Field(default=None, max_length=40)
    allow_low_memory: bool = False


class SetupRequest(BaseModel):
    accepted: bool = False
    license_sha256: str = Field(min_length=1, max_length=64)


def startup_readiness() -> dict:
    """Installation requirements only; per-run pressure checks stay in the worker."""
    supported = platform.system() == "Darwin" and platform.machine() == "arm64"
    blockers = []
    total = None
    if not supported:
        blockers.append(
            "Embervoice requires an Apple Silicon Mac (M1 or later). Intel Macs, Windows and Linux are not supported."
        )
    else:
        version = platform.mac_ver()[0]
        if not version or int(version.split(".")[0]) < 26:
            blockers.append("The pinned MLX runtime requires macOS 26 or later. Update macOS before installing.")
        try:
            total = int(subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True, timeout=5)) / 2**30
            if total < 12:
                blockers.append(
                    "This computer has too little RAM. Use a Mac with at least 16 GB; 8 GB Macs cannot narrate safely."
                )
        except (OSError, ValueError, subprocess.SubprocessError):
            blockers.append("Could not check this computer's RAM. Restart the app and try again.")
    tools = {name: bool(shutil.which(name)) for name in ("ffmpeg", "ffprobe")}
    if not all(tools.values()):
        if getattr(sys, "frozen", False):
            blockers.append(
                "The bundled ffmpeg/ffprobe tools are missing. Download a complete Embervoice Mac app ZIP and reopen it."
            )
        else:
            blockers.append("Install ffmpeg and ffprobe, then restart the app: brew install ffmpeg")
    return {"supported": supported, "total_gib": total, "blockers": blockers, **tools}


class RetryRequest(BaseModel):
    chapter: int
    passage: int
    text: str = Field(min_length=1, max_length=320)


def _reference_info(path: Path) -> dict:
    """Accept a small clean spoken sample, not merely a file with an audio extension."""
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
            raise ValueError("Reference audio must decode to mono 24 kHz PCM")
        frames = wav.getnframes()
        duration = frames / 24000
        if not 2 <= duration <= 30:
            raise ValueError("Choose 2–30 seconds of clean speech")
        samples = array.array("h")
        samples.frombytes(wav.readframes(frames))
        if not samples or max(abs(value) for value in samples) < 100:
            raise ValueError("Reference audio is silent or too quiet")
    return {"duration_seconds": round(duration, 1)}


def _book_info(source: Path, book_id: str) -> dict:
    book = extract_book(source)
    if not book.language.startswith(("en", "zh")):
        raise ValueError(f"Local model supports English/Chinese, not {book.language}")
    return {
        "id": book_id,
        "title": book.title,
        "author": book.author,
        "language": book.language,
        "chapters": [
            {"title": c.title, "characters": len(c.text), "passages": len(split_passages(c.text))}
            for c in book.chapters
        ],
    }


def _valid_epub(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            files = archive.infolist()
            if len(files) > 10000 or sum(f.file_size for f in files) > MAX_UNPACKED:
                raise ValueError("EPUB is too large when unpacked")
            if "mimetype" not in archive.namelist() or archive.read("mimetype") != b"application/epub+zip":
                raise ValueError("Not an EPUB archive")
            if any(f.flag_bits & 1 for f in files):
                raise ValueError("Encrypted EPUBs are not supported")
    except zipfile.BadZipFile as exc:
        raise ValueError("File is not a valid EPUB archive") from exc


class LowMemoryError(RuntimeError):
    """Below the recommended free memory but above the hard limits: the user may opt into low-memory mode."""


def default_runner(
    source: Path,
    output: Path,
    style: str,
    preview_chapter: int | None = None,
    *,
    preview_passage: int = 1,
    ref_audio: Path | None = None,
    ref_text: str | None = None,
    correction: dict | None = None,
    allow_low_memory: bool = False,
    on_process=None,
    on_line=None,
):
    cmd = worker_command("audiobook", [str(source), "--output", str(output), "--style", style, "--quiet-summary"])
    if allow_low_memory:
        cmd.append("--allow-low-memory")
    if preview_chapter is not None:
        cmd += [
            "--start-chapter",
            str(preview_chapter),
            "--max-chapters",
            str(preview_chapter),
            "--max-passages",
            "1",
            "--preview-passage",
            str(preview_passage),
        ]
    if correction is not None:
        output.mkdir(parents=True, exist_ok=True)
        corrected = output / "corrected-passage.txt"
        if corrected.is_symlink():
            raise ValueError("Unsafe corrected passage file")
        if corrected.exists():
            if corrected.read_text(encoding="utf-8") != correction["retry_text"]:
                raise ValueError("Corrected text changed in this resumable version")
        else:
            temp = output / (".corrected-" + uuid.uuid4().hex + ".tmp")
            try:
                with os.fdopen(
                    os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w", encoding="utf-8"
                ) as stream:
                    stream.write(correction["retry_text"])
                os.replace(temp, corrected)
            finally:
                temp.unlink(missing_ok=True)
        cmd += [
            "--base-output",
            str(correction["base_output"]),
            "--retry-chapter",
            str(correction["retry_chapter"]),
            "--retry-passage",
            str(correction["retry_passage"]),
            "--retry-attempt",
            str(correction["retry_attempt"]),
            "--retry-text-file",
            str(corrected),
            "--max-chars",
            str(correction["max_chars"]),
        ]
    if ref_audio is not None:
        # Keep the transcript out of ps/argv; the output version is already isolated.
        output.mkdir(parents=True, exist_ok=True)
        transcript = output / ".reference-text"
        fd = os.open(transcript, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(ref_text)
        cmd += ["--reference-audio", str(ref_audio), "--reference-text-file", str(transcript)]
    env = {
        **os.environ,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PYTHONUNBUFFERED": "1",
        "AUDIOBOOK_SETUP_DIR": str(output.parents[3] / "setup"),
    }
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    proc = subprocess.Popen(
        cmd,
        cwd=PROJECT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    if on_process:
        on_process(proc)
    lines = []
    for line in proc.stdout:
        lines.append(line.rstrip()[:500])
        lines = lines[-20:]
        if on_line:
            on_line(lines[-1])
    code = proc.wait()
    if code:
        if any("Swap pressure is too high for local inference" in line for line in lines):
            raise RuntimeError(
                "Narration paused: swap usage exceeded the 90% safety limit. "
                "Your saved passages are intact. Wait for host pressure to ease, "
                "then retry with the same book and voice direction to resume. "
                "This does not mean the model cannot fit in this computer's memory."
            )
        low = next(
            (m for line in lines if (m := re.search(r"Low memory: ([\d.]+) GiB free of ([\d.]+) GiB", line))), None
        )
        if low:
            raise LowMemoryError(
                f"Only {low.group(1)} GiB of memory (RAM) is free; {low.group(2)} GiB is "
                "recommended: about 8.5 GiB for the voice model plus a 4 GiB reserve so the "
                "rest of the computer stays responsive. You can run in low-memory mode: "
                "narration may be much slower and other apps may feel sluggish, and it stops "
                "safely before the next passage if memory gets critical. Or close other apps "
                "and try again. Saved passages are intact either way."
            )
        hard = next((m for line in lines if (m := re.search(r"Memory hard limit: (.+)", line))), None)
        if hard:
            raise RuntimeError(
                f"Narration can't start: {hard.group(1).rstrip('.')}. This is a hard limit "
                "because running anyway would likely exhaust memory. Saved passages are intact."
            )
        if any("Memory pressure critical" in line for line in lines):
            raise RuntimeError(
                "Narration stopped: this computer's memory pressure became critical, so it "
                "stopped before the next passage. Saved passages are intact; close other apps, "
                "then generate again with the same settings to resume."
            )
        if any("A competing MLX job is running" in line for line in lines):
            raise RuntimeError(
                "Narration paused: another MLX job is running. Saved passages are intact; retry when it finishes."
            )
        raise RuntimeError("Conversion stopped (exit %s): %s" % (code, " | ".join(lines[-6:])))


def create_app(store: Path | None = None, runner=None, *, setup=None, readiness=None) -> FastAPI:
    root = Path(store or Path.home() / "Audiobooks" / "Studio").expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    from model_setup import ModelSetup, licence_text

    run = runner or default_runner
    enforce_setup = runner is None or setup is not None
    model_setup = setup if setup is not None else ModelSetup(root / "setup")
    inspect_readiness = readiness or startup_readiness

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            stop_own_workers()
            model_setup.close()

    app = FastAPI(title="Embervoice", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def require_environment():
        blockers = inspect_readiness()["blockers"]
        if blockers:
            raise HTTPException(409, " ".join(blockers))

    @app.get("/api/setup")
    def setup_status():
        environment = inspect_readiness()
        status = model_setup.poll() if hasattr(model_setup, "poll") else model_setup.status()
        return {
            **status,
            "model_ready": status["ready"],
            "ready": status["ready"] and not environment["blockers"],
            "environment": environment,
        }

    @app.get("/api/setup/license")
    def setup_license():
        return {"text": licence_text(), "license_sha256": model_setup.status()["license_sha256"]}

    @app.post("/api/setup/start")
    def setup_start(req: SetupRequest):
        require_environment()
        try:
            return model_setup.start(req.accepted, req.license_sha256)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/setup/cancel")
    def setup_cancel():
        return model_setup.cancel()

    @app.get("/setup.js")
    def setup_script():
        return FileResponse(PROJECT / "web" / "setup.js", media_type="text/javascript")

    jobs: dict[str, dict] = {}
    lock = threading.Lock()
    processes = {}

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"detail": "Local access only"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in ("http://" + request.headers.get("host", ""),):
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    def book_dir(book_id: str) -> Path:
        if not ID.fullmatch(book_id):
            raise HTTPException(404, "Unknown book")
        target = root / "books" / book_id
        if not (target / "info.json").is_file():
            raise HTTPException(404, "Unknown book")
        return target

    def version_dir(book_id: str, version_id: str) -> Path:
        if not VERSION_ID.fullmatch(version_id):
            raise HTTPException(404, "Unknown voice version")
        path = book_dir(book_id) / "versions" / version_id
        if path.is_symlink() or (path / "manifest.json").is_symlink() or not (path / "manifest.json").is_file():
            raise HTTPException(404, "Unknown voice version")
        return path

    def version_summary(book_id: str, version_id: str, info: dict) -> dict:
        path = version_dir(book_id, version_id)
        manifest = json.loads((path / "manifest.json").read_text())
        wavs = [
            p
            for p in (path / "passages").glob("*.wav")
            if PASSAGE_FILE.fullmatch(p.name) and p.is_file() and not p.is_symlink()
        ]
        chapters = []
        for index, part in enumerate(manifest["chapters"], 1):
            matches = [p for p in (path / "chapters").glob(f"{index:03d}-*.m4a") if p.is_file() and not p.is_symlink()]
            if matches:
                label = chapter_label(Chapter(part["title"], "", part["source"]))
                chapters.append(
                    {
                        "title": label,
                        "name": matches[0].name,
                        "url": f"/api/books/{book_id}/versions/{version_id}/files/chapters/{quote(matches[0].name)}",
                    }
                )
        books = [p for p in path.glob("*.m4b") if p.is_file() and not p.is_symlink()]
        book_url = f"/api/books/{book_id}/versions/{version_id}/files/book/{quote(books[0].name)}" if books else None
        total = sum(part["passages"] for part in info["chapters"])
        return {
            "id": version_id,
            "passages_done": len(wavs),
            "passages_total": total,
            "complete": bool(book_url and len(chapters) == len(info["chapters"]) and len(wavs) == total),
            "chapters": chapters,
            "book_url": book_url,
            "updated_at": path.stat().st_mtime,
            "retry": manifest.get("retry"),
        }

    def snapshot(job: dict) -> dict:
        out = root / "books" / job["book_id"] / "versions" / job["version"]
        wavs = sorted(
            p
            for p in (out / "passages").glob("*.wav")
            if PASSAGE_FILE.fullmatch(p.name) and p.is_file() and not p.is_symlink()
        )
        chapters = sorted((out / "chapters").glob("[0-9][0-9][0-9]-*.m4a"))
        book_file = next(iter(sorted(out.glob("*.m4b"))), None)
        prefix = "/api/jobs/" + job["id"] + "/files/"
        selected = [p for p in wavs if p.name == f"{job['chapter']:03d}-{job['passage']:04d}.wav"]
        samples = job["timings"] or job["prior_timings"]
        eta = (
            estimate_narration(samples, job["total_characters"], job["done_characters"])
            if job["mode"] == "full" and job["state"] == "running" and job["phase"] not in ("assembling", "ready")
            else None
        )
        return {
            "id": job["id"],
            "book_id": job["book_id"],
            "version": job["version"],
            "mode": job["mode"],
            "state": job["state"],
            "error": job.get("error"),
            "error_code": job.get("error_code"),
            "low_memory": job.get("low_memory", False),
            "log": job.get("log", [])[-12:],
            "phase": job["phase"],
            "stage_message": job["stage_message"],
            "started_at": job["started_at"],
            "eta_narration_seconds": eta,
            "eta_total_seconds": max(0, round(time.time() - job["started_at"])) + eta if eta is not None else None,
            "eta_samples": len(samples),
            "eta_basis": "current" if job["timings"] else "previous" if samples else None,
            "passages_total": job["total_passages"],
            "current_chapter": job["active_chapter"],
            "current_passage_index": job["active_passage"],
            "passages_done": len(wavs),
            "chapters_done": len(chapters),
            "preview_url": prefix + "passages/" + quote(selected[0].name) if selected else None,
            "chapters": [{"name": p.name, "url": prefix + "chapters/" + quote(p.name)} for p in chapters],
            "book_url": prefix + "book/" + quote(book_file.name) if book_file else None,
        }

    @app.get("/")
    def index():
        return FileResponse(PROJECT / "web" / "index.html", media_type="text/html")

    @app.get("/style.css")
    def css():
        return FileResponse(PROJECT / "web" / "style.css", media_type="text/css")

    @app.get("/app.js")
    def javascript():
        return FileResponse(PROJECT / "web" / "app.js", media_type="text/javascript")

    @app.get("/voices/{name}")
    def voice_sample(name: str):
        # Pre-made, shipped samples of each preset direction; only known preset names resolve.
        key = name.removesuffix(".m4a")
        path = PROJECT / "web" / "voices" / f"{key}.m4a"
        if not name.endswith(".m4a") or key not in PRESETS or not path.is_file():
            raise HTTPException(404, "Unknown voice sample")
        return FileResponse(path, media_type="audio/mp4")

    @app.get("/api/presets")
    def presets():
        return PRESETS

    @app.post("/api/books")
    async def upload(file: UploadFile = File(...)):
        if not file.filename or not file.filename.lower().endswith(".epub"):
            raise HTTPException(400, "Choose an .epub file")
        book_id = uuid.uuid4().hex
        target = root / "books" / book_id
        target.mkdir(parents=True)
        source = target / "book.epub"
        size = 0
        try:
            with source.open("wb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise ValueError("EPUB exceeds the 50 MiB upload limit")
                    stream.write(chunk)
            _valid_epub(source)
            info = _book_info(source, book_id)
            (target / "info.json").write_text(json.dumps(info, ensure_ascii=False))
            return info
        except Exception as exc:
            source.unlink(missing_ok=True)
            try:
                target.rmdir()
            except OSError:
                pass
            raise HTTPException(400, str(exc)) from exc
        finally:
            await file.close()

    @app.get("/api/books/{book_id}")
    def get_book(book_id: str):
        return json.loads((book_dir(book_id) / "info.json").read_text())

    @app.get("/api/library")
    def library():
        directory = root / "books"
        books = []
        if directory.is_dir():
            for folder in directory.iterdir():
                if not ID.fullmatch(folder.name) or folder.is_symlink() or not (folder / "info.json").is_file():
                    continue
                info = json.loads((folder / "info.json").read_text())
                versions = folder / "versions"
                count = (
                    sum(
                        1
                        for part in versions.iterdir()
                        if VERSION_ID.fullmatch(part.name)
                        and not part.is_symlink()
                        and (part / "manifest.json").is_file()
                        and version_summary(folder.name, part.name, info)["complete"]
                    )
                    if versions.is_dir()
                    else 0
                )
                if not count:
                    continue  # Uploads, previews and interrupted runs are not finished audiobooks.
                books.append(
                    {
                        "id": folder.name,
                        "title": info["title"],
                        "author": info["author"],
                        "sections": len(info["chapters"]),
                        "versions": count,
                        "updated_at": folder.stat().st_mtime,
                    }
                )
        books.sort(key=lambda item: item["updated_at"], reverse=True)
        return {"books": books}

    @app.get("/api/books/{book_id}/versions")
    def versions(book_id: str):
        info = get_book(book_id)
        directory = book_dir(book_id) / "versions"
        result = []
        if directory.is_dir():
            for folder in directory.iterdir():
                if (
                    VERSION_ID.fullmatch(folder.name)
                    and not folder.is_symlink()
                    and (folder / "manifest.json").is_file()
                ):
                    result.append(version_summary(book_id, folder.name, info))
        result.sort(key=lambda item: item["updated_at"], reverse=True)
        return {"versions": result}

    @app.get("/api/books/{book_id}/versions/{version_id}/files/{kind}/{name}")
    def saved_audio(book_id: str, version_id: str, kind: str, name: str):
        out = version_dir(book_id, version_id)
        if kind not in ("chapters", "book") or name != Path(name).name or name.startswith("."):
            raise HTTPException(404, "Unknown audio")
        location = out / ("chapters" if kind == "chapters" else "") / name
        if (
            location.suffix != (".m4a" if kind == "chapters" else ".m4b")
            or not location.is_file()
            or location.is_symlink()
        ):
            raise HTTPException(404, "Unknown audio")
        return FileResponse(location, media_type="audio/mp4", filename=name if kind == "book" else None)

    @app.get("/api/books/{book_id}/chapters/{chapter}/passages")
    def chapter_passages(book_id: str, chapter: int, version: str | None = None):
        book = extract_book(book_dir(book_id) / "book.epub")
        if not 1 <= chapter <= len(book.chapters):
            raise HTTPException(400, "Unknown chapter")
        manifest = json.loads((version_dir(book_id, version) / "manifest.json").read_text()) if version else None
        parts = split_passages(book.chapters[chapter - 1].text, manifest["max_chars"] if manifest else 320)
        if version is not None:
            parts = [
                manifest.get("corrections", {}).get(f"{chapter:03d}-{i:04d}", part) for i, part in enumerate(parts, 1)
            ]
        return {
            "chapter": chapter,
            "passages": [
                {
                    "index": i,
                    "characters": len(text),
                    "text": text,
                    "excerpt": text[:140] + ("…" if len(text) > 140 else ""),
                }
                for i, text in enumerate(parts, 1)
            ],
        }

    def reference(book_id: str, reference_id: str) -> tuple[Path, dict]:
        if not REF_ID.fullmatch(reference_id):
            raise HTTPException(404, "Unknown voice clip")
        folder = book_dir(book_id) / "references"
        audio, metadata = folder / (reference_id + ".wav"), folder / (reference_id + ".json")
        if not audio.is_file() or audio.is_symlink() or not metadata.is_file() or metadata.is_symlink():
            raise HTTPException(404, "Unknown voice clip")
        if hashlib.sha256(audio.read_bytes()).hexdigest() != reference_id:
            raise HTTPException(400, "Voice clip changed on disk; choose it again")
        return audio, json.loads(metadata.read_text())

    def adopt_anchor(book_id: str, key: str) -> tuple[str, str]:
        """Store a style's built-in clip with the book's clips, so versions, resume and retry treat it alike."""
        data = (ANCHORS / f"{key}.wav").read_bytes()
        reference_id = hashlib.sha256(data).hexdigest()
        target = book_dir(book_id) / "references"
        target.mkdir(exist_ok=True)
        final = target / (reference_id + ".wav")
        if final.is_symlink():
            raise HTTPException(409, "Unsafe stored voice clip")
        if not final.exists():
            temp = target / (uuid.uuid4().hex + ".partial.wav")
            try:
                with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                    stream.write(data)
                os.replace(temp, final)
            finally:
                temp.unlink(missing_ok=True)
        meta_path = target / (reference_id + ".json")
        if not meta_path.exists():
            meta_path.write_text(
                json.dumps({"id": reference_id, "name": f"{key} built-in voice", **_reference_info(final)})
            )
            os.chmod(meta_path, 0o600)
        return reference_id, PRESET_ANCHORS[key]

    @app.get("/api/books/{book_id}/references/{reference_id}")
    def get_reference(book_id: str, reference_id: str):
        return reference(book_id, reference_id)[1]

    @app.post("/api/books/{book_id}/references")
    async def upload_reference(book_id: str, file: UploadFile = File(...)):
        if enforce_setup:
            require_environment()
        target = book_dir(book_id) / "references"
        name = re.split(r"[/\\]", file.filename or "")[-1][:100]
        suffix = Path(name).suffix.lower()
        if suffix not in (".wav", ".mp3", ".m4a", ".flac", ".webm", ".ogg"):
            raise HTTPException(400, "Choose a WAV, MP3, M4A, FLAC, WebM or OGG voice clip")
        target.mkdir(exist_ok=True)
        source = target / (uuid.uuid4().hex + suffix)
        converted = target / (uuid.uuid4().hex + ".partial.wav")
        try:
            size = 0
            with os.fdopen(os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_REFERENCE:
                        raise ValueError("Voice clip exceeds the 10 MiB limit")
                    stream.write(chunk)
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(source),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if probe.returncode or not probe.stdout.strip():
                raise ValueError("Could not read the voice clip")
            duration = float(probe.stdout.strip())
            if not 2 <= duration <= 30:
                raise ValueError("Choose 2–30 seconds of clean speech")
            # ffmpeg truncates this existing private file rather than creating a public temp.
            os.close(os.open(converted, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-map",
                    "0:a:0",
                    "-ac",
                    "1",
                    "-ar",
                    "24000",
                    "-c:a",
                    "pcm_s16le",
                    str(converted),
                ],
                check=True,
                capture_output=True,
                timeout=40,
            )
            quality = _reference_info(converted)
            reference_id = hashlib.sha256(converted.read_bytes()).hexdigest()
            final = target / (reference_id + ".wav")
            metadata = {"id": reference_id, "name": name, **quality}
            if final.is_symlink() or (
                final.exists() and hashlib.sha256(final.read_bytes()).hexdigest() != reference_id
            ):
                raise ValueError("Stored voice clip changed; choose another file")
            if not final.exists():
                os.replace(converted, final)
                os.chmod(final, 0o600)
            meta_path = target / (reference_id + ".json")
            if not meta_path.exists():
                meta_path.write_text(json.dumps(metadata, ensure_ascii=False))
                os.chmod(meta_path, 0o600)
            return json.loads(meta_path.read_text())
        except (ValueError, subprocess.SubprocessError, wave.Error, OSError) as exc:
            raise HTTPException(400, str(exc)[:200]) from exc
        finally:
            source.unlink(missing_ok=True)
            converted.unlink(missing_ok=True)
            await file.close()

    @app.post("/api/jobs")
    def start(req: JobRequest):
        return _start(req)

    @app.post("/api/books/{book_id}/versions/{version_id}/retry")
    def retry_passage(book_id: str, version_id: str, req: RetryRequest):
        info = get_book(book_id)
        if (
            not 1 <= req.chapter <= len(info["chapters"])
            or not 1 <= req.passage <= info["chapters"][req.chapter - 1]["passages"]
        ):
            raise HTTPException(400, "Choose an existing passage")
        base = version_dir(book_id, version_id)
        if not version_summary(book_id, version_id, info)["complete"]:
            raise HTTPException(409, "Finish the base audiobook before correcting a passage")
        manifest = json.loads((base / "manifest.json").read_text())
        text = re.sub(r"\s+", " ", req.text).strip()
        if not text or len(text) > manifest["max_chars"]:
            raise HTTPException(400, f"Corrected passage must contain 1–{manifest['max_chars']} characters")
        ref_id = manifest["reference_sha256"]
        if ref_id:
            reference(book_id, ref_id)
        attempt = 0
        while True:
            identity = json.dumps([version_id, req.chapter, req.passage, text, attempt], ensure_ascii=False)
            new_version = hashlib.sha256(identity.encode()).hexdigest()[:16]
            candidate = book_dir(book_id) / "versions" / new_version
            if candidate.is_symlink():
                raise HTTPException(409, "Unsafe existing voice version path")
            if not (candidate / "manifest.json").exists():
                break
            if version_summary(book_id, new_version, info)["complete"]:
                attempt += 1
                continue
            break  # A failed/incomplete attempt is resumed with the same identity.
        request = JobRequest(
            book_id=book_id,
            style=manifest["style"],
            mode="full",
            chapter=req.chapter,
            reference_id=ref_id,
            reference_text=manifest["reference_text"],
        )
        correction = {
            "base_output": base,
            "retry_chapter": req.chapter,
            "retry_passage": req.passage,
            "retry_text": text,
            "retry_attempt": attempt,
            "max_chars": manifest["max_chars"],
            "version": new_version,
        }
        return _start(request, correction=correction)

    def _start(req: JobRequest, correction: dict | None = None):
        if enforce_setup:
            require_environment()
            try:
                model_setup.require_ready()
            except (RuntimeError, OSError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from exc
        target = book_dir(req.book_id)
        info = json.loads((target / "info.json").read_text())
        style = req.style.strip()
        if req.mode not in ("preview", "full") or not 1 <= req.chapter <= len(info["chapters"]):
            raise HTTPException(400, "Invalid mode or chapter")
        if (
            not 1 <= req.passage <= info["chapters"][req.chapter - 1]["passages"]
            or req.mode == "full"
            and req.passage != 1
        ):
            raise HTTPException(400, "Invalid preview passage")
        if bool(req.reference_id) != bool(req.reference_text and req.reference_text.strip()):
            raise HTTPException(400, "A voice clip requires its exact spoken transcript, and vice versa")
        ref_audio, ref_text, ref_id = None, None, req.reference_id
        if ref_id:
            ref_text = req.reference_text.strip()
        elif req.preset in PRESET_ANCHORS and correction is None:
            ref_id, ref_text = adopt_anchor(req.book_id, req.preset)
        if ref_id:
            ref_audio, _ = reference(req.book_id, ref_id)
        # Preserve pre-existing style-only versions; clip versions include the clip, its words and strength.
        identity = style if not ref_audio else json.dumps([style, ref_id, ref_text, CLIP_CFG], ensure_ascii=False)
        version = correction["version"] if correction else hashlib.sha256(identity.encode()).hexdigest()[:16]
        out = target / "versions" / version
        parsed = extract_book(target / "book.epub")
        passage_chars = {
            (ci, pi): len(text)
            for ci, chapter in enumerate(parsed.chapters, 1)
            for pi, text in enumerate(split_passages(chapter.text, correction["max_chars"] if correction else 320), 1)
        }
        cached = {
            key
            for key in passage_chars
            if (out / "passages" / f"{key[0]:03d}-{key[1]:04d}.wav").is_file()
            and not (out / "passages" / f"{key[0]:03d}-{key[1]:04d}.wav").is_symlink()
        }
        job = {
            "id": uuid.uuid4().hex,
            "book_id": req.book_id,
            "version": version,
            "chapter": req.chapter,
            "passage": req.passage,
            "mode": req.mode,
            "state": "running",
            "low_memory": req.allow_low_memory,
            "phase": "preparing",
            "started_at": time.time(),
            "stage_message": "Preparing local model and checking host resources…",
            "log": ["Preparing local model and checking host resources…"],
            "total_characters": sum(passage_chars.values()),
            "total_passages": len(passage_chars),
            "done_characters": sum(passage_chars[key] for key in cached),
            "timings": [],
            "prior_timings": load_narration_timings(out),
            "seen_passages": cached,
            "active_chapter": req.chapter if req.mode == "preview" else 1,
            "active_passage": req.passage if req.mode == "preview" else 1,
            "current_passage": None,
        }
        with lock:
            if any(j["state"] in ("running", "cancelling") for j in jobs.values()):
                raise HTTPException(409, "Another narration is running. Cancel or wait before starting a new job.")
            jobs[job["id"]] = job

        def on_proc(proc):
            with lock:
                processes[job["id"]] = proc

        def on_line(line):
            with lock:
                if line.startswith("STUDIO_EVENT "):
                    try:
                        event = json.loads(line[len("STUDIO_EVENT ") :])
                        stage, message = event["stage"], event["message"]
                        if stage not in (
                            "planning",
                            "loading",
                            "synthesizing",
                            "saved",
                            "reused",
                            "encoding",
                            "assembling",
                            "ready",
                        ) or not isinstance(message, str):
                            raise ValueError("Invalid stage event")
                        job["phase"] = stage
                        job["stage_message"] = message[:180]
                        line = message[:180]
                        if stage == "planning" and type(event.get("total_characters")) is int:
                            job["total_characters"] = max(0, event["total_characters"])
                        if stage == "planning" and type(event.get("total_passages")) is int:
                            job["total_passages"] = max(0, event["total_passages"])
                        key = (event.get("chapter"), event.get("passage"))
                        chars = event.get("characters")
                        valid_passage = all(type(n) is int and n > 0 for n in key) and type(chars) is int and chars > 0
                        if valid_passage:
                            job["active_chapter"], job["active_passage"] = key
                        if stage == "synthesizing" and valid_passage:
                            job["current_passage"] = (key, elapsed_clock())
                        elif stage in ("saved", "reused") and valid_passage:
                            if key not in job["seen_passages"]:
                                job["seen_passages"].add(key)
                                job["done_characters"] += chars
                                current = job["current_passage"]
                                if stage == "saved" and current and current[0] == key:
                                    duration = elapsed_clock() - current[1]
                                    if duration > 0:
                                        job["timings"].append((chars, duration))
                                        save_narration_timings(out, job["prior_timings"] + job["timings"])
                            job["current_passage"] = None
                    except (ValueError, KeyError, TypeError):
                        line = "Worker sent an unreadable progress event"
                elif line.startswith("Generated chapter "):
                    return  # Structured saved event already records this without a file path.
                job["log"] = (job["log"] + [line])[-20:]

        def work():
            try:
                if run is default_runner:
                    kwargs = {"ref_audio": ref_audio, "ref_text": ref_text} if ref_audio else {}
                    if correction is not None:
                        kwargs["correction"] = correction
                    if req.allow_low_memory:
                        kwargs["allow_low_memory"] = True
                    if req.mode == "preview" and "preview_passage" in inspect.signature(run).parameters:
                        kwargs["preview_passage"] = req.passage
                    run(
                        target / "book.epub",
                        out,
                        style,
                        req.chapter if req.mode == "preview" else None,
                        on_process=on_proc,
                        on_line=on_line,
                        **kwargs,
                    )
                else:
                    kwargs = {"ref_audio": ref_audio, "ref_text": ref_text} if ref_audio else {}
                    if correction is not None:
                        kwargs.update(
                            {
                                "base_output": correction["base_output"],
                                "retry_chapter": correction["retry_chapter"],
                                "retry_passage": correction["retry_passage"],
                                "retry_text": correction["retry_text"],
                                "retry_attempt": correction["retry_attempt"],
                                "max_chars": correction["max_chars"],
                            }
                        )
                    if req.mode == "preview" and req.passage != 1:
                        kwargs["preview_passage"] = req.passage
                    if req.allow_low_memory:
                        kwargs["allow_low_memory"] = True
                    run(target / "book.epub", out, style, req.chapter if req.mode == "preview" else None, **kwargs)
                with lock:
                    job["state"] = "cancelled" if job["state"] == "cancelling" else "complete"
            except Exception as exc:
                with lock:
                    if job["state"] == "cancelling":
                        job["state"] = "cancelled"
                    elif job["state"] == "running":
                        job["state"] = "error"
                        job["error"] = str(exc)[-1500:]
                        if isinstance(exc, LowMemoryError):
                            job["error_code"] = "low_memory"
            finally:
                with lock:
                    processes.pop(job["id"], None)

        threading.Thread(target=work, name="audiobook-studio-job", daemon=True).start()
        return snapshot(job)

    @app.get("/api/jobs/{job_id}")
    def status(job_id: str):
        with lock:
            job = jobs.get(job_id)
            if not job:
                raise HTTPException(404, "Unknown job")
            return snapshot(job)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        with lock:
            job = jobs.get(job_id)
            if not job:
                raise HTTPException(404, "Unknown job")
            proc = processes.get(job_id)
            if job["state"] != "running":
                return snapshot(job)
            if proc is None and run is default_runner:
                raise HTTPException(409, "Worker is still starting; retry shortly")
            job["state"] = "cancelling"
            if proc and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
            return snapshot(job)

    @app.get("/api/jobs/{job_id}/files/{kind}/{name}")
    def audio(job_id: str, kind: str, name: str):
        with lock:
            job = jobs.get(job_id)
            if not job:
                raise HTTPException(404, "Unknown job")
            if kind not in ("passages", "chapters", "book") or name != Path(name).name or name.startswith("."):
                raise HTTPException(404, "Unknown audio")
            out = book_dir(job["book_id"]) / "versions" / job["version"]
            location = out / (kind if kind != "book" else "") / name
            ext = {"passages": ".wav", "chapters": ".m4a", "book": ".m4b"}[kind]
            if location.suffix != ext or not location.is_file() or location.is_symlink():
                raise HTTPException(404, "Unknown audio")
            return FileResponse(
                location,
                media_type={".wav": "audio/wav", ".m4a": "audio/mp4", ".m4b": "audio/mp4"}[ext],
                filename=name if kind == "book" else None,
            )

    def stop_own_workers():
        # Snapshot while locked, but never block worker bookkeeping during reaping.
        with lock:
            owned = tuple(processes.values())
        for proc in owned:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 1.5
        for proc in owned:
            try:
                proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=1)

    atexit.register(stop_own_workers)
    return app


def main():
    from launcher import main as launch

    launch()


if __name__ == "__main__":
    main()
