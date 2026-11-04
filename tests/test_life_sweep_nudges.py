"""Sweep regressions (LIFE-3, 6, 9, 12): J's own timers always ring, nothing is said into a call, a dismissed nudge
stays dismissed, and the Sunday preview waits out a Sunday stream and comes once across a restart."""

from datetime import datetime

import pytest

from assistant.skills import Skills
from assistant.skills import automations as au
from assistant.skills.nudges import Nudges
from assistant.skills.planner import Planner


class Clock:
    def __init__(self, dt):
        self.t = dt.timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def world(svc):
    said = []
    svc.speak = lambda text, **kw: said.append(text)
    state = {"live": False, "fs": ""}
    svc.live = lambda: state["live"]
    svc.calendars.agenda = lambda days=1, **kw: []
    svc.calendars.events = lambda *a, **k: []
    return svc, said, state


def _nudges(svc, clock, state):
    n = Nudges(svc, clock=clock, idle=lambda: 0, fullscreen=lambda: state["fs"])
    n.cfg["quiet_hours"] = "00:00-00:00"
    n.chime = lambda: None
    return n


# ---- LIFE-9: dismissed stays dismissed; once means once, restarts included ---------------------------------------------

def test_dismissing_the_weekly_review_nudge_keeps_it_dismissed(world):
    """J clicks ✕ on "It's the end of the week…" (Friday 4 PM); the planner asks again with the same key every tick."""
    svc, said, state = world
    clock = Clock(datetime(2026, 10, 16, 16, 5, tzinfo=svc.tz))  # a Friday
    n = _nudges(svc, clock, state)
    p = Planner(svc, clock=clock)
    p.tick()
    n.deliver(at_break=True)
    first = [x for x in n.feed if x.key.startswith("planner:review")]
    assert len(first) == 1 and said == [first[0].text]
    n.feedback(first[0].key, useful=False)  # the ✕ on the card
    for _ in range(3):  # the next 15 minutes
        clock.t += 300
        p.tick()
        n.deliver(at_break=True)
    again = [x for x in n.feed if x.key.startswith("planner:review")]
    assert again == [], f"came back {len(again)} times after J dismissed it; spoken {said.count(first[0].text)} times"
    assert said.count(first[0].text) == 1


def test_a_dismissed_or_cleared_nudge_stays_away_for_its_time(world):
    svc, said, state = world
    clock = Clock(datetime(2026, 10, 14, 12, 0, tzinfo=svc.tz))
    n = _nudges(svc, clock, state)
    x = n.add("Dana replied.", source="email", key="email:dana", expires_in=3600)
    n.deliver(at_break=True)
    n.feedback(x.key, useful=False)
    clock.t += 600
    assert n.add("Dana replied.", source="email", key="email:dana") is None  # its skill asking again
    n.add("Stretch.", source="break", key="break:1", expires_in=1800)
    n.deliver(at_break=True)
    n.actions()["clear"]({})
    assert n.add("Stretch.", source="break", key="break:1") is None
    clock.t += 3600  # their time is up: a new one may come
    assert n.add("Dana replied.", source="email", key="email:dana") is not None
    assert n.add("Stretch.", source="break", key="break:1") is not None


def test_sunday_preview_once_across_a_restart(world):
    """Vesper restarts (an update) on Sunday at 19:00: the preview must not come again."""
    svc, said, state = world
    clock = Clock(datetime(2026, 10, 18, 18, 5, tzinfo=svc.tz))
    n = _nudges(svc, clock, state)
    Planner(svc, clock=clock).tick()
    n.deliver(at_break=True)
    clock.t += 3600
    n2 = _nudges(svc, clock, state)  # the restart: new skills, same storage
    Planner(svc, clock=clock).tick()
    n2.deliver(at_break=True)
    previews = [s for s in said if s.startswith("Next week:")]
    assert len(previews) == 1, f"the Sunday preview was said {len(previews)} times"


def test_a_preview_held_by_a_restart_still_comes(world):
    """A restart before the preview was shown (held while live) makes it again: once counts what J saw."""
    svc, said, state = world
    clock = Clock(datetime(2026, 10, 18, 18, 5, tzinfo=svc.tz))
    state["live"] = True
    n = _nudges(svc, clock, state)
    Planner(svc, clock=clock).tick()
    n.deliver(at_break=True)
    assert n.feed == [] and len(n.queue) == 1
    clock.t += 3600
    state["live"] = False
    n2 = _nudges(svc, clock, state)
    Planner(svc, clock=clock).tick()
    n2.deliver(at_break=True)
    assert [s for s in said if s.startswith("Next week:")] != []


# ---- LIFE-12: the preview waits out a Sunday stream --------------------------------------------------------------------

def test_sunday_preview_reaches_j_after_a_sunday_stream(world):
    """J streams Sunday 17:00-21:30. The preview is made at 18:00 and held while live; it used to expire at 21:00."""
    svc, said, state = world
    clock = Clock(datetime(2026, 10, 18, 18, 1, tzinfo=svc.tz))  # a Sunday
    n = _nudges(svc, clock, state)
    p = Planner(svc, clock=clock)
    state["live"] = True
    while clock.t < datetime(2026, 10, 18, 21, 30, tzinfo=svc.tz).timestamp():
        p.tick()
        n.tick()
        clock.t += 1200  # low-power ticks while live
    assert said == []
    state["live"] = False
    for _ in range(6):
        clock.t += 300
        p.tick()
        n.deliver(at_break=True)
    shown = [x for x in n.feed if x.key.startswith("planner:week")]
    assert shown, "the Sunday preview never reached the feed or the voice"
    assert len([s for s in said if s.startswith("Next week:")]) == 1


# ---- LIFE-3: timers and reminders J set always ring --------------------------------------------------------------------

@pytest.fixture
def auto(svc):
    said = []
    svc.speak = lambda text, **kw: said.append(text)
    state = {"fs": "", "live": False}
    svc.live = lambda: state["live"]
    sk = Skills(svc, ["nudges"])
    nud = sk["nudges"]
    clock = Clock(datetime(2026, 10, 13, 15, 0, tzinfo=svc.tz))
    nud.clock = clock
    nud.idle, nud.fullscreen = (lambda: 0), (lambda: state["fs"])
    nud.cfg["quiet_hours"] = "00:00-00:00"
    rings = []
    nud.chime = lambda: rings.append(clock.t)
    notices = []
    svc.bus.on(lambda ev: ev["type"] == "notice" and notices.append(ev["data"]))
    a = au.Automations(svc, clock=clock)
    return a, nud, clock, said, state, rings, notices


def test_a_timer_rings_in_a_game(auto):
    """automations is game_essential ("timers and reminders ring on time, game or not"), but the gate is shut in a game."""
    a, nud, clock, said, state, rings, notices = auto
    a.timer(600, "pizza")
    state["fs"] = "game"
    clock.t += 601
    a.tick()
    assert said == ["pizza timer is done."], f"said {said}"
    assert len(rings) == 1


def test_two_timers_close_together_both_ring(auto):
    """Spoken nudges are spaced 90 s apart and capped at 6 an hour: a timer J set isn't held to that."""
    a, nud, clock, said, state, rings, notices = auto
    a.timer(300, "eggs")
    a.timer(330, "toast")
    clock.t += 301
    a.tick()
    clock.t += 30
    a.tick()
    assert said == ["eggs timer is done.", "toast timer is done."], f"said {said}"


def test_a_timer_rings_when_j_walked_away(auto):
    """Ten minutes with no keyboard or mouse is 'away', and the gate held even urgent nudges while away."""
    a, nud, clock, said, state, rings, notices = auto
    idle = {"s": 0}
    nud.idle = lambda: idle["s"]
    a.timer(600, "pizza")
    clock.t += 601
    idle["s"] = 600
    a.tick()
    nud.tick()
    assert said == ["pizza timer is done."], f"said {said}; held: {[(n.text, n.held) for n in nud.queue]}"
    assert [x["text"] for x in notices if x.get("level") == "urgent"] == ["pizza timer is done."]  # the phone's push


def test_a_reminder_rings_in_quiet_hours_focus_and_quiet_mode(auto):
    a, nud, clock, said, state, rings, notices = auto
    nud.cfg["quiet_hours"] = "00:00-23:59"
    nud.focus = lambda: True
    nud.mute(60)
    for _ in range(4):  # a source J keeps dismissing goes quiet: not his own reminders
        x = nud.add(f"x{_}", source="automations")
        nud.feedback(x.key, useful=False)
    a.remind("take the laundry out", "in 5 minutes")
    clock.t += 301
    a.tick()
    assert said == ["Reminder: take the laundry out."] and len(rings) == 1


def test_live_a_reminder_is_the_chime_and_the_hud_never_its_words(auto):
    a, nud, clock, said, state, rings, notices = auto
    a.remind("call the clinic about the results", "in 5 minutes")
    a.timer(400, "pizza")
    state["live"] = True
    clock.t += 401
    a.tick()
    assert said == [] and len(rings) == 2
    assert {x["text"] for x in notices if x.get("ring")} == {"Reminder: call the clinic about the results.",
                                                              "pizza timer is done."}
    state["live"], state["fs"] = False, "presentation"  # presenting: others would hear it too
    a.timer(60, "tea")
    clock.t += 61
    a.tick()
    assert said == [] and len(rings) == 3


def test_ordinary_nudges_keep_their_gate(auto):
    a, nud, clock, said, state, rings, notices = auto
    state["fs"] = "game"
    nud.add("Dana replied.", source="email", priority="urgent")
    assert said == [] and rings == []


def test_with_nudges_off_a_timer_still_rings(svc, monkeypatch):
    said, rang = [], []
    svc.speak = lambda text, **kw: said.append(text)
    svc.nudge = None
    monkeypatch.setattr("assistant.skills.nudges.ring_sound", lambda: rang.append(1))
    clock = Clock(datetime(2026, 10, 13, 15, 0, tzinfo=svc.tz))
    a = au.Automations(svc, clock=clock)
    a.timer(60, "tea")
    clock.t += 61
    a.tick()
    assert said == ["tea timer is done."] and rang == [1]


# ---- LIFE-6: nothing is said into a call -------------------------------------------------------------------------------

def test_nudges_are_not_spoken_into_a_call_being_recorded(svc):
    """While the autopilot records a Zoom call, an urgent email nudge is shown on the HUD, not said into the call."""
    from test_skill_recorder_autopilot import Auto

    a = Auto(svc)
    spoken = []
    svc.speak = lambda text, **kw: spoken.append(text)
    n = Nudges(svc, clock=a.clock, idle=lambda: 0, fullscreen=lambda: "")
    n.cfg["quiet_hours"] = "00:00-00:00"
    rings = []
    n.chime = lambda: rings.append(1)
    a.r.start_recording()  # recording the Weekly sync call, with its audio
    assert a.r.session is not None and a.r.system is not None
    n.add("Important email from Dr. Lee: “Your test results”.", source="inbox_zero", priority="urgent")
    assert spoken == [], f"said during the call: {spoken}"
    assert n.feed and n.feed[0].text.startswith("Important email") and n.gate()["why"] == "in a call"
    n.add("Reminder: call mom.", source="automations", key="reminder:1", ring=True)
    assert spoken == [] and rings == [1]  # J's own reminder: the chime, never its words into the call
    a.r.stop_recording()
    n.add("Dana replied.", source="email", priority="urgent")
    assert spoken == ["Dana replied."]


def test_a_call_app_up_during_a_meeting_holds_nudges_until_it_ends(svc):
    from test_skill_recorder_autopilot import Auto, meeting

    a = Auto(svc, events=[meeting(svc, link="", start=(9, 45), minutes=60)], wins=[("zoom", "Zoom Meeting", False)])
    a.r.cfg["auto_offer"] = False
    n = Nudges(svc, clock=a.clock, idle=lambda: 0, fullscreen=lambda: "")
    n.cfg["quiet_hours"] = "00:00-00:00"
    a.r.tick()
    assert n.gate()["why"] == "in a call"
    a.clock.minutes(46)  # 10:46: the meeting is over
    assert n.gate()["why"] == ""
