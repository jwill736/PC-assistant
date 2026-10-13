"""Focus sessions: the nudges gate, one gentle drift nudge, the summary, today's numbers and the streak."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import focus as fo

NY = ZoneInfo("America/New_York")


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 13, 10, 0, tzinfo=NY).timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def env(svc):
    said = []
    svc.speak = lambda text, **kw: said.append(text)
    clock = Clock()
    f = fo.Focus(svc, clock=clock)
    return f, clock, said


def visit(svc, app, title, category="other"):
    svc.bus.publish("activity_now", {"app": app, "title": title, "category": category}, sticky=True)


def test_a_session_holds_nudges_flags_drift_once_and_sums_up(env, svc):
    f, clock, said = env
    nud = svc.skills["nudges"]
    r = f.start_session(50, "the q3 deck")
    assert r["ok"] and nud.focus() and nud.gate()["why"] in ("focus session", "away from the PC")
    assert f.say("focus_start", {}, r) == "Focus on: 50 minutes on the q3 deck. I'll hold everything but urgent stuff."
    assert f.start_session(25)["ok"] is False  # one at a time
    clock.t += 600
    visit(svc, "chrome", "Funny cats - YouTube")
    clock.t += 30
    f.tick()
    assert said == []  # a short look is fine
    clock.t += 40
    f.tick()
    assert said == ["That's YouTube. Back to the q3 deck?"]
    clock.t += 30
    visit(svc, "code", "deck.md", "work")
    clock.t += 300
    visit(svc, "chrome", "r/programming - Reddit")
    clock.t += 120
    f.tick()
    assert len(said) == 1  # once per session
    visit(svc, "code", "deck.md", "work")
    clock.t = f.session["end"] + 5
    f.tick()
    assert f.session is None and not nud.focus()
    last = f.last
    assert last["minutes"] == 50 and last["distractions"] == 2 and last["what"] == ["Reddit", "YouTube"]
    assert last["on_task_pct"] == 93 and last["completed"] and last["break_min"] == 10
    assert any(n.text.startswith("Focus done: 50 minutes on the q3 deck, 2 distractions, 93% on task. Take 10")
               for n in nud.queue + nud.feed)
    assert f.today() == {"minutes": 50, "sessions": 1, "distractions": 2}


def test_stop_early_and_stats(env, svc):
    f, clock, said = env
    assert f.stop_session()["ok"] is False
    f.start_session(25)
    clock.t += 20 * 60
    st = f.stats()
    assert st["active"]["left_s"] == 300 and st["today"]["minutes"] == 20
    assert f.say("focus_stats", {}, st) == "20 minutes of focus today over 1 session. 5 minutes left on this one."
    r = f.stop_session()
    assert f.say("focus_stop", {}, r) == ("Focus ended: 20 minutes, no distractions. Take 5: stand up, water, look away "
                                          "from the screen.")
    assert f.streak() == 0  # stopped early doesn't count toward the streak


def test_streak_and_briefing(env, svc):
    f, clock, said = env
    for days_ago in (1, 2):
        start = clock.t - days_ago * 86400
        svc.storage.execute("INSERT INTO focus_sessions(start,end,planned_min,label,completed) VALUES (?,?,?,?,1)",
                            (start, start + 3000, 50, ""))
    assert f.streak() == 2 and f.briefing() == {"yesterday_min": 50, "streak": 2}
    f.start_session(1)
    clock.t += 61
    f.tick()
    assert f.streak() == 3
    svc.live = lambda: True
    assert "secret" not in f.say("focus_start", {}, {"session": {"minutes": 25, "label": "secret plan"}})


def test_what_counts_as_a_distraction(env):
    f, clock, said = env
    assert f.distraction("chrome", "Home / X") == "X"
    assert f.distraction("Discord", "#general", "work") == "Discord"
    assert f.distraction("Discord", "#general", "stream") is None
    assert f.distraction("code", "focus.py - Vesper") is None
    f.cfg["distractions"] = ["steam"]
    assert f.distraction("steam", "Library") == "Steam" and f.distraction("chrome", "YouTube") is None


def test_works_without_nudges(svc):
    svc.skills = Skills(svc, ["focus"])
    f = svc.skills["focus"]
    assert f.start_session(10)["ok"] and f.status()["active"]["minutes"] == 10


@pytest.mark.parametrize("said,tool,args", [
    ("start a 50 minute focus session on the q3 deck", "focus_start", {"minutes": 50, "label": "the q3 deck"}),
    ("start a fifty minute focus session", "focus_start", {"minutes": 50}),
    ("focus mode", "focus_start", {"minutes": 0}),
    ("start focus mode", "focus_start", {"minutes": 0}),
    ("focus for 25 minutes", "focus_start", {"minutes": 25}),
    ("focus on the report for an hour", "focus_start", {"minutes": 60, "label": "the report"}),
    ("start focusing on the newsletter", "focus_start", {"minutes": 0, "label": "the newsletter"}),
    ("stop focus", "focus_stop", {}),
    ("end my focus session", "focus_stop", {}),
    ("how much focus time today", "focus_stats", {}),
    ("how much focus time did i get today", "focus_stats", {}),
    ("how long is left on my focus session", "focus_stats", {}),
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["focus"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


def test_focus_on_a_window_is_left_alone(svc):
    sk = Skills(svc, ["focus"])
    assert route("focus on the report", RouterContext(skill_route=sk.route)).tool == "focus_window"


def test_through_the_assistant(svc):
    from assistant.brain.assistant import Assistant

    a = Assistant(svc)
    assert a.handle("start a 25 minute focus session")["reply"].startswith("Focus on: 25 minutes")
    assert svc.skills["nudges"].focus()
    assert a.handle("stop focus")["reply"].startswith("Focus ended")
    assert a.handle("track a habit read daily")["reply"] == "Tracking read, daily."
    assert a.handle("i read")["reply"] == "Read done."
    assert a.handle("journal: shipped it, felt great")["reply"] == "Saved. Sounds like a good one."
    assert a.handle("am i overbooked")["reply"].startswith("You're fine")
