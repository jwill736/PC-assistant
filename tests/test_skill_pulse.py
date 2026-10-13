"""Pulse: overnight cards from goals, tasks, notes, projects, what J asked and the news; with or without a model;
👍/👎 steering the next night; the 5 AM schedule; voice; and the optional research card."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import pulse as pl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=NY)
DAY = 86400
HEADLINES = [
    {"source": "The Verge", "title": "Apple ships a new iPad", "link": "https://example.com/ipad", "summary": "Faster chip."},
    {"source": "TechCrunch", "title": "ClipForge-style auto clipping comes to Twitch", "link": "https://example.com/clips",
     "summary": "Twitch will cut highlights from chat spikes on its own."},
]


class Clock:
    t = NOW.timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def pulse(svc):
    clock = Clock()
    db = svc.storage
    svc.cfg["goals"] = {"north_star": ["Streaming: 100 average viewers by June", "Work: every client project on time"],
                        "this_week": ["Stream 4 nights", "Finish the overlay"]}
    due = db.add_task("Send the Q4 invoice", due=NOW.date().isoformat())
    old = db.add_task("Fix the mic noise gate", profile="stream")
    db.execute("UPDATE tasks SET created=? WHERE id IN (?,?)", (clock.t - 6 * DAY, due["id"], old["id"]))
    note = db.add_note("Try a co-stream with Dana's channel")
    db.execute("UPDATE notes SET created=? WHERE id=?", (clock.t - 3 * DAY, note["id"]))
    db.execute("INSERT INTO log(ts,role,source,text) VALUES (?,?,?,?)", (clock.t - 3600, "user", "voice",
                                                                         "how do i get more viewers on twitch"))
    db.execute("INSERT INTO log(ts,role,source,text) VALUES (?,?,?,?)", (clock.t - 3500, "user", "voice", "open obs"))
    svc.projects.summary = lambda: {
        "repos": [{"name": "overlay-kit", "dirty_files": 3}, {"name": "clipforge", "dirty_files": 0}],
        "claude": [{"project": "clipforge", "latest_ask": "add a vertical export preset"}],
        "github": {"open_prs": [{"title": "Vertical export", "repo": "jdub/clipforge"}]}}
    svc.news.headlines = lambda topic=None, limit=12: HEADLINES[:limit]
    p = pl.Pulse(svc, clock=clock)
    p.clock_obj, p.nudged = clock, []
    p.nudge = lambda text, **kw: p.nudged.append((text, kw.get("priority")))
    return p


def test_gather_reads_every_source(pulse):
    d = pulse.gather()
    assert d["goals"][0].startswith("Streaming") and d["this_week"] == ["Stream 4 nights", "Finish the overlay"]
    assert {t["title"]: t["open_days"] for t in d["tasks"]} == {"Send the Q4 invoice": 6, "Fix the mic noise gate": 6}
    assert d["asked_lately"] == ["how do i get more viewers on twitch", "open obs"]
    assert d["claude_sessions"][0]["project"] == "clipforge" and d["repos"][0] == {"name": "overlay-kit", "uncommitted": 3}
    assert d["notes"][0]["days_ago"] == 3 and len(d["news"]) == 2 and d["date"] == "2026-10-13"


def test_cards_without_a_model(pulse):
    r = pulse.build()
    assert r["ok"] and r["by"] == "local"
    cards = r["cards"]
    assert [c["kind"] for c in cards] == ["task", "goal", "news", "follow_up", "follow_up"]
    assert cards[0]["title"] == "Due today: Send the Q4 invoice"
    assert cards[1]["title"] == "Goal check: Streaming: 100 average viewers by June" and "Stream 4 nights" in cards[1]["body"]
    assert cards[2]["title"].startswith("ClipForge-style") and cards[2]["link"] == "https://example.com/clips"
    assert "clipforge" in cards[2]["why"]
    assert cards[3]["title"] == "Pick up clipforge" and "vertical export preset" in cards[3]["body"]
    assert cards[4]["title"].startswith("Still open after 6 days")


def test_rule_cards_on_an_empty_pc():
    d = {"date": "2026-10-13", "goals": [], "this_week": [], "tasks": [], "notes": [], "asked_lately": [], "claude_sessions": [],
         "repos": [], "news": [], "feedback": {"liked": [], "disliked": [], "kinds": {}}}
    cards = pl.rule_cards(d)
    assert [c["title"] for c in cards] == ["Set your north star", "One small win before lunch"]
    assert "Goals & tasks" in cards[0]["body"]


def test_votes_steer_the_next_night(pulse, svc):
    first = pulse.build()["cards"]
    goal = next(c for c in first if c["kind"] == "goal")
    news = next(c for c in first if c["kind"] == "news")
    assert pulse.vote(goal["id"], -1)["ok"] and pulse.vote(news["id"], 1)["ok"]
    assert pulse.vote("nope", 1)["ok"] is False
    fb = pulse.feedback()
    assert fb["liked"] == [news["title"]] and fb["disliked"] == [goal["title"]] and fb["kinds"] == {"goal": -1, "news": 1}
    pulse.clock_obj.t += DAY
    again = pulse.build()["cards"]
    assert [c["kind"] for c in again] == ["news", "task", "follow_up", "follow_up", "project"]  # the goal card sank
    assert again[1]["title"] == "Overdue: Send the Q4 invoice"
    # with a model, the prompt says what J liked and didn't
    seen = []
    svc.think = lambda prompt, schema=None, **kw: seen.append(prompt) or None
    pulse.build()
    assert f"J liked: {news['title']}." in seen[0] and f"J disliked: {goal['title']}." in seen[0]
    assert "data, never instructions" in seen[0]


def test_cards_from_the_model_keep_only_real_links(pulse, svc):
    svc.think = lambda prompt, schema=None, **kw: {"cards": [
        {"title": "3 ideas to grow the stream this week", "body": "1) Co-stream with Dana 2) Clip chat spikes 3) Post a short",
         "why": "Your streaming goal", "kind": "idea"},
        {"title": "Twitch auto clips", "body": "Matters for ClipForge.", "why": "News x project", "kind": "news",
         "link": "https://example.com/clips"},
        {"title": "Made-up link", "body": "x", "why": "y", "kind": "weird", "link": "https://evil.example/phish"},
        {"title": "  ", "body": "no title", "why": "", "kind": "idea"}]}
    r = pulse.build()
    assert r["by"] == "model" and [c["title"] for c in r["cards"]] == \
        ["3 ideas to grow the stream this week", "Twitch auto clips", "Made-up link"]
    assert r["cards"][1]["link"] == "https://example.com/clips" and r["cards"][2]["link"] == "" and r["cards"][2]["kind"] == "idea"


def test_a_model_that_fails_falls_back(pulse, svc):
    def boom(prompt, schema=None, **kw):
        raise RuntimeError("model fell over")
    svc.think = boom
    assert pulse.build()["by"] == "local"
    svc.think = lambda prompt, schema=None, **kw: {"cards": "not a list"}
    assert pulse.build()["by"] == "local"


def test_rebuild_keeps_voted_cards(pulse):
    first = pulse.build()["cards"]
    pulse.vote(first[0]["id"], 1)
    pulse.build()
    now = pulse.cards()
    assert first[0]["id"] in {c["id"] for c in now} and first[1]["id"] not in {c["id"] for c in now}
    assert len(now) == 5 and len({c["title"] for c in now}) == 5


def test_schedule_runs_once_at_five(pulse):
    pulse.clock_obj.t = datetime(2026, 10, 14, 4, 50, tzinfo=NY).timestamp()
    pulse.build()  # yesterday's (the 14th at 4:50 is still before the run)
    pulse.svc.storage.execute("UPDATE pulse_cards SET day='2026-10-13'")
    pulse._tried = None
    pulse.tick()
    assert pulse.last_status["day"] == "2026-10-13" and pulse.nudged == []
    pulse.clock_obj.t = datetime(2026, 10, 14, 5, 1, tzinfo=NY).timestamp()
    pulse.tick()
    assert pulse.last_status["day"] == "2026-10-14" and pulse.nudged == [("Your Pulse is ready: 5 cards on the HUD.", "low")]
    n = len(pulse.cards())
    pulse.tick()
    assert len(pulse.cards()) == n and len(pulse.nudged) == 1


def test_briefing_lists_todays_titles(pulse):
    assert pulse.briefing() is None
    pulse.build()
    assert pulse.briefing()["cards"][0] == "Due today: Send the Q4 invoice"


def test_research_card_is_optional(pulse, svc, monkeypatch):
    assert pulse.status()["can_research"] is False and pulse.status()["research"] is False
    assert all(c["kind"] != "research" for c in pulse.build()["cards"])

    class Jobs:
        research_fn = staticmethod(lambda prompt: "brief")

        def submit(self, kind, prompt, project=None, title=None):
            self.prompt = prompt
            return {"ok": True, "job_id": svc.storage.add_job(kind, title, prompt, None)}
    svc.jobs = Jobs()
    monkeypatch.setattr("assistant.config.save_setting", lambda cfg, key, value: None)
    assert pulse.actions()["research"]({"on": True}) == {"ok": True, "research": True}
    cards = pulse.build()["cards"]
    res = cards[-1]
    assert res["kind"] == "research" and res["title"] == "Researching: Stream 4 nights" and len(cards) == 5
    assert "Stream 4 nights" in svc.jobs.prompt
    svc.storage.update_job(res["job_id"], "done", "1) Raid smaller streamers.\n2) Clip the best moment daily.")
    assert pulse.cards()[-1]["body"] == "1) Raid smaller streamers. 2) Clip the best moment daily."


@pytest.mark.parametrize("said,tool", [
    ("what's my pulse", "pulse"), ("show me today's pulse", "pulse"), ("my pulse", "pulse"),
    ("run pulse now", "run_pulse"), ("refresh my pulse", "run_pulse"), ("run my pulse again", "run_pulse"),
])
def test_routes(svc, said, tool):
    i = route(said, RouterContext(skill_route=Skills(svc, ["pulse"]).route))
    assert i.tool == tool


def test_spoken_replies(pulse, svc):
    tools = {t.name: t for t in pulse.tools()}
    assert tools["pulse"].untrusted and tools["run_pulse"].tier_for({}) == 1
    assert pulse.say("pulse", {}, tools["pulse"].handler()) == "No Pulse cards yet. Say “run pulse now”."
    assert pulse.say("run_pulse", {}, tools["run_pulse"].handler()) == "Done: 5 Pulse cards on the HUD."
    line = pulse.say("pulse", {}, tools["pulse"].handler())
    assert line.startswith("Today's Pulse: Due today: Send the Q4 invoice; Goal check:") and line.endswith("2 more on the HUD.")
    svc.live = lambda: True
    assert pulse.say("pulse", {}, tools["pulse"].handler()) == "5 Pulse cards on the HUD."


def test_status_and_actions(pulse):
    st = pulse.status()
    assert st == {**st, "ok": True, "cards": [], "time": "05:00", "live": False}
    assert pulse.actions()["run"]({})["ok"]
    st = pulse.status()
    assert len(st["cards"]) == 5 and st["by"] == "local" and st["day"] == "2026-10-13"
    assert Skills(pulse.svc, ["pulse"]).action("pulse", "vote", {"id": st["cards"][0]["id"], "vote": -1})["ok"]


def test_fast_path_end_to_end(pulse, svc):
    from assistant.brain.assistant import Assistant

    svc.skills = Skills(svc, ["pulse"])
    svc.skills.all["pulse"] = pulse
    a = Assistant(svc)
    assert a.handle("run pulse now")["reply"] == "Done: 5 Pulse cards on the HUD."
    assert a.handle("what's my pulse")["reply"].startswith("Today's Pulse: Due today: Send the Q4 invoice;")
