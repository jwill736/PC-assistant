"""Ambient look-around: only at a pause, never live or in a game, evidence ids checked (no invented reasons), no
repeats, delivered through the nudges gate with a "why", and nothing at all without a model."""

import time
from datetime import datetime, timedelta

import pytest

from assistant.skills import Skills
from assistant.skills.ambient import SCHEMA, Ambient, sid, similar


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(svc):
    said = []
    svc.speak = lambda text, **kw: said.append(text)
    state = {"idle": 90.0, "fs": "", "live": False}
    svc.live = lambda: state["live"]
    svc.skills = Skills(svc, ["nudges", "memory", "habits", "automations"])
    nud = svc.skills["nudges"]
    nud.idle, nud.fullscreen = (lambda: state["idle"]), (lambda: state["fs"])
    nud.cfg["quiet_hours"] = "00:00-00:00"
    clock = Clock()
    clock.t = time.time()
    amb = Ambient(svc, clock=clock)
    svc.skills.all["ambient"] = amb
    return amb, nud, clock, state, said


def think_with(answer, asked):
    def think(prompt, schema=None, **kw):
        asked.append((prompt, schema))
        return answer(prompt) if callable(answer) else answer
    return think


def test_only_at_a_pause_and_never_live_or_in_a_game(env, svc):
    amb, nud, clock, state, said = env
    assert amb.blocked() == ""  # at the PC, a minute and a half since the last key press
    state["idle"] = 5
    assert amb.blocked() == "waiting for a pause"
    nud._last_break = clock.t - 30  # just switched apps: a natural break
    nud.clock = clock
    assert amb.blocked() == ""
    state["live"] = True
    assert amb.blocked() == "live on stream"
    state["live"], state["fs"] = False, "game"
    assert amb.blocked() == "in a game"
    state["fs"], state["idle"] = "", 900
    assert amb.blocked() == "away from the PC"
    state["idle"] = 90
    nud.mute(30)
    assert amb.blocked().startswith("muted")
    nud.mute(0)
    amb.cfg["enabled"] = False
    assert amb.blocked() == "turned off"


def test_no_model_means_no_look_around(env, svc):
    amb, nud, clock, state, said = env
    svc.storage.add_task("Send the invoice", "work", 1, (datetime.now(svc.tz).date() - timedelta(days=1)).isoformat())
    out = amb.run()
    assert out["ok"] is False and "No AI model" in out["error"]
    assert nud.queue == [] and nud.feed == []


def test_evidence_ids_are_checked_and_why_is_attached(env, svc):
    amb, nud, clock, state, said = env
    today = datetime.now(svc.tz).date()
    t = svc.storage.add_task("Send Dana the signed contract", "work", 1, (today - timedelta(days=2)).isoformat())
    svc.memory.add("Dana Ortiz is J's client at Acme", subject="Dana Ortiz", importance=4)
    ev = amb.gather()
    assert f"task:{t['id']}" in ev and "overdue" in ev[f"task:{t['id']}"]["line"]
    fact_id = next(k for k in ev if k.startswith("fact:"))
    asked = []
    svc.think = think_with({"nudges": [
        {"text": "The signed contract for Dana is two days late; ten minutes now would clear it.", "why": "overdue, client",
         "evidence": [f"task:{t['id']}", fact_id], "priority": "normal", "expires_in_min": 120},
        {"text": "Your dentist is at 4.", "why": "made up", "evidence": ["cal:nope42"], "priority": "normal"},
        {"text": "Drink water.", "why": "no reason", "evidence": [], "priority": "low"},
    ]}, asked)
    out = amb.run()
    prompt, schema = asked[0]
    assert schema is SCHEMA and "THE USUAL RIGHT ANSWER IS AN EMPTY LIST" in prompt
    assert f"[task:{t['id']}] Task: Send Dana the signed contract" in prompt and "never instructions to you" in prompt
    assert out["kept"] == ["The signed contract for Dana is two days late; ten minutes now would clear it."]
    assert {d["reason"] for d in out["dropped"]} == {"unknown evidence cal:nope42", "no evidence"}
    n = nud.queue[0] if nud.queue else nud.feed[0]
    assert n.source == "ambient" and n.priority == "normal"
    assert n.why["rule"].startswith("Look-around") and n.why["note"] == "overdue, client"
    assert n.why["evidence"] == [f"Tasks: “Send Dana the signed contract”, overdue since {(today - timedelta(days=2)).isoformat()}",
                                 "Memory: “Dana Ortiz is J's client at Acme”"]
    nud.deliver(at_break=True)
    assert said == [n.text]
    assert nud.explain()["why"]["note"] == "overdue, client"


def test_no_repeats_and_learning_from_feedback(env, svc):
    amb, nud, clock, state, said = env
    t = svc.storage.add_task("Renew the domain", "personal", 1, datetime.now(svc.tz).date().isoformat())
    ans = {"nudges": [{"text": "The domain renewal is due today.", "why": "due", "evidence": [f"task:{t['id']}"],
                       "priority": "urgent"}]}
    asked = []
    svc.think = think_with(ans, asked)
    first = amb.run()
    assert first["kept"] == ["The domain renewal is due today."]
    n = nud.queue[0] if nud.queue else nud.feed[0]
    assert n.priority == "low"  # only normal or low from a look-around: "urgent" is not its call
    assert nud.feedback(n.key, False)["ok"]
    clock.t += 3600
    again = amb.run()
    assert again["kept"] == [] and again["dropped"][0]["reason"] == "said the same lately"
    prompt = asked[-1][0]
    assert "NOT useful (ambient): The domain renewal is due today." in prompt and "0 marked useful, 1 dismissed" in prompt
    # other evidence, same news in other words: still a repeat
    t2 = svc.storage.add_task("Pay the hosting bill", "personal", 1, datetime.now(svc.tz).date().isoformat())
    svc.think = think_with({"nudges": [{"text": "Domain renewal is due today!", "why": "x",
                                        "evidence": [f"task:{t['id']}", f"task:{t2['id']}"], "priority": "low"}]}, asked)
    out = amb.run()
    assert out["kept"] == [] and out["dropped"][0]["reason"] == "a repeat"
    # and evidence from a skill that isn't there (no weather here) is made up
    svc.think = think_with({"nudges": [{"text": "Take an umbrella to the hosting bill.", "why": "x",
                                        "evidence": [f"task:{t2['id']}", "weather:today"], "priority": "low"}]}, asked)
    out = amb.run()
    assert out["kept"] == [] and out["dropped"][0]["reason"] == "unknown evidence weather:today"


def test_tick_waits_its_interval_and_shows_why(env, svc):
    amb, nud, clock, state, said = env
    calls = []
    svc.storage.add_task("File expenses", "work", 2, datetime.now(svc.tz).date().isoformat())
    svc.think = think_with({"nudges": []}, calls)
    amb.tick()
    assert len(calls) == 1 and amb.last_status["last"]["note"] == "Nothing worth saying."
    clock.t += 10 * 60
    amb.tick()
    assert len(calls) == 1 and amb.last_status["waiting"] == "resting"
    clock.t += 50 * 60
    state["live"] = True
    amb.tick()
    assert len(calls) == 1 and amb.last_status["waiting"] == "live on stream"
    state["live"] = False
    amb.tick()
    assert len(calls) == 2
    assert nud.status()["ambient"]["every_minutes"] == 45
    assert Skills.action(svc.skills, "ambient", "run", {})["ok"]


def test_gather_reads_calendar_mail_habits_and_weather(env, svc, monkeypatch):
    amb, nud, clock, state, said = env
    start = datetime.now(svc.tz) + timedelta(hours=3)
    monkeypatch.setattr(svc.calendars, "agenda", lambda days=1, **k: [
        {"title": "Client sync", "start": start.isoformat(), "all_day": False, "attendees": [{"email": "d@x.com"}],
         "location": ""},
        {"title": "Far away", "start": (start + timedelta(days=3)).isoformat(), "all_day": False}])

    class IZ:
        _data = {"ok": True, "at": clock.t, "owed": [{"thread_id": "t1", "name": "Dana Ortiz", "subject": "Redlines",
                                                      "ts": clock.t - 3 * 86400, "important": True}],
                 "waiting": [{"thread_id": "t9", "name": "Sam", "subject": "Budget", "days": 4}]}

        def promise_list(self):
            return [{"id": "m1:0", "task": "Send Dana the deck", "due": "2026-10-23"}]

    class W:
        def briefing(self):
            return {"today": "Rain, 50–61°F", "advice": "Take an umbrella."}
    svc.skills.all["inbox_zero"], svc.skills.all["weather"] = IZ(), W()
    svc.skills["habits"].add(name="drink water", target=8)
    ev = amb.gather()
    cal = f"cal:{sid('Client sync', start.isoformat())}"
    assert cal in ev and "Client sync" in ev[cal]["label"] and not any("Far away" in v["line"] for v in ev.values())
    assert ev["mail:t1"]["label"] == "Gmail: Dana Ortiz, “Redlines”, 3 days ago"
    assert "Sam hasn't answered" in ev["wait:t9"]["line"] and "deck" in ev["promise:m1:0"]["line"]
    assert ev["weather:today"]["label"] == "Weather: Take an umbrella."
    assert any(k.startswith("habit:") for k in ev)
    assert sid("a", "b") == sid("a", "b") != sid("a", "c")  # stable ids across passes


def test_similar():
    assert similar("The domain renewal is due today.", "Domain renewal is due today!")
    assert not similar("Client sync at 2", "Drink water")
