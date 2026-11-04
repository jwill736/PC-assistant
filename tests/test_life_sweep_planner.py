"""Sweep regressions for the week planner (LIFE-11, LIFE-16)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from assistant.skills.planner import build_week

NY = ZoneInfo("America/New_York")
HOURS = {"start": "09:00", "end": "17:00"}
DAYS = [date(2026, 10, 12) + timedelta(days=i) for i in range(7)]  # Mon Oct 12 - Sun Oct 18
NOW = datetime(2026, 10, 12, 8, 0, tzinfo=NY)


def ev(title, day, start, end, profile="work", all_day=False, days=1):
    s = datetime(2026, 10, day, *start, tzinfo=NY)
    e = datetime(2026, 10, day, *end, tzinfo=NY) if not all_day else s + timedelta(days=days)
    return {"title": title, "start": s.isoformat(), "end": e.isoformat(), "all_day": all_day, "profile": profile}


def test_a_work_meeting_with_run_in_its_title_is_a_meeting_not_a_gym_session():
    events = [ev("Dry run: client demo", 15, (17, 0), (18, 45)), ev("Ranked", 15, (19, 0), (23, 0), "stream")]
    w = build_week(events, DAYS, NOW, NY, hours=HOURS, gym={"target": 1, "done": 0})
    thu = next(d for d in w["days"] if d["day"] == "Thursday")
    assert thu["meetings"] == 1 and any(i["kind"] == "clash" for i in thu["issues"]), thu["line"]
    assert w["gym"]["booked"] == 0, f"counted the dry run as a gym session: {w['gym']}"


def test_what_still_counts_as_gym_on_the_calendar():
    events = [ev("Tabletop exercise", 12, (10, 0), (11, 0)), ev("Weights & Biases sync", 13, (10, 0), (11, 0)),
              ev("Gym", 14, (18, 0), (19, 0), "personal"), ev("🔒 Gym", 16, (7, 0), (8, 15))]
    w = build_week(events, DAYS, NOW, NY, hours=HOURS, gym={"target": 3, "done": 0})
    booked = [d["day"] for d in w["days"] if d["gym"] and d["gym"]["booked"]]
    assert booked == ["Wednesday", "Friday"] and w["gym"]["booked"] == 2
    assert [d["meetings"] for d in w["days"][:2]] == [1, 1]


def test_an_all_day_vacation_is_not_a_work_day():
    events = [ev("Vacation", d, (0, 0), (0, 0), all_day=True) for d in range(12, 17)]
    w = build_week(events, DAYS, NOW, NY, hours=HOURS)
    assert not any(d["workday"] for d in w["days"]), [d["line"] for d in w["days"]][:3]
    assert w["days"][0]["line"] == "Monday: day off"


def test_a_multi_day_ooo_and_a_holiday_but_not_any_all_day_event():
    events = [ev("OOO", 13, (0, 0), (0, 0), all_day=True, days=2), ev("Company holiday", 16, (0, 0), (0, 0), all_day=True),
              ev("Mom's birthday", 12, (0, 0), (0, 0), "personal", all_day=True)]
    w = build_week(events, DAYS, NOW, NY, hours=HOURS)
    assert [d["workday"] for d in w["days"][:5]] == [True, False, False, True, False]
