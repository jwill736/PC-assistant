"""Journal: entries with mood and tags (model or word list), search by time and topic, private on air."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import journal as jn

NY = ZoneInfo("America/New_York")
DAY = 86400


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 13, 20, 0, tzinfo=NY).timestamp()  # a Tuesday evening

    def __call__(self):
        return self.t


@pytest.fixture
def env(svc):
    clock = Clock()
    j = jn.Journal(svc, clock=clock)
    return j, clock


def at(clock, days_ago, fn):
    clock.t -= days_ago * DAY
    try:
        return fn()
    finally:
        clock.t += days_ago * DAY


@pytest.mark.parametrize("text,mood,tags", [
    ("Shipped the deck, felt great", 2, ["work"]),
    ("Long day. Tired and a bit stressed about the client", -2, ["work", "sleep"]),
    ("Not bad, stream was fun", 1, ["stream"]),
    ("Ate lunch", 0, ["health"]),
    ("I am really happy", 2, []),
])
def test_mood_from_words(text, mood, tags):
    assert jn.mood_words(text) == (mood, tags)


def test_entries_without_and_with_a_model(env, svc):
    j, clock = env
    r = j.add("shipped the deck, felt great")
    assert r["mood"] == 2 and r["tags"] == ["work"] and r["mood_by"] == "words"
    assert j.say("journal_add", {}, r) == "Saved. Sounds like a good one."
    seen = []
    svc.think = lambda prompt, schema=None, **kw: seen.append(prompt) or {"mood": -1.4, "tags": ["Family", " "]}
    r = j.add("Argued with my brother")
    assert (r["mood"], r["tags"], r["mood_by"]) == (-1, ["family"], "model") and "Argued" in seen[0]
    assert j._rows(date(2026, 10, 13), date(2026, 10, 13))[0]["text"] == "Argued with my brother"
    assert j.add("hi")["ok"] is False
    assert not svc.storage.search_memory("brother")  # not in the memory index (notes)


def test_prompted_entry_takes_the_next_thing_said(env, svc):
    j, clock = env
    sk = Skills(svc, ["journal"])
    sk.all["journal"] = j
    assert route("end of day journal", RouterContext(skill_route=sk.route)).tool == "journal_prompt"
    assert j.say("journal_prompt", {}, j.prompt()) == "How did today go?"
    intent = route("pretty good actually, got a lot done", RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == ("journal_add", {"text": "pretty good actually got a lot done"})
    j.add(**intent.args)
    assert route("pretty good actually, got a lot done", RouterContext(skill_route=sk.route)) is None  # only once
    clock.t += 1000
    j.prompt()
    clock.t += 200  # too late: back to normal
    assert route("open the pod bay doors", RouterContext(skill_route=sk.route)).tool != "journal_add"


def test_search_and_mood_over_time(env):
    j, clock = env
    at(clock, 8, lambda: j.add("Stream went great, raid at the end"))  # Monday last week
    at(clock, 6, lambda: j.add("Exhausted after the client meeting"))  # Wednesday last week
    at(clock, 1, lambda: j.add("Good focus day at work"))
    j.add("Calm evening")
    r = j.search(when="last week")
    assert r["when"] == "last week" and r["count"] == 2 and r["entries"][0]["text"].startswith("Exhausted")
    assert j.say("journal_search", {}, r) == ("2 entries from last week. Wednesday: Exhausted after the client meeting. "
                                             "Monday: Stream went great, raid at the end.")
    r = j.search("stream")
    assert r["count"] == 1 and r["when"] == "this past year"
    m = j.mood("this week")
    assert m["count"] == 2 and m["average"] == 1.0 and m["days"][0] == {"day": "2026-10-12", "mood": 1.0, "n": 1}
    assert j.say("journal_mood", {}, m) == "Mostly good this week, over 2 entries; best on Monday. Work came up most."
    assert [d["n"] for d in j.week()] == [1, 0, 0, 0, 0, 1, 1]
    assert j.say("journal_search", {}, j.search("dragons")) == "No journal entries from this past year about dragons."


def test_private_on_air(env, svc):
    j, clock = env
    j.add("Secret plans for the launch, feeling great")
    svc.live = lambda: True
    r = j.search(when="today")
    said = j.say("journal_search", {}, r)
    assert said.startswith("1 entry from today.") and "Secret" not in said
    assert "great" not in j.say("journal_mood", {}, j.mood("today"))
    assert j.say("journal_add", {}, {"mood": 2}) == "Saved."
    st = j.status()
    assert st["live"] and "text" not in st["entries"][0] and st["tags_week"] == []
    assert j.briefing() == {"entries_this_week": 1}


def test_evening_prompt_nudge(env, svc):
    j, clock = env
    j.tick()  # 8 PM: not yet
    nud = svc.skills["nudges"]
    assert not any(n.key.startswith("journal:") for n in nud.queue + nud.feed)
    clock.t += 3600 + 300
    j.tick()
    assert [n.text for n in nud.queue + nud.feed if n.key.startswith("journal:")] == [
        "How did today go? Say “journal” and tell me, or type it on the HUD."]
    clock.t = datetime(2026, 10, 14, 21, 30, tzinfo=NY).timestamp()
    j.add("Done already")
    j.tick()
    assert not any(n.key == "journal:2026-10-14" for n in nud.queue + nud.feed)


@pytest.mark.parametrize("said,tool,args", [
    ("journal: shipped the deck today", "journal_add", {"text": "shipped the deck today"}),
    ("dear diary today was long", "journal_add", {"text": "today was long"}),
    ("end of day journal", "journal_prompt", {}),
    ("what did i journal about last week", "journal_search", {"query": "", "when": "last week"}),
    ("what did i journal about the stream", "journal_search", {"query": "the stream", "when": ""}),
    ("show my journal entries from this week", "journal_search", {"query": "", "when": "this week"}),
    ("search my journal for raid", "journal_search", {"query": "raid", "when": ""}),
    ("how have i been feeling", "journal_mood", {"when": ""}),
    ("how have i been feeling this week", "journal_mood", {"when": "this week"}),
    ("how's my mood been lately", "journal_mood", {"when": "lately"}),
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["journal"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


def test_journal_words_stay_out_of_the_conversation_log(svc):
    from assistant.brain.assistant import Assistant
    from assistant.skills import Skills

    svc.skills = Skills(svc, ["journal"])
    a = Assistant(svc)
    a.handle("journal: rough day, the client call went badly")
    logged = " ".join(r["text"] for r in svc.storage.recent_log(20))
    assert "client call went badly" not in logged
    assert not svc.storage.search_memory("client call badly", 5)
