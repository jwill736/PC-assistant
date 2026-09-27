"""Real Windows API calls. These only run on Windows (CI's windows-latest), which
is the one place the ctypes window/idle code, Start Menu discovery and powercfg
actually execute."""

import sys
import time

import pytest
from fastapi.testclient import TestClient

from assistant.integrations import desktop
from assistant.integrations.system import SystemMonitor

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows API paths")


@windows_only
def test_where_am_i_enumerates_windows():
    info = desktop.where_am_i()
    assert set(info) == {"active", "open_windows", "apps"}
    assert isinstance(info["open_windows"], int)
    for app, titles in info["apps"].items():
        assert isinstance(app, str) and all(isinstance(t, str) for t in titles)


@windows_only
def test_idle_seconds_reads_last_input():
    idle = desktop.idle_seconds()
    assert isinstance(idle, float) and idle >= 0


@windows_only
def test_launcher_resolves_system_apps():
    launcher = desktop.AppLauncher({"notes": {"path": "notepad.exe", "process": "notepad"}})
    assert launcher.resolve("cmd")["kind"] == "exe"  # System32 is always on PATH
    assert launcher.resolve("notes")["kind"] == "alias"
    assert isinstance(launcher.shortcut_index(), dict)  # walks the real Start Menu folders
    assert launcher.resolve("zzz-no-such-app-zzz")["kind"] == "missing"
    assert "notepad" in launcher.process_names_for("notes")


@windows_only
def test_system_snapshot_sees_c_drive_and_power_plan():
    mon = SystemMonitor()
    snap = mon.snapshot()
    assert any(d["mount"].upper().startswith("C:") for d in snap["disks"])
    assert snap["processes"]["count"] > 0
    plan = mon.active_power_plan()
    assert plan is None or isinstance(plan, str)


def test_runtime_boots_pollers_and_tracks_activity(cfg, svc):
    """Full runtime with background pollers, as start.bat runs it (voice off)."""
    from assistant.runtime import Runtime
    from assistant.server import create_app

    cfg["tracking"]["sample_seconds"] = 0.2
    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=True)
    with TestClient(app) as client:
        client.headers["X-Assistant-Token"] = app.state.token
        rt.svc.activity.tick()  # one real foreground-window sample
        assert rt.svc.activity.current is not None
        deadline = time.time() + 10
        state = {}
        while time.time() < deadline:
            state = client.get("/api/state").json()
            if state.get("system") and state.get("activity"):
                break
            time.sleep(0.2)
    assert state["system"]["cpu"]["threads"] >= 1
    assert "by_category" in state["activity"]
    assert rt.svc.activity.current is None  # stop() flushed the open segment


@windows_only
def test_tray_icon_renders_and_pystray_imports():
    import pystray  # noqa: F401  (installed from requirements on Windows)

    from assistant.tray import COLORS, Tray

    for state in COLORS:
        img = Tray.image(state)
        assert img.size == (64, 64) and img.getpixel((32, 32))[3] == 255  # solid core
