"""Sweep regressions for the meeting autopilot and the recorder (LIFE-2, 4, 5, 10, 14, 15, 17, 18)."""

import json
import os
import threading
import time
from pathlib import Path

import numpy as np
from test_skill_recorder import SPEECH, STT, Env, notes_think
from test_skill_recorder_autopilot import Auto, meeting

from assistant.skills import recorder as rmod
from assistant.voice import recording


# ---- LIFE-2: "always record X" is that series only ---------------------------------------------------------------------

def test_always_record_weekly_sync_does_not_auto_record_a_different_meeting(svc):
    a = Auto(svc, events=[meeting(svc, "Weekly sync", start=(10, 0))])
    assert a.r.always("the weekly sync")["title"] == "Weekly sync"
    a.events = [meeting(svc, "Weekly HR sync with Dana (confidential)", start=(11, 0))]
    a.clock.minutes(60)  # 11:00:30
    a.r.autopilot()
    assert a.r.session is None, "recorded a meeting J never approved, without asking"
    assert [t for _, t in a.offers] == ["record notes for Weekly HR sync with Dana (confidential)"]


def test_an_approved_series_is_its_exact_words():
    assert rmod.series("Weekly sync (Oct 14) #12") == "weekly sync"
    assert rmod.series("Sprint 42 planning, 3rd") == "sprint planning"
    assert rmod.series("Q4 planning") == "q4 planning"


def test_an_approved_series_matches_only_itself(svc):
    a = Auto(svc, events=[meeting(svc, "Q4 planning", start=(10, 0))])
    a.r.always("q4 planning")
    for other in ("Succession planning", "Sprint planning", "Weekly sync: legal", "Planning"):
        assert not a.r._is_approved(other), other
    assert a.r._is_approved("Q4 planning (Oct 21)")


# ---- LIFE-10: Teams open or Discord in front is not a call for a personal block -----------------------------------------

def test_teams_open_is_not_a_call_for_a_lunch_block(svc):
    lunch = meeting(svc, "Lunch", start=(12, 0), minutes=60, link="", attendees=[])
    a = Auto(svc, events=[lunch], wins=[("ms-teams", "Chat | Microsoft Teams", False)])
    a.clock.minutes(120)  # 12:00:30
    a.r.autopilot()
    assert a.offers == [], f"offered to record 'Lunch' because Teams is open: {a.offers} / said {a.said}"
    a.wins = [("discord", "#general | Friends - Discord", True)]  # Discord in front over lunch
    a.r.autopilot()
    assert a.offers == [] and not a.r.in_call()


def test_a_teams_meeting_window_is_still_a_call(svc):
    assert rmod.Recorder.call_app("Lunch", [("ms-teams", "Meeting with Dana Ortiz | Microsoft Teams", False)]) == "Teams"
    assert rmod.Recorder.call_app("Lunch", [("ms-teams", "Calendar | Microsoft Teams", False)]) == ""
    assert rmod.Recorder.call_app("Lunch", [("discord", "#general - Discord", True)], others=False) == ""


# ---- LIFE-5: auto-stop never cuts J off -------------------------------------------------------------------------------

def test_auto_stop_does_not_cut_off_j_while_he_is_talking(svc):
    a = Auto(svc)
    a.r.start_recording()
    assert a.r.system is not None
    a.clock.minutes(36)  # 10:36: past the end (10:30) and the 5-minute grace
    for _ in range(5):  # 2.5 minutes of J talking on his mic; the call's sound is silent (others listening)
        a.frames(30, SPEECH, source="mic")
        a.r.tick()
    assert a.r.session is not None, "stopped the recording in the middle of J talking"


# ---- LIFE-15: the recording follows the meeting J said yes to ------------------------------------------------------------

def test_auto_stop_uses_the_meeting_that_was_offered(svc):
    sync = meeting(svc, "Weekly sync", start=(10, 0), minutes=45)
    client = meeting(svc, "Client call", start=(10, 30), minutes=60, link="https://zoom.us/j/123", attendees=[])
    a = Auto(svc, events=[sync, client])
    a.clock.minutes(30)  # 10:30:30
    a.r.autopilot()
    assert [t for _, t in a.offers][-1] == "record notes for Client call"
    a.r.start_recording(title="Client call")  # what the yes runs (recording_start {"title": "Client call"})
    assert a.r.rec["meeting"]["title"] == "Client call", f"tied to {a.r.rec['meeting']['title']}"
    a.r.stop_recording()
    a.r.start_recording()  # no title: the meeting with people, as before
    assert a.r.rec["meeting"]["title"] == "Weekly sync"


# ---- LIFE-4: a meeting being recorded isn't debriefed, and "that's a wrap" stops the recording --------------------------

def test_debrief_does_not_ask_about_a_meeting_being_recorded(svc):
    from assistant.skills.debrief import Debrief

    a = Auto(svc)
    asked = []
    svc.nudge = lambda text, **kw: asked.append((text, kw)) or svc.bus.publish(
        "notice", {"text": text, "key": kw.get("key")})
    d = Debrief(svc, clock=a.clock)
    a.r.start_recording()  # J said yes to "Record notes for Weekly sync?"
    a.clock.minutes(31)  # 10:31: the calendar end has passed; the call is still on (overran), still recording
    d.check()
    assert asked == [], f"debrief asked while recording: {asked[0][0]!r}"
    a.r.stop_recording()  # the notes are linked to the meeting: still never asked
    d.check()
    assert asked == [] and d._row("Weekly sync|" + meeting(svc)["start"])["state"] == "done"


def test_while_debrief_listens_the_recorders_phrases_still_reach_the_recorder(svc, monkeypatch):
    from assistant.skills.debrief import Debrief

    a = Auto(svc)
    monkeypatch.setitem(svc.skills.all, "recorder", a.r)  # the recorder that's recording
    d = Debrief(svc, clock=a.clock)
    d._asking, d._listen_until = {"key": "x", "title": "Design review", "end": meeting(svc)["end"]}, a.clock() + 180
    a.r.start_recording()
    for said in ("that's a wrap", "end the recording", "resume recording"):
        assert d.route(said) is None, said
    assert d.route("i will send dana the deck by friday").tool == "debrief_save"


# ---- LIFE-17: notes written mid-game wait for the model instead of "transcript only" for good --------------------------

def test_notes_wait_for_the_game_to_end(svc):
    a = Auto(svc, think=lambda *x, **k: None)  # a game holds the only (big) model: think() says None
    mode = {"m": "game"}
    a.e.assistant.low_power = lambda: mode["m"]
    sent = []
    svc.skills["phone"].push = lambda text: sent.append(text) or {"ok": True, "sent": ["telegram"]}
    a.r.start_recording()
    a.e.talk()
    a.r.stop_recording()
    assert a.e.row()["state"] == "later" and sent == []
    assert a.r.status()["recent"][0]["job"] == "Notes after the game"
    a.r.tick()  # still in the game
    assert a.e.row()["state"] == "later"
    svc.think, mode["m"] = notes_think(), ""
    a.r.tick()
    row = a.e.row()
    assert row["state"] == "done" and row["by"] == "model"
    assert sent and sent[0].startswith("Notes from Weekly sync: We agreed the budget is $40k.")


def test_no_model_at_all_is_still_transcript_only(svc):
    a = Auto(svc, think=lambda *x, **k: None)
    a.e.assistant.low_power = lambda: ""
    a.r.start_recording()
    a.e.talk()
    a.r.stop_recording()
    assert a.e.row()["state"] == "done" and a.e.row()["by"] == "none"


# ---- LIFE-18: audio speech-to-text hadn't reached at a crash ------------------------------------------------------------

def _crashed_recording(svc, tmp_path):
    folder = tmp_path / "rec"
    (folder / ".pending").mkdir(parents=True)
    (folder / "transcript.jsonl").write_text(json.dumps({"n": 0, "t": 1.0, "end": 3.0, "src": "mic", "who": "J",
                                                          "text": "before the crash"}) + "\n", encoding="utf-8")
    (folder / ".pending" / "000000.pcm").write_bytes(np.tile(SPEECH, 50).tobytes())
    (folder / ".pending" / "000001.pcm").write_bytes(np.tile(SPEECH, 50).tobytes())
    Env(svc)  # makes the tables (and recovers nothing yet)
    svc.storage.execute("INSERT INTO recorder_recordings(id, title, started, folder, state) VALUES ('old', 'Before', ?, ?, "
                        "'recording')", (time.time() - 600, str(folder)))
    return folder


def test_after_a_crash_the_leftover_audio_is_transcribed_into_the_notes(svc, tmp_path):
    folder = _crashed_recording(svc, tmp_path)
    e = Env(svc, stt=STT("we moved the launch to May", "and Dana owns the budget"))  # Vesper starts again
    row = e.row("old")
    md = Path(row["notes_path"]).read_text(encoding="utf-8")
    assert row["state"] == "done" and "before the crash" in md and "we moved the launch to May" in md
    assert "Dana owns the budget" in md
    assert not (folder / ".pending").exists()


def test_retention_deletes_leftover_audio_too(svc, tmp_path):
    folder = tmp_path / "rec"
    (folder / ".pending").mkdir(parents=True)
    (folder / ".pending" / "000000.pcm").write_bytes(b"\0" * 960)
    (folder / "chunk-mic-000.ogg").write_bytes(b"x")
    e = Env(svc)
    svc.storage.execute("INSERT INTO recorder_recordings(id, title, started, folder, state) VALUES ('old', 'Before', ?, ?, "
                        "'done')", (time.time() - 40 * 86400, str(folder)))
    e.r.clock = lambda: time.time()
    assert e.r.retention() == 2
    assert not (folder / ".pending").exists() and not list(folder.glob("*.ogg"))


# ---- LIFE-14: a damaged file's error lines don't hang the decode ---------------------------------------------------------

def test_a_chatty_ffmpeg_doesnt_hang_the_decode(tmp_path):
    """ffmpeg with -v error writes a line per bad packet; with nobody reading stderr the pipe fills and both block."""

    class Proc:
        def __init__(self, args, **kw):
            er, ew = os.pipe()
            outr, outw = os.pipe()
            self.stdout, self.stderr = os.fdopen(outr, "rb"), os.fdopen(er, "rb")

            def ffmpeg():
                with os.fdopen(ew, "wb") as err, os.fdopen(outw, "wb") as out:
                    for _ in range(3000):  # ~300 KB: more than any pipe holds
                        err.write(b"[mp3float @ 0x55] Header missing / Error while decoding stream #0:0\n")
                    err.flush()
                    out.write(bytes(96000))
            self.t = threading.Thread(target=ffmpeg, daemon=True)
            self.t.start()

        def wait(self):
            self.t.join()
            return 0
    f = tmp_path / "call.mp3"
    f.write_bytes(b"\xff\xfb")
    got = {}
    t = threading.Thread(target=lambda: got.update(n=len(list(recording.decode_file(f, "ffmpeg", runner=Proc)))),
                         daemon=True)
    t.start()
    t.join(15)
    assert not t.is_alive(), "the decode hung on ffmpeg's stderr"
    assert got["n"] == 100
