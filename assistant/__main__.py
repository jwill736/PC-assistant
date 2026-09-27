"""Entry point: ``python -m assistant [--config path] [--no-window] [--port N]``."""

from __future__ import annotations

import argparse
import logging
import threading
import time
import webbrowser

import uvicorn

from .config import load_config
from .integrations.browser import Browser
from .runtime import Runtime
from .server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice-controlled PC assistant + HUD dashboard")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-window", action="store_true", help="don't open the dashboard window")
    parser.add_argument("--no-voice", action="store_true", help="disable the microphone listener")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "faster_whisper", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = load_config(args.config)
    if args.no_voice:
        cfg["voice"]["enabled"] = False
    host = cfg["server"]["host"]
    port = args.port or cfg["server"]["port"]
    runtime = Runtime(cfg)
    app = create_app(runtime)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"

    if cfg["server"].get("open_window", True) and not args.no_window:
        def open_window():
            time.sleep(1.5)
            if not Browser().app_window(url):
                webbrowser.open(url)

        threading.Thread(target=open_window, daemon=True).start()

    name = cfg["assistant"]["name"]
    logging.getLogger("assistant").info("%s online at %s — say “%s, good morning”", name, url, name)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
