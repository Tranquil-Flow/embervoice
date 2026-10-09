"""Read-only integrity check for a completed local EPUB audiobook.

Usage: python tests/verify_book.py EPUB OUTPUT_DIR
Prints JSON; does not play audio or load a model.
"""

import array
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from audiobook import chapter_label, extract_book


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path):
    return json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_chapters",
                "-show_entries",
                "format=duration:format_tags=title,artist:chapter=start_time,end_time:chapter_tags=title",
                "-of",
                "json",
                str(path),
            ],
            text=True,
        )
    )


def verify(source, output):
    source, output = Path(source), Path(output)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["epub_sha256"] == sha256(source), "Source EPUB changed"
    book = extract_book(source)
    assert len(book.chapters) == len(manifest["chapters"]), "Chapter count differs"
    expected = [
        f"{ci:03d}-{pi:04d}.wav"
        for ci, chapter in enumerate(manifest["chapters"], 1)
        for pi in range(1, len(chapter["passage_sha256"]) + 1)
    ]
    audio_s = 0
    rms_min, peak_max = 32767, 0
    for filename in expected:
        path = output / "passages" / filename
        with wave.open(str(path), "rb") as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000), filename
            frames = wav.getnframes()
            assert frames > 0, filename
            data = array.array("h", wav.readframes(frames))
            peak = max(abs(v) for v in data)
            rms = math.sqrt(sum(v * v for v in data) / len(data))
            assert peak >= 20 and rms >= 5, f"Silent passage {filename}"
            rms_min, peak_max = min(rms_min, rms), max(peak_max, peak)
            audio_s += frames / 24000
    chapters = sorted((output / "chapters").glob("*.m4a"))
    assert len(chapters) == len(book.chapters), "Missing chapter audio"
    books = list(output.glob("*.m4b"))
    assert len(books) == 1, "Expected exactly one complete M4B"
    metadata = probe(books[0])
    got = metadata["chapters"]
    assert [c["tags"]["title"] for c in got] == [chapter_label(c) for c in book.chapters], "Chapter titles differ"
    assert metadata["format"]["tags"]["title"] == book.title, "Book title differs"
    duration = float(metadata["format"]["duration"])
    assert duration >= audio_s, "Combined M4B shorter than source passages"
    assert all(float(a["start_time"]) < float(a["end_time"]) for a in got), "Zero length chapter"
    assert all(float(a["end_time"]) <= float(b["start_time"]) + 0.1 for a, b in zip(got, got[1:])), (
        "Overlapping chapter markers"
    )
    # Decode with the shipping WAV muxer, rather than requiring a raw-PCM muxer
    # used nowhere by the app. Temporary output never changes the book store.
    decoded = 0
    decoded_peak = 0
    with tempfile.TemporaryDirectory() as scratch:
        decoded_wav = Path(scratch) / "decoded.wav"
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(books[0]),
                "-ac",
                "1",
                "-ar",
                "24000",
                "-c:a",
                "pcm_s16le",
                str(decoded_wav),
            ],
            capture_output=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr.decode()
        with wave.open(str(decoded_wav), "rb") as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
            while chunk := wav.readframes(32768):
                samples = array.array("h", chunk)
                decoded += len(samples)
                decoded_peak = max(decoded_peak, max(abs(v) for v in samples))
    assert decoded_peak >= 20 and decoded / 24000 >= audio_s, "Decoded M4B is empty or truncated"
    return {
        "title": book.title,
        "author": book.author,
        "epub_sha256": manifest["epub_sha256"],
        "passages": len(expected),
        "chapters": len(got),
        "raw_audio_seconds": round(audio_s, 2),
        "book_seconds": round(duration, 2),
        "decoded_seconds": round(decoded / 24000, 2),
        "minimum_passage_rms": round(rms_min, 1),
        "maximum_passage_peak": peak_max,
        "decoded_peak": decoded_peak,
        "book_path": str(books[0]),
        "chapter_titles": [c["tags"]["title"] for c in got],
    }


if __name__ == "__main__":
    print(json.dumps(verify(sys.argv[1], sys.argv[2]), indent=2, ensure_ascii=False))
