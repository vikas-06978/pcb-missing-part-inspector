#!/usr/bin/env python3
"""Start the PCB Missing-Component Inspector.

    python run.py

Binds to 127.0.0.1 only and opens your browser. Nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

BANNER = r"""
   ___  _________    ____                           __
  / _ \/ ___/ __ )  /  _/__  ___ ___  ___ ______ __/ /____  ____
 / ___/ /__/ /_/ / _/ // _ \(_-</ _ \/ -_) __/ // / __/ _ \/ __/
/_/   \___/_____/ /___/_//_/___/ .__/\__/\__/\_,_/\__/\___/_/
                              /_/
"""

REQUIRED = {
    "cv2": "opencv-python",
    "numpy": "numpy",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "PIL": "Pillow",
    "multipart": "python-multipart",
}


def check_dependencies() -> None:
    import importlib

    missing = []
    for module, package in REQUIRED.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)

    if missing:
        print("Missing dependencies:", ", ".join(missing), file=sys.stderr)
        print("\nInstall them with:\n    pip install -r requirements.txt\n", file=sys.stderr)
        raise SystemExit(1)


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def find_port(host: str, preferred: int, attempts: int = 20) -> int:
    for offset in range(attempts):
        candidate = preferred + offset
        if port_is_free(host, candidate):
            return candidate
    raise SystemExit(
        f"No free port between {preferred} and {preferred + attempts - 1}."
    )


def open_browser_when_ready(url: str, host: str, port: int, timeout: float = 20.0) -> None:
    """Poll the socket rather than sleeping a fixed time.

    Cold-starting OpenCV on a laptop can take a few seconds; a fixed delay
    either opens a dead tab or makes everyone wait for the slowest machine.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            if sock.connect_ex((host, port)) == 0:
                webbrowser.open(url)
                return
        time.sleep(0.25)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Preferred port (default 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    parser.add_argument(
        "--read-labels",
        action="store_true",
        help=(
            "Switch on reading the designators printed on the board, so they "
            "do not all have to be typed. Off by default: it is the one part "
            "of this app that uses a model, it never affects a missing or "
            "present verdict, and it still runs entirely on this machine."
        ),
    )
    args = parser.parse_args()

    if args.read_labels:
        # Imported here rather than at module scope so that --help still
        # works before the dependencies are installed.
        from app.naming import ENABLE_FLAG

        os.environ[ENABLE_FLAG] = "1"

    check_dependencies()

    import uvicorn

    from app.config import APP_NAME, APP_VERSION

    port = args.port if port_is_free(args.host, args.port) else find_port(args.host, args.port)
    url = f"http://{args.host}:{port}"

    print(BANNER)
    print(f"  {APP_NAME} v{APP_VERSION}")
    print(f"  Running at {url}")
    if port != args.port:
        print(f"  (port {args.port} was busy)")
    print("  Offline: no network access is used at runtime.")
    print("  Press Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Thread(
            target=open_browser_when_ready, args=(url, args.host, port), daemon=True
        ).start()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=port,
        reload=args.reload,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
