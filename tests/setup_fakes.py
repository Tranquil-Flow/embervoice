"""Test-only admission stand-in for synthetic workers; never writes consent."""

from pathlib import Path


class ReadySetup:
    def status(self):
        return {
            "state": "ready",
            "ready": True,
            "accepted": True,
            "total_bytes": 1,
            "completed_bytes": 1,
            "license_sha256": "test-only",
            "error": None,
        }

    def require_ready(self):
        return Path("/test-only-model")

    def close(self):
        pass


def ready_environment():
    return {"supported": True, "blockers": [], "ffmpeg": True, "ffprobe": True}
