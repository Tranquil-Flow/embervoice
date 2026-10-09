"""PyInstaller entry point; child modes never open the launcher recursively."""

import os
import sys
from runtime import configure_bundled_tools, worker_main


def main():
    os.umask(0o077)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("DO_NOT_TRACK", "1")
    configure_bundled_tools()
    code = worker_main(sys.argv[1:])
    if code is not None:
        return code
    from launcher import main as launch

    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
