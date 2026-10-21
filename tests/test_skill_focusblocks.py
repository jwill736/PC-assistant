"""Focus blocks: "protect 2 hours tomorrow for the Q3 deck" finds free time across the calendars and, after J's yes to
the exact time, puts a busy "🔒" event on the right Google calendar. The Day plan's blocks can go on too."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fake_workspace import ALL_BUT, FakeWorkspace, accounts_for
from test_google import HOME, WORK

from assistant.brain.assistant import Assistant
from assistant.brain.router import RouterContext, route
from assistant.skills.focusblocks import parse_minutes, parse_request, span_text


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def fake():
    return FakeWorkspace()


@pytest.fixture
def fb(svc, fake, tmp_path):
    svc.google = accounts_for(tmp_path, fake)
    tz = svc.tz
    now = datetime(2026, 10, 21, 10, 7, tzinfo=tz)  # Wednesday 10:07
    tomorrow = now.date() + timedelta(days=1)

    def at(d, h, m=0):
        return datetime(d.year, d.month, d.day, h, m, tzinfo=tz).isoformat()
    events = {now.date(): [{"title": "Standup", "start": at(now.date(), 9), "end": at(now.date(), 9, 30), "all_day": False},
                           {"title": "Client call", "start": at(now.date(), 11), "end": at(now.date(), 12), "all_day": False},
                           {"title": "Gym", "start": at(now.date(), 13), "end": at(now.date(), 17), "all_day": False}],
              tomorrow: [{"title": "Sprint planning", "start": at(tomorrow, 9), "end": at(tomorrow, 10, 30), "all_day": False},
                         {"title": "Lunch", "start": at(tomorrow, 12), "end": at(tomorrow, 13), "all_day": False},
                         {"title": "Offsite", "start": at(tomorrow, 0), "end": at(tomorrow + timedelta(days=1), 0), "all_day": True}]}
    svc.calendars.agenda = lambda days=1, profile=None, start=None: list(events.get((start or now).date(), []))
    svc.calendars.refresh = lambda force=False: None
    svc.cfg["google"] = {**(svc.cfg.get("google") or {}), "calendars": [
        {"account": WORK, "id": WORK, "name": "Work", "profile": "work"},
        {"account": HOME, "id": "family@group.calendar.google.com", "name": "Family", "profile": "personal"},
        {"account": HOME, "id": HOME, "name": "J", "profile": "personal"}]}
    skill = svc.skills["focusblocks"]
    skill.clock = Clock(now.timestamp())
    skill.events = events
    return skill


def test_parses_what_j_says():
    assert parse_request("protect 2 hours tomorrow for the q3 deck") == {"title": "Q3 deck", "minutes": 120, "day": "tomorrow"}
    assert parse_request("block my afternoon for deep work") == {"title": "Deep work", "part": "afternoon"}
    assert parse_request("block 90 minutes this afternoon") == {"title": "Focus time", "minutes": 90, "part": "afternoon",
                                                                "day": "today"}
    assert parse_request("protect an hour and a half on friday for video editing")["profile"] == "personal"
    assert parse_minutes("an hour and a half") == 90 and parse_minutes("half an hour") == 30
    for no in ("block youtube for 30 minutes", "block distracting sites", "protect my privacy", "book a flight"):
        assert parse_request(no) is None, no


def test_finds_the_slot_and_asks_with_the_exact_time(fb, svc, fake):
    a = Assistant(svc)
    out = a.handle("protect 2 hours tomorrow for the Q3 deck")
    assert out["kind"] == "pending" and fake.events == []  # nothing written before the yes
    assert "put “🔒 Q3 deck” on your work calendar tomorrow (Thu Oct 22), 10:30 AM–12:30 PM" not in out["reply"]
    assert "tomorrow (Thu Oct 22), 1–3 PM" in out["reply"] and "work calendar" in out["reply"]  # 10:30–12 is too short
    done = a.handle("yes")
    [ev] = fake.events
    assert ev["account"] == WORK and ev["calendar"] == WORK
    b = ev["body"]
    assert b["summary"] == "🔒 Q3 deck" and b["transparency"] == "opaque"
    assert b["start"]["dateTime"].startswith("2026-10-22T13:00") and b["end"]["dateTime"].startswith("2026-10-22T15:00")
    assert done["reply"].startswith("Done: 🔒 Q3 deck is on your work calendar tomorrow, 1 PM to 3 PM")
    assert fb.status()["made"][0]["title"] == "🔒 Q3 deck"


def test_block_my_afternoon_takes_the_longest_free_stretch(fb, svc, fake):
    fb.clock.t += 86400  # Thursday 10:07: the afternoon after lunch is free until 6
    r = fb.protect_time(part="afternoon", title="Deep work")
    assert r["ok"] and r["start"].startswith("2026-10-22T13:00") and r["end"].startswith("2026-10-22T18:00")


def test_personal_goes_on_the_personal_calendar(fb, svc, fake):
    r = fb.protect_time(minutes=60, day="tomorrow", title="Gym", profile="personal")
    assert r["ok"] and fake.events[0]["account"] == HOME and fake.events[0]["calendar"] == HOME  # his own, not Family
    fb.cfg["calendars"] = {"personal": {"account": HOME, "id": "family@group.calendar.google.com"}}  # chosen in settings
    fb.protect_time(minutes=30, day="tomorrow", title="School run", profile="personal")
    assert fake.events[1]["calendar"] == "family@group.calendar.google.com"


def test_no_room_says_so_without_asking(fb, svc, fake):
    a = Assistant(svc)
    out = a.handle("protect 3 hours today for the budget")  # today after 10:07: 10:15–11, 12–1, 5–6
    assert out["kind"] == "tool" and "No free 3h stretch today" in out["reply"] and "longest is 1h" in out["reply"]
    assert a.pending_view() is None and fake.events == []


def test_a_clash_since_the_question_is_caught(fb, svc, fake):
    a = Assistant(svc)
    a.handle("protect 2 hours tomorrow for the q3 deck")
    day = sorted(fb.events)[1]
    fb.events[day].append({"title": "New meeting", "start": f"{day}T14:00:00-04:00", "end": f"{day}T14:30:00-04:00",
                           "all_day": False})
    out = a.handle("yes")
    assert "just got booked" in out["reply"] and fake.events == []


def test_needs_the_calendar_events_box(svc, fake, tmp_path, fb):
    fake.grant[WORK] = ALL_BUT["Calendar events"]
    svc.google = accounts_for(tmp_path, fake)
    r = fb.protect_time(minutes=60, day="tomorrow")
    assert r["ok"] is False and WORK in r["error"] and "“View and edit events on all your calendars”" in r["error"]
    assert fake.events == []
    st = fb.status()
    assert st["calendars"]["work"] is None and st["calendars"]["personal"]["account"] == HOME and "Reconnect" in st["missing"]


def test_day_plan_blocks_go_on_the_calendar_after_a_yes(fb, svc, fake):
    now = datetime.fromtimestamp(fb.clock(), svc.tz)
    plan = {"date": now.date().isoformat(), "blocks": [
        {"kind": "task", "title": "Finish PR (90m)", "start": (now + timedelta(minutes=8)).isoformat(),
         "end": (now + timedelta(minutes=53)).isoformat(), "task_id": 1, "profile": "work"},
        {"kind": "meeting", "title": "Client call", "start": "x", "end": "y"},
        {"kind": "task", "title": "Book dentist", "start": (now + timedelta(hours=7)).isoformat(),
         "end": (now + timedelta(hours=7, minutes=30)).isoformat(), "task_id": 2, "profile": "personal"},
        {"kind": "task", "title": "Old", "start": (now - timedelta(hours=2)).isoformat(),
         "end": (now - timedelta(hours=1)).isoformat(), "task_id": 3, "profile": "work"}]}
    svc.skills["planner"].current = lambda: plan
    assert [b["title"] for b in fb.plan_blocks()] == ["Finish PR", "Book dentist"]
    assert fb.status()["plan_blocks"] == 2
    a = Assistant(svc)
    fb.runtime = SimpleNamespace(assistant=a)
    r = svc.skills.action("focusblocks", "plan", {})
    assert r["ok"] and r["pending"] and "2 blocks from today's plan" in r["reply"] and fake.events == []
    a.handle("yes")
    assert [(e["account"], e["body"]["summary"]) for e in fake.events] == [(WORK, "🔒 Finish PR"), (HOME, "🔒 Book dentist")]
    intent = route("put my plan on my calendar", RouterContext(skill_route=svc.skills.route))
    assert intent.tool == "calendar_plan_blocks"


def test_hud_propose(fb, svc, fake):
    a = Assistant(svc)
    fb.runtime = SimpleNamespace(assistant=a)
    r = svc.skills.action("focusblocks", "propose", {"text": "2 hours tomorrow for the Q3 deck"})
    assert r["ok"] and r["pending"] and "1–3 PM" in r["reply"]
    bad = svc.skills.action("focusblocks", "propose", {"text": "make me a sandwich"})
    assert bad["ok"] is False
    fb.runtime = None
    assert svc.skills.action("focusblocks", "propose", {"text": "2 hours tomorrow"})["ok"] is False


def test_nothing_connected(svc):
    fb = svc.skills["focusblocks"]
    st = fb.status()
    assert st["ok"] is False and "Google" in st["error"]
    assert fb.protect_time(minutes=60)["ok"] is False


def test_span_text_and_on_air(fb, svc):
    today = datetime.fromtimestamp(fb.clock(), svc.tz).date()
    assert span_text("2026-10-22T13:30:00-04:00", "2026-10-22T15:00:00-04:00", today) == "tomorrow (Thu Oct 22), 1:30–3 PM"
    assert span_text("2026-10-22T11:00:00-04:00", "2026-10-22T13:00:00-04:00", today) == "tomorrow (Thu Oct 22), 11 AM–1 PM"
    svc.live = lambda: True
    r = fb.protect_time(minutes=60, day="tomorrow", title="Secret project")
    said = svc.skills.say("protect_time", {}, r)
    assert "Secret" not in said and "blocked" in said
