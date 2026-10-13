"""Meeting prep: a card per meeting with other people (attendees, last emails, docs, notes, tasks, link), a nudge
10–15 minutes before, a follow-up question after, and nothing private on air."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from test_skill_inbox_zero import fake, gmail  # noqa: F401  (fixtures: Gmail for work and personal, faked)

from assistant.brain.router import RouterContext, route
from assistant.brain.tools import ToolBox
from assistant.skills import Skills
from assistant.skills.meetings import Meetings, title_words
from assistant.skills.people import People

ME = {"email": "jon@talent-sdk.com", "name": "Jon", "self": True, "response": "accepted"}
DANA = {"email": "dana@client.com", "name": "Dana Ortiz", "response": "accepted", "organizer": True}
SAM = {"email": "sam@talent-sdk.com", "name": "", "response": "needsAction"}


def event(title, start, minutes=30, attendees=(), **kw):
    end = start + timedelta(minutes=minutes)
    return {"calendar": "Work", "profile": "work", "color": "#6f9bff", "title": title, "start": start.isoformat(),
            "end": end.isoformat(), "all_day": False, "location": kw.get("location", ""),
            "description": kw.get("description", ""), "attendees": list(attendees), "link": kw.get("link", ""),
            "account": "jon@talent-sdk.com"}


@pytest.fixture
def cal(svc):
    now = datetime.now(svc.tz)
    events = [
        event("Retro", now - timedelta(minutes=40), 30, [ME, SAM]),  # ended 10 minutes ago
        event("Focus block", now + timedelta(minutes=5), 60),  # no one else: not a meeting to prep
        event("Contract review", now + timedelta(minutes=12), 30, [ME, DANA, SAM],
              description="Join: https://meet.google.com/abc-defg-hij"),
        event("Budget sync w/ Priya", now + timedelta(hours=5), 30, [ME, {"email": "priya@vendor.io", "name": "Priya N"}]),
    ]
    svc.calendars.events = lambda start, end, profile=None: [
        e for e in events if datetime.fromisoformat(e["end"]) > start and datetime.fromisoformat(e["start"]) < end]
    svc.calendars.status = lambda: [{"kind": "google", "name": "Work"}]
    return events


@pytest.fixture
def mt(svc, gmail, cal):  # noqa: F811
    svc.gmail = gmail
    svc.library = SimpleNamespace(search=lambda q, limit=5: [
        {"title": "Contract v3", "file": "contract-v3.docx", "path": "C:/Docs/contract-v3.docx", "folder": "Legal"}]
        if "contract" in q else [])
    svc.storage.add_note("Contract review: push back on clause 7")
    svc.storage.add_task("Send Dana the redlines")
    svc.storage.add_task("Water the plants")
    svc.skills = Skills(svc, [])
    people = People(svc)
    svc.skills.all["people"] = people
    people.build()
    people.remember_about("dana", "ask about her daughter's college next time")
    nudged = []
    svc.nudge = lambda text, **kw: nudged.append((text, kw))
    m = Meetings(svc)
    svc.skills.all["meetings"] = m
    m.nudged = nudged
    return m


def test_prep_card(mt):
    card = mt.meeting_prep()
    assert card["ok"] and card["title"] == "Contract review" and 10 <= card["in_minutes"] <= 12
    assert [a["name"] for a in card["attendees"]] == ["Dana Ortiz", "Sam Lee"]  # J left out; Sam's full name from People
    dana = card["attendees"][0]
    assert [m["subject"] for m in dana["emails"]] == ["Quick question", "Contract redlines", "Pricing"]
    assert dana["emails"][2]["from_me"] and dana["ask"] == ["Ask about her daughter's college"]
    assert card["docs"][0]["title"] == "Contract v3" and card["notes"] == ["Contract review: push back on clause 7"]
    assert card["tasks"] == ["Send Dana the redlines"] and card["link"] == "https://meet.google.com/abc-defg-hij"
    assert mt.meeting_prep("budget")["title"] == "Budget sync w/ Priya"
    assert mt.meeting_prep("priya")["title"] == "Budget sync w/ Priya"
    assert mt.meeting_prep("board offsite")["ok"] is False


def test_nudges_before_and_after(mt):
    mt.tick()
    texts = {t: kw["priority"] for t, kw in mt.nudged}
    assert texts == {"Prep for Contract review is on the HUD: last thing from Dana was “Quick question” today.": "urgent",
                     "Any follow-ups from Retro? Say “note: …”.": "normal"}
    mt.tick()
    assert len(mt.nudged) == 2  # once each
    st = mt.status()
    assert st["next"]["title"] == "Contract review" and [u["title"] for u in st["upcoming"]] == ["Contract review",
                                                                                                "Budget sync w/ Priya"]


def test_spoken_and_on_air(mt, svc):
    said = mt.say("meeting_prep", {}, mt.meeting_prep())
    assert said.startswith("Contract review at ") and "with Dana Ortiz and Sam Lee." in said
    assert "Last thing from Dana was “Quick question” today." in said
    assert "Remember to ask about her daughter's college with Dana." in said
    assert said.endswith("1 related document and 1 open task on the HUD.")
    nxt = mt.next_meeting()
    assert nxt["meeting"]["title"] == "Focus block" and mt.say("next_meeting", {}, nxt).endswith("with just you.")
    svc.live = lambda: True
    live = mt.meeting_prep()
    assert "title" not in live and "attendees" in live and "emails" not in live["attendees"][0]
    assert mt.say("meeting_prep", {}, live).endswith("has 2 people. The prep is on the HUD.")
    assert "Focus" not in mt.say("next_meeting", {}, mt.next_meeting())
    box = ToolBox(svc)
    assert box.tools["meeting_prep"].untrusted and box.tools["meeting_prep"].untrusted_label == "an email"


@pytest.mark.parametrize("said,tool,args", [
    ("prep me for my next meeting", "meeting_prep", {}),
    ("meeting prep", "meeting_prep", {}),
    ("prep me for the budget sync", "meeting_prep", {"title": "budget sync"}),
    ("prepare me for contract review meeting", "meeting_prep", {"title": "contract review"}),
    ("who's in my next meeting", "next_meeting", {}),
    ("who am i meeting next", "next_meeting", {}),
    ("what's my next meeting", "calendar", {"days": 2, "next_only": True}),  # the calendar's own phrase
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["meetings"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)
    assert Skills(svc, ["meetings"]).route("prep me for the stream") is None


def test_briefing_and_no_google(svc, cal):
    m = Meetings(svc)  # no Google account: still lists meetings, prep has no emails and says why
    now = datetime.now(svc.tz)
    if (now + timedelta(minutes=12)).date() == now.date():  # not just before midnight
        first = m.briefing()["meetings"][0]
        assert (first["title"], first["with"]) == ("Contract review", ["Dana Ortiz", "Sam"])
    card = m.meeting_prep()
    assert card["ok"] and card["attendees"][0]["emails"] == [] and "No Google account" in card["gmail_error"]
    svc.calendars.events = lambda start, end, profile=None: []
    svc.calendars.status = lambda: []
    m2 = Meetings(svc)
    out = m2.meeting_prep()
    assert out["ok"] is False and "Google calendars" in out["error"]
    m2.tick()
    assert m2.status()["next"] is None and "Google" in m2.status()["hint"]

    def boom(*a, **k):
        raise RuntimeError("calendar down")
    svc.calendars.events = boom
    m3 = Meetings(svc)
    m3.tick()  # never raises
    assert "calendar" in m3.status()["warning"].lower()


def test_title_words():
    assert title_words("Weekly 1:1 sync with Dana / Contract") == ["dana", "contract"]
