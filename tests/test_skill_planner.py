"""Planner: plan my day, am I overbooked, what now, shut down for the day, weekly review."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import planner as pl

NY = ZoneInfo("America/New_York")


class Clock:
    def __init__(self, dt):
        self.t = dt.timestamp()

    def __call__(self):
        return self.t

    def at(self, *a):
        self.t = datetime(*a, tzinfo=NY).timestamp()


def ev(title, start, end, profile="work", all_day=False, day=13):
    s = datetime(2026, 10, day, *start, tzinfo=NY)
    e = datetime(2026, 10, day, *end, tzinfo=NY)
    return {"title": title, "start": s.isoformat(), "end": e.isoformat(), "all_day": all_day, "profile": profile,
            "calendar": profile.title(), "location": ""}


EVENTS = [ev("Payday", (0, 0), (23, 59), all_day=True), ev("Standup", (10, 0), (10, 30)),
          ev("Client sync", (14, 0), (15, 0)), ev("Ranked night", (20, 0), (23, 0), profile="stream")]


@pytest.fixture
def env(svc):
    clock = Clock(datetime(2026, 10, 13, 8, 30, tzinfo=NY))  # a Tuesday
    svc.calendars.agenda = lambda days=1, profile=None, start=None: list(EVENTS)
    svc.calendars.status = lambda: [{"name": "Work"}]
    st = svc.storage
    ids = {
        "deck": st.add_task("Q3 deck (2h)", priority=1, due="2026-10-13")["id"],
        "news": st.add_task("Newsletter edit", priority=2)["id"],
        "exp": st.add_task("Expense report", priority=3)["id"],
        "overlay": st.add_task("Stream overlay", profile="stream", priority=2)["id"],
    }
    p = pl.Planner(svc, clock=clock)
    return p, clock, ids


def times(plan, kind="task"):
    return [(b["title"], b["start"][11:16], b["end"][11:16]) for b in plan["blocks"] if b["kind"] == kind]


def test_plan_lays_tasks_into_free_blocks_without_a_model(env):
    p, clock, ids = env
    plan = p.plan_day()
    assert times(plan) == [("Q3 deck (2h)", "09:00", "10:00"), ("Q3 deck (2h)", "10:30", "11:30"),
                           ("Newsletter edit", "11:30", "12:15"), ("Expense report", "12:15", "13:00"),
                           ("Stream overlay", "18:00", "18:45")]
    assert times(plan, "stream") == [("Stream prep", "19:30", "20:00"), ("Ranked night", "20:00", "23:00")]
    assert [b["kind"] for b in plan["blocks"]].count("meeting") == 2 and plan["all_day"] == ["Payday"]
    assert ("Open", "13:00", "14:00") in times(plan, "free")
    assert plan["load"] == {"meetings_min": 90, "tasks_min": 120, "capacity_min": 540, "free_min": 450,
                            "over_min": 0, "overbooked": False}
    assert plan["by"] == "local" and any("No model" in n for n in plan["notes"])
    said = p.say("plan_day", {}, plan)
    assert said == "2 meetings and 5 task blocks. First: Q3 deck (2h) at 9 AM. Stream at 8 PM."


def test_the_model_only_reorders(env, svc):
    p, clock, ids = env
    asked = []
    svc.think = lambda prompt, schema=None, **kw: asked.append(prompt) or {"order": [ids["exp"], 999, ids["deck"]],
                                                                         "headline": "Clear the small stuff first."}
    plan = p.plan_day()
    assert times(plan)[0] == ("Expense report", "09:00", "09:45") and plan["by"] == "model"
    assert "Q3 deck (2h)" in asked[0] and plan["headline"] == "Clear the small stuff first."
    svc.think = lambda *a, **k: None
    assert times(p.plan_day(polish=False))[0][0] == "Expense report"  # the model's order sticks for the day


def test_overbooked_and_what_to_push(env, svc):
    p, clock, ids = env
    for i in range(3):
        svc.storage.add_task(f"Migration part {i} (2h)", priority=1)
    r = p.overbooked()
    assert r["overbooked"] and r["tasks_min"] == 480 and r["over_min"] == 30
    assert r["push"] == ["Migration part 0 (2h)"]
    assert p.say("am_i_overbooked", {}, r) == ("1 hour 30 minutes in meetings and 8 hours of must-do tasks: that's "
                                               "30 minutes over. Push Migration part 0 (2h) to tomorrow?")


def test_what_now_and_next(env):
    p, clock, ids = env
    p.plan_day()
    clock.at(2026, 10, 13, 10, 45)
    r = p.what_next()
    assert r["now"]["title"] == "Q3 deck (2h)" and r["next"]["title"] == "Newsletter edit"
    assert p.say("what_next", {}, r) == "Right now: Q3 deck (2h) until 11:30 AM. Then: Newsletter edit at 11:30 AM."


def test_shutdown_recaps_carries_over_and_offers_stream_mode(env, svc):
    p, clock, ids = env
    clock.at(2026, 10, 13, 17, 45)
    svc.storage.execute("UPDATE tasks SET status='done', done_at=? WHERE id=?",
                        (datetime(2026, 10, 13, 12, 0, tzinfo=NY).timestamp(), ids["news"]))
    svc.storage.add_activity(datetime(2026, 10, 13, 9, tzinfo=NY).timestamp(),
                             datetime(2026, 10, 13, 11, tzinfo=NY).timestamp(), "code", "planner.py", "work")
    r = p.shutdown_day()
    assert r["done"] == ["Newsletter edit"] and r["work_min"] == 120 and r["deep_min"] == 120
    assert r["carried"] == ["Q3 deck (2h)"] and r["carried_to"] == "2026-10-14"
    assert next(t for t in svc.storage.list_tasks() if t["id"] == ids["deck"])["due"] == "2026-10-14"
    assert r["offer"] == {"label": "Switch to stream mode", "command": "start stream mode"}
    said = p.say("shutdown_day", {}, r)
    assert said.startswith("Shutting down. You closed 1 task, 2 hours on work, 2 hours of it deep. 1 task rolls to Wednesday.")
    assert "Want stream mode?" in said
    ran = []
    p.run_command = lambda text: ran.append(text) or {"reply": "Stream mode is up."}
    assert p.actions()["stream_mode"]({}) == {"ok": True, "reply": "Stream mode is up."} and ran == ["start stream mode"]
    assert p.status()["shutdown"]["offer"] is None


def test_shutdown_with_nothing_connected(svc):
    p = pl.Planner(svc, clock=Clock(datetime(2026, 10, 13, 18, 0, tzinfo=NY)))
    r = p.shutdown_day()
    assert r["offer"] is None and not r["tracked"]
    assert p.say("shutdown_day", {}, r) == "Shutting down. You closed 0 tasks."
    plan = p.status()["plan"]
    assert any("No calendar connected" in n for n in plan["notes"]) and any("No open tasks" in n for n in plan["notes"])


def test_weekly_review_and_friday_nudge(env, svc):
    p, clock, ids = env
    clock.at(2026, 10, 16, 16, 5)  # Friday
    for d in (12, 13, 14):
        svc.storage.add_activity(datetime(2026, 10, d, 9, tzinfo=NY).timestamp(),
                                 datetime(2026, 10, d, 12, tzinfo=NY).timestamp(), "code", "x", "work")
    svc.storage.add_activity(datetime(2026, 10, 14, 20, tzinfo=NY).timestamp(),
                             datetime(2026, 10, 14, 22, tzinfo=NY).timestamp(), "obs64", "Live", "stream")
    svc.storage.execute("UPDATE tasks SET status='done', done_at=? WHERE id=?",
                        (datetime(2026, 10, 15, 12, tzinfo=NY).timestamp(), ids["exp"]))
    r = p.weekly_review()
    assert r["hours"] == {"work": 9.0, "stream": 2.0} and r["active_h"] == 11.0 and r["tasks_done"] == 1
    assert r["meetings"] == 2 and r["streams"] == 1 and r["deep_sessions"] == 3
    assert r["wins"][0] == "1 task closed" and r["next_week"][0] == "Q3 deck (2h)"
    assert p.say("weekly_review", {}, r).startswith("This week: 11.0 hours active, 9.0 on work, 1 task done, 2 meetings, 1 stream.")
    p.tick()
    nud = svc.skills["nudges"]
    assert any("weekly review" in n.text for n in nud.queue + nud.feed)
    svc.think = lambda *a, **k: {"wins": ["Deck shipped"], "leaks": [], "next_week": ["Launch", "Hire", "Rest"]}
    assert p.weekly_review()["next_week"] == ["Launch", "Hire", "Rest"]


def test_status_briefing_and_estimates(env):
    p, clock, ids = env
    st = p.status()
    assert st["plan"]["date"] == "2026-10-13" and st["shutdown"] is None
    assert p.briefing() == {"first": ["9 AM Q3 deck (2h)", "10:30 AM Q3 deck (2h)", "11:30 AM Newsletter edit"],
                            "overbooked": None, "stream": "8 PM"}
    assert pl.estimate({"title": "Fix bug (30m)"}) == 30 and pl.estimate({"title": "Edit ~1.5h"}) == 90
    assert pl.estimate({"title": "Plain"}, 45) == 45 and pl.next_workday(date(2026, 10, 16)) == date(2026, 10, 19)
    assert times(p.plan_day("tomorrow"))[0][1] == "09:00"


@pytest.mark.parametrize("said,tool,args", [
    ("plan my day", "plan_day", {"day": "today"}),
    ("plan tomorrow", "plan_day", {"day": "tomorrow"}),
    ("replan", "plan_day", {"day": "today"}),
    ("what should i work on now", "what_next", {}),
    ("what should i work on next", "what_next", {}),
    ("am i overbooked", "am_i_overbooked", {}),
    ("am i overbooked today", "am_i_overbooked", {}),
    ("shut down for the day", "shutdown_day", {}),
    ("end my work day", "shutdown_day", {}),
    ("i'm done for the day", "shutdown_day", {}),
    ("weekly review", "weekly_review", {}),
    ("how did my week go", "weekly_review", {}),
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["planner"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


def test_close_app_still_routes(svc):
    sk = Skills(svc, ["planner"])
    assert route("shut down discord", RouterContext(skill_route=sk.route)).tool == "close_app"
