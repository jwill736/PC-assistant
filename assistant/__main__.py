"""Entry point: ``python -m assistant [--config path] [--no-window] [--port N]``.

Launched with ``pythonw`` (no console) it runs as a tray app: logs go to
``data/logs/assistant.log`` and the tray icon is the only visible piece.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser

import httpx
import uvicorn

from . import discovery
from .config import load_config
from .doctor import format_report, run_doctor
from .integrations.browser import Browser
from .runtime import Runtime
from .server import create_app
from .watchdog import setup_logging


def already_running(host: str, port: int) -> bool:
    """True when another copy of the assistant is serving the HUD on this port."""
    try:
        r = httpx.get(f"http://{host}:{port}/", timeout=1.5)
    except httpx.HTTPError:
        return False
    return r.status_code == 200 and "HUD</title>" in r.text


def open_hud(url: str) -> None:
    if not Browser().app_window(url):
        webbrowser.open(url)


def run_cli_calibration(cfg) -> int:
    """Console version of the Setup tab's calibration wizard."""
    from .voice import calibrate as calib

    class ConsoleBus:
        def __init__(self):
            self.last = None

        def publish(self, _type, data, sticky=False):
            key = (data.get("step"), data.get("index"), data.get("recording"))
            if data.get("prompt") and key != self.last:
                self.last = key
                tag = f"[{data['index']}/{data['total']}] " if data.get("total", 1) > 1 else ""
                print(("  ● recording… " if data.get("recording") else "\n") + (tag + data["prompt"] if not data.get("recording") else ""))

    print("Voice calibration: quiet room, normal speaking voice, about a minute.")
    embedder = calib.default_embedder(cfg.data_dir)
    cal = calib.Calibrator(cfg, ConsoleBus(), calib.mic_recorder(cfg["voice"].get("input_device")),
                           calib.engine_transcriber(cfg), embedder)
    result = cal.run()
    if result.get("error"):
        print(f"\nCalibration failed: {result['error']}")
        return 1
    print(f"\nRoom noise {result['noise_rms']} → speech threshold (min_rms) {result['min_rms']}")
    print(f"Wake word heard as: {', '.join(result.get('wake_heard', [])) or '—'}")
    if result.get("wake_variants"):
        print(f"Added wake-word spellings: {', '.join(result['wake_variants'])}")
    if result.get("profile"):
        p = result["profile"]
        print(f"Voice profile: {p['clips']} samples, threshold {p['threshold']} — strangers are now ignored.")
    elif result.get("profile_error"):
        print(f"Voice profile: {result['profile_error']}")
    print(f"Saved to {result['saved_to']}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice-controlled PC assistant + HUD dashboard")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-window", action="store_true", help="don't open the dashboard window")
    parser.add_argument("--no-voice", action="store_true", help="disable the microphone listener")
    parser.add_argument("--no-tray", action="store_true", help="don't show the system tray icon")
    parser.add_argument("--scan", action="store_true", help="scan this PC for things to connect, then exit")
    parser.add_argument("--doctor", action="store_true", help="scan + live health check of every connection, then exit")
    parser.add_argument("--calibrate", action="store_true", help="calibrate the mic and enroll your voice, then exit")
    parser.add_argument("--bench-voice", action="store_true",
                        help="record 10 commands and compare speech engines on your voice and PC, then exit")
    parser.add_argument("--apply", action="store_true", help="with --bench-voice: switch to the best engine")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    headless = sys.stdout is None or sys.stderr is None  # pythonw: no console attached
    if headless:
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - lives for the process
    try:
        sys.stdout.reconfigure(errors="replace")  # scene/app names can hold emoji the console can't show
    except (AttributeError, ValueError):
        pass

    cfg = load_config(args.config)
    log_path = setup_logging(cfg.data_dir, args.debug, console=not headless)
    log = logging.getLogger("assistant")

    if args.scan or args.doctor:
        result = discovery.discover(cfg)
        discovery.write_discovered(result, cfg.root, cfg.data_dir)
        report = None
        if args.doctor:
            report = run_doctor(load_config(args.config), load_model=cfg["voice"].get("enabled", True))
        print(format_report(result, report))
        print(f"\nWrote {discovery.DISCOVERED_FILE} (merged under config.yaml; your settings win).")
        sys.exit(1 if report and report["summary"].get("fail") else 0)

    if args.calibrate:
        sys.exit(run_cli_calibration(cfg))

    if args.bench_voice:
        from .voice import bench
        from .voice import calibrate as calib

        try:
            report = bench.run(cfg, calib.mic_recorder(cfg["voice"].get("input_device")), apply=args.apply)
        except Exception as exc:
            print(f"Voice benchmark failed: {exc}")
            sys.exit(1)
        sys.exit(0 if report["best"] else 1)

    host = cfg["server"]["host"]
    port = args.port or cfg["server"]["port"]
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    if already_running("127.0.0.1" if host in ("0.0.0.0", "::") else host, port):
        log.info("Already running — opening the HUD instead of starting a second copy.")
        open_hud(url)
        return

    if discovery.is_stale(cfg.root):
        # First launch (or a day since the last scan): find apps, games, OBS scenes,
        # bookmarks and repos so they work by voice without editing config.
        log.info("Scanning this PC for apps, games, OBS and projects…")
        try:
            result = discovery.discover(cfg)
            discovery.write_discovered(result, cfg.root, cfg.data_dir)
            cfg = load_config(args.config)
            s = result["summary"]
            log.info("Scan done in %ss: %s connected, %s found, %s need attention (see the Setup tab)",
                     result["duration_s"], s.get("connected", 0), s.get("found", 0), s.get("action", 0))
        except Exception:
            log.exception("PC scan failed; continuing with existing config")
    if args.no_voice:
        cfg["voice"]["enabled"] = False

    runtime = Runtime(cfg)
    app = create_app(runtime)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_config=None, log_level="warning"))

    tray = None
    if cfg.get("tray", {}).get("enabled", True) and not args.no_tray:
        from .tray import Tray

        tray = Tray(runtime, url, on_quit=lambda: setattr(server, "should_exit", True))
        if not tray.start():
            tray = None

    if cfg["server"].get("open_window", True) and not args.no_window:
        threading.Thread(target=lambda: (time.sleep(1.5), open_hud(url)), daemon=True).start()

    name = cfg["assistant"]["name"]
    log.info("%s online at %s — say “%s, good morning”. Log: %s", name, url, name, log_path)
    try:
        server.run()
    finally:
        if tray:
            tray.stop()
        log.info("%s stopped.", name)


if __name__ == "__main__":
    main()
