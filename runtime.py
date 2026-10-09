"""Shared source/frozen paths and bounded executable worker dispatch."""

from __future__ import annotations
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

WORKERS = {"audiobook", "model_setup", "diagnostics"}


def app_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def worker_command(module: str, args: list[str]) -> list[str]:
    if module not in WORKERS:
        raise ValueError("Unknown Embervoice worker: " + module)
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", module, *args]
    return [sys.executable, "-m", module, *args]


def configure_bundled_tools() -> None:
    tools = app_root() / "tools"
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if tools.is_dir():
        parts.insert(0, str(tools))
    for directory in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if directory not in parts:
            parts.append(directory)
    os.environ["PATH"] = os.pathsep.join(parts)


def tool_path(name: str) -> str | None:
    if name not in ("ffmpeg", "ffprobe"):
        raise ValueError("Unknown audio tool")
    return shutil.which(name)


def diagnostics() -> int:
    """Packaging gate only: imports and executables, never weights or inference."""
    from studio import startup_readiness
    from model_setup import ModelSetup, licence_digest
    import mlx.core as mx
    import mlx_audio.tts.models.breeze_tts as breeze
    from transformers import PreTrainedTokenizerFast
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel

    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=Tokenizer(WordLevel({"hello": 0, "[UNK]": 1}, unk_token="[UNK]")), unk_token="[UNK]"
    )
    result = {
        "app": "Embervoice",
        "frozen": bool(getattr(sys, "frozen", False)),
        "readiness": startup_readiness(),
        "mlx_imported": bool(mx),
        "breeze_imported": bool(breeze),
        "tokenizer_fixture_ok": tokenizer.encode("hello") == [0],
        "licence_sha256": licence_digest(),
        "model_ready": ModelSetup(Path.home() / "Audiobooks/Studio/setup").status()["ready"],
        "tools": {
            name: subprocess.check_output([tool_path(name), "-version"], text=True).splitlines()[0]
            for name in ("ffmpeg", "ffprobe")
        },
    }
    print(json.dumps(result), flush=True)
    return 0 if not result["readiness"]["blockers"] else 1


def worker_main(argv: list[str]) -> int | None:
    if not argv or argv[0] != "--worker":
        return None
    if len(argv) < 2 or argv[1] not in WORKERS:
        raise SystemExit("Unknown Embervoice worker")
    if argv[1] == "diagnostics":
        return diagnostics()
    module = importlib.import_module(argv[1])
    return module.main(argv[2:]) or 0
