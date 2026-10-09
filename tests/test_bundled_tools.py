"""The shipped decoder supports the formats already accepted by the voice-clip UI."""

from pathlib import Path
import os
import subprocess
import wave
import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / ".local/tools"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.skipif(not (TOOLS / "ffmpeg").exists(), reason="local bundled-tools qualification; build_ffmpeg.py first")
@pytest.mark.parametrize(
    "filename",
    [
        "reference-u8.wav",
        "reference-alac.m4a",
        "reference-opus.webm",
        "reference-vorbis.ogg",
        "reference-mp3.mp3",
        "reference-flac.flac",
    ],
)
def test_bundled_tools_decode_accepted_reference_formats(tmp_path, filename):
    out = tmp_path / "reference.wav"
    env = {**os.environ, "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [
            str(TOOLS / "ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(FIXTURES / filename),
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(out),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    with wave.open(str(out)) as wav:
        assert wav.getnchannels() == 1 and wav.getframerate() == 24000 and wav.getsampwidth() == 2
        assert 2.8 <= wav.getnframes() / wav.getframerate() <= 3.2
