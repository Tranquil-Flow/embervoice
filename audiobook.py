"""Local EPUB -> chapter M4A and chapter-marked M4B. No network or playback."""

from __future__ import annotations

import argparse
import array
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT, epub

from model_setup import ModelSetup, resolve_model_path

MODEL = resolve_model_path()
SAMPLE_RATE = 24000
STYLE_CFG = 4
# A voice clip sets the speaker. The written direction is still given but not amplified, so it can't pull the
# voice away from the clip; measured closest to the clip, and it skips the second guidance pass, so it's faster.
CLIP_CFG = 1


@contextmanager
def gpu_slot():
    """One local Breeze CLI/GUI process at a time, even for different books."""
    path = Path.home() / ".cache" / "audiobook-studio-gpu.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Embervoice narration holds the GPU lane") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass(frozen=True)
class Chapter:
    title: str
    text: str
    source: str


@dataclass(frozen=True)
class Book:
    title: str
    author: str
    language: str
    chapters: tuple[Chapter, ...]


def chapter_label(chapter: Chapter) -> str:
    """Distinguish a front-matter title page from the work it introduces."""
    name = Path(chapter.source).stem.casefold().replace("-", "").replace("_", "")
    return "Title page" if name == "titlepage" else chapter.title


def _clean(text: str) -> str:
    return " ".join(text.split())


def _slug(text: str) -> str:
    return re.sub(r"[^\w.-]+", "-", text, flags=re.UNICODE).strip(".-_")[:90] or "book"


def _toc_entries(nodes):
    for node in nodes:
        if isinstance(node, (tuple, list)):
            yield from _toc_entries(node)
        elif hasattr(node, "href"):
            yield (
                unquote(node.href.split("#")[0]),
                unquote(node.href.split("#", 1)[1]) if "#" in node.href else "",
                _clean(node.title),
            )


def _content(item, toc):
    raw = item.get_content()
    soup = BeautifulSoup(raw, "xml" if raw.lstrip().startswith(b"<?xml") else "lxml")
    for tag in soup(["script", "style", "nav", "svg", "sup"]):
        tag.decompose()
    for tag in list(soup.select(".pagenum, .page-number, [epub\\:type='pagebreak'], [role='doc-pagebreak']")):
        tag.decompose()
    blocks = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote"]):
        if tag.find_parent(["p", "li", "blockquote"]):
            continue
        value = _clean(tag.get_text(" ", strip=True))
        if value:
            blocks.append((tag, value))
    if not blocks:
        return []
    sections = []
    for href, anchor, title in toc:
        if not anchor:
            sections.append((0, title))
            continue
        target = soup.find(id=anchor) or soup.find(attrs={"name": anchor})
        if target is None:
            continue
        for n, (block, _) in enumerate(blocks):
            if block is target or target in block.descendants:
                sections.append((n, title))
                break
            if target.find_parent(block.name) is block:
                sections.append((n, title))
                break
    # Deduplicate nested TOC labels at the same boundary and retain document order.
    sections = sorted({n: title for n, title in sections}.items())
    if not sections:
        sections = [(0, _clean(item.title) or blocks[0][1][:80])]
    if sections[0][0] > 0:
        sections.insert(0, (0, _clean(item.title) or "Front matter"))
    return [
        Chapter(title, "\n\n".join(v for _, v in blocks[start:end]), item.file_name)
        for (start, title), (end, _) in zip(sections, sections[1:] + [(len(blocks), "")])
        if start < end
    ]


def extract_book(source: Path) -> Book:
    source = Path(source)
    if source.suffix.lower() != ".epub" or not source.is_file():
        raise ValueError("Provide a readable .epub file")
    book = epub.read_epub(str(source))
    meta = lambda field: _clean(book.get_metadata("DC", field)[0][0]) if book.get_metadata("DC", field) else ""
    title = meta("title") or source.stem
    author = meta("creator") or "Unknown author"
    language = (meta("language") or "en").lower()
    chapters = []
    toc = list(_toc_entries(book.toc))
    for item_id, _ in book.spine:
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ITEM_DOCUMENT or item_id == "nav":
            continue
        name = unquote(item.file_name)
        own_toc = [(h, a, t) for h, a, t in toc if h == name or h.endswith("/" + name)]
        chapters.extend(_content(item, own_toc))
    if not chapters:
        raise ValueError("EPUB has no readable spine chapters (DRM or image-only books are unsupported)")
    return Book(title, author, language, tuple(chapters))


def split_passages(text: str, max_chars: int = 320) -> list[str]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    words = text.split()
    if any(len(word) > max_chars for word in words):
        raise ValueError("A word is too long for the passage limit")
    parts = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if len(candidate) > max_chars:
            parts.append(current)
            current = word
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _run(*args: str) -> None:
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _wav_ok(path: Path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError(f"Invalid audio layout: {path}")
        data = wav.readframes(wav.getnframes())
        samples = array.array("h")
        samples.frombytes(data)
        if not samples or max(abs(x) for x in samples) < 20:
            raise ValueError(f"Silent or empty audio: {path}")
        return wav.getnframes(), wav.getframerate()


# Measured 2026-10-07: the 8-bit model peaks at ~8.4 GiB of MLX memory while narrating a full passage.
MODEL_PEAK_GIB = 8.5
RECOMMENDED_FREE_GIB = MODEL_PEAK_GIB + 4  # plus a reserve so the rest of the computer stays responsive
MIN_FREE_GIB = 4  # below this even low-memory mode would push several GiB of other apps into swap at once
MIN_TOTAL_GIB = 12  # model peak plus a minimal OS working set; smaller machines would live in swap


def memory_snapshot() -> dict | None:
    """Reclaimable memory, physical RAM and swap on macOS; None where these readings are unavailable."""
    if os.uname().sysname != "Darwin":
        return None
    stat = subprocess.check_output(["vm_stat"], text=True)
    page = int(re.search(r"page size of (\d+)", stat).group(1))
    pages = lambda name: int(m.group(1)) if (m := re.search(rf"^Pages {name}:\s+(\d+)", stat, re.M)) else 0
    swap = subprocess.check_output(["sysctl", "vm.swapusage"], text=True)
    swap_total, swap_used = (float(x) for x in re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", swap).groups())
    return {
        "available_gib": sum(pages(n) for n in ("free", "inactive")) * page / 2**30,
        "total_gib": int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)) / 2**30,
        "swap_fraction": swap_used / swap_total if swap_total else 0.0,
    }


def check_memory(snapshot: dict | None, allow_low_memory: bool = False) -> None:
    """Hard limits refuse outright; below the recommended amount, narration needs the user's explicit opt-in."""
    if snapshot is None:
        return
    total, free = snapshot["total_gib"], snapshot["available_gib"]
    if total < MIN_TOTAL_GIB:
        raise RuntimeError(
            f"Memory hard limit: this computer has {total:.0f} GiB of RAM; narration needs at least {MIN_TOTAL_GIB} GiB"
        )
    if snapshot["swap_fraction"] > 0.9:
        raise RuntimeError("Swap pressure is too high for local inference")
    if free < MIN_FREE_GIB:
        raise RuntimeError(
            f"Memory hard limit: only {free:.1f} GiB free; at least {MIN_FREE_GIB} GiB is "
            "needed even in low-memory mode"
        )
    if free < RECOMMENDED_FREE_GIB and not allow_low_memory:
        raise RuntimeError(f"Low memory: {free:.1f} GiB free of {RECOMMENDED_FREE_GIB:g} GiB recommended")


def memory_pressure_critical() -> bool:
    """macOS's own pressure level (1 normal, 2 warning, 4 critical); unknown elsewhere."""
    if os.uname().sysname != "Darwin":
        return False
    try:
        level = subprocess.check_output(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], text=True)
        return int(level.strip()) >= 4
    except (OSError, subprocess.CalledProcessError, ValueError):
        return False


def mlx_synth(
    model_path: Path, ref_audio: Path | None = None, ref_text: str | None = None, allow_low_memory: bool = False
) -> Callable:
    # Library and model are loaded only when generation begins, not for plan/test.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    ModelSetup(
        Path(os.environ.get("AUDIOBOOK_SETUP_DIR", str(Path.home() / "Audiobooks" / "Studio" / "setup")))
    ).require_ready()
    check_memory(memory_snapshot(), allow_low_memory)
    if os.uname().sysname == "Darwin":
        processes = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
        for line in processes.splitlines():
            pid = line.strip().split(maxsplit=1)[0]
            if pid == str(os.getpid()):
                continue
            if re.search(r"(?:mlx_lm\.lora|mlx_lm\.generate)", line):
                raise RuntimeError("A competing MLX job is running; wait for it before narrating")
    import mlx.core as mx

    mx.set_memory_limit(8 * 2**30)
    mx.set_cache_limit(1 * 2**30)
    from mlx_audio.tts import load
    import numpy as np

    model = load(str(model_path))

    def synth(path: Path, text: str, style: str, seed: int) -> None:
        chunks = []
        for result in model.generate(
            text=text,
            instruct=style,
            cfg_scale=CLIP_CFG if ref_audio else STYLE_CFG,
            seed=seed,
            ref_audio=str(ref_audio) if ref_audio else None,
            ref_text=ref_text,
            max_tokens=1200,
            split_pattern=None,
        ):
            data = np.asarray(result.audio, dtype=np.float32).reshape(-1)
            if not np.isfinite(data).all():
                raise ValueError("Nonfinite model audio")
            if getattr(result, "token_count", 0) >= 1200:
                raise ValueError("Generation hit token cap; shorten the passage")
            if result.sample_rate != SAMPLE_RATE:
                raise ValueError(f"Unexpected sample rate: {result.sample_rate}")
            chunks.append(data)
        if not chunks:
            raise ValueError("Model produced no audio")
        audio = np.concatenate(chunks)
        if not len(audio) or float(np.max(np.abs(audio))) < 0.0007:
            raise ValueError("Model produced silence")
        audio = np.clip(audio, -1, 1)
        pcm = (audio * 32767).astype("<i2")
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm.tobytes())

    return synth


def _concat_file(paths: list[Path], dest: Path) -> None:
    # ffmpeg's concat list is generated in the output directory; no shell quoting.
    with tempfile.NamedTemporaryFile("w", suffix=".txt", dir=dest.parent, delete=False) as listing:
        listpath = Path(listing.name)
        silence = dest.parent / ".passage-pause.wav"
        with wave.open(str(silence), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(b"\x00\x00" * (SAMPLE_RATE // 4))
        for index, path in enumerate(paths):
            if index:
                listing.write("file '" + str(silence.resolve()).replace("'", "'\\''") + "'\n")
            listing.write("file '" + str(path.resolve()).replace("'", "'\\''") + "'\n")
    try:
        _run(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listpath),
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-ar",
            str(SAMPLE_RATE),
            str(dest),
        )
    finally:
        listpath.unlink(missing_ok=True)
        silence.unlink(missing_ok=True)


def _duration_ms(path: Path) -> int:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return round(float(result.stdout.strip()) * 1000)


def _ffmeta(text: str) -> str:
    return re.sub(r"([\\=;#])", r"\\\1", text.replace("\r", " ").replace("\n", " "))


def _progress(stage: str, message: str, **counts: int) -> None:
    """Emit a line the studio can display before a long blocking step."""
    print("STUDIO_EVENT " + json.dumps({"stage": stage, "message": message, **counts}), flush=True)


def _assemble(chapters: list[tuple[Chapter, Path]], output: Path, title: str, author: str) -> None:
    with tempfile.TemporaryDirectory(dir=output) as temp:
        temp = Path(temp)
        listing = temp / "list.txt"
        listing.write_text(
            "".join("file '" + str(path.resolve()).replace("'", "'\\''") + "'\n" for _, path in chapters)
        )
        pos = 0
        lines = [
            ";FFMETADATA1",
            "title=" + _ffmeta(title),
            "artist=" + _ffmeta(author),
            "comment=Narrated with Breeze TTS 2 (synthetic voice; non-commercial use)",
        ]
        for chapter, path in chapters:
            end = pos + _duration_ms(path)
            lines.extend(
                [
                    "[CHAPTER]",
                    "TIMEBASE=1/1000",
                    f"START={pos}",
                    f"END={end}",
                    "title=" + _ffmeta(chapter_label(chapter)),
                ]
            )
            pos = end
        meta = temp / "chapters.ffmeta"
        meta.write_text("\n".join(lines) + "\n")
        partial = temp / "book.m4b"
        _run(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-f",
            "ffmetadata",
            "-i",
            str(meta),
            "-map",
            "0:a:0",
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
            "-c:a",
            "copy",
            str(partial),
        )
        if not partial.stat().st_size:
            raise ValueError("Empty audiobook")
        display_name = re.sub(r"[/\\:\x00-\x1f]+", "-", title).strip(" .")[:90] or "book"
        os.replace(partial, output / (display_name + ".m4b"))


def convert(
    source: Path,
    output: Path,
    style: str,
    *,
    synth: Callable | None = None,
    max_chars: int = 320,
    max_chapters: int | None = None,
    max_passages: int | None = None,
    start_chapter: int = 1,
    preview_passage: int | None = None,
    model_path: Path = MODEL,
    ref_audio: Path | None = None,
    ref_text: str | None = None,
    base_output: Path | None = None,
    retry_chapter: int | None = None,
    retry_passage: int | None = None,
    retry_text: str | None = None,
    retry_attempt: int = 0,
    allow_low_memory: bool = False,
) -> dict:
    source, output, model_path = Path(source), Path(output), Path(model_path)
    if not style.strip():
        raise ValueError("Voice style prompt must not be empty")
    if (ref_audio is None) != (ref_text is None):
        raise ValueError("Reference audio and its exact transcript must be provided together")
    if max_chapters is not None and max_chapters < 1 or max_passages is not None and max_passages < 1:
        raise ValueError("Sample limits must be positive")
    if start_chapter < 1:
        raise ValueError("start_chapter must be positive")
    book = extract_book(source)
    if not book.language.startswith(("en", "zh")):
        raise ValueError(f"Local Breeze supports English/Chinese, not {book.language}")
    chapters = [(c, split_passages(c.text, max_chars)) for c in book.chapters]
    correction = None
    base_manifest = None
    if base_output is not None:
        base_output = Path(base_output)
        if (
            base_output.resolve() == output.resolve()
            or max_chapters is not None
            or max_passages is not None
            or preview_passage is not None
            or start_chapter != 1
            or retry_chapter is None
            or retry_passage is None
            or retry_text is None
            or retry_attempt < 0
            or not 1 <= retry_chapter <= len(chapters)
            or not 1 <= retry_passage <= len(chapters[retry_chapter - 1][1])
        ):
            raise ValueError("Retry needs a separate output version and an existing passage in a full book")
        retry_text = re.sub(r"\s+", " ", retry_text).strip()
        if not retry_text or len(retry_text) > max_chars:
            raise ValueError(f"Corrected passage must contain 1–{max_chars} characters")
        base_manifest = json.loads((base_output / "manifest.json").read_text())
        base_corrections = base_manifest.get("corrections", {})
        if not isinstance(base_corrections, dict):
            raise ValueError("Invalid base voice version")
        # Verify the base version belongs to precisely this EPUB/voice before reusing audio.
        original = [
            {
                "title": c.title,
                "source": c.source,
                "passage_sha256": [
                    hashlib.sha256(base_corrections.get(f"{ci:03d}-{pi:04d}", p).encode()).hexdigest()
                    for pi, p in enumerate(parts, 1)
                ],
            }
            for ci, (c, parts) in enumerate(chapters, 1)
        ]
        expected = {
            "epub_sha256": _hash_file(source),
            "model_revision": model_path.name,
            "style": style,
            "max_chars": max_chars,
            "reference_sha256": _hash_file(ref_audio) if ref_audio else None,
            "reference_text": ref_text,
            "chapters": original,
        }
        if ref_audio:
            expected["clip_cfg"] = CLIP_CFG
        if {key: base_manifest.get(key) for key in expected} != expected:
            raise ValueError("Base voice version does not match this book or voice")
        if not list(base_output.glob("*.m4b")) or any(
            not (base_output / "passages" / f"{ci:03d}-{pi:04d}.wav").is_file()
            for ci, (_, parts) in enumerate(chapters, 1)
            for pi in range(1, len(parts) + 1)
        ):
            raise ValueError("Finish the base audiobook before correcting a passage")
        key = f"{retry_chapter:03d}-{retry_passage:04d}"
        updated = dict(base_corrections)
        updated[key] = retry_text
        correction = {
            "origin": base_output.name,
            "chapter": retry_chapter,
            "passage": retry_passage,
            "attempt": retry_attempt,
        }
        chapters = [
            (c, [updated.get(f"{ci:03d}-{pi:04d}", p) for pi, p in enumerate(parts, 1)])
            for ci, (c, parts) in enumerate(chapters, 1)
        ]
    elif any(value is not None for value in (retry_chapter, retry_passage, retry_text)) or retry_attempt:
        raise ValueError("Retry requires a completed base voice version")
    if preview_passage is not None:
        if (
            max_chapters != start_chapter
            or max_passages != 1
            or start_chapter > len(chapters)
            or not 1 <= preview_passage <= len(chapters[start_chapter - 1][1])
        ):
            raise ValueError("Select an existing passage in the preview chapter (one preview passage only)")
    config = {
        "epub_sha256": _hash_file(source),
        "model_revision": model_path.name,
        "style": style,
        "max_chars": max_chars,
        "reference_sha256": _hash_file(ref_audio) if ref_audio else None,
        "reference_text": ref_text,
        "chapters": [
            {
                "title": c.title,
                "source": c.source,
                "passage_sha256": [hashlib.sha256(p.encode()).hexdigest() for p in parts],
            }
            for c, parts in chapters
        ],
    }
    if ref_audio:
        config["clip_cfg"] = CLIP_CFG  # Only clip versions carry it, so style-only manifests are unchanged.
    if correction is not None:
        config["corrections"] = updated
        config["retry"] = correction
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another conversion owns this output directory") from exc
        manifest = output / "manifest.json"
        if manifest.exists() and json.loads(manifest.read_text()) != config:
            raise ValueError("Output has different settings or source; choose another output directory")
        if not manifest.exists():
            manifest.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
            os.chmod(manifest, 0o600)  # The reference transcript is part of this immutable manifest.
        (output / "passages").mkdir(exist_ok=True)
        (output / "chapters").mkdir(exist_ok=True)
        if correction is not None:
            # Hardlinks are safe: every fresh synthesis/encode writes a separate temporary
            # file and atomically replaces it. Original audio is never modified in place.
            def reuse(src: Path, dest: Path) -> None:
                if dest.exists():
                    return
                if src.is_symlink() or not src.is_file():
                    raise ValueError(f"Base audio is missing or unsafe: {src.name}")
                try:
                    os.link(src, dest)
                except OSError:
                    shutil.copy2(src, dest)

            for ci, (chapter, parts) in enumerate(chapters, 1):
                for pi in range(1, len(parts) + 1):
                    if (ci, pi) != (retry_chapter, retry_passage):
                        name = f"{ci:03d}-{pi:04d}.wav"
                        reuse(base_output / "passages" / name, output / "passages" / name)
                if ci != retry_chapter:
                    name = f"{ci:03d}-{_slug(chapter.title)}.m4a"
                    reuse(base_output / "chapters" / name, output / "chapters" / name)
        planned_passages = sum(len(parts) for _, parts in chapters)
        planned_characters = sum(len(part) for _, parts in chapters for part in parts)
        _progress(
            "planning",
            f"Mapped {len(chapters)} sections and {planned_passages} passages",
            total_passages=planned_passages,
            total_characters=planned_characters,
        )
        made = 0
        visited = 0
        completed = []
        for ci, (chapter, parts) in enumerate(chapters, 1):
            if ci < start_chapter:
                continue
            if max_chapters is not None and ci > max_chapters:
                break
            wavs = []
            for pi, text in enumerate(parts, 1):
                if max_passages is not None and visited >= max_passages:
                    break
                if preview_passage is not None and pi != preview_passage:
                    continue
                path = output / "passages" / f"{ci:03d}-{pi:04d}.wav"
                if path.exists():
                    _wav_ok(path)
                    _progress(
                        "reused",
                        f"Reusing saved chapter {ci}/{len(chapters)}, passage {pi}/{len(parts)}",
                        chapter=ci,
                        passage=pi,
                        characters=len(text),
                    )
                else:
                    if synth is None:
                        if not model_path.is_dir():
                            raise FileNotFoundError(f"Local checkpoint missing: {model_path}")
                        _progress("loading", "Checking host resources and loading the local voice model")
                        synth = (
                            mlx_synth(model_path, ref_audio, ref_text, allow_low_memory=True)
                            if allow_low_memory
                            else mlx_synth(model_path, ref_audio, ref_text)
                        )
                    if memory_pressure_critical():
                        raise RuntimeError("Memory pressure critical: stopped before the next passage")
                    tmp = path.with_suffix(".partial.wav")
                    seed_material = f"{config['epub_sha256']}:{ci}:{pi}:{style}"
                    if correction is not None and (ci, pi) == (retry_chapter, retry_passage):
                        seed_material += f":retry:{base_output.name}:{retry_attempt}"
                    seed = int(hashlib.sha256(seed_material.encode()).hexdigest()[:8], 16)
                    _progress(
                        "synthesizing",
                        f"Synthesizing chapter {ci}/{len(chapters)}, passage {pi}/{len(parts)}",
                        chapter=ci,
                        passage=pi,
                        characters=len(text),
                    )
                    try:
                        synth(tmp, text, style, seed)
                        _wav_ok(tmp)
                        os.replace(tmp, path)
                    finally:
                        tmp.unlink(missing_ok=True)
                    made += 1
                    _progress(
                        "saved",
                        f"Saved chapter {ci}/{len(chapters)}, passage {pi}/{len(parts)}",
                        chapter=ci,
                        passage=pi,
                        characters=len(text),
                    )
                    print(f"Generated chapter {ci}/{len(chapters)} passage {pi}/{len(parts)}: {path}", flush=True)
                wavs.append(path)
                visited += 1
            if len(wavs) != len(parts):
                break
            chapter_audio = output / "chapters" / f"{ci:03d}-{_slug(chapter.title)}.m4a"
            if not chapter_audio.exists():
                tmp_audio = chapter_audio.with_suffix(".partial.m4a")
                _progress("encoding", f"Encoding chapter {ci}/{len(chapters)} as M4A", chapter=ci)
                try:
                    _concat_file(wavs, tmp_audio)
                    if _duration_ms(tmp_audio) <= 0:
                        raise ValueError("Empty chapter")
                    os.replace(tmp_audio, chapter_audio)
                finally:
                    tmp_audio.unlink(missing_ok=True)
            if _duration_ms(chapter_audio) <= 0:
                raise ValueError(f"Invalid cached chapter: {chapter_audio}")
            completed.append((chapter, chapter_audio))
        if len(completed) == len(chapters):
            _progress("assembling", "Combining chapters and writing M4B chapter markers")
            _assemble(completed, output, book.title, book.author)
            _progress("ready", "Chaptered audiobook saved and ready")
        return {
            "title": book.title,
            "chapters": len(chapters),
            "chapters_complete": len(completed),
            "passages_generated": made,
            "complete": len(completed) == len(chapters),
            "output": str(output),
        }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local, resumable EPUB to chaptered audiobook (no playback or cloud)")
    parser.add_argument("epub", type=Path)
    parser.add_argument("--output", type=Path, help="Default: ~/Audiobooks/<EPUB stem>")
    style = parser.add_mutually_exclusive_group()
    style.add_argument("--style", help="Narrator voice and delivery description")
    style.add_argument("--style-file", type=Path, help="UTF-8 file with book voice style")
    parser.add_argument("--max-chars", type=int, default=320)
    parser.add_argument("--max-chapters", type=int)
    parser.add_argument("--max-passages", type=int)
    parser.add_argument(
        "--start-chapter", type=int, default=1, help="Preview from this chapter, then resume the whole book later"
    )
    parser.add_argument(
        "--preview-passage",
        type=int,
        help="Speak just this numbered passage from --start-chapter (with --max-passages 1)",
    )
    parser.add_argument(
        "--base-output", type=Path, help="Completed version to copy unchanged audio from for a correction"
    )
    parser.add_argument("--retry-chapter", type=int)
    parser.add_argument("--retry-passage", type=int)
    parser.add_argument("--retry-text-file", type=Path, help="Private UTF-8 corrected passage text")
    parser.add_argument("--retry-attempt", type=int, default=0)
    parser.add_argument("--model", type=Path, default=MODEL)
    parser.add_argument("--reference-audio", type=Path, help="Optional voice reference (use with --reference-text)")
    reference_text = parser.add_mutually_exclusive_group()
    reference_text.add_argument("--reference-text", help="Exact spoken transcript of reference audio")
    reference_text.add_argument(
        "--reference-text-file", type=Path, help="Local UTF-8 transcript file (avoids process-argument exposure)"
    )
    parser.add_argument(
        "--inspect", action="store_true", help="Show chapter titles and lengths without generating audio"
    )
    parser.add_argument(
        "--allow-low-memory",
        action="store_true",
        help="Narrate with less than the recommended free memory (slower; hard limits still apply)",
    )
    parser.add_argument(
        "--quiet-summary",
        action="store_true",
        help="Keep progress events but omit the final CLI JSON (for the local studio)",
    )
    args = parser.parse_args(argv)
    if args.inspect:
        book = extract_book(args.epub)
        print(
            json.dumps(
                {
                    "title": book.title,
                    "author": book.author,
                    "language": book.language,
                    "chapters": [{"title": c.title, "characters": len(c.text)} for c in book.chapters],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    if args.style is None and args.style_file is None:
        parser.error("Choose --style or --style-file to generate audio")
    text = args.style if args.style is not None else args.style_file.read_text(encoding="utf-8").strip()
    ref_text = (
        args.reference_text
        if args.reference_text_file is None
        else args.reference_text_file.read_text(encoding="utf-8").strip()
    )
    retry_text = args.retry_text_file.read_text(encoding="utf-8") if args.retry_text_file else None
    out = args.output or Path.home() / "Audiobooks" / _slug(args.epub.stem)
    # CLI and studio use the same acceptance receipt; generation never downloads.
    if args.model != resolve_model_path():
        os.environ["AUDIOBOOK_MODEL_DIR"] = str(args.model.expanduser().resolve())
    with gpu_slot():
        result = convert(
            args.epub,
            out,
            text,
            max_chars=args.max_chars,
            max_chapters=args.max_chapters,
            max_passages=args.max_passages,
            start_chapter=args.start_chapter,
            preview_passage=args.preview_passage,
            model_path=args.model,
            ref_audio=args.reference_audio,
            ref_text=ref_text,
            base_output=args.base_output,
            retry_chapter=args.retry_chapter,
            retry_passage=args.retry_passage,
            retry_text=retry_text,
            retry_attempt=args.retry_attempt,
            allow_low_memory=args.allow_low_memory,
        )
    if not args.quiet_summary:
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
