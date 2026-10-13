"""People memory: contacts from J's Gmail (work and personal) and calendar, what he asked Vesper to remember about
them, and who has gone quiet. Gmail and the calendar are faked."""

import time

import pytest
from test_google import HOME
from test_skill_inbox_zero import fake, gmail, mail  # noqa: F401  (fixtures: Gmail for work and personal, faked)
from test_skill_meetings import cal  # noqa: F401  (fixture: a calendar with attendees)

from assistant.brain.router import RouterContext, route
from assistant.brain.tools import ToolBox
from assistant.skills import Skills
from assistant.skills.people import People, display_name


@pytest.fixture
def ppl(svc, gmail, cal):  # noqa: F811
    svc.gmail = gmail
    p = People(svc)
    svc.skills = Skills(svc, [])
    svc.skills.all["people"] = p
    p.build()
    return p


def test_builds_contacts_from_mail_and_calendar(ppl):
    rows = {r["email"]: r for r in ppl.svc.storage.query("SELECT * FROM people_contacts")}
    assert "jira@talent-sdk.atlassian.net" not in rows and "jon@talent-sdk.com" not in rows  # robots and J himself
    dana = rows["dana@client.com"]
    assert (dana["name"], dana["account"], dana["count"], dana["sent"]) == ("Dana Ortiz", "work", 3, 1)
    assert dana["last_subject"] == "Quick question" and dana["next_meeting"] == "Contract review"
    assert (rows["sam@talent-sdk.com"]["name"], rows["sam@talent-sdk.com"]["last_meeting"]) == ("Sam Lee", "Retro")
    assert rows["marcus@example.com"]["account"] == "personal" and rows["marcus@example.com"]["sent"] == 2
    assert rows["priya@vendor.io"]["count"] == 2


def test_incremental_rebuild_counts_each_message_once(ppl, fake):  # noqa: F811
    ppl.build()
    assert ppl.known("dana")["count"] == 3
    fake.mail[HOME].append(mail("h9", "u9", "Mom <mom@example.com>", "J <jwill736@gmail.com>", "Re: Sunday dinner", "6 it is", 0.5))
    ppl.build()
    mom = ppl.known("mom")
    assert mom["count"] == 2 and mom["last_subject"] == "Sunday dinner"  # "Re:" taken off


def test_who_is_remember_and_spoken(ppl, svc):
    svc.storage.add_task("Send Dana the redlines")
    r = ppl.remember_about("dana", "Dana's daughter starts college in the fall")
    assert r["ok"] and r["kind"] == "note" and ppl.say("remember_about", {}, r) == "Got it, I'll remember that about Dana."
    r = ppl.remember_about("Dana Ortiz", "ask about the Lisbon trip next time we talk")
    assert r["kind"] == "ask" and ppl.say("remember_about", {}, r) == "Got it. I'll bring that up next time you talk to Dana."
    who = ppl.who_is("dana")
    p = who["person"]
    assert p["subjects"][:2] == ["Quick question", "Contract redlines"] and p["tasks"] == ["Send Dana the redlines"]
    assert [n["text"] for n in p["notes"]] == ["Dana's daughter starts college in the fall"]
    assert [n["text"] for n in p["ask"]] == ["Ask about the Lisbon trip"]
    said = ppl.say("who_is", {}, who)
    assert said == ("Dana Ortiz, work: in touch today, about “Quick question”. Next meeting: Contract review. "
                    "You noted: Dana's daughter starts college in the fall. Ask Dana about the Lisbon trip.")
    assert ppl.who_is("zelda")["ok"] is False
    unknown = ppl.remember_about("zelda", "likes jazz")
    assert unknown["person"] is None and svc.storage.list_notes()[0]["text"] == "zelda: likes jazz"
    note_id = p["notes"][0]["id"]
    assert ppl.actions()["forget"]({"id": note_id})["ok"] and ppl.who_is("dana")["person"]["notes"] == []
    svc.live = lambda: True
    live = ppl.who_is("dana")
    assert "subjects" not in live["person"] and "Quick" not in ppl.say("who_is", {}, live)


def test_overdue(ppl):
    now = time.time()
    for i in range(5):
        ppl.touch("old@friend.com", "Old Friend", HOME, now - 40 * 86400 - i, "Catch up")
    out = ppl.overdue()
    assert [(p["name"], p["days_since"], p["count"]) for p in out["people"]] == [("Old Friend", 40, 5)]
    assert ppl.say("people_overdue", {}, out) == "You haven't been in touch with Old Friend (40 days)."
    assert ppl.briefing() == {"quiet": ["Old Friend"]}


def test_routes_only_take_known_names(ppl, svc):
    ppl.touch("will@example.com", "Will Turner", HOME, time.time(), "Boat")
    ctx = RouterContext(skill_route=svc.skills.route)
    cases = [
        ("who is dana", "who_is", {"name": "dana"}),
        ("what do i know about dana ortiz", "who_is", {"name": "dana ortiz"}),
        ("when did i last talk to marcus", "who_is", {"name": "marcus"}),
        ("remember that dana's daughter starts college in the fall", "remember_about",
         {"name": "dana", "text": "Dana's daughter starts college in the fall"}),
        ("remember to ask priya about the invoice", "remember_about", {"name": "priya", "text": "ask about the invoice"}),
        ("ask sam about his move next time", "remember_about", {"name": "sam", "text": "ask about his move"}),
        ("remember that will's birthday is in may", "remember_about", {"name": "will", "text": "Will's birthday is in may"}),
        ("who haven't i talked to in a while", "people_overdue", {}),
        # not a contact: the ordinary routes keep these
        ("remember that the meeting moved to 3", "remember", {"text": "the meeting moved to 3"}),
        ("remember that will be due friday", "remember", {"text": "will be due friday"}),
        ("what do i know about kubernetes", "recall", {"query": "kubernetes"}),
    ]
    for said, tool, args in cases:
        intent = route(said, ctx)
        assert (intent.tool, intent.args) == (tool, args), said
    assert ppl.route("who is the president") is None


def test_tools_and_hud(ppl, svc):
    box = ToolBox(svc)
    assert box.tools["who_is"].untrusted and box.tools["remember_about"].tier_for({}) == 1
    st = ppl.status()
    assert st["ok"] and st["count"] == 5 and [r["name"] for r in st["recent"][:2]] == ["Sam Lee", "Dana Ortiz"]  # the retro just ended
    found = ppl.actions()["search"]({"q": "mar"})
    assert [p["name"] for p in found["people"]] == ["Marcus"]
    assert ppl.actions()["remember"]({"name": "marcus@example.com", "text": "Moving in May"})["person"] == "Marcus"


def test_without_google_it_uses_the_calendar_and_says_why(svc, cal):  # noqa: F811
    p = People(svc)
    st = p.status()
    assert st["ok"] is False and "No Google account" in st["error"]
    p.tick()
    st = p.status()
    assert st["ok"] and st["count"] == 3 and "No Google account" in st["warning"]  # Dana, Sam, Priya from invites
    assert p.known("dana")["count"] == 0 and p.known("sam")["last_meeting"] == "Retro"
    svc.calendars.events = lambda *a, **k: []
    svc.gmail = None
    empty = People(svc)
    empty.svc.storage.execute("DELETE FROM people_contacts")
    empty.tick()
    assert empty.status() == {"ok": False, "error": "Gmail isn't set up."}


def test_display_name():
    assert display_name("", "dana.ortiz@client.com") == "Dana Ortiz"
    assert display_name("sam@x.com", "sam@x.com") == "Sam"
    assert display_name('"Priya N"', "p@x.com") == "Priya N"
