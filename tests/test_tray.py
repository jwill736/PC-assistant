from types import SimpleNamespace

from assistant.bus import EventBus
from assistant.tray import COLORS, Tray, tray_state


def test_state_priorities():
    assert tray_state("listening", False, False, 0) == "listening"
    assert tray_state("listening", True, False, 0) == "speaking"
    assert tray_state("listening", False, True, 0) == "thinking"
    assert tray_state("transcribing", False, False, 0) == "hearing"
    assert tray_state("muted", False, False, 0) == "muted"
    assert tray_state("listening", True, False, 2) == "attention"   # problems outrank activity
    assert tray_state("error", False, False, 0) == "error"
    assert tray_state("disabled", False, False, 0) == "off"
    assert set(COLORS) >= {"listening", "attention", "error", "muted", "off"}


class FakeListener:
    def __init__(self):
        self.state, self.muted, self.armed = "listening", False, 0

    def arm(self):
        self.armed += 1

    def set_muted(self, muted):
        self.muted = muted
        self.state = "muted" if muted else "listening"


def fake_runtime(tmp_path, listener=True):
    bus = EventBus()
    health = [{"name": "obs", "state": "ok"}]
    cfg_dict = {"assistant": {"name": "Friday", "wake_words": ["friday"]}}
    cfg_obj = type("Cfg", (dict,), {"data_dir": tmp_path})(cfg_dict)
    rt = SimpleNamespace(
        bus=bus, cfg=cfg_obj, listener=FakeListener() if listener else None,
        supervisor=SimpleNamespace(snapshot=lambda: health),
        svc=SimpleNamespace(activity=SimpleNamespace(paused=False)),
        rescan=lambda: None,
    )
    return rt, health


def test_menu_actions_drive_runtime(tmp_path):
    rt, _ = fake_runtime(tmp_path)
    quit_called = []
    tray = Tray(rt, "http://127.0.0.1:8765/", on_quit=lambda: quit_called.append(1))
    items = {i.label: i for i in tray.menu_items() if not i.separator}
    assert items["Open HUD"].default
    items["Push to talk"].action()
    assert rt.listener.armed == 1
    items["Mute microphone"].action()
    assert rt.listener.muted and items["Mute microphone"].checked()
    items["Pause activity tracking"].action()
    assert rt.svc.activity.paused and rt.bus.latest["tracking"]["data"] == {"paused": True}
    items["Quit Friday"].action()
    assert quit_called == [1]


def test_menu_without_voice(tmp_path):
    rt, _ = fake_runtime(tmp_path, listener=False)
    labels = [i.label for i in Tray(rt, "u", lambda: None).menu_items()]
    assert "Push to talk" not in labels and "Mute microphone" not in labels


def test_state_follows_bus_events(tmp_path):
    rt, health = fake_runtime(tmp_path)
    tray = Tray(rt, "u", lambda: None)
    rt.bus.publish("speaking", {"active": True})
    assert tray.state == "speaking"
    rt.bus.publish("speaking", {"active": False})
    assert tray.state == "listening"
    health[0]["state"] = "error"
    rt.bus.publish("health", health, sticky=True)
    assert tray.state == "attention" and "needs attention" in tray.tooltip()


def test_already_running_detects_hud(monkeypatch):
    import httpx

    from assistant import __main__ as entry

    monkeypatch.setattr(httpx, "get", lambda url, timeout: SimpleNamespace(status_code=200, text="<title>Friday HUD</title>"))
    assert entry.already_running("127.0.0.1", 8765)
    monkeypatch.setattr(httpx, "get", lambda url, timeout: SimpleNamespace(status_code=200, text="some other app"))
    assert not entry.already_running("127.0.0.1", 8765)

    def refuse(url, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", refuse)
    assert not entry.already_running("127.0.0.1", 8765)
