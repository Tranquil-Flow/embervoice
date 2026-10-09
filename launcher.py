"""Loopback-only launcher for Embervoice.

Owns the whole lifecycle: bind a free loopback port and hold it (no
find-then-close race), hand that socket to uvicorn, open the browser only
after the server really answers, and shut down cleanly on Ctrl-C or SIGTERM.

Heavy modules are imported lazily so tests can import this file without
touching the ML stack.
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import sys
from runtime import configure_bundled_tools
import threading
import time
import urllib.error
import urllib.request

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_SEARCH_ATTEMPTS = 20
READY_TIMEOUT_S = 60.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="embervoice",
        description="Start Embervoice on this Mac, reachable only from this Mac.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"preferred loopback port (default {DEFAULT_PORT}); if it is busy, "
        "the next free loopback port is used instead",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    parser.add_argument("--store", default=None, help="directory for books and audio (default ~/Audiobooks/Studio)")
    parser.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_S, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def bind_loopback(preferred: int, attempts: int = PORT_SEARCH_ATTEMPTS) -> socket.socket:
    """Bind and hold 127.0.0.1:preferred, else the next free port."""
    last_error: OSError | None = None
    for port in range(preferred, min(preferred + attempts, 65536)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
            sock.listen(128)
        except OSError as exc:
            last_error = exc
            sock.close()
            continue
        return sock
    raise SystemExit(
        f"No free loopback port in {preferred}..{preferred + attempts - 1}"
        + (f" (last error: {last_error})" if last_error else "")
    )


def server_answers(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/setup", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # the server is answering, whatever it says about setup
    except Exception:
        return False


def open_browser_when_ready(port: int, ready: threading.Event, should_stop, timeout: float):
    """Open the browser only after the server actually answers."""

    def run() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready.is_set():
                break
            if should_stop():
                return
            if server_answers(port):
                break
            time.sleep(0.2)
        if not ready.is_set() and not server_answers(port):
            return
        with contextlib.suppress(Exception):
            import webbrowser

            webbrowser.open(f"http://{HOST}:{port}/")

    thread = threading.Thread(target=run, name="browser-wait", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    configure_bundled_tools()
    args = parse_args(argv)

    from studio import create_app  # lazy: keeps ML imports out of tests

    app = create_app(args.store) if args.store else create_app()

    sock = bind_loopback(args.port)
    port = sock.getsockname()[1]

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, access_log=False, log_level="warning"))
    ready = threading.Event()
    original_startup = server.startup

    async def startup(*args, **kwargs) -> None:
        await original_startup(*args, **kwargs)
        ready.set()

    server.startup = startup  # type: ignore[method-assign]

    url = f"http://{HOST}:{port}/"
    print(f"Embervoice is starting at {url}", flush=True)
    if not args.no_browser:
        open_browser_when_ready(port, ready, lambda: getattr(server, "should_exit", False), args.ready_timeout)

    try:
        server.run(sockets=[sock])
    finally:
        ready.set()
        with contextlib.suppress(Exception):
            sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
