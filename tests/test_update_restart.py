"""An update closes the old Vesper completely before starting the new one: its process (not just its HUD), any
copy that hung on without the HUD, and the launcher around it; then the HUD window already open is reused."""

import os
import subprocess
import sys
import threading
import time

import httpx
import psutil
import pytest
from fastapi.testclient import TestClient

import assistant.__main__ as main_mod
from assistant.bus import EventBus
from assistant.__main__ import open_hud_unless_one_reconnects, quit_running, vesper_processes
from assistant.runtime import Runtime
from assistant.server import create_app

# A stand-in "assistant" package: `-m assistant 0.5` lives half a second, `-m assistant launch ...` starts the real
# one and waits for it, like the .venv python.exe launcher on Windows, and plain `-m assistant` hangs on. Each
# leaves a closed-* file when it ends by itself (exit codes can't tell: psutil collects them first).
FAKE = '''import pathlib, subprocess, sys, time
args = sys.argv[1:]
if args[:1] == ["launch"]:
    code = subprocess.call([sys.executable, "-m", "assistant", *args[1:]])
else:
    time.sleep(float(args[0]) if args and args[0][0].isdigit() else 60)
    code = 0
pathlib.Path("closed-" + ("launcher" if args[:1] == ["launch"] else "real")).write_text("")
sys.exit(code)
'''


@pytest.fixture
def folder(tmp_path):
    def make(name):
        root = tmp_path / name
        (root / "assistant").mkdir(parents=True)
        (root / "assistant" / "__init__.py").write_text("")
        (root / "assistant" / "__main__.py").write_text(FAKE)
        (root / "data").mkdir()
        return root
    return make


@pytest.fixture
def spawn():
    procs = []

    def start(root, *args):
        procs.append(subprocess.Popen([sys.executable, "-m", "assistant", *args], cwd=root))
        return procs[-1]
    yield start
    for p in procs:
        for c in psutil.Process(p.pid).children(recursive=True) if p.poll() is None else []:
            c.kill()
        p.kill()


def _wait_for(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_finds_the_copies_started_from_this_folder_only(folder, spawn):
    root, other = folder("Vesper"), folder("Elsewhere")
    mine = spawn(root)
    spawn(other)  # another folder's
    spawn(root, "--quit")  # the closer itself
    assert _wait_for(lambda: mine.pid in [p.pid for p in vesper_processes(root)])
    own = {mine.pid} | {c.pid for c in psutil.Process(mine.pid).children(recursive=True)}  # a venv launcher's child
    assert {p.pid for p in vesper_processes(root)} <= own


def test_ends_a_copy_that_hung_on_without_its_hud(folder, spawn, monkeypatch):
    root = folder("Vesper")
    stuck = spawn(root)
    assert _wait_for(lambda: vesper_processes(root))
    monkeypatch.setattr(main_mod, "already_running", lambda h, p: False)  # its HUD is gone, the process isn't
    assert quit_running("127.0.0.1", 8765, root / "data", root, wait=5) is True
    assert not psutil.pid_exists(stuck.pid) or psutil.Process(stuck.pid).status() == psutil.STATUS_ZOMBIE
    assert not (root / "closed-real").exists()
    assert vesper_processes(root) == []


def _serving(monkeypatch, pid_of):
    """The HUD answers until /api/quit is posted; /api/quit reports pid_of()."""
    state = {"up": True}

    def post(url, headers, timeout):
        state["up"] = False
        return httpx.Response(200, json={"ok": True, "pid": pid_of()})

    monkeypatch.setattr(main_mod, "already_running", lambda h, p: state["up"])
    monkeypatch.setattr(httpx, "post", post)


def test_waits_for_the_running_copy_to_finish_closing_itself(folder, spawn, monkeypatch):
    root = folder("Vesper")
    launcher = spawn(root, "launch", "1.5")  # closes cleanly 1.5 s in
    def real():
        return next((c for c in psutil.Process(launcher.pid).children(recursive=True)
                     if "launch" not in c.cmdline()), None)
    assert _wait_for(lambda: real() and real().pid in [p.pid for p in vesper_processes(root)])
    real = real()
    _serving(monkeypatch, lambda: real.pid)
    assert quit_running("127.0.0.1", 8765, root / "data", root, wait=10) is True
    # the launcher wasn't ended early (that would cut the real one off mid-shutdown): both closed by themselves
    assert _wait_for(lambda: (root / "closed-launcher").exists())
    assert (root / "closed-real").exists()
    assert vesper_processes(root) == []


def test_ends_the_running_copy_when_it_does_not_close(folder, spawn, monkeypatch):
    root = folder("Vesper")
    hung = spawn(root)
    assert _wait_for(lambda: vesper_processes(root))
    _serving(monkeypatch, lambda: hung.pid)
    started = time.monotonic()
    assert quit_running("127.0.0.1", 8765, root / "data", root, wait=1) is True
    assert time.monotonic() - started < 10
    assert not psutil.pid_exists(hung.pid) or psutil.Process(hung.pid).status() == psutil.STATUS_ZOMBIE
    assert not (root / "closed-real").exists()  # ended, not finished
    assert vesper_processes(root) == []


def test_reports_still_running_when_the_hud_never_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "already_running", lambda h, p: True)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json={"ok": True}))
    assert quit_running("127.0.0.1", 8765, tmp_path, wait=0.3) is False


def test_reuses_the_hud_window_that_reconnects(monkeypatch):
    opened, bus = [], EventBus()
    monkeypatch.setattr(main_mod, "open_hud", opened.append)
    threading.Timer(0.3, bus.subscribe).start()  # the window left open reconnects
    assert open_hud_unless_one_reconnects(bus, "http://x/", wait=2) is False
    assert opened == []


def test_opens_a_window_when_none_was_open(monkeypatch):
    opened = []
    monkeypatch.setattr(main_mod, "open_hud", opened.append)
    ready = {"at": time.monotonic() + 0.3}
    started = time.monotonic()
    assert open_hud_unless_one_reconnects(EventBus(), "http://x/", ready=lambda: time.monotonic() >= ready["at"],
                                          wait=0.3) is True
    assert opened == ["http://x/"]
    assert time.monotonic() - started >= 0.6  # counted from when the server was up, not from launch


def test_a_plain_start_opens_the_window_as_soon_as_the_server_is_up(monkeypatch):
    opened = []
    monkeypatch.setattr(main_mod, "open_hud", opened.append)
    started = time.monotonic()
    assert open_hud_unless_one_reconnects(EventBus(), "http://x/", wait=0) is True
    assert opened == ["http://x/"] and time.monotonic() - started < 0.5


def test_the_hud_learns_vesper_was_updated(cfg, svc):
    app = create_app(Runtime(cfg, services=svc), start_background=False)
    with TestClient(app) as c:
        page = c.get("/").text
        version = c.get("/api/state", headers={"X-Assistant-Token": app.state.token}).json()["hud_version"]
    assert version and f'<meta name="hud-version" content="{version}">' in page
    assert f"app.js?v={version}" in page


def test_quit_reports_its_process(cfg, svc):
    app = create_app(Runtime(cfg, services=svc), start_background=False)
    app.state.on_quit = lambda: None
    with TestClient(app) as c:
        r = c.post("/api/quit", headers={"X-Assistant-Token": app.state.token})
    assert r.json()["pid"] == os.getpid()
