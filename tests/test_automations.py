"""Automations: reminders, timers, routines and "when X happens" moments, understood without a model."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import automations as au

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 13, 10, 15, tzinfo=NY)  # a Tuesday


@pytest.mark.parametrize("text,want", [
    ("in 20 minutes", datetime(2026, 10, 13, 10, 35, tzinfo=NY)),
    ("in an hour and a half", datetime(2026, 10, 13, 11, 45, tzinfo=NY)),
    ("at 3 pm", datetime(2026, 10, 13, 15, 0, tzinfo=NY)),
    ("at 3", datetime(2026, 10, 13, 15, 0, tzinfo=NY)),
    ("at 9", datetime(2026, 10, 14, 9, 0, tzinfo=NY)),  # 9 already passed: tomorrow
    ("at 9:30 am", datetime(2026, 10, 14, 9, 30, tzinfo=NY)),
    ("tomorrow at 9", datetime(2026, 10, 14, 9, 0, tzinfo=NY)),
    ("tomorrow", datetime(2026, 10, 14, 9, 0, tzinfo=NY)),
    ("tonight", datetime(2026, 10, 13, 20, 0, tzinfo=NY)),
    ("friday at 10 am", datetime(2026, 10, 16, 10, 0, tzinfo=NY)),
    ("at noon", datetime(2026, 10, 13, 12, 0, tzinfo=NY)),
])
def test_parse_when(text, want):
    assert au.parse_when(text, NOW) == want


def test_parse_every_and_next_run():
    s = au.parse_every("weekday at 9 am")
    assert s == {"days": [0, 1, 2, 3, 4], "time": "09:00"}
    assert au.next_run(s, NOW) == datetime(2026, 10, 14, 9, 0, tzinfo=NY)
    assert au.parse_every("monday and thursday at 4 pm") == {"days": [0, 3], "time": "16:00"}
    assert au.describe_schedule(s) == "weekdays at 9 AM"
    assert au.next_run(au.parse_every("friday at 4 pm"), datetime(2026, 10, 16, 17, 0, tzinfo=NY)).day == 23


class Clock:
    def __init__(self):
        self.t = NOW.timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def auto(svc, cfg):
    said = []
    svc.speak = lambda text, **kw: said.append(text)
    sk = Skills(svc, ["nudges"])
    nud = sk["nudges"]
    clock = Clock()
    nud.clock = clock
    nud.idle, nud.fullscreen = (lambda: 0), (lambda: "")
    nud.cfg["quiet_hours"] = "00:00-00:00"
    a = au.Automations(svc, clock=clock)
    a.said, a.nud = said, nud
    return a, clock


def test_timer_fires_through_nudges(auto):
    a, clock = auto
    r = a.timer(600, "pasta")
    assert r["item"]["in_s"] == 600 and a.say("set_timer", {"label": "pasta"}, r) == "Timer set for 10 minutes: pasta."
    clock.t += 601
    a.tick()
    assert a.said == ["pasta timer is done."] and a.listing()["items"] == []


def test_reminder_persists_and_fires(auto, svc):
    a, clock = auto
    a.remind("call Dana", "in 20 minutes")
    again = au.Automations(svc, clock=clock)  # survives a restart
    assert [i["text"] for i in again.listing()["items"]] == ["call Dana"]
    clock.t += 20 * 60 + 1
    a.tick()
    assert a.said == ["Reminder: call Dana."]


def test_moments_after_stream_and_email(auto, svc):
    a, clock = auto
    state = {"live": True}
    svc.live = lambda: state["live"]
    a.nud.svc.live = svc.live
    a.remind("post the VOD", trigger="after_stream")
    a.tick()
    assert a.said == []
    state["live"] = False
    a.tick()
    assert a.said == ["Reminder: post the VOD."]
    a.remind("send the redlines", trigger="email_from:dana")

    class G:
        def accounts(self):
            return ["x"]

        def needs_you(self):
            return {"accounts": [{"people": [{"from": "Dana Ortiz", "from_email": "dana@c.com", "subject": "Redlines"}]}]}
    svc.gmail = G()
    clock.t += 200  # spoken nudges are spaced out
    a.tick()
    assert a.said[-1] == "Dana Ortiz emailed: Redlines. Reminder: send the redlines."


def test_routine_runs_the_command_and_repeats(auto):
    a, clock = auto
    ran = []
    a.run_command = lambda text: ran.append(text) or {"reply": "Nobody's waiting on you."}
    a.routine("every weekday at 9 am", "check my email")
    clock.t = datetime(2026, 10, 14, 9, 0, 1, tzinfo=NY).timestamp()
    a.tick()
    import time
    time.sleep(0.2)
    assert ran == ["check my email"] and "Nobody's waiting on you." in a.said
    assert a.listing()["items"][0]["when"].startswith("Thu 9:00")


def test_cancel(auto):
    a, clock = auto
    a.remind("call Dana", "in 2 hours")
    a.timer(60)
    assert a.cancel("timer")["ok"] and len(a.listing()["items"]) == 1
    assert a.cancel("the reminder about dana")["ok"] and a.listing()["items"] == []
    assert a.cancel("nothing here")["ok"] is False


@pytest.mark.parametrize("said,tool,args", [
    ("remind me in 20 minutes to call dana", "remind", {"text": "call dana", "when": "in 20 minutes"}),
    ("remind me to send the invoice at 3 pm", "remind", {"text": "send the invoice", "when": "at 3 pm"}),
    ("remind me tomorrow at 9 to email the team", "remind", {"text": "email the team", "when": "tomorrow at 9"}),
    ("remind me to post the vod after the stream", "remind", {"text": "post the vod", "trigger": "after_stream"}),
    ("remind me to stretch when i'm back at my pc", "remind", {"text": "stretch", "trigger": "back"}),
    ("remind me to send the redlines when dana emails me", "remind", {"text": "send the redlines", "trigger": "email_from:dana"}),
    ("set a timer for 10 minutes", "set_timer", {"seconds": 600}),
    ("25 minute timer called pasta", "set_timer", {"seconds": 1500, "label": "pasta"}),
    ("every weekday at 9 am check my email", "add_routine", {"every": "weekday at 9 am", "command": "check my email"}),
    ("what are my reminders", "list_automations", {}),
    ("how long is left on the timer", "list_automations", {}),
    ("cancel the timer", "cancel_automation", {"what": "timer"}),
    ("cancel the reminder about dana", "cancel_automation", {"what": "reminder about dana"}),
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["automations"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)
