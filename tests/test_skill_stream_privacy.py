"""On-air safety net: private while live (or recording a screen capture that shows the HUD), the Stream-tab switch,
Vesper's own Windows toasts held back while private, the OBS screen-capture scan, and the voice toggle. Fake OBS and
fake toasts: nothing real."""

import httpx
import pytest

from assistant.brain.router import RouterContext, route
from assistant.bus import EventBus
from assistant.integrations.obs import OBSController
from assistant.integrations.twitch import TwitchClient
from assistant.skills import Skills
from assistant.skills import stream_privacy as sp


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


class FakeOBS:
    def __init__(self, captures):
        self.captures, self.calls = captures, 0

    def screen_captures(self):
        self.calls += 1
        return list(self.captures)


@pytest.fixture
def env(svc):
    clock, state = Clock(), {"live": False}
    svc.bus = EventBus()
    svc.live = lambda: state["live"]
    svc.obs = FakeOBS([])
    p = sp.StreamPrivacy(svc, clock=clock)
    return p, clock, state


def obs(svc, recording=False, streaming=False):
    svc.bus.publish("obs", {"connected": True, "streaming": {"active": streaming}, "recording": {"active": recording}}, sticky=True)


def test_private_while_live_and_the_switch(env, svc):
    p, clock, state = env
    assert p.status()["private"] is False and p.status()["on"] is True
    state["live"] = True
    st = p.status()
    assert st["private"] and st["why"] == "live" and "inbox" in st["panels"] and "bills" in st["panels"]
    assert p.actions()["toggle"]({"on": False}) == {"ok": True, "on": False}
    assert p.status()["private"] is False and p.status()["on"] is False
    assert (svc.cfg.data_dir / "settings.yaml").read_text().count("when_live: false") == 1  # kept across restarts
    p.actions()["toggle"]({})  # no value: flip
    assert p.on is True


def test_recording_a_screen_capture_counts_too(env, svc):
    p, clock, state = env
    svc.obs.captures = [{"name": "Chat popout", "kind": "window_capture", "display": False, "window": "Twitch chat:Chrome_WidgetWin_1:chrome.exe"}]
    obs(svc, recording=True)
    p.tick()
    assert p.status()["private"] is False  # a browser window that isn't the HUD
    svc.obs.captures.append({"name": "Desktop", "kind": "monitor_capture", "display": True, "window": ""})
    clock.t += 31
    p.tick()
    st = p.status()
    assert st["private"] and st["why"] == "recording your screen"
    assert [c["name"] for c in st["captures"] if c["shows_hud"]] == ["Desktop"]
    obs(svc, recording=False)
    assert p.status()["private"] is False  # captured, but nothing is being recorded or streamed
    clock.t += 5
    p.tick()
    assert svc.obs.calls == 2  # OBS is asked every 30 s, not every tick


def test_exposes_hud():
    assert sp.exposes_hud({"display": True}, "Friday HUD")
    assert sp.exposes_hud({"display": False, "window": "Friday HUD - Google Chrome:Chrome_WidgetWin_1:chrome.exe"}, "Friday HUD")
    assert not sp.exposes_hud({"display": False, "window": "VALORANT:UnrealWindow:VALORANT-Win64-Shipping.exe"}, "Friday HUD")
    assert not sp.exposes_hud({"display": False, "window": ""}, "Friday HUD")


def test_windows_toasts_wait_while_private(env, svc):
    p, clock, state = env
    shown = []
    rt = type("RT", (), {})()
    rt.assistant = type("A", (), {"on_pending": lambda self, pending: shown.append(pending)})()
    rt.assistant.on_pending = lambda pending: shown.append(pending)
    p.start(rt)
    p.start(rt)  # starting twice doesn't wrap twice
    rt.assistant.on_pending({"id": "1", "text": "close Chrome"})
    assert shown == [{"id": "1", "text": "close Chrome"}]
    state["live"] = True
    assert rt.assistant.on_pending({"id": "2", "text": "close Chrome"}) is None and len(shown) == 1
    p.toggle(False)
    rt.assistant.on_pending({"id": "3", "text": "close Chrome"})
    assert len(shown) == 2
    rt2 = type("RT", (), {})()
    rt2.assistant = type("A", (), {"on_pending": None})()
    p.start(rt2)  # no toasts on this PC: nothing to wrap
    assert rt2.assistant.on_pending is None


@pytest.mark.parametrize("text,on", [("turn off privacy while live", False), ("privacy mode on", True), ("turn on privacy", True)])
def test_routes(svc, text, on):
    intent = route(text, RouterContext(skill_route=Skills(svc, ["stream_privacy"]).route))
    assert (intent.tool, intent.args) == ("privacy_while_live", {"on": on})


def test_say(env, svc):
    p, clock, state = env
    assert p.say("privacy_while_live", {}, p.toggle(False)).startswith("Privacy while live is off")
    assert p.say("privacy_while_live", {}, p.toggle(True)).startswith("Privacy while live is on")


# ---- the integration helpers it (and the raid planner, and the moments) use ----------------------------------------

def test_obs_screen_captures_walks_nested_scenes_and_groups():
    o = OBSController(enabled=True)
    lists = {
        "Gameplay": [{"sourceName": "Game", "inputKind": "game_capture", "sceneItemEnabled": True},
                     {"sourceName": "Overlay", "sourceType": "OBS_SOURCE_TYPE_SCENE", "sceneItemEnabled": True},
                     {"sourceName": "Old desktop", "inputKind": "monitor_capture", "sceneItemEnabled": False},
                     {"sourceName": "Pip", "isGroup": True, "sceneItemEnabled": True}],
        "Overlay": [{"sourceName": "Desktop", "inputKind": "monitor_capture", "sceneItemEnabled": True}],
        "Pip": [{"sourceName": "HUD", "inputKind": "window_capture", "sceneItemEnabled": True}],
    }

    def send(request, data=None):
        if request == "GetCurrentProgramScene":
            return {"currentProgramSceneName": "Gameplay"}
        if request in ("GetSceneItemList", "GetGroupSceneItemList"):
            return {"sceneItems": lists[data["sceneName"]]}
        if request == "GetInputSettings":
            return {"inputSettings": {"window": "Friday HUD:Chrome_WidgetWin_1:chrome.exe"}}
        raise AssertionError(request)
    o._send = send
    assert o.screen_captures() == [
        {"name": "Desktop", "kind": "monitor_capture", "display": True, "window": ""},
        {"name": "HUD", "kind": "window_capture", "display": False, "window": "Friday HUD:Chrome_WidgetWin_1:chrome.exe"}]


def test_twitch_live_streams_raid_and_marker_id(tmp_path):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, dict(req.url.params)))
        if req.url.path.endswith("/validate"):
            return httpx.Response(200, json={"client_id": "cid", "login": "jstreams", "user_id": "42", "scopes": []})
        if req.url.path.endswith("/users"):
            return httpx.Response(200, json={"data": [{"id": "77"}]})
        if req.url.path.endswith("/streams") and req.method == "GET":
            return httpx.Response(200, json={"data": [{"user_login": "ana", "viewer_count": 45}]})
        if req.url.path.endswith("/raids"):
            return httpx.Response(200, json={"data": [{"created_at": "now", "is_mature": False}]})
        if req.url.path.endswith("/streams/markers"):
            return httpx.Response(200, json={"data": [{"id": "mk-9", "position_seconds": 61, "description": "x"}]})
        return httpx.Response(404)
    tw = TwitchClient("", "cid", http=httpx.Client(transport=httpx.MockTransport(handler)), token_path=tmp_path / "t.json")
    tw.auth.token = {"access_token": "a", "refresh_token": "r", "expires_at": 9e12, "validated_at": 9e12, "user_id": "42",
                     "login": "jstreams", "scopes": ["channel:manage:raids", "channel:manage:broadcast"]}
    assert tw.live_streams("123", language="en", first=500) == [{"user_login": "ana", "viewer_count": 45}]
    assert ("GET", "/helix/streams", {"game_id": "123", "type": "live", "first": "100", "language": "en"}) in seen
    assert tw.start_raid("Ana") == {"ok": True, "user": "ana", "is_mature": False}
    assert ("POST", "/helix/raids", {"from_broadcaster_id": "42", "to_broadcaster_id": "77"}) in seen
    assert tw.create_marker("x")["id"] == "mk-9"
    tw.auth.token["scopes"] = ["channel:manage:broadcast"]
    assert "doesn't allow raids" in tw.start_raid("ana")["error"]  # an old login: reconnect to allow raids
