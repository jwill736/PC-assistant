"""Phase 4a: deterministic system controls (fakes stand in for pycaw, the brightness library and pyvda)."""

from types import SimpleNamespace

from assistant.brain.router import number, route
from assistant.integrations import controls


class Endpoint:
    """pycaw's IAudioEndpointVolume, as far as we use it."""

    def __init__(self, level=0.5, muted=0):
        self.level, self.muted = level, muted

    def GetMasterVolumeLevelScalar(self):
        return self.level

    def SetMasterVolumeLevelScalar(self, v, _ctx):
        self.level = v

    def GetMute(self):
        return self.muted

    def SetMute(self, m, _ctx):
        self.muted = m


def test_volume_sets_exact_levels_moves_and_unmutes():
    ep = Endpoint(0.5, muted=1)
    assert controls.volume(30, endpoint=ep) == {"ok": True, "volume": 30, "muted": False}  # "volume 30" unmutes
    assert controls.volume(change=-40, endpoint=ep)["volume"] == 0
    assert controls.volume(change=250, endpoint=ep)["volume"] == 100
    assert controls.volume(mute=True, endpoint=ep)["muted"] is True
    assert controls.volume(endpoint=ep) == {"ok": True, "volume": 100, "muted": True}  # just reports


def test_volume_falls_back_to_the_volume_keys(monkeypatch):
    presses = []
    monkeypatch.setattr(controls, "IS_WINDOWS", True)
    monkeypatch.setattr(controls, "_endpoint", lambda: (_ for _ in ()).throw(ImportError("no pycaw")))
    monkeypatch.setattr(controls.desktop, "media_key", lambda action, times=1: presses.append((action, times)) or {"ok": True})
    out = controls.volume(30)
    assert out["approximate"] and out["volume"] == 30
    assert presses == [("volume_down", 25), ("volume_down", 25), ("volume_up", 15)]  # bottom out, then climb
    presses.clear()
    controls.volume(change=-10)
    assert presses == [("volume_down", 5)]


class Session:
    def __init__(self, name, level=1.0):
        self.Process = SimpleNamespace(name=lambda: name) if name else None
        self.SimpleAudioVolume = Endpoint(level)
        vol = self.SimpleAudioVolume
        vol.GetMasterVolume, vol.SetMasterVolume = vol.GetMasterVolumeLevelScalar, vol.SetMasterVolumeLevelScalar


def test_app_volume_changes_only_the_named_app():
    sessions = [Session("Discord.exe"), Session("Spotify.exe"), Session(None)]
    out = controls.app_volume("discord", level=20, sessions=sessions)
    assert out == {"ok": True, "apps": [{"app": "discord", "volume": 20, "muted": False}]}
    assert sessions[1].SimpleAudioVolume.level == 1.0
    assert controls.app_volume("spotify", mute=True, sessions=sessions)["apps"][0]["muted"] is True
    missing = controls.app_volume("steam", level=5, sessions=sessions)
    assert not missing["ok"] and "Discord, Spotify" in missing["error"].title()


def test_brightness_reports_sets_and_explains_monitors_that_refuse():
    state = {"b": [40, 60]}
    sbc = SimpleNamespace(get_brightness=lambda: state["b"],
                          set_brightness=lambda v: state.update(b=[v] * len(state["b"])))
    assert controls.brightness(sbc=sbc) == {"ok": True, "brightness": [40, 60]}
    assert controls.brightness(change=30, sbc=sbc) == {"ok": True, "brightness": [70, 70]}
    broken = SimpleNamespace(get_brightness=lambda: (_ for _ in ()).throw(RuntimeError("no WMI")))
    out = controls.brightness(50, sbc=broken)
    assert not out["ok"] and "DDC/CI" in out["error"]


def test_settings_pages_match_loosely_but_only_known_pages_open():
    opened = []
    assert controls.open_settings("Sound settings", opener=opened.append)["uri"] == "ms-settings:sound"
    assert controls.open_settings("blutooth", opener=opened.append)["page"] == "bluetooth"
    assert controls.open_settings("update", opener=opened.append)["page"] == "windows update"
    assert not controls.open_settings("registry editor", opener=opened.append)["ok"]
    assert opened == ["ms-settings:sound", "ms-settings:bluetooth", "ms-settings:windowsupdate"]


def test_virtual_desktops_with_pyvda_and_the_shortcut_fallback():
    state = {"cur": 1}

    class VD:
        def __init__(self, n):
            self.number = n

        @staticmethod
        def current():
            return VD(state["cur"])

        def go(self):
            state["cur"] = self.number
    vda = SimpleNamespace(get_virtual_desktops=lambda: [1, 2, 3], VirtualDesktop=VD)
    assert controls.virtual_desktop("next", vda=vda) == {"ok": True, "desktop": 2, "count": 3}
    assert controls.virtual_desktop("go", 3, vda=vda)["desktop"] == 3
    assert not controls.virtual_desktop("next", vda=vda)["ok"]  # there's no desktop 4
    chords = []
    assert controls.virtual_desktop("previous", vda=None, chord=lambda *k: chords.append(k))["moved"] == "previous"
    assert chords == [(controls.VK_CONTROL, controls.VK_LWIN, controls.VK_LEFT)]


def test_spoken_numbers_and_new_voice_commands():
    assert [number(w) for w in ("40", "forty", "forty-five", "a hundred", "seven", "lots")] == [40, 40, 45, 100, 7, None]
    cases = {
        "volume 30": ("set_volume", {"level": 30}),
        "set the volume to forty five percent": ("set_volume", {"level": 45}),
        "discord volume 20": ("app_volume", {"app": "discord", "level": 20}),
        "brightness seventy": ("set_brightness", {"level": 70}),
        "dimmer": ("set_brightness", {"change": -20}),
        "open bluetooth settings": ("open_settings", {"page": "bluetooth"}),
        "go to desktop two": ("virtual_desktop", {"action": "go", "number": 2}),
        "next desktop": ("virtual_desktop", {"action": "next"}),
    }
    for text, (tool, args) in cases.items():
        intent = route(text)
        assert (intent.tool, intent.args) == (tool, args), text
    assert route("go to discord").tool == "focus_window"  # "go to <window>" still works
    assert route("mute discord").tool == "obs_set_mute"   # the stream-audio meaning wins


def test_health_check_lists_missing_control_libraries(cfg):
    from assistant import doctor

    def importer(name):
        if name == "pyvda":
            raise ImportError(name)
    c = doctor.check_pc_control(cfg, importer=importer)
    assert c.status == doctor.WARN and "numbered virtual desktops" in c.detail and "ctrl+alt+k" in c.detail
    assert doctor.check_pc_control(cfg, importer=lambda n: None).status == doctor.PASS
