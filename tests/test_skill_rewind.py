"""Rewind: time questions answered from the activity tracker, titles grouped into things, nothing private on air."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import rewind as rw

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 13, 16, 0, tzinfo=NY)  # a Tuesday afternoon


def at(day, hh, mm=0):
    return datetime(2026, 10, day, hh, mm, tzinfo=NY).timestamp()


DAY = [  # (start, end, app, title, category)
    (at(12, 14, 30), at(12, 15, 10), "code", "main.py - PC-assistant - Visual Studio Code", "work"),
    (at(12, 15, 10), at(12, 15, 30), "chrome", "Lo-fi beats - YouTube - Google Chrome", "other"),
    (at(13, 9), at(13, 10, 30), "Code.exe", "● janitor.py - PC-assistant - Visual Studio Code", "work"),
    (at(13, 10, 30), at(13, 10, 50), "WINWORD", "Budget 2026.docx - Word", "work"),
    (at(13, 10, 50), at(13, 11), "chrome", "Pull requests · jwill/PC-assistant · GitHub - Google Chrome", "work"),
    (at(13, 11), at(13, 11, 30), "idle", "Away from keyboard", "idle"),
    (at(13, 15, 20), at(13, 15, 55), "msedge", "Some video - YouTube and 2 more pages - Personal - Microsoft​ Edge", "other"),
    (at(13, 15, 55), at(13, 16), "discord", "#general | Friends - Discord", "other"),
]


@pytest.fixture
def rewind(svc):
    for row in DAY:
        svc.storage.add_activity(*row)
    return rw.Rewind(svc, clock=lambda: NOW.timestamp())


def hm(d):
    return d.strftime("%a %H:%M")


@pytest.mark.parametrize("text,start,end,point,label", [
    ("at 3 pm yesterday", "Mon 14:40", "Mon 15:20", "Mon 15:00", "yesterday around 3 PM"),
    ("yesterday at 3 30 pm", "Mon 15:10", "Mon 15:50", "Mon 15:30", "yesterday around 3:30 PM"),
    ("at 9", "Tue 08:40", "Tue 09:20", "Tue 09:00", "today around 9 AM"),
    ("at 5 pm", "Mon 16:40", "Mon 17:20", "Mon 17:00", "yesterday around 5 PM"),  # 5 pm hasn't happened yet today
    ("this morning", "Tue 05:00", "Tue 12:00", None, "this morning"),
    ("this evening", "Mon 17:00", "Mon 22:00", None, "yesterday evening"),
    ("tuesday afternoon", "Tue 12:00", "Tue 16:00", None, "this afternoon"),
    ("monday", "Mon 00:00", "Tue 00:00", None, "Monday"),
    ("on last tuesday", "Tue 00:00", "Wed 00:00", None, "last Tuesday"),
    ("yesterday", "Mon 00:00", "Tue 00:00", None, "yesterday"),
    ("last night", "Mon 19:00", "Tue 00:00", None, "last night"),
    ("an hour ago", "Tue 14:40", "Tue 15:20", "Tue 15:00", "an hour ago"),
    ("today", "Tue 00:00", "Tue 16:00", None, "today"),
    ("this week", "Mon 00:00", "Tue 16:00", None, "this week"),
])
def test_parse_span(text, start, end, point, label):
    s = rw.parse_span(text, NOW)
    assert (hm(s["start"]), hm(s["end"]), s["point"] and hm(s["point"]), s["label"]) == (start, end, point, label)


def test_parse_span_needs_a_time():
    assert rw.parse_span("at work", NOW) is None and rw.parse_span("bananas", NOW) is None
    assert rw.parse_span("on last tuesday", NOW)["start"].day == 6


@pytest.mark.parametrize("app,title,want", [
    ("code", "● main.py - PC-assistant - Visual Studio Code", ("repo", "PC-assistant")),
    ("chrome", "Lo-fi beats - YouTube - Google Chrome", ("site", "YouTube")),
    ("msedge", "Inbox (3) - jwill736@gmail.com - Gmail and 4 more pages - Personal - Microsoft​ Edge", ("site", "Gmail")),
    ("winword", "Budget 2026.docx - Word", ("document", "Budget 2026.docx")),
    ("discord", "#general | Friends - Discord", ("app", "Discord")),
    ("steam", "", ("app", "Steam")),
])
def test_titles_become_things(app, title, want):
    assert rw.thing(app, title) == want


def test_where_was_i_is_the_work_before_the_break(rewind):
    r = rewind.where_was_i()
    assert r["found"] and r["away_s"] == 5 * 3600
    assert [(x["name"], x["app"], x["seconds"]) for x in r["things"]] == [("Budget 2026.docx", "Word", 1200), ("GitHub", "Chrome", 600)]
    assert rewind.say("where_was_i", {}, r) == "Before your break, 5 hours ago, you were on Budget 2026.docx in Word and GitHub in Chrome."


def test_what_was_i_doing_at_a_time(rewind):
    r = rewind.rewind("at 3 pm yesterday")
    assert r["at"] == {"app": "VS Code", "name": "PC-assistant", "kind": "repo"}
    assert rewind.say("rewind", {}, r) == "Yesterday around 3 PM you were on PC-assistant in VS Code. Around then also YouTube in Chrome."
    r = rewind.rewind("this morning")
    assert r["active_s"] == 2 * 3600 and r["things"][0] == {"name": "PC-assistant", "kind": "repo", "app": "VS Code", "seconds": 5400}
    assert rewind.say("rewind", {}, r) == ("This morning: 2 hours at the PC. Mostly PC-assistant in VS Code, 1 hour 30 minutes, "
                                           "Budget 2026.docx in Word, 20 minutes and GitHub in Chrome, 10 minutes.")
    r = rewind.rewind("sunday")
    assert not r["found"] and rewind.say("rewind", {}, r).startswith("Sunday the PC was idle")
    assert rewind.rewind("whenever")["ok"] is False


def test_how_long_on_a_site(rewind):
    r = rewind.time_spent("YouTube")
    assert r["seconds"] == 35 * 60 and rewind.say("time_spent", {}, r) == "35 minutes on YouTube today."
    assert rewind.time_spent("youtube", "yesterday")["seconds"] == 20 * 60
    assert rewind.time_spent("pc-assistant", "this week")["seconds"] == 40 * 60 + 90 * 60 + 10 * 60
    r = rewind.time_spent("netflix")
    assert rewind.say("time_spent", {}, r) == "I don't see any time on netflix today."


def test_on_air_only_apps_and_times(rewind, svc):
    svc.live = lambda: True
    r = rewind.where_was_i()
    assert "things" not in r and r["things_count"] == 2
    said = rewind.say("where_was_i", {}, r)
    assert "Budget" not in said and "Word and Chrome" in said and said.endswith("on the HUD.")
    r = rewind.rewind("at 3 pm yesterday")
    assert r["at"] == {"app": "VS Code"} and "things" not in r
    assert rewind.say("rewind", {}, r) == "Yesterday around 3 PM you were on VS Code."
    said = rewind.say("rewind", {}, rewind.rewind("this morning"))
    assert "PC-assistant" not in said and "VS Code, Word and Chrome" in said
    st = rewind.status()
    assert all(b["t"] == "" and b["n"] == "" for b in st["blocks"]) and st["today"]["things"] == []


def test_hud_timeline(rewind):
    st = rewind.status()
    assert st["from"] == NOW.timestamp() - 86400 and st["now"] == NOW.timestamp()
    first = st["blocks"][0]
    assert first["a"] == "VS Code" and first["n"] == "PC-assistant" and first["c"] == "work"
    assert st["blocks"][-1]["a"] == "Discord" and st["today"]["active_s"] == 2 * 3600 + 40 * 60
    assert st["where"]["things"][0]["name"] == "Budget 2026.docx"


def test_no_activity(svc):
    r = rw.Rewind(svc, clock=lambda: NOW.timestamp())
    assert r.say("where_was_i", {}, r.where_was_i()) == "I don't see any work in the last twelve hours."
    assert r.status()["blocks"] == [] and r.status()["where"] is None


@pytest.mark.parametrize("said,tool,args", [
    ("where was i", "where_was_i", {}),
    ("what was i working on before my break", "where_was_i", {}),
    ("what was i doing at 3 pm yesterday", "rewind", {"when": "at 3 pm yesterday"}),
    ("what was i working on tuesday", "rewind", {"when": "tuesday"}),
    ("what did i do this morning", "rewind", {"when": "this morning"}),
    ("how long was i on youtube today", "time_spent", {"target": "youtube", "when": "today"}),
    ("how much time did i spend on discord yesterday", "time_spent", {"target": "discord", "when": "yesterday"}),
    ("how long have i been in vs code", "time_spent", {"target": "vs code", "when": "today"}),
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["rewind"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


def test_the_day_recap_is_left_alone(svc):
    sk = Skills(svc, ["rewind"])
    assert route("what did i do today", RouterContext(skill_route=sk.route)).kind != "tool"
    assert sk.route("how long was i away") is None
