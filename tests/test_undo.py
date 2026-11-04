""""Undo that": the action history kept by ToolBox.run, each tool's own inverse, the exact earlier value put back,
nothing run for what can't be undone, never fighting a change J made himself since, and the tiers (kill switch, a
yes) applying to the undo like any action. Windows APIs, OBS, Spotify and Google are faked."""

from types import SimpleNamespace

import pytest

from assistant.brain.assistant import Assistant
from assistant.brain.tools import Tool, obj
from assistant.brain.undo import ActionHistory, Undo
from assistant.integrations import controls, desktop


class Endpoint:
    def __init__(self, level=0.35, muted=1):
        self.level, self.muted = level, muted

    def GetMasterVolumeLevelScalar(self):
        return self.level

    def SetMasterVolumeLevelScalar(self, v, _ctx):
        self.level = v

    def GetMute(self):
        return self.muted

    def SetMute(self, m, _ctx):
        self.muted = m


class FakeOBS:
    """obs-websocket's requests, as far as the OBS tools use them."""

    def __init__(self):
        self.current = "Gaming"
        self.items = {"Gaming": {"Webcam": True, "Chat": True}, "BRB": {"Webcam": False}}
        self.inputs = {"Mic/Aux": False, "Desktop Audio": False}

    def __call__(self, request, data=None):
        data = data or {}
        if request == "GetCurrentProgramScene":
            return {"currentProgramSceneName": self.current}
        if request == "SetCurrentProgramScene":
            self.current = data["sceneName"]
        if request == "GetSceneItemList":
            return {"sceneItems": [{"sceneItemId": i, "sourceName": n, "sceneItemEnabled": e}
                                   for i, (n, e) in enumerate(self.items[data["sceneName"]].items())]}
        if request == "SetSceneItemEnabled":
            name = list(self.items[data["sceneName"]])[data["sceneItemId"]]
            self.items[data["sceneName"]][name] = data["sceneItemEnabled"]
        if request == "GetInputList":
            return {"inputs": [{"inputName": n} for n in self.inputs]}
        if request == "GetInputMute":
            return {"inputMuted": self.inputs[data["inputName"]]}
        if request == "SetInputMute":
            self.inputs[data["inputName"]] = data["inputMuted"]
        if request == "ToggleInputMute":
            self.inputs[data["inputName"]] = not self.inputs[data["inputName"]]
        return {}


@pytest.fixture
def ep(monkeypatch):
    e = Endpoint()
    monkeypatch.setattr(controls, "IS_WINDOWS", True)
    monkeypatch.setattr(controls, "_endpoint", lambda: e)
    return e


@pytest.fixture
def obs(svc):
    fake = FakeOBS()
    svc.obs.enabled, svc.obs.scenes, svc.obs._send = True, ["Gaming", "BRB"], fake
    return fake


@pytest.fixture
def vesper(svc):
    a = Assistant(svc)
    svc.skills["undo"].start(SimpleNamespace(assistant=a))
    return a


# ---- the history itself ------------------------------------------------------------------------------------------

def test_the_history_puts_back_newest_first_and_says_what_it_did():
    h, log = ActionHistory(clock=lambda: 100.0), []
    h.add("a", "first", 1, Undo("A back", lambda: log.append("a") or {"ok": True}))
    h.add("b", "second", 1, Undo("B back", lambda: log.append("b") or {"ok": True}))
    assert h.pick(2) == [2, 1] and h.tier([2, 1]) == 1
    out = h.undo(h.pick(2))
    assert log == ["b", "a"] and out["message"] == "Undone: B back, A back."
    assert h.pick(1) == [] and h.undo([])["message"] == "There's nothing to undo yet."
    assert [v["state"] for v in h.view()] == ["undone", "undone"]


def test_it_keeps_one_session_only_and_tells_the_hud():
    seen = []
    h = ActionHistory(keep=3)
    h.on_change = lambda: seen.append(1)
    for i in range(5):
        h.add("t", f"action {i}", 1, None)
    assert [v["label"] for v in h.view()] == ["action 4", "action 3", "action 2"] and len(seen) == 5


# ---- through Vesper: the hook in ToolBox.run ------------------------------------------------------------------------

def test_undo_puts_the_volume_back_exactly_as_it_was(vesper, ep):
    assert vesper.handle("set volume to 40")["kind"] == "tool"
    assert (round(ep.level * 100), ep.muted) == (40, 0)  # "volume 40" unmutes
    out = vesper.handle("undo that")
    assert out["reply"] == "Undone: the volume back to 35%, muted."
    assert (round(ep.level * 100), ep.muted) == (35, 1)
    assert vesper.handle("undo that")["reply"] == "There's nothing to undo yet."


def test_the_volume_keys_can_be_undone_too(vesper, ep, monkeypatch):
    def key(action, times=1):  # Windows moves it 2 points a press; Vesper reads it before and after
        ep.level, ep.muted = ep.level + 0.02 * times, 0
        return {"ok": True, "action": action}
    monkeypatch.setattr(desktop, "media_key", key)
    vesper.handle("volume up")
    assert round(ep.level * 100) == 45
    assert vesper.handle("undo that")["reply"] == "Undone: the volume back to 35%, muted."
    assert (round(ep.level * 100), ep.muted) == (35, 1)


def test_undo_never_fights_a_change_j_made_himself(vesper, ep):
    vesper.handle("set volume to 40")
    ep.level = 0.6  # J turned it up himself afterwards
    out = vesper.handle("undo that")
    assert out["reply"] == "The volume is 60% now, not 40%, so I left it."
    assert round(ep.level * 100) == 60


def test_reading_a_setting_is_not_an_action(vesper, ep):
    vesper.handle("what's the volume")
    assert vesper.tools.history.view() == []


def test_what_cant_be_undone_is_said_and_nothing_runs(vesper, svc, monkeypatch):
    sent = []
    monkeypatch.setattr(svc.twitch, "send_chat", lambda message: sent.append(message) or {"ok": True})
    vesper.run_tool("twitch_chat_send", {"message": "gg"}, confirmed="voice")
    out = vesper.handle("undo that")
    assert out["reply"] == "I can't undo say in chat: “gg”; that can't be taken back." and sent == ["gg"]
    view = vesper.tools.history.view()
    assert view[0]["can_undo"] is False and view[0]["undo"] is None


def test_undo_the_last_three(vesper, svc, ep):
    old = svc.storage.add_task("Old one")
    vesper.run_tool("add_task", {"title": "Buy milk"})
    vesper.run_tool("set_volume", {"level": 70})
    vesper.run_tool("complete_task", {"id": old["id"]})
    out = vesper.handle("undo the last 3")
    assert out["reply"].startswith("Undone: “Old one” open again, the volume back to 35%, muted, “Buy milk” taken off")
    assert [t["title"] for t in svc.storage.list_tasks()] == ["Old one"]
    assert round(ep.level * 100) == 35


def test_a_task_j_finished_since_is_left_alone(vesper, svc):
    t = vesper.run_tool("add_task", {"title": "Buy milk"})["data"]["task"]
    svc.storage.update_task(t["id"], status="done")
    out = vesper.handle("undo that")
    assert out["reply"] == "The task “Buy milk” is done now, not open, so I left it."
    assert svc.storage.list_tasks("done")[0]["title"] == "Buy milk"


def test_a_timer_or_reminder_is_cancelled(vesper, svc):
    vesper.run_tool("set_timer", {"seconds": 600, "label": "pasta"})
    assert len(svc.skills["automations"].items) == 1
    assert vesper.handle("undo that")["reply"] == "Undone: the timer “pasta” cancelled."
    assert svc.skills["automations"].items == []


def test_obs_scene_source_and_mute_go_back(vesper, obs):
    vesper.run_tool("obs_switch_scene", {"scene": "BRB"})
    assert vesper.handle("undo that")["reply"] == "Undone: the OBS scene back to Gaming."
    assert obs.current == "Gaming"
    vesper.run_tool("obs_source", {"source": "webcam", "visible": False})
    vesper.run_tool("obs_set_mute", {"source": "mic", "muted": True})
    out = vesper.handle("undo the last 2")
    assert out["reply"] == "Undone: Mic/Aux back to unmuted, Webcam shown again."
    assert obs.items["Gaming"]["Webcam"] is True and obs.inputs["Mic/Aux"] is False


def test_an_obs_mute_that_changed_nothing_stays_as_it_was(vesper, obs):
    obs.inputs["Mic/Aux"] = True
    vesper.run_tool("obs_set_mute", {"source": "mic", "muted": True})  # already muted
    vesper.handle("undo that")
    assert obs.inputs["Mic/Aux"] is True  # the exact earlier value, not the opposite


def test_a_source_on_a_scene_j_left_is_not_touched(vesper, obs):
    vesper.run_tool("obs_source", {"source": "webcam", "visible": False})
    obs.current = "BRB"
    out = vesper.handle("undo that")
    assert out["reply"] == "Webcam is hidden (you're on BRB) now, not hidden, so I left it."
    assert obs.items["Gaming"]["Webcam"] is False


def test_spotify_volume_goes_back_even_while_spotify_still_reports_the_old_level(vesper, svc):
    sp = svc.skills["spotify"]
    state = {"reported": 50, "set": []}
    sp.now_playing = lambda: {"ok": True, "volume": state["reported"]}
    sp.volume = lambda level=None, change=None: state["set"].append(level) or {"ok": True, "level": level}
    vesper.tools.tools["spotify_volume"].handler = sp.volume
    vesper.run_tool("spotify_volume", {"level": 20})  # Spotify goes on saying 50 for a moment after a change
    state["reported"] = 20
    assert vesper.handle("undo that")["reply"] == "Undone: Spotify's volume back to 50%."
    assert state["set"] == [20, 50]


def test_a_minimised_window_goes_back_where_it_was(vesper, monkeypatch):
    disc = desktop.WindowInfo("Discord", "discord", 7, 200)
    place = {200: (3, (0, 0, 800, 600), (-1, -1), (-1, -1), 0)}  # maximised
    monkeypatch.setattr(desktop, "list_windows", lambda: [disc])
    monkeypatch.setattr(desktop, "_show", lambda h, cmd: place.update({h: (2, *place[h][1:])}) or True)
    monkeypatch.setattr(desktop, "_placement", lambda h: place.get(h))
    monkeypatch.setattr(desktop, "_set_placement", lambda h, p: place.update({h: p}) or True)
    assert vesper.handle("minimise discord")["reply"] == "Minimised discord."
    assert place[200][0] == 2
    assert vesper.handle("undo that")["reply"] == "Undone: discord back to maximised."
    assert place[200][0] == 3


def test_bringing_a_window_forward_can_be_undone(vesper, monkeypatch):
    chrome, disc = desktop.WindowInfo("Docs", "chrome", 1, 100), desktop.WindowInfo("Discord", "discord", 2, 200)
    front = {"w": chrome}
    monkeypatch.setattr(desktop, "list_windows", lambda: [chrome, disc])
    monkeypatch.setattr(desktop, "active_window", lambda: front["w"])
    monkeypatch.setattr(desktop, "_focus", lambda h: front.update(w=chrome if h == 100 else disc) or True)
    vesper.handle("switch to discord")
    assert front["w"] is disc
    assert vesper.handle("undo that")["reply"] == "Undone: chrome back in front."
    assert front["w"] is chrome


def test_hud_undo_button_puts_the_front_window_back(vesper, svc, monkeypatch):
    """INTEG-11: clicking the HUD's Undo button makes the HUD the front window, so "the front window is the HUD now,
    not discord" and the undo was refused. It looks past the HUD, at the window behind it."""
    name = str((svc.cfg.get("assistant") or {}).get("name") or "Vesper")
    chrome, disc = desktop.WindowInfo("Docs", "chrome", 1, 100), desktop.WindowInfo("Discord", "discord", 2, 200)
    hud = desktop.WindowInfo(f"{name} HUD", "chrome", 3, 300)
    z = [chrome, disc, hud]  # front-most first, as Windows lists them

    def focus(h):
        w = next(x for x in z if x.handle == h)
        z.remove(w)
        z.insert(0, w)
        return True
    monkeypatch.setattr(desktop, "list_windows", lambda: list(z))
    monkeypatch.setattr(desktop, "active_window", lambda: z[0])
    monkeypatch.setattr(desktop, "_focus", focus)
    vesper.handle("switch to discord")
    assert z[0] is disc
    item = vesper.tools.history.view()[0]
    focus(300)  # J clicks the HUD's Undo button: the HUD window is now in front, discord right behind it
    out = svc.skills["undo"].actions()["undo"]({"id": item["id"]})
    assert z[0] is chrome and out.get("ok") is not False, out
    vesper.handle("switch to discord")
    item = vesper.tools.history.view()[0]
    focus(100)  # J went back to chrome himself, then clicked Undo on the HUD: what he chose since is left alone
    focus(300)
    out = svc.skills["undo"].actions()["undo"]({"id": item["id"]})
    assert out.get("ok") is False and "not discord" in out["error"]


# ---- the tiers --------------------------------------------------------------------------------------------------

def test_the_kill_switch_stops_an_undo_too(vesper, ep):
    vesper.handle("set volume to 40")
    vesper.handle("stop everything")
    assert "paused" in vesper.handle("undo that")["reply"]
    assert round(ep.level * 100) == 40
    vesper.handle("resume control")
    vesper.handle("undo that")
    assert round(ep.level * 100) == 35


def test_an_undo_that_writes_to_the_calendar_asks_first(svc, vesper):
    fb = svc.skills["focusblocks"]
    svc.storage.execute("INSERT INTO focusblocks_made(account, calendar, event_id, title, start, end_at, link, ts) "
                        "VALUES ('j@work.com', 'primary', 'ev1', '🔒 Deck', '2026-11-04T14:00', '2026-11-04T16:00', '', 0)")
    deleted = []
    svc.google = SimpleNamespace(delete_event=lambda acct, cal, eid: deleted.append((acct, cal, eid)))
    vesper.tools.history.add("protect_time", "block 2–4 pm", 2, fb._undo_blocks({"id": "ev1"}))
    out = vesper.handle("undo that")
    assert out["kind"] == "pending" and "focus block taken off your calendar" in out["reply"] and deleted == []
    vesper.handle("yes")
    assert deleted == [("j@work.com", "primary", "ev1")]
    assert svc.storage.query("SELECT * FROM focusblocks_made") == []


def test_the_hud_undo_button_goes_through_the_same_door(vesper, svc, ep):
    vesper.handle("set volume to 40")
    item = svc.skills["undo"].status()["actions"][0]
    assert item["can_undo"] and item["undo"] == "the volume back to 35%, muted"
    out = svc.skills["undo"].actions()["undo"]({"id": item["id"]})
    assert out["ok"] and round(ep.level * 100) == 35
    assert any(e["tool"] == "undo" for e in vesper.tools.audit.tail(5))  # in the action log like anything else


def test_a_journal_entry_is_listed_without_its_words():
    t = Tool("journal_add", "", obj(), lambda text: {"ok": True}, tier=1)
    assert t.label({"text": "today was rough"}) == "(journal entry)"
    assert Tool("set_volume", "", obj(), lambda: {}, tier=1).label({"level": 30}) == "set volume · 30"
