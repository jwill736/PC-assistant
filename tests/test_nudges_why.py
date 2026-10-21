"""Every update carries a "why" (rule + evidence) to the HUD, and "why did you say that" explains the last one."""

from datetime import datetime, timedelta

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills.nudges import Nudges, ago_words, make_why


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(svc):
    said, events = [], []
    svc.speak = lambda text, **kw: said.append(text)
    svc.bus.on(lambda ev: events.append(ev) if ev["type"] == "notice" else None)
    state = {"live": False}
    svc.live = lambda: state["live"]
    clock = Clock()
    n = Nudges(svc, clock=clock, idle=lambda: 0, fullscreen=lambda: "")
    n.cfg["quiet_hours"] = "00:00-00:00"
    return n, clock, state, said, events


def test_make_why_shapes_and_ago_words():
    assert make_why(None, "calendar") == {"rule": "Meeting starting soon", "evidence": []}
    assert make_why("Because", "x") == {"rule": "Because", "evidence": []}
    assert make_why(["Calendar: Standup at 9"], "planner")["evidence"] == ["Calendar: Standup at 9"]
    w = make_why({"rule": "R", "evidence": "one line", "note": "n"})
    assert w == {"rule": "R", "evidence": ["one line"], "note": "n"}
    assert make_why(None, "stream_copilot")["rule"] == "Stream copilot update"
    assert [ago_words(s) for s in (30, 600, 7200, 3 * 86400)] == ["just now", "10 minutes ago", "2 hours ago", "3 days ago"]


def test_why_travels_with_the_notice_and_the_feed(env):
    n, clock, state, said, events = env
    why = {"rule": "Email follow-up", "evidence": ["Gmail: Dana Ortiz, “Redlines”, 3 days ago"]}
    n.add("Dana is still waiting on the redlines.", source="inbox_zero", why=why)
    n.deliver(at_break=True)
    assert events[-1]["data"]["why"] == why
    st = n.status()
    assert st["feed"][0]["why"] == why
    assert said == ["Dana is still waiting on the redlines."]


def test_built_in_rules_say_why(env, svc, monkeypatch):
    n, clock, state, said, events = env
    soon = datetime.now(svc.tz) + timedelta(minutes=6)
    monkeypatch.setattr(svc.calendars, "agenda", lambda days=1, **k: [
        {"title": "Client sync", "start": soon.isoformat(), "all_day": False, "location": "Zoom"}])
    n._rules(clock.t)
    m = next(x for x in n.feed + n.queue if x.source == "calendar")
    assert m.why["rule"] == "Meeting starting soon"
    assert m.why["evidence"][0] == f"Calendar: Client sync at {soon.strftime('%I:%M %p').lstrip('0').replace(':00', '')} (Zoom)"


def test_why_did_you_say_that(env, svc):
    n, clock, state, said, events = env
    assert n.explain()["ok"] is False
    assert n.say("explain_nudge", {}, n.explain()) == "I haven't brought anything up lately."
    n.add("Stand up for two minutes.", source="break",
          why={"rule": "Long stretch without a break", "evidence": ["Activity: at the PC since 1:05 PM", "Setting: a break after 90 minutes"]})
    n.deliver(at_break=True)
    r = n.explain()
    assert r["spoken"] and r["text"] == "Stand up for two minutes."
    assert n.say("explain_nudge", {}, r) == ("I said it because of: long stretch without a break. I saw: Activity: at the PC "
                                            "since 1:05 PM; Setting: a break after 90 minutes.")
    state["live"] = True  # on air: the rule, not the (maybe private) evidence
    line = n.say("explain_nudge", {}, n.explain())
    assert "Activity" not in line and "on the HUD" in line


def test_explain_falls_back_to_the_last_update_shown(env):
    n, clock, state, said, events = env
    n.add("Pulse is ready.", source="pulse", priority="low", why="Morning Pulse")
    r = n.explain()
    assert r["ok"] and not r["spoken"] and n.say("explain_nudge", {}, r) == "The last update because of: morning pulse."


@pytest.mark.parametrize("said", ["why did you say that", "why did you tell me that", "why are you telling me this",
                                  "what made you say that", "why did you bring that up", "why did you remind me"])
def test_routes(svc, said):
    sk = Skills(svc, ["nudges"])
    assert route(said, RouterContext(skill_route=sk.route)).tool == "explain_nudge"


def test_feedback_is_kept_per_update(env):
    n, clock, state, said, events = env
    n.add("A", source="ambient", priority="low", key="k1")
    n.add("B", source="calendar", priority="low", key="k2")
    n.feedback("k1", False)
    n.feedback("k2", True)
    assert [(f["text"], f["useful"]) for f in n.recent_feedback()] == [("B", True), ("A", False)]
    assert [f["text"] for f in n.recent_feedback("ambient")] == ["A"]


def test_asking_vesper_out_loud(svc):
    from assistant.brain.assistant import Assistant

    nud = svc.skills["nudges"]
    nud.cfg["quiet_hours"] = "00:00-00:00"
    svc.live = lambda: False
    nud.add("Client sync starts in 8 minutes.", source="calendar", priority="urgent",
            why={"rule": "Meeting starting soon", "evidence": ["Calendar: Client sync at 2 PM"]})
    reply = Assistant(svc).handle("why did you say that?")["reply"]
    assert reply == "I said it because of: meeting starting soon. I saw: Calendar: Client sync at 2 PM."
