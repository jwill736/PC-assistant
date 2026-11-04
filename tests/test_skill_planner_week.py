"""The week plan: work and meetings, stream nights, gym toward the weekly target, and sleep; which days break and one
fix; "book it"; the Sunday-evening preview; DST nights. Every clock is injected, and the calendar, habits and rhythm
are fakes."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import planner as pl

NY = ZoneInfo("America/New_York")
HOURS = {"start": "09:00", "end": "18:00"}


class Clock:
    def __init__(self, *a):
        self.at(*a)

    def __call__(self):
        return self.t

    def at(self, *a):
        self.t = datetime(*a, tzinfo=NY).timestamp()


def ev(title, day, start, end, profile="work", month=10, year=2026):
    """An event on ``day``; an end hour of 24 or more is after midnight."""
    s = datetime(year, month, day, *start, tzinfo=NY)
    e = datetime(year, month, day, 0, 0, tzinfo=NY).replace(tzinfo=None) + timedelta(hours=end[0], minutes=end[1])
    return {"title": title, "start": s.isoformat(), "end": e.replace(tzinfo=NY).isoformat(), "all_day": False,
            "profile": profile, "calendar": profile.title(), "location": ""}


def week(events, first, n=7, now=None, **kw):
    days = [first + timedelta(days=i) for i in range(n)]
    now = now or datetime.combine(first, datetime.min.time(), tzinfo=NY).replace(hour=8)
    return pl.build_week(events, days, now, NY, hours=kw.pop("hours", HOURS), **kw)


def day(w, name):
    return next(d for d in w["days"] if d["day"] == name)


# ---- the pure layout -------------------------------------------------------------------------------------------------

def test_a_normal_week_spreads_gym_after_work_and_flags_the_late_stream():
    evs = [ev("Standup", 13, (10, 0), (10, 30)), ev("Ranked", 13, (20, 0), (23, 0), "stream"),
           ev("Design review", 15, (16, 0), (18, 0)), ev("Ranked", 15, (19, 0), (25, 0), "stream"),
           ev("Ranked", 17, (19, 0), (23, 0), "stream")]
    w = week(evs, date(2026, 10, 12), gym={"target": 3, "done": 0})
    assert [d["gym"]["start"][11:16] if d["gym"] else None for d in w["days"]] == \
        ["18:00", None, "18:00", None, "18:00", None, None]  # Mon/Wed/Fri after work: the stream nights stay free
    assert w["gym"] == {"target": 3, "done": 0, "booked": 0, "planned": 3, "short": 0, "minutes": 75}
    assert w["breaks"] == ["Thursday"]
    assert day(w, "Thursday")["line"] == "Thursday: meeting till 6 PM, stream at 7 PM, bed after 1 AM (6.5 h sleep)"
    assert day(w, "Monday")["line"] == "Monday: gym at 6 PM" and day(w, "Sunday")["line"] == "Sunday: free"
    assert day(w, "Tuesday")["bed"][11:16] == "00:30" and day(w, "Tuesday")["sleep_h"] == 7.5
    assert w["fix"] == {"kind": "stream_end", "day": "Thursday", "at": "2026-10-16T00:00:00-04:00",
                        "text": "End Thursday's stream by midnight to get 7.5 hours of sleep before your 9 AM start."}
    assert w["offer"] == {"title": "Gym", "start": "2026-10-12T18:00:00-04:00", "end": "2026-10-12T19:15:00-04:00",
                          "profile": "personal"}


def test_the_day_that_breaks_and_one_fix():
    """Thursday: meeting till 6, stream at 7, no gym slot, bed after 1 AM."""
    evs = [ev("Client sync", 15, (17, 0), (18, 0)), ev("Ranked", 15, (19, 0), (25, 0), "stream"),
           ev("Chill", 16, (18, 30), (23, 0), "stream")]
    w = week(evs, date(2026, 10, 15), n=4, gym={"target": 3, "done": 0})  # Thursday to Sunday
    assert w["breaks"] == ["Thursday", "Friday"]
    assert day(w, "Thursday")["line"] == "Thursday: meeting till 6 PM, stream at 7 PM, no gym slot, bed after 1 AM (6.5 h sleep)"
    assert day(w, "Friday")["line"] == "Friday: stream at 6:30 PM, no gym slot"
    assert [d["day"] for d in w["days"] if d["gym"]] == ["Saturday", "Sunday"] and w["gym"]["short"] == 1
    assert w["fix"]["kind"] == "stream_end" and w["fix"]["day"] == "Thursday"  # the worst day: two problems


def test_a_meeting_into_the_stream_moves_the_stream_start():
    evs = [ev("Vendor call", 14, (18, 0), (19, 15)), ev("Ranked", 14, (19, 0), (23, 0), "stream")]
    w = week(evs, date(2026, 10, 14), n=1)
    assert day(w, "Wednesday")["line"] == "Wednesday: meeting till 7:15 PM runs into the stream, stream at 7 PM"
    assert w["fix"]["kind"] == "stream_start" and w["fix"]["text"] == \
        "Start Wednesday's stream at 7:45 PM, after the meeting and 30 minutes of prep."
    w = week([ev("Vendor call", 14, (17, 0), (18, 45)), ev("Ranked", 14, (19, 0), (23, 0), "stream")], date(2026, 10, 14), n=1)
    assert day(w, "Wednesday")["issues"][0]["text"] == "meeting till 6:45 PM runs into stream prep"


def test_no_room_for_a_full_session_offers_a_short_one_to_book():
    evs = [ev("Ranked", d, (18, 45), (23, 0), "stream") for d in range(12, 19)]  # streams every night
    w = week(evs, date(2026, 10, 12), n=5, gym={"target": 2, "done": 0}, gym_minutes=90,
             hours={"start": "09:00", "end": "17:00"})
    assert w["gym"]["planned"] == 0 and w["gym"]["short"] == 2 and all(d["issues"] for d in w["days"])
    assert w["fix"]["kind"] == "gym" and w["fix"]["text"] == "Fit a 45-minute gym session in on Monday at 5 PM."
    assert w["offer"] == w["fix"]["book"] == {"title": "Gym", "start": "2026-10-12T17:00:00-04:00",
                                              "end": "2026-10-12T17:45:00-04:00", "profile": "personal"}


def test_gym_already_done_or_booked_counts_and_usual_streams_fill_in():
    evs = [ev("Gym", 13, (7, 0), (8, 0), "personal")]
    usual = {1: ("19:00", "23:00"), 3: ("19:00", "23:00")}  # Tue, Thu, from the rhythm
    w = week(evs, date(2026, 10, 12), gym={"target": 3, "done": 1}, usual=usual)
    assert day(w, "Tuesday")["gym"] == {"title": "Gym", "start": "2026-10-13T07:00:00-04:00",
                                        "end": "2026-10-13T08:00:00-04:00", "booked": True}
    assert w["gym"]["planned"] == 1 and w["gym"]["booked"] == 1
    assert day(w, "Thursday")["stream"]["source"] == "usual" and day(w, "Monday")["stream"] is None
    with_cal = week(evs + [ev("Ranked", 17, (20, 0), (23, 0), "stream")], date(2026, 10, 12), usual=usual)
    assert [d["day"] for d in with_cal["days"] if d["stream"]] == ["Saturday"]  # a stream calendar wins over habit


def test_today_only_plans_what_is_still_ahead():
    w = week([], date(2026, 10, 12), n=1, now=datetime(2026, 10, 12, 18, 10, tzinfo=NY), gym={"target": 1, "done": 0})
    assert day(w, "Monday")["gym"]["start"][11:16] == "18:15"


def test_the_usual_bedtime_and_the_morning_after():
    w = week([ev("Early call", 13, (7, 30), (8, 0))], date(2026, 10, 12), n=2, sleep={"bedtime": "00:45"})
    assert day(w, "Monday")["up"][11:16] == "06:30" and day(w, "Monday")["line"] == "Monday: bed after midnight (6 h sleep)"
    assert day(w, "Tuesday")["bed"][11:16] == "00:45" and day(w, "Tuesday")["issues"] == []  # 7.25 h: a normal night


@pytest.mark.parametrize("first,evs,bed,sleep_h,broken", [
    # fall back: Sat Oct 31 -> Sun Nov 1 has 25 hours; bed at the first 1 AM (EDT), wake 8 AM EST is 8 real hours
    (date(2026, 10, 31), [ev("Late one", 31, (20, 0), (24, 30), "stream")], "2026-11-01T01:00:00-04:00", 8.0, False),
    # ...and a stream to 1:30 AM EDT plus wind-down is bed at the second 1 AM (EST): 7 hours, not the wall clock's 6
    (date(2026, 10, 31), [ev("Later", 31, (20, 0), (25, 30), "stream")], "2026-11-01T01:00:00-05:00", 7.0, True),
    # spring forward: Sat Mar 7 -> Sun Mar 8 has 23 hours; bed 1 AM EST, wake 8 AM EDT is 6 real hours
    (date(2026, 3, 7), [ev("Late one", 7, (20, 0), (24, 30), "stream", month=3)], "2026-03-08T01:00:00-05:00", 6.0, True),
])
def test_dst_nights_are_their_real_length(first, evs, bed, sleep_h, broken):
    w = week(evs, first, n=1, sleep={"bedtime": "00:30"})
    d = w["days"][0]
    assert (d["bed"], d["sleep_h"], bool(d["issues"])) == (bed, sleep_h, broken)


def test_a_bedtime_dst_skips_and_work_hours_across_the_change():
    w = week([], date(2026, 3, 7), n=3, sleep={"bedtime": "02:30"})
    sat = day(w, "Saturday")
    assert sat["bed"] == "2026-03-08T03:30:00-04:00"  # 2:30 doesn't exist that night: just after the jump
    mon = day(w, "Monday")
    assert mon["work"] == {"start": "2026-03-09T09:00:00-04:00", "end": "2026-03-09T18:00:00-04:00"}


# ---- the skill ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def env(svc):
    clock = Clock(2026, 10, 14, 8, 0)  # a Wednesday
    events = [ev("Client sync", 15, (17, 0), (18, 0)), ev("Ranked", 15, (19, 0), (25, 0), "stream")]
    asked = []

    def agenda(days=1, profile=None, start=None):
        asked.append((days, start))
        return list(events)
    svc.calendars.agenda = agenda
    svc.calendars.status = lambda: [{"name": "Work"}]
    svc.skills["habits"].clock = clock
    svc.skills["memory"].rhythm = lambda: {"ok": False}
    p = pl.Planner(svc, clock=clock)
    return p, clock, events, asked


def test_plan_my_week_from_the_calendar_habits_and_settings(env, svc):
    p, clock, events, asked = env
    svc.skills["habits"].add(text="track a habit: gym 3 times a week")
    svc.skills["habits"].log("gym")
    w = p.plan_week()
    assert w["from"] == "2026-10-14" and w["to"] == "2026-10-18" and not w["next"]
    assert asked[0] == (7, datetime(2026, 10, 13, tzinfo=NY))  # the night before, through the morning after
    assert w["gym"]["done"] == 1 and w["gym"]["planned"] == 2 and w["sources"]["gym"] == "gym 3x a week"
    assert any("Bedtime: the latest" in n for n in w["notes"])
    said = p.say("plan_week", {}, w)
    assert said.startswith("This week: 3 work days, 1 stream (Thursday), gym 3 of 3, Wednesday at 6 PM, Friday at 6 PM.")
    assert "Thursday: meeting till 6 PM, stream at 7 PM, bed after 1 AM (6.5 h sleep)." in said
    assert "One fix: End Thursday's stream by midnight" in said
    assert said.endswith("Say “book it” to put Wednesday's gym at 6 PM on your calendar.")
    assert p.status()["week"] is w and p.status()["can_book"]


def test_am_i_overbooked_this_week_says_which_day_breaks(env, svc):
    p, *_ = env
    sk = Skills(svc, ["planner"])
    intent = route("am i overbooked this week", RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == ("am_i_overbooked", {"period": "this week"})
    r = p.overbooked("this week")
    assert r["period"] == "week" and r["breaks"] == ["Thursday"]
    assert p.say("am_i_overbooked", {}, r) == ("1 day breaks. Thursday: meeting till 6 PM, stream at 7 PM, bed after 1 AM "
                                               "(6.5 h sleep). One fix: End Thursday's stream by midnight to get 7.5 hours "
                                               "of sleep before your 9 AM start.")
    svc.live = lambda: True
    assert p.say("am_i_overbooked", {}, r) == "This week: 1 day won't fit. It's on the HUD."
    svc.live = lambda: False
    assert "meetings_min" in p.overbooked() and "period" not in p.overbooked()  # today's check is unchanged


def test_a_week_that_fits(env, svc):
    p, clock, events, _ = env
    events.clear()
    r = p.overbooked("this week")
    assert r["breaks"] == [] and r["fix"] is None
    assert p.say("am_i_overbooked", {}, r) == "This week works: 7.5 hours of sleep every night, around work and the streams."
    assert any("No stream nights found" in n for n in r["notes"]) and any("No gym target" in n for n in r["notes"])


def test_stream_nights_from_settings_or_the_rhythm(env, svc):
    p, clock, events, _ = env
    events.clear()
    svc.skills["memory"].rhythm = lambda: {"ok": True, "stream": {"days": ["Thu", "Sat"], "start": "19:00", "end": "00:30"},
                                           "bedtime": {"time": "00:45"}, "wake": {"time": "08:30"}}
    w = p.plan_week()
    assert [d["day"] for d in w["days"] if d["stream"]] == ["Thursday", "Saturday"]
    assert w["sources"] == {"stream": "rhythm", "gym": None, "bedtime": "00:45"}
    assert any("from your rhythm" in n for n in w["notes"])
    p.cfg["stream_nights"] = {"days": ["fri"], "start": "20:00", "end": "23:00"}
    assert [d["day"] for d in p.plan_week()["days"] if d["stream"]] == ["Friday"]


def test_sunday_plans_next_week_and_next_week_on_request(env, svc):
    p, clock, *_ = env
    assert p.plan_week("next")["from"] == "2026-10-19"
    clock.at(2026, 10, 18, 11, 0)  # Sunday
    w = p.plan_week()
    assert w["next"] and (w["from"], w["to"]) == ("2026-10-19", "2026-10-25") and len(w["days"]) == 7


def test_no_calendar_or_a_broken_one_still_plans(env, svc):
    p, *_ = env
    svc.calendars.agenda = lambda *a, **k: []
    svc.calendars.status = lambda: []
    assert any("No calendar connected" in n for n in p.plan_week()["notes"])

    def boom(*a, **k):
        raise OSError("offline")
    svc.calendars.agenda = boom
    w = p.plan_week()
    assert w["ok"] and any("didn't load (OSError)" in n for n in w["notes"])


def test_book_it_is_the_tier_2_focus_block_with_the_time_pinned(env, svc):
    p, clock, *_ = env
    svc.skills["habits"].add(text="track a habit: gym 3 times a week")
    w = p.plan_week()
    intent = route("book it", RouterContext(skill_route=p.route))
    assert (intent.tool, intent.args) == ("protect_time", w["offer"])
    fb = svc.skills["focusblocks"]
    tool = next(t for t in fb.tools() if t.name == "protect_time")
    fb.calendar_for = lambda profile: ({"account": "j@gmail.com", "id": "j@gmail.com", "name": "J"}, "")
    a = dict(intent.args)
    assert tool.tier_for(a) == 2 and a["start"] == "2026-10-14T18:00:00-04:00" and a["calendar"] == "j@gmail.com"
    assert "6–7:15 PM" in fb._describe(dict(intent.args))
    bad = {"title": "Gym", "start": "2026-10-14T18:00:00-04:00", "end": "2026-10-14T17:00:00-04:00", "profile": "personal"}
    assert tool.tier_for(bad) == 0 and "doesn't look right" in fb.protect_time(**bad)["error"]
    clock.t += 3600  # half an hour on, "book it" means nothing any more
    assert p.route("book it") is None
    assert p.book()["ok"] is False


def test_the_hud_book_button_asks_for_the_same_yes(env, svc):
    p, *_ = env
    svc.skills["habits"].add(text="track a habit: gym 2 times a week")
    p.plan_week()
    ran = []
    p.runtime = type("RT", (), {"assistant": type("A", (), {"run_tool": lambda self, n, a: ran.append((n, a)) or
                                                             {"kind": "pending", "reply": "Put it on? Say yes or no."}})()})()
    assert p.actions()["book"]({}) == {"ok": True, "pending": True, "reply": "Put it on? Say yes or no."}
    assert ran == [("protect_time", p.week["offer"])]


def test_sunday_evening_preview_once_through_the_nudge_gate(env, svc):
    p, clock, *_ = env
    got = []
    svc.nudge = lambda text, **kw: got.append((text, kw))
    clock.at(2026, 10, 18, 17, 50)
    p.tick()
    assert got == []
    clock.at(2026, 10, 18, 18, 5)
    p.tick()
    p.tick()
    assert len(got) == 1
    text, kw = got[0]
    assert text.startswith("Next week: 0 streams") and text.endswith("Say “plan my week” for the rest.")
    assert kw["priority"] == "normal" and kw["key"] == "planner:week:2026-10-19"  # normal: quiet hours hold it
    assert kw["action"] == {"label": "Plan my week", "command": "plan my week"} and kw["why"]["rule"]
    clock.at(2026, 10, 25, 18, 5)
    p.cfg["week_preview"] = ""
    p.tick()
    assert len(got) == 1


def test_the_preview_line_names_the_first_day_that_breaks(env, svc):
    p, clock, events, _ = env
    events[:] = [ev("Client sync", 22, (17, 0), (18, 0)), ev("Ranked", 22, (19, 0), (25, 0), "stream")]
    clock.at(2026, 10, 18, 18, 5)
    line = p.preview_line(p.plan_week("next"))
    assert line == ("Next week: 1 stream. 1 day won't fit, starting Thursday: meeting till 6 PM, stream at 7 PM, "
                    "bed after 1 AM (6.5 h sleep). Say “plan my week” for the rest.")


@pytest.mark.parametrize("said,tool,args", [
    ("plan my week", "plan_week", {"week": "this"}),
    ("plan next week", "plan_week", {"week": "next"}),
    ("plan out my week", "plan_week", {"week": "this"}),
    ("preview next week", "plan_week", {"week": "next"}),
    ("balance my week", "plan_week", {"week": "this"}),
    ("am i overbooked next week", "am_i_overbooked", {"period": "next week"}),
    ("is my week too packed", "am_i_overbooked", {"period": "this week"}),
    ("am i overbooked", "am_i_overbooked", {}),
    ("plan my day", "plan_day", {"day": "today"}),
])
def test_week_routes(svc, said, tool, args):
    sk = Skills(svc, ["planner"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


def test_book_it_without_a_week_plan_is_not_ours(svc):
    sk = Skills(svc, ["planner"])
    intent = route("book it", RouterContext(skill_route=sk.route))
    assert intent is None or intent.tool != "protect_time"


@pytest.mark.parametrize("said,args", [
    ("gym 3 times a week", {"name": "gym", "target": 3, "period": "week"}),
    ("gym 3x a week", {"name": "gym", "target": 3, "period": "week"}),
    ("set my gym target to four times a week", {"name": "gym", "target": 4, "period": "week"}),
    ("i want to go to the gym three times a week", {"name": "gym", "target": 3, "period": "week"}),
])
def test_the_gym_target_by_voice_is_one_weekly_habit(svc, said, args):
    sk = Skills(svc, ["habits"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == ("habit_add", args)


def test_the_gym_target_updates_the_habit_already_tracked(svc):
    hab = svc.skills["habits"]
    hab.add(text="track a habit: workout 2 times a week")
    sk = Skills(svc, ["habits"])
    intent = route("gym 4 times a week", RouterContext(skill_route=sk.route))
    assert intent.args == {"name": "workout", "target": 4, "period": "week"}
    hab.add(**intent.args)
    assert [(h["name"], h["target"]) for h in hab._habits()] == [("workout", 4)]
