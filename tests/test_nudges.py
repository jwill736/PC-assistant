"""Nudges: queued until a good moment, never spoken on air, budgeted, and quiet for kinds J keeps dismissing."""

from datetime import datetime, timedelta

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills.nudges import Nudges, in_quiet_hours, streak


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(svc, monkeypatch):
    said, events = [], []
    svc.speak = lambda text, **kw: said.append(text)
    svc.bus.on(lambda ev: events.append(ev) if ev["type"] == "notice" else None)
    state = {"idle": 0.0, "fs": "", "live": False}
    svc.live = lambda: state["live"]
    clock = Clock()
    n = Nudges(svc, clock=clock, idle=lambda: state["idle"], fullscreen=lambda: state["fs"])
    n.cfg["quiet_hours"] = "00:00-00:00"
    return n, clock, state, said, events


def test_waits_for_a_break_then_speaks(env):
    n, clock, state, said, events = env
    n.add("Dana replied about the contract.", source="email")
    assert said == [] and len(n.queue) == 1
    n._last_break = clock.t - 60
    n.deliver()
    assert said == []  # no pause yet
    n.deliver(at_break=True)
    assert said == ["Dana replied about the contract."] and events[-1]["data"]["spoken"]
    assert n.queue == [] and n.feed[0].text.startswith("Dana")


def test_never_spoken_on_air_and_held_in_a_game(env):
    n, clock, state, said, events = env
    state["live"] = True
    n.add("Standup starts in 5 minutes.", priority="urgent", source="calendar")
    assert said == [] and events[-1]["data"]["text"].startswith("Standup")  # on the HUD, not in the stream audio
    state["live"], state["fs"] = False, "game"
    n.add("Break time.", source="break")
    n.deliver(at_break=True)
    assert len(n.queue) == 1 and n.queue[0].held == "in a game"
    state["fs"] = ""
    n.deliver(at_break=True)
    assert said == ["Break time."]


def test_budget_spacing_and_duplicates(env):
    n, clock, state, said, events = env
    assert n.add("Same thing.") is not None and n.add("Same thing.") is None
    n.deliver(at_break=True)
    n.add("Second thing.")
    n.deliver(at_break=True)
    assert said == ["Same thing."]  # too soon after the last one: shown, not spoken
    assert n.feed[0].text == "Second thing." and not n.feed[0].spoken


def test_dismissed_kinds_go_quiet(env, tmp_path):
    n, clock, state, said, events = env
    for i in range(3):
        clock.t += 1000
        x = n.add(f"Stretch {i}", source="break")
        n.deliver(at_break=True)
        assert n.feedback(x.key, useful=False)["ok"]
    assert n._quiet_source("break")
    clock.t += 1000
    n.add("Stretch again", source="break")
    n.deliver(at_break=True)
    assert "Stretch again" not in said and any(e["data"]["text"] == "Stretch again" for e in events)


def test_welcome_back_after_being_away(env):
    n, clock, state, said, events = env
    state["idle"] = 1800
    n.tick()
    clock.t += 10
    state["idle"] = 5
    n.tick()
    assert any(s.startswith("Welcome back. You were away 30 minutes") for s in said)


def test_mute_and_quiet_hours(env):
    n, clock, state, said, events = env
    n.mute(30)
    n.add("Hello.")
    n.deliver(at_break=True)
    assert said == [] and n.gate()["why"].startswith("muted until")
    n.mute(0)
    n.deliver(at_break=True)
    assert said == ["Hello."]
    assert in_quiet_hours(datetime(2026, 1, 1, 23, 30), "23:00-07:30")
    assert in_quiet_hours(datetime(2026, 1, 1, 6, 0), "23:00-07:30")
    assert not in_quiet_hours(datetime(2026, 1, 1, 12, 0), "23:00-07:30")


def test_meeting_soon_rule(env, svc):
    n, clock, state, said, events = env
    start = datetime.now(svc.tz) + timedelta(minutes=8)
    svc.calendars.agenda = lambda days, **kw: [{"title": "Client sync", "start": start.isoformat(), "all_day": False,
                                                "location": "Zoom"}]
    n._rules(clock.t)
    assert said and said[0].startswith("Client sync starts in 8 minutes")


def test_streak():
    segs = [{"start": 0, "end": 3000, "category": "work"}, {"start": 3100, "end": 6000, "category": "work"}]
    assert streak(segs, 6010) == 6010
    assert streak(segs + [{"start": 6000, "end": 7000, "category": "idle"}], 7100) == 0


@pytest.mark.parametrize("said,tool,args", [
    ("what did i miss", "what_did_i_miss", {}),
    ("catch me up", "what_did_i_miss", {}),
    ("quiet mode", "quiet_mode", {"minutes": 60}),
    ("do not disturb for 30 minutes", "quiet_mode", {"minutes": 30}),
    ("hold my notifications for two hours", "quiet_mode", {"minutes": 120}),
    ("you can talk again", "quiet_mode", {"minutes": 0}),
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["nudges"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)
