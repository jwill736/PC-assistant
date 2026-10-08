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
from pathlib import Path

import httpx
import psutil
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


HUD_WAS_OPEN = "hud_was_open"  # in data/: written on quit while a HUD window is connected


def vesper_processes(root: Path) -> list[psutil.Process]:
    """Other copies of Vesper started from this folder ("python -m assistant", from start.bat, the tray shortcut
    or the desktop icon), whether or not they still serve the HUD. Never this process or another --quit."""
    root = Path(root).resolve()
    me, found = os.getpid(), []
    for p in psutil.process_iter(["pid", "cmdline", "cwd", "exe"]):
        try:
            cmd = p.info["cmdline"] or []
            if p.info["pid"] == me or "--quit" in cmd or "-m" not in cmd:
                continue
            i = cmd.index("-m")
            if cmd[i + 1:i + 2] != ["assistant"]:
                continue
            if any(_inside(x, root) for x in (p.info["cwd"], p.info["exe"])):
                found.append(p)
        except (psutil.Error, OSError, ValueError):
            continue
    return found


def _inside(path: str | None, root: Path) -> bool:
    if not path:
        return False
    path, root_s = os.path.normcase(str(Path(path).resolve())), os.path.normcase(str(root))
    return path == root_s or path.startswith(root_s.rstrip(os.sep) + os.sep)


def quit_running(host: str, port: int, data_dir, root: Path | None = None, wait: float = 20.0) -> bool:
    """Close the copy that's running (the installer does this before updating). True once none is left.

    Waits for its process to end, not just for the HUD to stop answering: on Windows a voice or tray thread
    could keep the old copy alive (and listening) after its HUD closed, so an update ended with two. Anything
    still running after the wait, or started from this folder and no longer serving the HUD, is ended.
    """
    pid = None
    if already_running(host, port):
        token_file = data_dir / "api_token"
        token = token_file.read_text().strip() if token_file.exists() else ""
        try:
            r = httpx.post(f"http://{host}:{port}/api/quit", headers={"x-assistant-token": token}, timeout=5)
            pid = r.json().get("pid") if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
    others = vesper_processes(root) if root else []
    asked = []
    if pid:
        try:
            # its parents too: on Windows the .venv python.exe is a small launcher that started the real one, and
            # ending the launcher early would cut the real one off mid-shutdown
            me = psutil.Process(pid)
            asked = [me] + [p for p in others if p.pid in {x.pid for x in me.parents()}]
        except psutil.Error:
            pass
    elif already_running(host, port):  # an older copy that doesn't say its process: give them all time
        asked, others = others, []
    strays = [p for p in others if p.pid not in {x.pid for x in asked}]
    for p in strays:  # not serving the HUD any more: stuck, so no point waiting
        _end(p)
    _, alive = psutil.wait_procs(asked, timeout=wait)
    for p in alive:
        _end(p)
    _, alive = psutil.wait_procs(alive + strays, timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=3)
    deadline = time.monotonic() + (5 if asked else wait)
    while already_running(host, port) and time.monotonic() < deadline:
        time.sleep(0.5)
    return not already_running(host, port) and not (vesper_processes(root) if root else [])


def _end(p: psutil.Process) -> None:
    try:
        p.terminate()
    except psutil.Error:
        pass


def open_hud_unless_one_reconnects(bus, url: str, ready=lambda: True, wait: float = 6.0,
                                   startup: float = 120.0) -> bool:
    """Open the HUD once the server is up. After an update (``wait`` > 0: a HUD window was open when the last copy
    quit) that window reconnects by itself and reloads onto the new version, so open a second one only if it
    hasn't within ``wait`` seconds. True when one was opened."""
    deadline = time.monotonic() + startup
    while not ready() and time.monotonic() < deadline:
        time.sleep(0.25)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if bus.client_count:
            logging.getLogger("assistant").info("The HUD window that was open came back; not opening another.")
            return False
        time.sleep(0.25)
    open_hud(url)
    return True


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
    parser.add_argument("--quit", action="store_true", help="close the copy that's running, then exit")
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
    host = cfg["server"]["host"]
    port = args.port or cfg["server"]["port"]
    local_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if args.quit:
        closed = quit_running(local_host, port, cfg.data_dir, cfg.root)
        print("Vesper is closed." if closed else "Vesper is still running: quit it from the tray icon.")
        sys.exit(0 if closed else 1)
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

    url = f"http://{local_host}:{port}/"
    if already_running(local_host, port):
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

    hud_was_open = cfg.data_dir / HUD_WAS_OPEN
    reuse = hud_was_open.exists() and time.time() - hud_was_open.stat().st_mtime < 600
    hud_was_open.unlink(missing_ok=True)
    runtime = Runtime(cfg)
    app = create_app(runtime)
    # timeout_graceful_shutdown: an open HUD socket must not hold the shutdown up
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_config=None, log_level="warning",
                                           timeout_graceful_shutdown=3))

    def quit_now() -> None:
        if runtime.bus.client_count:  # the HUD window stays open and reconnects to the next start: reuse it then
            hud_was_open.write_text(str(time.time()), encoding="utf-8")
        server.should_exit = True
        # Last resort: if a voice, tray or worker thread still holds the process 15 s later, end it anyway, so
        # an update never leaves an old copy listening alongside the new one.
        timer = threading.Timer(15, lambda: os._exit(0))
        timer.daemon = True  # a clean exit doesn't wait for it
        timer.start()
    app.state.on_quit = quit_now

    tray = None
    if cfg.get("tray", {}).get("enabled", True) and not args.no_tray:
        from .tray import Tray

        tray = Tray(runtime, url, on_quit=quit_now)
        if not tray.start():
            tray = None

    if cfg["server"].get("open_window", True) and not args.no_window:
        threading.Thread(target=open_hud_unless_one_reconnects, args=(runtime.bus, url, lambda: server.started,
                                                                      6.0 if reuse else 0.0), daemon=True).start()

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
