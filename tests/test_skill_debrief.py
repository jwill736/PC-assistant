"""Meeting debrief: the question after a meeting with people, the next words (or the HUD box) become a note, tasks with
due dates and People notes; "debrief my last meeting"; nothing private on air."""

from datetime import date, datetime, timedelta

import pytest

from assistant.brain.router import RouterContext, route
from assistant.brain.tools import ToolBox
from assistant.skills import Skills
from assistant.skills.debrief import Debrief, due_of, find_tasks

WED = date(2026, 10, 21)  # a Wednesday


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def setup(svc):
    tz = svc.tz
    end = datetime(2026, 10, 21, 11, 0, tzinfo=tz)
    clk = Clock((end + timedelta(minutes=10)).timestamp())
    events = [
        {"title": "Contract review", "start": (end - timedelta(hours=1)).isoformat(), "end": end.isoformat(), "all_day": False,
         "profile": "work", "attendees": [{"email": "dana@client.com", "name": "Dana Ortiz"},
                                          {"email": "sam@talent-sdk.com", "name": "Sam Lee"},
                                          {"email": "jon@talent-sdk.com", "name": "Jon", "self": True}]},
        {"title": "Focus", "start": (end - timedelta(hours=3)).isoformat(), "end": (end - timedelta(hours=2)).isoformat(),
         "all_day": False, "profile": "work", "attendees": []},
    ]
    svc.calendars.events = lambda start, end_, profile=None: [e for e in events if start <= datetime.fromisoformat(e["end"]) <= end_]
    svc.calendars.status = lambda: [{"kind": "google"}]
    svc.skills = Skills(svc, ["people"])
    ppl = svc.skills["people"]
    ppl.touch("dana@client.com", "Dana Ortiz", "jon@talent-sdk.com", clk.t - 86400, "Redlines")
    nudged = []
    svc.nudge = lambda text, **kw: nudged.append((text, kw))
    db = Debrief(svc, clock=clk)
    svc.skills.all["debrief"] = db
    svc.think = lambda *a, **k: None
    return db, clk, nudged, events


def deliver(svc, kw):
    """The nudge queue put the question out (spoken or on the HUD)."""
    svc.bus.publish("notice", {"source": "debrief", "text": "x", "key": kw["key"]})


def test_asks_once_after_a_meeting_with_people_then_listens(setup, svc):
    db, clk, nudged, _ = setup
    db.tick()
    [(text, kw)] = nudged
    assert text == "Anything to note from Contract review?" and kw["key"].startswith("debrief:Contract review|")
    assert kw["action"]["command"] == "debrief my last meeting"
    assert db.status()["asking"]["title"] == "Contract review" and not db.listening()  # not spoken yet
    deliver(svc, kw)
    assert db.listening()
    db.tick()
    assert len(nudged) == 1  # once
    ctx = RouterContext(skill_route=svc.skills.route)
    said = "I'll send Dana the revised deck by Friday. We need to book the venue next week. Sam is out Monday."
    intent = route(said, ctx)
    assert intent.tool == "debrief_save"
    svc.bus.publish("user_said", {"text": said, "source": "voice"})  # as Assistant.handle does before running it
    r = ToolBox(svc).run("debrief_save", intent.args)
    assert r["ok"] and [t["title"] for t in r["tasks"]] == ["Send Dana the revised deck", "Book the venue"]
    assert [t["due"] for t in r["tasks"]] == ["2026-10-23", "2026-10-26"]
    assert {t["title"] for t in svc.storage.list_tasks()} >= {"Send Dana the revised deck", "Book the venue"}
    assert svc.storage.list_tasks()[0]["profile"] == "work"
    notes = svc.storage.list_notes(3)
    assert notes[0]["text"].startswith("Debrief · Contract review (Oct 21): I'll send Dana")
    assert sorted(r["people"]) == ["Dana Ortiz", "Sam Lee"]
    about = svc.skills["people"].about("dana@client.com")["notes"]
    assert about and about[0]["text"].startswith("Contract review: I'll send Dana")
    said = svc.skills.say("debrief_save", {}, r)
    assert said.startswith("Noted for Contract review: 2 tasks (Send Dana the revised deck, due Friday, and 1 more)")
    assert "note on Dana and Sam" in said
    assert not db.listening() and db.status()["asking"] is None and db.status()["recent"][0]["title"] == "Contract review"


def test_window_closes_and_commands_still_work(setup, svc):
    db, clk, nudged, _ = setup
    db.tick()
    deliver(svc, nudged[0][1])
    ctx = RouterContext(skill_route=svc.skills.route)
    assert route("what's the weather", ctx) is None or route("what's the weather", ctx).tool != "debrief_save"
    assert route("ok", ctx) is None or route("ok", ctx).tool != "debrief_save"
    assert route("nothing to note", ctx).tool == "debrief_skip"
    clk.t += 200  # 3 minutes passed
    assert route("i will send the deck tomorrow", ctx) is None


def test_too_late_or_no_people_no_question(setup, svc):
    db, clk, nudged, events = setup
    clk.t += 40 * 60  # 50 minutes after it ended
    db.tick()
    assert nudged == []
    events[0]["attendees"] = []
    clk.t -= 40 * 60
    db.tick()
    assert nudged == []


def test_debrief_my_last_meeting_by_voice(setup, svc):
    db, clk, nudged, _ = setup
    clk.t += 3 * 3600  # long after: no nudge, but J can still ask
    ctx = RouterContext(skill_route=svc.skills.route)
    i = route("debrief my last meeting", ctx)
    r = ToolBox(svc).run(i.tool, i.args)
    assert r["ok"] and svc.skills.say(i.tool, {}, r) == "What should I note from Contract review?"
    i2 = route("we should email legal about the indemnity clause", ctx)
    assert i2.tool == "debrief_save"
    r2 = ToolBox(svc).run(i2.tool, i2.args)
    assert [t["title"] for t in r2["tasks"]] == ["Email legal about the indemnity clause"]
    one = route("debrief my last meeting: send Sam the numbers tomorrow", ctx)
    assert one.tool == "debrief_save" and "send sam the numbers tomorrow" in one.args["text"]


def test_model_tidies_and_is_checked(setup, svc):
    db, clk, nudged, _ = setup
    svc.think = lambda prompt, schema=None, **kw: {"tasks": [{"title": "Send Dana the deck", "due": "2026-10-23"},
                                                              {"title": "Bad date", "due": "Friday"}],
                                                    "people": [{"email": "dana@client.com", "note": "Wants the deck Friday"},
                                                               {"email": "stranger@x.com", "note": "not in the meeting"}]}
    r = db.save("dana wants the deck friday so i'll send it", "")
    assert r["by"] == "model" and r["tasks"][0]["due"] == "2026-10-23" and r["tasks"][1]["due"] is None
    assert r["people"] == ["Dana Ortiz"]
    assert "Dana wants the deck" in r["note"]  # spoken names get their capitals back


def test_hud_box_and_skip(setup, svc):
    db, clk, nudged, _ = setup
    db.tick()
    key = db.status()["asking"]["key"]
    r = svc.skills.action("debrief", "save", {"text": "Follow up with Sam on hiring by 10/30", "key": key})
    assert r["ok"] and [(t["title"], t["due"]) for t in r["tasks"]] == [("Follow up with Sam on hiring", "2026-10-30")]
    assert svc.skills.action("debrief", "skip", {})["ok"]
    assert svc.skills.action("debrief", "save", {"text": "x"})["ok"] is False


def test_on_air_says_little(setup, svc):
    db, clk, nudged, _ = setup
    svc.live = lambda: True
    r = db.save("I'll send Dana the deck by Friday")
    said = svc.skills.say("debrief_save", {}, r)
    assert "Dana" not in said and "HUD" in said
    assert db.status()["recent"] == []


def test_without_calendar_people(svc):
    svc.calendars.events = lambda *a, **k: []
    svc.calendars.status = lambda: []
    svc.skills = Skills(svc, ["debrief"])
    r = svc.skills["debrief"].start_debrief()
    assert r["ok"] is False and "Google calendars" in r["error"]


def test_due_words():
    assert due_of("send it by friday", WED) == ("2026-10-23", "send it")
    assert due_of("call her tomorrow", WED)[0] == "2026-10-22"
    assert due_of("book it next week", WED)[0] == "2026-10-26"
    assert due_of("pay by oct 30", WED)[0] == "2026-10-30"
    assert due_of("by end of week", WED)[0] == "2026-10-23"
    assert due_of("by wednesday", WED)[0] == "2026-10-21"
    assert due_of("no date here", WED) == (None, "no date here")
    assert find_tasks("Great meeting. Dana liked it.", WED) == []
    # speech without full stops still splits at each commitment
    assert [t["title"] for t in find_tasks("i'll send dana the deck by friday we need to book the venue", WED)] == \
        ["Send dana the deck", "Book the venue"]
    assert find_tasks("todo: update the roadmap", WED) == [{"title": "Update the roadmap", "due": None}]


@pytest.mark.parametrize("said,tool", [("debrief my last meeting", "debrief_start"), ("debrief", "debrief_start"),
                                       ("let's debrief the last call", "debrief_start")])
def test_routes(svc, said, tool):
    skills = Skills(svc, ["debrief"])
    assert route(said, RouterContext(skill_route=skills.route)).tool == tool
