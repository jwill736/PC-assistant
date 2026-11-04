"""The meeting autopilot: offer once to record a calendar call (a link that just started, or a call app up), never
without J's yes and never live; "always record <series>"; stop once the meeting is over and the call has gone quiet;
the notes to the phone (or a HUD card). A fake clock, calendar, window list, audio levels and assistant."""

import json
from datetime import datetime, timedelta

import pytest
from test_skill_recorder import SILENCE, SPEECH, Env, notes_think

from assistant.brain.assistant import Assistant
from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import recorder as rmod


class Clock:
    def __init__(self, dt):
        self.t = dt.timestamp()

    def __call__(self):
        return self.t

    def minutes(self, n):
        self.t += n * 60


class Auto:
    """A recorder with a fake clock (10:00:30 on a Wednesday), calendar, windows and assistant."""

    def __init__(self, svc, events=None, wins=None, **kw):
        self.svc = svc
        self.clock = Clock(datetime(2026, 10, 14, 10, 0, 30, tzinfo=svc.tz))
        self.wins = wins if wins is not None else []
        self.events = events if events is not None else [meeting(svc)]
        svc.calendars.events = lambda start, end, profile=None: list(self.events)
        self.e = Env(svc, clock=self.clock, windows=lambda: self.wins, **kw)
        self.r = self.e.r
        self.offers = []
        self.e.assistant.offer = lambda calls, text: self.offers.append((calls, text)) or True
        self.said = []
        svc.speak = lambda text, **k: self.said.append((text, k))
        self.notices = []
        svc.bus.on(lambda ev: ev.get("type") == "notice" and self.notices.append(ev["data"]["text"]))

    def frames(self, seconds, frame=SPEECH, source="system"):
        """``seconds`` of sound (or silence) from a source, with the clock moving along."""
        for _ in range(int(seconds / 0.03)):
            self.e.push([frame], source)
            self.clock.t += 0.03


def meeting(svc, title="Weekly sync", start=(10, 0), minutes=30, link="https://meet.google.com/abc-defg-hij", **extra):
    s = datetime(2026, 10, 14, *start, tzinfo=svc.tz)
    return {"title": title, "start": s.isoformat(), "end": (s + timedelta(minutes=minutes)).isoformat(), "all_day": False,
            "profile": "work", "link": link, "location": "", "description": "",
            "attendees": [{"email": "dana@client.com", "name": "Dana Ortiz"}, {"email": "me@x.com", "self": True}], **extra}


# ---- the offer -------------------------------------------------------------------------------------------------------

def test_a_meeting_with_a_link_starting_is_offered_once_and_nothing_records(svc):
    a = Auto(svc)
    a.r.autopilot()
    assert a.offers == [([("recording_start", {"title": "Weekly sync"})], "record notes for Weekly sync")]
    assert a.said == [("Record notes for Weekly sync? Say yes or no.", {"expects_reply": True})]
    assert a.r.session is None and not a.e.mics  # nothing without the yes
    a.clock.minutes(1)
    a.r.tick()
    assert len(a.offers) == 1 and len(a.said) == 1  # once, yes or no


def test_the_yes_runs_exactly_the_offered_recording(svc):
    a = Auto(svc)
    asst = Assistant(svc)
    asst.tools.tools["recording_start"] = next(t for t in a.r.tools() if t.name == "recording_start")
    a.e.rt.assistant = asst
    a.r.autopilot()
    assert asst.pending_view()["text"] == "record notes for Weekly sync" and a.r.session is None
    reply = asst.confirm(via="voice")
    assert a.r.session is not None and a.r.rec["title"] == "Weekly sync" and a.r.rec["meeting"]["title"] == "Weekly sync"
    assert a.r.system is not None and reply.startswith("Recording Weekly sync.")  # a meeting: the call's audio too


def test_a_no_leaves_it_and_the_next_meeting_is_asked_on_its_own(svc):
    a = Auto(svc, events=[meeting(svc), meeting(svc, "Design review", start=(11, 0))])
    a.r.autopilot()
    a.clock.minutes(60)
    a.r.autopilot()
    assert [t for _, t in a.offers] == ["record notes for Weekly sync", "record notes for Design review"]


def test_no_offer_live_off_the_record_too_late_or_not_a_meeting(svc):
    a = Auto(svc)
    svc.live = lambda: True
    a.r.autopilot()
    a.e.assistant.off, svc.live = True, (lambda: False)
    a.r.autopilot()
    a.e.assistant.off = False
    a.clock.minutes(15)  # the link alone only counts as the meeting starts
    a.r.autopilot()
    assert a.offers == [] and a.r.session is None
    for ev in (meeting(svc, "Ranked", profile="stream"), meeting(svc, "🔒 Deep work"), meeting(svc, link=""),
               meeting(svc, attendees=[{"email": "me@x.com", "self": True, "response": "declined"}]),
               {**meeting(svc), "all_day": True}):
        b = Auto(svc, events=[ev])
        b.r.autopilot()
        assert b.offers == [], ev["title"]


def test_a_call_app_during_a_meeting_is_offered_even_without_a_link(svc):
    a = Auto(svc, events=[meeting(svc, link="", start=(9, 45), minutes=60)])
    a.r.autopilot()
    assert a.offers == []
    a.wins.append(("zoom", "Zoom Meeting", False))
    a.r.autopilot()
    assert [t for _, t in a.offers] == ["record notes for Weekly sync"]


@pytest.mark.parametrize("wins,app", [
    ([("zoom", "Zoom Meeting", False)], "Zoom"),
    ([("chrome", "Meet - abc-defg-hij - Google Chrome", False)], "Google Meet"),
    ([("ms-teams", "Weekly sync | Microsoft Teams", False)], "Teams"),
    ([("discord", "#general | Friends - Discord", True)], "Discord"),  # in front during the meeting
    ([("discord", "#general | Friends - Discord", False)], ""),  # in the background: just Discord open
    ([("chrome", "Weekly sync notes - Google Docs", True)], ""),
    ([], ""),
])
def test_what_counts_as_a_call_app(wins, app):
    assert rmod.Recorder.call_app("Weekly sync", wins) == app


def test_presenting_asks_by_card_not_by_voice(svc):
    a = Auto(svc)
    svc.skills["nudges"].fullscreen = lambda: "presentation"
    a.r.autopilot()
    assert len(a.offers) == 1 and a.said == []


def test_auto_offer_off_and_the_hud_switch(svc):
    a = Auto(svc)
    a.r.actions()["set"]({"auto_offer": False})
    a.r.autopilot()
    assert a.offers == [] and a.r.status()["auto_offer"] is False


def test_without_the_assistant_it_is_a_nudge_with_a_button(svc):
    a = Auto(svc)
    a.e.rt.assistant = None
    got = []
    svc.nudge = lambda text, **kw: got.append((text, kw))
    a.r.autopilot()
    assert got[0][0] == "Record notes for Weekly sync?"
    assert got[0][1]["action"] == {"label": "Record notes", "command": "take notes of this meeting"}


# ---- always record a series ------------------------------------------------------------------------------------------

def test_always_record_a_series_starts_without_asking_that_series_only(svc):
    a = Auto(svc, events=[meeting(svc, "Weekly sync (Oct 14)", start=(10, 0)), meeting(svc, "Budget review", start=(11, 0))])
    out = a.r.always("the weekly sync")
    assert out == {"ok": True, "on": True, "title": "Weekly sync (Oct 14)"}
    assert a.r.say("recording_always", {}, out).startswith("Okay: I'll record Weekly sync (Oct 14) every time")
    a.r.autopilot()
    assert a.offers == [] and a.r.session is not None and a.r.rec["title"] == "Weekly sync (Oct 14)"
    assert a.e.speaker.chimes == 1 and any("you said to always record it" in n for n in a.notices)
    a.r.stop_recording()
    a.clock.minutes(60)
    a.r.autopilot()
    assert [t for _, t in a.offers] == ["record notes for Budget review"]  # another series is still asked
    assert a.r._is_approved("Weekly sync (Oct 21)") and not a.r._is_approved("Sync")
    assert a.r.status()["always"] == ["Weekly sync (Oct 14)"]
    gone = a.r.always("weekly sync", on=False)
    assert gone == {"ok": True, "on": False, "removed": ["Weekly sync (Oct 14)"]} and a.r._approved() == []
    assert a.r.always("weekly sync", on=False)["ok"] is False


def test_always_record_this_meeting_and_never_live(svc):
    a = Auto(svc)
    assert a.r.always("this meeting")["title"] == "Weekly sync"
    a.events = []
    assert "None is on now" in a.r.always("")["error"]
    a.events = [meeting(svc, "Weekly sync", start=(11, 0))]
    a.clock.minutes(60)
    svc.live = lambda: True
    a.r.autopilot()
    assert a.r.session is None


@pytest.mark.parametrize("said,args", [
    ("always record the weekly sync", {"meeting": "weekly sync", "on": True}),
    ("always record my standup meetings", {"meeting": "standup", "on": True}),
    ("always take notes of this meeting", {"meeting": "this", "on": True}),
    ("stop always recording the weekly sync", {"meeting": "weekly sync", "on": False}),
    ("don't record the weekly sync automatically", {"meeting": "weekly sync", "on": False}),
])
def test_always_routes(svc, said, args):
    sk = Skills(svc, ["recorder"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == ("recording_always", args)


def test_always_record_the_pc_audio_is_not_a_meeting(svc):
    sk = Skills(svc, ["recorder"])
    intent = route("always record the pc audio", RouterContext(skill_route=sk.route))
    assert intent is None or intent.tool != "recording_always"


# ---- the auto-stop -----------------------------------------------------------------------------------------------------

def test_stops_once_the_meeting_is_over_and_the_call_is_quiet(svc):
    a = Auto(svc)
    a.r.start_recording()
    assert a.r.system is not None and a.r.rec["meeting"]["title"] == "Weekly sync"
    a.clock.minutes(33)  # past the end (10:30) but inside the 5-minute grace
    a.frames(1)
    a.r.tick()
    assert a.r.session is not None
    a.clock.minutes(3)  # 10:36: past the grace, but they're still talking
    a.frames(1)
    a.r.tick()
    assert a.r.session is not None
    a.frames(61, SILENCE)
    a.r.tick()
    assert a.r.session is not None  # a minute of quiet isn't the end
    a.frames(61, SILENCE)
    a.r.tick()
    assert a.r.session is None and a.e.row()["state"] == "done"
    assert ("Weekly sync is over, so I stopped recording. Notes are coming.", {}) in a.said
    assert any("is over, so I stopped recording" in n for n in a.notices)


def test_no_call_audio_listens_to_the_mic_and_a_pause_counts_as_quiet(svc):
    a = Auto(svc)
    a.r.start_recording(system_audio=False)
    a.clock.minutes(36)
    a.frames(1, source="mic")
    a.r.tick()
    assert a.r.session is not None
    a.r.pause()
    a.r.tick()
    assert a.r.session is None


def test_still_talking_long_after_is_one_reminder_never_a_stop(svc):
    a = Auto(svc)
    a.r.start_recording()
    a.clock.minutes(60)
    a.frames(1)
    a.r.tick()
    a.frames(1)
    a.r.tick()
    assert a.r.session is not None
    assert sum("half an hour after it was due to end" in n for n in a.notices) == 1


def test_a_recording_without_a_meeting_or_with_auto_stop_off_keeps_going(svc):
    a = Auto(svc, events=[])
    a.r.start_recording()
    a.clock.minutes(600)
    a.r.tick()
    assert a.r.session is not None
    a.r.stop_recording()
    b = Auto(svc)
    b.r.cfg["auto_stop"] = False
    b.r.start_recording()
    b.clock.minutes(60)
    b.r.tick()
    assert b.r.session is not None


# ---- the notes ---------------------------------------------------------------------------------------------------------

def _meeting_notes(svc, push=None, live=False):
    a = Auto(svc, think=notes_think())
    got = []
    phone = svc.skills["phone"]
    phone.push = push or (lambda text: got.append(text) or {"ok": True, "sent": ["telegram"]})
    a.r.start_recording()
    a.e.talk()
    svc.live = (lambda: live)
    a.r.stop_recording()
    return a, got


def test_a_meetings_notes_go_to_the_phone_when_paired(svc):
    a, got = _meeting_notes(svc)
    assert got == ["Notes from Weekly sync: We agreed the budget is $40k. 2 action items wait for your OK on the PC."]
    assert json.loads(a.e.row()["tasks"])[0]["task"] == "Send Dana the plan" and a.e.row()["tasks_state"] == "ask"


def test_no_phone_is_a_hud_card_with_the_summary_and_not_on_air(svc):
    def no_phone(text):
        return {"ok": False, "error": "No phone is set up."}
    a, _ = _meeting_notes(svc, push=no_phone)
    assert not any(n.startswith("Notes from Weekly sync:") for n in a.notices)  # the "Add 2 tasks?" card carries it
    b = Auto(svc, think=lambda *x, **k: None)  # no model: the transcript alone, no action items
    svc.skills["phone"].push = no_phone
    b.r.start_recording()
    b.e.talk()
    b.r.stop_recording()
    assert "Notes from Weekly sync: transcript only (no model)" in b.notices
    c = Auto(svc)
    c.r.start_recording(on_stream=True)
    c.e.talk()
    svc.live = lambda: True
    c.r.stop_recording()
    assert "Notes from Weekly sync." in c.notices


def test_push_notes_off_and_a_phone_that_fails(svc):
    def boom(text):
        raise OSError("offline")
    a = Auto(svc)
    svc.skills["phone"].push = boom
    a.r.start_recording()
    a.e.talk()
    a.r.stop_recording()
    assert any(n.startswith("Notes from Weekly sync") for n in a.notices)
    b = Auto(svc)
    b.r.cfg["push_notes"] = False
    sent = []
    svc.skills["phone"].push = lambda text: sent.append(text) or {"ok": True}
    b.r.start_recording()
    b.e.talk()
    b.r.stop_recording()
    assert sent == []
