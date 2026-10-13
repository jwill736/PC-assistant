"""Habits: understood from speech, logged by fuzzy name, streaks for daily and weekly targets, one quiet evening nudge."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import habits as hb

NY = ZoneInfo("America/New_York")


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 13, 9, 0, tzinfo=NY).timestamp()  # a Tuesday

    def __call__(self):
        return self.t


@pytest.fixture
def env(svc):
    clock = Clock()
    h = hb.Habits(svc, clock=clock)
    return h, clock


@pytest.mark.parametrize("text,want", [
    ("track a habit drink water 8 times a day", ("drink water", 8, "day")),
    ("track a habit: workout 3 times a week", ("workout", 3, "week")),
    ("add a habit read daily", ("read", 1, "day")),
    ("new habit stretch twice a day", ("stretch", 2, "day")),
    ("start tracking go to the gym 3x a week", ("go to the gym", 3, "week")),
    ("track a habit meditate five times a week", ("meditate", 5, "week")),
    ("track a habit journal every day", ("journal", 1, "day")),
    ("track a habit relax a day", ("relax", 1, "day")),
])
def test_parse_habit(text, want):
    assert hb.parse_habit(text) == want


def test_log_by_what_you_say_and_progress(env):
    h, clock = env
    h.add("drink water", 8)
    h.add("workout", 3, "week")
    h.add(text="track a habit read daily")
    assert h.match("i drank 2 glasses of water")["name"] == "drink water"
    assert h.match("did my workout")["name"] == "workout" and h.match("i worked out")["name"] == "workout"
    assert h.match("i read")["name"] == "read" and h.match("i called mom") is None
    r = h.log("i drank water", 2)
    assert r["habit"]["count"] == 2 and h.say("habit_log", {}, r) == "Drink water 2 of 8."
    r = h.log("read", 30)  # a once-a-day habit counts once
    assert r["added"] == 1 and r["habit"]["done"] and h.say("habit_log", {}, r) == "Read done."
    r = h.log("workout")
    assert h.say("habit_log", {}, r) == "Workout 1 of 3 this week."
    rep = h.report()
    assert [v["name"] for v in rep["open_today"]] == ["drink water"]
    assert h.say("habit_status", {}, rep) == "1 of 2 daily habits done; still open: drink water 2 of 8; workout 1 of 3 this week."
    assert h.log("floss")["ok"] is False


def test_streaks_daily_and_weekly(env):
    h, clock = env
    h.add("read")
    h.add("workout", 2, "week")
    day = 86400
    for i in (3, 2, 1):  # Sat, Sun, Mon
        clock.t -= i * day
        h.log("read")
        clock.t += i * day
    v = next(x for x in h.views() if x["name"] == "read")
    assert v["streak"] == 3 and not v["done"]  # today still open: the streak waits
    r = h.log("read")
    assert r["habit"]["streak"] == 4 and h.say("habit_log", {}, r) == "Read done. 4-day streak."
    assert [d["n"] for d in r["habit"]["days"]] == [0, 0, 0, 1, 1, 1, 1]
    for i in (8, 7):  # two workouts last week
        clock.t -= i * day
        h.log("workout")
        clock.t += i * day
    w = next(x for x in h.views() if x["name"] == "workout")
    assert w["streak"] == 1 and w["count"] == 0 and w["unit"] == "week"
    assert h.undo(v["id"])["ok"] and h.undo(v["id"])["ok"] is False


def test_evening_nudge_once_and_low_key(env, svc):
    h, clock = env
    h.add("drink water", 8)
    h.tick()
    nud = svc.skills["nudges"]
    assert not any("Still open" in n.text for n in nud.queue + nud.feed)
    clock.t += 12 * 3600  # 9 PM
    h.tick()
    hits = [n for n in nud.queue + nud.feed if "Still open" in n.text]
    assert len(hits) == 1 and hits[0].priority == "low" and not hits[0].spoken
    assert hits[0].text == "Still open today: drink water 0 of 8."
    h.tick()
    assert len([n for n in nud.queue + nud.feed if "Still open" in n.text]) == 1


def test_remove_actions_status_briefing(env):
    h, clock = env
    assert h.briefing() is None and h.say("habit_status", {}, h.report()).startswith("No habits yet")
    r = h.actions()["add"]({"text": "track a habit read daily"})
    assert r["ok"] and h.say("habit_add", {}, r) == "Tracking read, daily."
    hid = r["habit"]["id"]
    assert h.actions()["log"]({"id": hid})["ok"]
    st = h.status()
    assert st["habits"][0]["done"] and len(st["days"]) == 7 and st["days"][-1] == "2026-10-13"
    assert h.briefing() == {"daily": 1, "weekly": 0, "best_streak": {"name": "read", "streak": 1, "unit": "day"}}
    assert h.say("habit_remove", {}, h.remove("reading")) == "Stopped tracking read."
    assert h.status()["habits"] == []


@pytest.mark.parametrize("said,tool,args", [
    ("track a habit drink water 8 times a day", "habit_add", {"name": "drink water", "target": 8, "period": "day"}),
    ("add a habit workout 3 times a week", "habit_add", {"name": "workout", "target": 3, "period": "week"}),
    ("i drank water", "habit_log", {"name": "drink water", "count": 1}),
    ("i drank two glasses of water", "habit_log", {"name": "drink water", "count": 2}),
    ("did my workout", "habit_log", {"name": "workout", "count": 1}),
    ("i just worked out", "habit_log", {"name": "workout", "count": 1}),
    ("how are my habits", "habit_status", {}),
    ("stop tracking workout", "habit_remove", {"name": "workout"}),
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["habits"])
    sk["habits"].add("drink water", 8)
    sk["habits"].add("workout", 3, "week")
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


def test_other_i_sentences_are_not_habits(svc):
    sk = Skills(svc, ["habits"])
    sk["habits"].add("call mom")
    assert sk.route("i need to call dana") is None and sk.route("i called mom") is not None
