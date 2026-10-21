"""Recorder: start/stop/pause by voice, HUD and hotkey; audio in chunks; a live transcript; notes (map-reduce, or the
transcript alone with no model); bookmarks and typed notes; the task card; search; the meeting link; file import; and the
privacy rules (live, off the record, retention, nothing in the conversation log). Mic, speech-to-text, the model, the
calendar and ffmpeg are all fakes; the listener's tap fan-out is tested with dictation alongside."""

import json
import sys
import threading
import time
import types
import wave
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import recorder as rmod
from assistant.voice import dictate, recording
from assistant.voice.listener import VoiceListener

SPEECH = (np.sin(np.arange(480) / 3) * 8000).astype(np.int16)
SILENCE = np.zeros(480, np.int16)


# ---- fakes ----------------------------------------------------------------------------------------------------------

class Mic:
    def __init__(self, push):
        self.push, self.started, self.closed = push, False, False

    def start(self):
        self.started = True

    def stop(self):
        self.closed = True

    def close(self):
        self.closed = True


class Speaker:
    def __init__(self):
        self.speaking = threading.Event()
        self.last_end = 0.0
        self.chimes = 0

    def chime(self):
        self.chimes += 1


class STT:
    """Says the next scripted line for each utterance (then "more words")."""

    def __init__(self, *lines):
        self.lines, self.calls = list(lines), 0

    def __call__(self, samples):
        self.calls += 1
        return self.lines.pop(0) if self.lines else "more words here"


class Assistant:
    def __init__(self):
        self.off = False

    def off_record(self):
        return self.off


class SysAudio:
    def __init__(self, ok=True, why="no loopback device"):
        self.ok, self.why, self.push, self.stopped = ok, "" if ok else why, None, False

    def start(self, push):
        self.push = push
        return self.ok

    def stop(self):
        self.stopped = True


class Env:
    def __init__(self, svc, stt=None, listener=None, threads=False, system=None, think=None, **kw):
        self.svc = svc
        self.mics: list[Mic] = []
        self.stt = stt or STT()
        self.speaker = Speaker()
        self.assistant = Assistant()
        self.spoken: list[str] = []
        self.opened: list[Path] = []
        self.hotkeys: list = []
        self.systems: list[SysAudio] = []
        svc.speak = lambda text, **k: self.spoken.append(text)
        if think is not None:
            svc.think = think

        def mic(push):
            self.mics.append(Mic(push))
            return self.mics[-1]

        def sysaudio():
            self.systems.append(system() if system else SysAudio())
            return self.systems[-1]
        self.r = rmod.Recorder(svc, mic=mic, stt=self.stt, threads=threads, ffmpeg="", opener=self.opened.append,
                               register=lambda combo, cb: self.hotkeys.append((combo, cb)) or True, system=sysaudio, **kw)
        self.rt = SimpleNamespace(listener=listener, speaker=self.speaker, voice_taps=[], assistant=self.assistant)
        self.r.start(self.rt)

    def push(self, frames, source="mic"):
        target = self.mics[-1].push if source == "mic" else self.systems[-1].push
        for f in frames:
            target(f)

    def talk(self, seconds=1.2, quiet=1.0, source="mic"):
        self.push([SPEECH] * int(seconds / 0.03), source)
        self.push([SILENCE] * int(quiet / 0.03), source)

    def row(self, rid=None):
        rows = self.svc.storage.query("SELECT * FROM recorder_recordings ORDER BY started DESC")
        return next(r for r in rows if rid is None or r["id"] == rid)


@pytest.fixture
def env(svc):
    return Env(svc)


def notes_think(notes=None, answer="The budget stayed at $40k (Tuesday's Acme review, about 2 minutes in)."):
    calls = []
    base = notes or {"title": "Budget sync", "summary": "We agreed the budget is $40k.", "key_points": ["Budget is $40k"],
                     "decisions": ["Keep the budget at $40k"], "open_questions": ["Is phase two funded?"], "people": ["Dana"],
                     "action_items": [{"task": "Send Dana the plan", "owner": "me", "due": "by Friday"},
                                      {"task": "Share pricing", "owner": "Dana", "due": "2026-10-30"}]}

    def think(prompt, schema=None, **kw):
        calls.append(prompt)
        return dict(base) if schema is not None else answer
    think.calls = calls
    return think


# ---- the listener's taps: several consumers at once ------------------------------------------------------------------

def _listener(cfg=None, tmp=None):
    bus = SimpleNamespace(publish=lambda *a, **k: None, client_count=0)
    sp = SimpleNamespace(speaking=threading.Event(), last_end=0.0, last_text="", chime=lambda: None, interrupt=lambda: None)
    return VoiceListener(bus, sp, ["friday"], lambda t: None, cfg or {}, models_dir=tmp)


def test_listener_feeds_dictation_and_every_added_tap():
    lst = _listener()
    got_a, got_b, dict_frames = [], [], []
    lst.add_tap(got_a.append)
    lst.add_tap(got_a.append)  # the same consumer twice is still one tap
    lst.add_tap(got_b.append)
    lst.tap = dict_frames.append
    lst._feed_taps(SPEECH)
    assert len(got_a) == len(got_b) == len(dict_frames) == 1
    lst.remove_tap(got_a.append)
    lst._feed_taps(SPEECH)
    assert len(got_a) == 1 and len(got_b) == 2 and len(dict_frames) == 2


def test_one_failing_tap_never_stops_the_others():
    lst = _listener()
    got = []
    lst.add_tap(lambda f: 1 / 0)
    lst.add_tap(got.append)
    lst._feed_taps(SPEECH)
    assert got == [SPEECH]


def test_audio_dropped_while_a_command_is_understood_still_reaches_the_taps():
    lst = _listener()
    got = []
    lst.add_tap(got.append)
    for _ in range(5):
        lst._audio.put(SPEECH)
    lst._drain()
    assert len(got) == 5 and lst._audio.empty()


def test_dictation_and_a_recording_share_the_listener(svc):
    """Dictation borrows ``tap`` (and pauses commands); the recorder's tap keeps getting every frame meanwhile."""
    lst = _listener()
    lst.state = "listening"
    e = Env(svc, listener=lst)
    assert e.r.start_recording()["via"] == "listener" and lst.taps == [e.r._tap]
    cap = dictate.Capture(lst)
    assert cap.start() == "listener" and lst.paused
    for _ in range(10):
        lst._feed_taps(np.full(480, 16384, np.int16))
    audio = cap.stop()
    assert len(audio) == 4800 and lst.tap is None and lst.paused is False
    assert e.r.session.seconds == pytest.approx(0.3)  # the recorder heard the same 10 frames
    lst._feed_taps(SPEECH)
    assert e.r.session.seconds == pytest.approx(0.33)
    e.r.stop_recording()
    assert lst.taps == []


def test_the_listener_loop_hands_every_mic_frame_to_the_taps(monkeypatch, tmp_path):
    """The real audio loop with a fake sound card: frames reach the taps whether or not commands are paused."""
    sent = 40

    class Stream:
        def __init__(self, callback=None, **kw):
            self.cb = callback

        def __enter__(self):
            def feed():
                for i in range(sent):
                    self.cb(np.full((480, 1), i, np.int16), 480, None, None)
            threading.Thread(target=feed, daemon=True).start()
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=Stream))
    lst = _listener({"vad": "energy"}, tmp_path)
    lst.stt = SimpleNamespace(name="parakeet", transcribe=lambda s, h=(): "")
    lst.paused = True  # e.g. dictation or calibration has the mic: the taps still get everything
    got = []

    def tap(frame):
        got.append(int(frame[0]))
        if len(got) == sent:
            lst._stop.set()
    lst.add_tap(tap)
    t = threading.Thread(target=lst._run, args=(lst._gen,), daemon=True)
    t.start()
    t.join(5)
    assert got == list(range(sent))


# ---- start, stop, pause, one at a time -------------------------------------------------------------------------------

def test_start_talk_stop_makes_audio_a_transcript_and_notes_without_a_model(env):
    out = env.r.start_recording()
    assert out["ok"] and out["via"] == "own" and env.mics[0].started and env.r.status()["state"] == "recording"
    env.stt.lines = ["Hello everyone, let's talk budget.", "We agreed the budget is forty k."]
    env.talk()
    env.talk()
    st = env.r.status()
    assert [ln["text"] for ln in st["rec"]["lines"]] == ["Hello everyone, let's talk budget.", "We agreed the budget is forty k."]
    assert st["rec"]["lines"][0]["who"] == "J" and st["rec"]["elapsed"] > 4
    stop = env.r.stop_recording()
    assert stop["ok"] and env.mics[0].closed and env.r.status()["state"] == "idle"
    row = env.row()
    assert row["state"] == "done" and row["by"] == "none" and row["words"] == 12
    folder = Path(row["folder"])
    assert (folder / "mic-000.wav").exists() and len(list(recording.read_lines(folder))) == 2
    md = Path(row["notes_path"]).read_text(encoding="utf-8")
    assert "No model: transcript only." in md and "We agreed the budget is forty k." in md
    assert Path(row["notes_path"]).parent == Path(env.svc.cfg.data_dir) / "notes"
    with wave.open(str(folder / "mic-000.wav")) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)


def test_only_one_recording_at_a_time_and_stop_needs_one(env):
    assert env.r.stop_recording()["ok"] is False
    env.r.start_recording()
    again = env.r.start_recording()
    assert again["ok"] is False and again["already"] and len(env.mics) == 1


def test_pause_keeps_nothing_until_resume(env):
    env.r.start_recording()
    env.talk(0.6, 0.3)
    at = env.r.session.seconds
    assert env.r.pause()["paused"] is True and env.r.status()["state"] == "paused"
    env.talk()
    assert env.r.session.seconds == at
    assert env.r.pause(resume=True)["paused"] is False
    env.talk(0.6, 0.3)
    assert env.r.session.seconds > at


def test_hotkey_registers_and_toggles(env):
    assert env.hotkeys and env.hotkeys[0][0] == "ctrl+alt+r"
    toggle = env.hotkeys[0][1]
    toggle()
    assert env.r.session is not None and env.speaker.chimes == 1
    toggle()
    assert env.r.session is None and env.speaker.chimes == 2


def test_hud_buttons(env):
    sk = env.r
    assert sk.actions()["start"]({})["ok"]
    assert sk.actions()["mark"]({"note": "ship it"})["note"] is True
    assert sk.actions()["pause"]({})["paused"] and not sk.actions()["pause"]({"resume": True})["paused"]
    assert sk.actions()["stop"]({})["ok"]
    rid = env.row()["id"]
    view = sk.actions()["notes"]({"id": rid})
    assert view["ok"] and view["marks"][0]["note"] == "ship it" and "ship it" in view["markdown"]
    assert sk.actions()["open"]({"id": rid})["ok"] and env.opened[-1] == Path(view["path"])


# ---- chunks, compression, spill, threads ---------------------------------------------------------------------------

def test_long_recordings_are_written_in_chunks(svc):
    e = Env(svc)
    e.r.cfg["chunk_minutes"] = 0.01  # 0.6 s per file
    e.r.start_recording()
    e.talk(1.2, 1.2)
    e.r.stop_recording()
    files = sorted(Path(e.row()["folder"]).glob("mic-*.wav"))
    assert len(files) == 4
    total = 0
    for f in files:
        with wave.open(str(f)) as w:
            assert w.getnframes() <= 0.6 * 16000
            total += w.getnframes()
    assert total == e.row()["seconds"] * 16000


def test_a_finished_chunk_is_compressed_when_ffmpeg_is_there(tmp_path):
    wav = tmp_path / "mic-000.wav"
    wav.write_bytes(b"RIFF")
    ran = []

    def run(args, **kw):
        ran.append(args)
        Path(args[-1]).write_bytes(b"OggS")
        return SimpleNamespace(returncode=0)
    out = recording.compress(wav, ffmpeg="ffmpeg", run=run)
    assert out == tmp_path / "mic-000.ogg" and out.exists() and not wav.exists() and "libopus" in ran[0]
    wav.write_bytes(b"RIFF")
    assert recording.compress(wav, ffmpeg="") is None and wav.exists()  # no ffmpeg: the WAV stays
    assert recording.compress(wav, ffmpeg="ffmpeg", run=lambda a, **k: SimpleNamespace(returncode=1)) is None and wav.exists()


def test_a_slow_speech_engine_spills_waiting_audio_to_disk(tmp_path):
    s = recording.Session(tmp_path, lambda a: "hello there", threads=False, max_backlog=0)
    utt = recording.Utterance("mic", 0.0, 1.0, np.full(16000, 1000, np.int16))
    s._queue(utt)
    assert isinstance(utt.audio, Path) and utt.audio.exists()
    s._drain_utterances()
    assert not utt.audio.exists() and [ln["text"] for ln in s.lines] == ["hello there"]
    s.stop()
    assert not (tmp_path / ".pending").exists()


def test_threaded_session_transcribes_in_the_background_and_finishes_on_stop(tmp_path):
    said = iter(["one two three", "four five six"])
    s = recording.Session(tmp_path, lambda a: next(said, "seven"), threads=True)
    for _ in range(2):
        for f in [SPEECH] * 40 + [SILENCE] * 34:
            s.push(f)
    s.stop(timeout=5)
    assert [ln["text"] for ln in s.lines] == ["one two three", "four five six"] and s.words == 6
    assert [ln["text"] for ln in recording.read_lines(tmp_path)] == ["one two three", "four five six"]


def test_framer_resamples_any_rate_to_16k_frames():
    fr = recording.Framer(48000, 2)
    stereo = np.zeros((48000, 2), np.int16)
    frames = fr.feed(stereo)
    assert all(len(f) == 480 and f.dtype == np.int16 for f in frames)
    assert abs(len(frames) * 480 - 16000) <= 480


# ---- what goes in the transcript -------------------------------------------------------------------------------------

def test_commands_to_vesper_and_vesper_herself_stay_out_of_the_notes(svc):
    e = Env(svc, stt=STT("Friday, mark that.", "Real words from the meeting.", "It's 3 PM.", "Open the budget sheet."))
    e.r.start_recording()
    e.talk()  # "Friday, mark that": a command
    e.talk()
    e.speaker.speaking.set()  # Vesper answers something out loud
    e.talk()
    e.speaker.speaking.clear()
    e.speaker.last_end = 0
    svc.bus.publish("heard", {"text": "open the budget sheet", "wake": False, "armed": True})  # a follow-up, no name
    e.talk()
    lines = list(e.r.session.lines)
    assert [ln["text"] for ln in lines] == ["Real words from the meeting.", "It's 3 PM."]
    assert lines[1]["vesper"] and lines[1]["who"] == "Vesper" and e.r.session.dropped == 2
    e.r.stop_recording()
    md = Path(e.row()["notes_path"]).read_text(encoding="utf-8")
    assert "Real words" in md and "3 PM" not in md and "mark that" not in md


def test_a_password_said_out_loud_is_never_written_down(svc):
    e = Env(svc, stt=STT("The wifi password is hunter22 okay"))
    e.r.start_recording()
    e.talk()
    e.r.stop_recording()
    text = (Path(e.row()["folder"]) / "transcript.jsonl").read_text(encoding="utf-8")
    assert "hunter22" not in text and "[secret]" in text


def test_the_pcs_own_sound_is_the_other_people(svc):
    e = Env(svc, stt=STT("Hi, it's Dana.", "Hi Dana."))
    out = e.r.start_recording(system_audio=True)
    assert out["system"] and out["consent"]
    e.push([SILENCE] * 40)  # the PC is silent: Windows sends no loopback audio at all
    for f in [SPEECH] * 40 + [SILENCE] * 34:  # then Dana talks, while the mic keeps running
        e.systems[-1].push(f)
        e.mics[-1].push(SILENCE)
    e.talk()
    whos = [(ln["who"], ln["src"]) for ln in e.r.session.lines]
    assert whos == [("Others", "system"), ("J", "mic")]
    first = e.r.session.lines[0]
    assert first["t"] >= 1.0  # on the mic's clock, not the PC track's (which had no audio before Dana spoke)
    e.r.stop_recording()
    assert e.systems[0].stopped
    # the consent reminder: the first time only
    notices = [ev for ev in svc.bus.recent if ev["type"] == "notice" and ev["data"]["key"] == "recorder:consent"]
    assert len(notices) == 1 and "Let the people on the call know" in notices[0]["data"]["text"]
    assert e.r.start_recording(system_audio=True)["consent"] is False


def test_no_way_to_hear_the_pc_records_the_mic_alone(svc):
    e = Env(svc, system=lambda: SysAudio(ok=False))
    out = e.r.start_recording(system_audio=True)
    assert out["ok"] and not out["system"] and out["system_why"]
    assert "Just your mic" in e.r.say("recording_start", {}, out)
    assert e.r.status()["rec"]["system_why"]


def test_a_muted_mic_records_nothing(svc):
    lst = _listener()
    lst.state = "listening"
    e = Env(svc, listener=lst)
    e.r.start_recording()
    lst.muted = True
    for _ in range(20):
        lst._feed_taps(SPEECH)
    assert e.r.session.seconds == 0 and e.r.status()["rec"]["muted"]
    lst.muted = False
    lst._feed_taps(SPEECH)
    assert e.r.session.seconds > 0


# ---- bookmarks and typed notes -----------------------------------------------------------------------------------------

def test_mark_that_and_note_by_voice_while_recording(env):
    assert env.r.voice_tap("mark that", "match") is None  # not recording: the usual meaning
    env.r.start_recording()
    env.talk()
    assert env.r.voice_tap("mark that", "match") is True and env.speaker.chimes == 1
    assert env.r.voice_tap("Note: the budget is 40k.", "match") is True
    assert env.r.voice_tap("note that dana likes coffee", "mismatch") is None  # someone else's voice
    assert env.r.voice_tap("what time is it", "match") is None
    marks = env.r.rec["marks"]
    assert len(marks) == 2 and marks[1]["note"] == "the budget is 40k" and marks[0]["t"] > 1
    assert env.spoken == []  # a chime, never a spoken reply over the meeting
    env.r.stop_recording()
    md = Path(env.row()["notes_path"]).read_text(encoding="utf-8")
    assert "## Bookmarks" in md and "Note: the budget is 40k" in md and "](#t-" in md


def test_mark_that_on_stream_also_lets_the_twitch_marker_through(svc):
    e = Env(svc)
    svc.live = lambda: True
    e.r.start_recording(on_stream=True)
    assert e.r.voice_tap("mark that", "match") is None and len(e.r.rec["marks"]) == 1


def test_typed_notes_and_bookmarks_never_reach_the_conversation_log(svc):
    from assistant.brain.assistant import Assistant as Brain

    rec = svc.skills["recorder"]
    rec.mic_factory, rec._stt_fn, rec.threads, rec.ffmpeg = (lambda push: Mic(push)), STT(), False, ""
    rec.start_recording()
    a = Brain(svc)
    said = []
    svc.bus.on(lambda ev: ev["type"] == "user_said" and said.append(ev["data"]["text"]))
    out = a.handle("note the budget is 40k", "text")
    assert out["reply"].startswith("Noted") and rec.rec["marks"][0]["note"] == "the budget is 40k"
    log = " ".join(r["text"] for r in svc.storage.recent_log(20))
    assert "40k" not in log and "Noted" not in log and said == ["(recording note)"]
    rec.stop_recording()


# ---- privacy: live, off the record, retention ---------------------------------------------------------------------------

def test_never_starts_live_unless_asked_and_stops_when_going_live(svc):
    e = Env(svc)
    svc.live = lambda: True
    out = e.r.start_recording()
    assert out["ok"] is False and out["live"] and "record on stream" in out["error"] and not e.mics
    assert e.r.start_recording(on_stream=True)["ok"]
    e.r.stop_recording()
    svc.live = lambda: False
    e.r.start_recording()
    e.talk(0.3, 0.1)
    svc.live = lambda: True
    svc.bus.publish("obs", {"streaming": {"active": True}}, sticky=True)
    assert e.r.session is None and e.row()["state"] == "done"
    assert any("You're live, so I stopped recording" in s for s in e.spoken)


def test_status_hides_what_was_said_while_live(svc):
    e = Env(svc, stt=STT("secret plans"))
    svc.live = lambda: True
    e.r.start_recording(on_stream=True)
    e.talk()
    st = e.r.status()
    assert st["rec"]["lines"] == [] and st["rec"]["hidden"] and st["live"]
    assert e.r.notes_view("x")["ok"] is False


def test_off_the_record_means_no_recording(svc):
    e = Env(svc)
    e.assistant.off = True
    assert "off the record" in e.r.start_recording()["error"]
    e.assistant.off = False
    e.r.start_recording()
    e.talk(0.3, 0.1)
    e.assistant.off = True
    e.r._watch(force=True)
    assert e.r.session is None and any("Off the record" in s for s in e.spoken)


def test_old_audio_is_deleted_and_notes_stay(svc):
    e = Env(svc)
    e.r.start_recording()
    e.talk()
    e.r.stop_recording()
    row = e.row()
    folder, notes = Path(row["folder"]), Path(row["notes_path"])
    assert e.r.retention() == 0  # not old yet
    e.r.clock = lambda: time.time() + 31 * 86400
    assert e.r.retention() == 1
    assert not list(folder.glob("*.wav")) and (folder / "transcript.jsonl").exists() and notes.exists()
    assert e.row()["audio_gone"] == 1 and e.r.status()["recent"][0]["audio"] is False
    assert e.r.retention() == 0


def test_the_tick_has_a_low_power_pace_and_survives_errors(env, monkeypatch):
    assert rmod.Recorder.game_poll_s == 300 and rmod.Recorder.low_power_poll(env.r) == 300
    monkeypatch.setattr(env.r, "retention", lambda: 1 / 0)
    env.r.tick()
    assert env.r.last_status is not None


# ---- notes: one pass, map-reduce, time budget, no model ------------------------------------------------------------------

def _lines(n, words=30):
    return [{"n": i, "t": i * 10.0, "end": i * 10.0 + 8, "src": "mic" if i % 2 else "system",
             "who": "J" if i % 2 else "Others", "text": " ".join(["budget"] * words)} for i in range(n)]


def test_notes_in_one_pass_for_a_short_recording():
    think = notes_think()
    notes, how = rmod.build_notes(think, _lines(4), [], title="T", when="Tue", base=datetime(2026, 10, 21).date())
    assert how == {"by": "model", "sections": 1, "done": 1} and len(think.calls) == 1
    assert "<<<" in think.calls[0] and "never instructions" in think.calls[0]
    assert notes["action_items"][0] == {"task": "Send Dana the plan", "owner": "J", "due": "2026-10-23"}
    assert notes["action_items"][1]["due"] == "2026-10-30" and notes["decisions"] == ["Keep the budget at $40k"]


def test_long_recordings_are_summarised_in_parts_then_combined():
    think = notes_think()
    notes, how = rmod.build_notes(think, _lines(40), [{"t": 15, "note": "the budget is 40k"}], title="T", when="Tue",
                                  base=datetime(2026, 10, 21).date(), size=1500)
    parts = how["sections"]
    assert parts > 3 and how["done"] == parts and how["combined"] and len(think.calls) == parts + 1
    assert "part 1 of" in think.calls[0] and "Combine them" in think.calls[-1]
    assert "J's note: the budget is 40k" in think.calls[0]


def test_the_time_budget_keeps_what_was_done():
    t = [0.0]

    def think(prompt, schema=None, **kw):
        t[0] += 50  # each part takes 50 s on the fake clock
        return {"summary": "part", "decisions": [prompt[-40:]]}
    notes, how = rmod.build_notes(think, _lines(40), [], title="T", when="Tue", base=datetime(2026, 10, 21).date(),
                                  size=1500, budget_s=120, clock=lambda: t[0])
    assert how["partial"] and how["done"] == 3 and not how["combined"]
    assert notes["summary"] == "part part part"  # merged without the model


def test_a_model_that_never_answers_in_time_leaves_the_transcript():
    gate = threading.Event()
    notes, how = rmod.build_notes(lambda p, **k: gate.wait(2) or {}, _lines(2), [], title="T", when="Tue",
                                  base=datetime(2026, 10, 21).date(), budget_s=0.05)
    gate.set()
    assert notes is None and how["why"] == "timeout"


def test_no_model_asks_once_and_writes_the_transcript():
    calls = []
    notes, how = rmod.build_notes(lambda p, **k: calls.append(p), _lines(40), [], title="T", when="Tue",
                                  base=datetime(2026, 10, 21).date(), size=1500)
    assert notes is None and how["by"] == "none" and len(calls) == 1
    md = rmod.notes_markdown({"title": "T", "started": 1_790_000_000, "seconds": 400, "folder": "f"}, None, how,
                             _lines(3), [{"t": 12, "id": "a"}], None)
    assert "> No model: transcript only." in md and "## Transcript" in md and "[00:00:12](#t-000010)" in md


def test_the_model_writes_the_notes_and_the_task_card_waits_for_a_yes(svc):
    think = notes_think()
    e = Env(svc, think=think, stt=STT("I'll send Dana the plan by Friday.", "I'll share pricing."))
    e.r.start_recording()
    e.talk()
    e.talk()
    e.r.stop_recording()
    row = e.row()
    md = Path(row["notes_path"]).read_text(encoding="utf-8")
    assert row["by"] == "model" and "## Decisions" in md and "- [ ] Send Dana the plan — **J**" in md
    assert row["title"] == "Budget sync"  # no meeting: the model names it
    before = svc.storage.list_tasks()
    card = e.r.status()["tasks_card"]
    assert card["items"][1]["owner"] == "Dana" and svc.storage.list_tasks() == before  # nothing added on its own
    added = e.r.add_tasks(card["id"], pick=[0])
    assert added["added"] == 1 and added["tasks"] == ["Send Dana the plan"]
    task = next(t for t in svc.storage.list_tasks() if t["title"] == "Send Dana the plan")
    assert task["due"] is not None
    assert e.r.status()["tasks_card"] is None and e.r.add_tasks()["ok"] is False
    assert e.r.say("recording_add_tasks", {}, added) == "Added 1 task."


def test_add_them_by_voice_and_dismiss(svc):
    e = Env(svc, think=notes_think())
    e.r.start_recording()
    e.talk()
    e.r.stop_recording()
    ctx = RouterContext(skill_route=e.r.route)
    for said in ("add the recording tasks", "add them", "yes add those tasks", "add the action items"):
        assert route(said, ctx).tool == "recording_add_tasks", said
    out = e.r.add_tasks()
    assert out["added"] == 2 and "Share pricing (Dana)" in out["tasks"]
    assert route("add them", ctx) is None  # nothing waiting any more
    e.r.start_recording()
    e.talk()
    e.r.stop_recording()
    assert e.r.dismiss_tasks()["ok"] and e.r.status()["tasks_card"] is None


def test_notes_go_to_the_notes_folder_j_chose(svc, tmp_path):
    e = Env(svc)
    drive = tmp_path / "My Drive" / "Vesper Notes"
    drive.parent.mkdir()
    assert e.r.actions()["set"]({"notes_dir": str(tmp_path / "nowhere" / "x" / "y")})["ok"] is False
    assert e.r.actions()["set"]({"notes_dir": str(drive)})["ok"]
    e.r.start_recording()
    e.talk()
    e.r.stop_recording()
    assert Path(e.row()["notes_path"]).parent == drive


def test_notes_survive_a_missing_notes_folder(svc, tmp_path):
    e = Env(svc)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    e.r.cfg["notes_dir"] = str(blocker / "sub")
    e.r.start_recording()
    e.talk()
    e.r.stop_recording()
    assert Path(e.row()["notes_path"]).name == "notes.md"  # next to the transcript instead


# ---- the calendar and the debrief -------------------------------------------------------------------------------------

def _meeting_now(svc, title="Client review — Acme"):
    now = datetime.now(svc.tz)
    ev = {"title": title, "start": (now - timedelta(minutes=10)).isoformat(), "end": (now + timedelta(minutes=20)).isoformat(),
          "all_day": False, "profile": "work",
          "attendees": [{"email": "dana@client.com", "name": "Dana Ortiz"}, {"email": "me@x.com", "self": True}]}
    svc.calendars.events = lambda start, end, profile=None: [ev]
    return ev


def test_a_meeting_now_names_the_notes_turns_on_call_audio_and_the_debrief_uses_them(svc):
    ev = _meeting_now(svc)
    e = Env(svc, think=notes_think())
    out = e.r.start_recording(said="start recording")
    assert out["meeting"] == "Client review — Acme" and out["system"]  # "meetings" mode: call audio on
    e.talk()
    e.r.stop_recording()
    row = e.row()
    assert row["title"] == "Client review — Acme"
    md = Path(row["notes_path"]).read_text(encoding="utf-8")
    assert "with Dana Ortiz" in md and "Calendar: Client review — Acme" in md
    deb = svc.skills["debrief"]
    log = deb._row(f"{ev['title']}|{ev['start']}")
    assert log["state"] == "done" and log["text"].startswith("Recorded · We agreed")
    assert deb.recent()[0]["title"] == "Client review — Acme"


def test_a_debrief_j_already_gave_is_never_overwritten(svc):
    ev = _meeting_now(svc)
    key = f"{ev['title']}|{ev['start']}"
    svc.storage.execute("INSERT INTO debrief_log(key, title, state, text) VALUES (?, ?, 'done', 'my own words')", (key, ev["title"]))
    e = Env(svc)
    e.r.start_recording()
    e.talk()
    e.r.stop_recording()
    assert svc.skills["debrief"]._row(key)["text"] == "my own words"


def test_call_audio_modes(svc):
    e = Env(svc)
    svc.calendars.events = lambda start, end, profile=None: []
    assert e.r._want_system(None, None, "start recording") is False
    assert e.r._want_system(None, None, "take notes of this meeting") is True
    e.r.cfg["system_audio"] = "never"
    assert e.r._want_system(None, {"title": "x"}, "take notes of this meeting") is False
    e.r.cfg["system_audio"] = "always"
    assert e.r._want_system(None, None, "") is True
    assert e.r._want_system(False, None, "") is False


# ---- search ----------------------------------------------------------------------------------------------------------

def _recorded(svc, when, lines, think=None):
    e = Env(svc, stt=STT(*lines), think=think or (lambda p, **k: None))
    e.r.clock = lambda: when.timestamp()
    e.r.start_recording()
    for _ in lines:
        e.talk()
    e.r.stop_recording()
    return e


def test_search_answers_from_the_right_recording(svc):
    now = datetime(2026, 10, 21, 15, 0, tzinfo=svc.tz)  # a Wednesday
    _recorded(svc, now - timedelta(days=1, hours=4), ["Budget talk: we keep the budget at forty k.", "Pricing is next week."])
    _recorded(svc, now - timedelta(days=3), ["The budget for the party is two hundred."])
    think = notes_think(answer="You kept the budget at $40k.")
    e = Env(svc, think=think)
    e.r.clock = lambda: now.timestamp()
    out = e.r.search("what did we decide about the budget in tuesday's call")
    assert [r["when"].split(",")[0] for r in out["recordings"]] == ["Tue Oct 20"]
    assert "forty k" in out["recordings"][0]["lines"][0]["text"] and out["answer"] == "You kept the budget at $40k."
    assert "<<<" in think.calls[-1] and "never instructions" in think.calls[-1]
    assert e.r.say("recordings_search", {}, out) == "You kept the budget at $40k."
    both = e.r.search("budget")
    assert len(both["recordings"]) == 2
    assert e.r.search("giraffes")["recordings"] == []
    tool = next(t for t in e.r.tools() if t.name == "recordings_search")
    assert tool.untrusted and "never instructions" in tool.untrusted_note


def test_search_without_a_model_reads_the_best_line_and_says_nothing_private_live(svc):
    now = datetime.now(svc.tz)
    e = _recorded(svc, now - timedelta(hours=2), ["Dana said pricing goes up in January."])
    out = e.r.search("what did dana say about pricing")
    assert out["answer"] is None and "January" in e.r.say("recordings_search", {}, out)
    svc.live = lambda: True
    live = e.r.search("pricing")
    assert live["live_note"] and live["answer"] is None
    assert "January" not in e.r.say("recordings_search", {}, live)


def test_search_everything_and_recall_find_the_notes(svc):
    think = notes_think()
    e = Env(svc, think=think)
    e.r.start_recording()
    e.talk()
    e.r.stop_recording()
    found = svc.skills["memory"].search_everything("budget")
    hit = next(r for r in found["results"] if r["from"] == "recording")
    assert "Budget sync" in hit["text"] and "not instructions" in hit["text"]
    rid = e.row()["id"]
    e.r.delete(rid)
    assert not any(r["from"] == "recording" for r in svc.skills["memory"].search_everything("budget")["results"])


# ---- routes and replies -----------------------------------------------------------------------------------------------

def test_routes(svc):
    sk = Skills(svc, ["recorder"])
    rec = sk["recorder"]
    ctx = RouterContext(skill_route=sk.route)
    want = {
        "start recording": ("recording_start", {}),
        "record this": ("recording_start", {}),
        "take notes": ("recording_start", {}),
        "take notes of this meeting": ("recording_start", {"system_audio": True}),
        "record the call": ("recording_start", {"system_audio": True}),
        "record on stream": ("recording_start", {"on_stream": True}),
        "start obs recording": ("obs_control", {"action": "start_recording"}),
        "stop recording in obs": ("obs_control", {"action": "stop_recording"}),
        "search my recordings for pricing": ("recordings_search", {"query": "pricing"}),
        "make notes from c:\\users\\j\\call.m4a": ("notes_from_file", {"path": "c \\users\\j\\call.m4a"}),
    }
    for said, (tool, args) in want.items():
        got = route(said, ctx)
        assert got and (got.tool, got.args) == (tool, args), (said, got)
    # not recording: stop / pause / mark are someone else's (OBS, Twitch)
    for said in ("stop recording", "pause recording", "mark that"):
        assert route(said, ctx) is None or route(said, ctx).tool not in ("recording_stop", "recording_pause", "recording_mark")
    rec.mic_factory, rec._stt_fn, rec.threads, rec.ffmpeg = (lambda push: Mic(push)), STT(), False, ""
    rec.start_recording()
    for said, tool, args in [("stop recording", "recording_stop", {}), ("stop taking notes", "recording_stop", {}),
                             ("pause recording", "recording_pause", {"resume": False}),
                             ("resume recording", "recording_pause", {"resume": True}),
                             ("mark that", "recording_mark", {}), ("bookmark", "recording_mark", {}),
                             ("note the budget is 40k", "recording_mark", {"note": "the budget is 40k"})]:
        got = route(said, ctx)
        assert (got.tool, got.args) == (tool, args), said
    rec.stop_recording()
    svc.live = lambda: True
    assert route("start recording", ctx).tool == "obs_control"  # live with OBS: OBS's recording
    assert route("record on stream", ctx).args == {"on_stream": True}


def test_asking_about_recordings_only_routes_when_there_are_some(svc):
    sk = Skills(svc, ["recorder"])
    ctx = RouterContext(skill_route=sk.route)
    assert route("what did we decide about the budget in tuesday's call", ctx) is None or \
        route("what did we decide about the budget in tuesday's call", ctx).tool != "recordings_search"
    _recorded(svc, datetime.now(svc.tz), ["hello"])
    got = route("what did we decide about the budget in tuesday's call", ctx)
    assert got.tool == "recordings_search" and got.args["when"] == "tuesday's call"
    assert route("what did dana say about pricing", ctx).tool == "recordings_search"
    assert route("what did i say about pizza", ctx).tool == "recall"  # J's own notes: the core's
    assert route("what did you say", ctx) is None or route("what did you say", ctx).tool != "recordings_search"


def test_spoken_replies(env):
    r = env.r
    assert r.say("recording_start", {}, {"ok": True, "meeting": "Acme", "system": True, "consent": True}) == \
        "Recording Acme. The call audio too. Let people on the call know."
    assert r.say("recording_stop", {}, {"ok": True, "seconds": 600}) == "Stopped after 10 minutes. Notes will be on the HUD."
    assert r.say("recording_stop", {}, {"ok": True, "seconds": 20}).startswith("Stopped after under a minute")
    assert r.say("recording_mark", {}, {"ok": True, "at": "00:01:02", "note": False}) == "Marked at 00:01:02."
    assert r.say("recording_pause", {}, {"ok": True, "paused": True}) == "Paused."
    assert r.say("recording_start", {}, {"ok": False, "error": "nope"}) == "nope"
    tools = {t.name: t for t in r.tools()}
    assert set(tools) >= {"recording_start", "recording_stop", "recording_mark", "recordings_search", "notes_from_file"}
    assert tools["recording_start"].tier_for({}) == 1 and tools["recordings_search"].tier_for({}) == 0


# ---- notes from a file J has --------------------------------------------------------------------------------------------

def _wav(path, seconds_on=(1.2, 1.2), rate=44100, channels=2):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        for on in seconds_on:
            n = int(on * rate)
            tone = (np.sin(np.arange(n) / 3 * 16000 / rate) * 8000).astype(np.int16)
            silent = np.zeros(int(1.0 * rate), np.int16)
            for chunk in (tone, silent):
                w.writeframes(np.repeat(chunk, channels).tobytes())


def test_notes_from_a_wav_file_without_ffmpeg(svc, tmp_path):
    e = Env(svc, stt=STT("First thing on the call.", "Second thing."))
    path = tmp_path / "call.wav"
    _wav(path)
    out = e.r.notes_from_file(str(path))
    assert out["ok"] and out["file"] == "call.wav"
    row = e.row(out["id"])
    assert row["state"] == "done" and row["title"] == "call" and row["audio_gone"] == 1
    assert [ln["text"] for ln in recording.read_lines(Path(row["folder"]))] == ["First thing on the call.", "Second thing."]
    assert not list(Path(row["folder"]).glob("*.wav"))  # J's own file is the audio: no second copy


def test_notes_from_a_file_needs_a_file_and_ffmpeg_for_most_kinds(env, tmp_path):
    assert "no file" in env.r.notes_from_file(str(tmp_path / "missing.m4a"))["error"].lower()
    doc = tmp_path / "notes.txt"
    doc.write_text("x")
    assert "isn't an audio or video file" in env.r.notes_from_file(str(doc))["error"]
    m4a = tmp_path / "call.m4a"
    m4a.write_bytes(b"\x00" * 100)
    err = env.r.notes_from_file(str(m4a))["error"]
    assert "ffmpeg" in err and "winget install" in err
    assert "full path" in env.r.notes_from_file("")["error"]


def test_notes_from_a_file_through_ffmpeg(svc, tmp_path):
    e = Env(svc, stt=STT("Decoded by ffmpeg."))
    e.r.ffmpeg = "ffmpeg"
    pcm = np.concatenate([np.tile(SPEECH, 40), np.zeros(480 * 40, np.int16)]).tobytes()

    class Proc:
        def __init__(self, args, **kw):
            import io

            self.args, self.stdout, self.stderr = args, io.BytesIO(pcm), io.BytesIO(b"")

        def wait(self):
            return 0
    m4a = tmp_path / "call.m4a"
    m4a.write_bytes(b"\x00")
    frames = list(recording.decode_file(m4a, "ffmpeg", runner=Proc))
    assert len(frames) == 80 and all(len(f) == 480 for f in frames)

    class Broken(Proc):
        def __init__(self, args, **kw):
            import io

            super().__init__(args)
            self.stdout, self.stderr = io.BytesIO(b""), io.BytesIO(b"Invalid data found when processing input")

        def wait(self):
            return 1
    with pytest.raises(recording.DecodeError, match="Invalid data"):
        list(recording.decode_file(m4a, "ffmpeg", runner=Broken))


def test_the_typed_path_keeps_its_case(env):
    env.svc.bus.publish("user_said", {"text": r"make notes from C:\Users\J\Calls\Acme Review.m4a"})
    assert env.r._raw_path(r"c \users\j\calls\acme review.m4a") == r"C:\Users\J\Calls\Acme Review.m4a"
    assert env.r._raw_path(r"D:\x\y.mp3") == r"D:\x\y.mp3"


# ---- crashes, deletes, the HUD's list ------------------------------------------------------------------------------------

def test_a_recording_cut_short_by_a_crash_still_gets_notes(svc, tmp_path):
    folder = tmp_path / "rec"
    folder.mkdir()
    (folder / "transcript.jsonl").write_text(json.dumps({"n": 0, "t": 1.0, "end": 3.0, "src": "mic", "who": "J",
                                                          "text": "before the crash"}) + "\n{broken", encoding="utf-8")
    svc.storage.execute("CREATE TABLE IF NOT EXISTS recorder_recordings (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                        "started REAL NOT NULL, ended REAL, seconds REAL NOT NULL DEFAULT 0, folder TEXT NOT NULL, "
                        "notes_path TEXT, state TEXT NOT NULL, source TEXT, meeting TEXT, system INTEGER NOT NULL DEFAULT 0, "
                        "on_stream INTEGER NOT NULL DEFAULT 0, marks TEXT NOT NULL DEFAULT '[]', summary TEXT NOT NULL "
                        "DEFAULT '', notes TEXT, tasks TEXT NOT NULL DEFAULT '[]', tasks_state TEXT NOT NULL DEFAULT '', "
                        "words INTEGER NOT NULL DEFAULT 0, by TEXT NOT NULL DEFAULT '', audio_gone INTEGER NOT NULL DEFAULT 0, "
                        "error TEXT NOT NULL DEFAULT '')")
    svc.storage.execute("INSERT INTO recorder_recordings(id, title, started, folder, state) VALUES ('old', 'Before', ?, ?, "
                        "'recording')", (time.time() - 600, str(folder)))
    e = Env(svc)
    row = e.row("old")
    assert row["state"] == "done" and "before the crash" in Path(row["notes_path"]).read_text(encoding="utf-8")
    assert "Vesper closed during it" in row["error"]


def test_delete_removes_audio_transcript_notes_and_index(env):
    env.r.start_recording()
    env.talk()
    assert env.r.delete(env.r.rec["id"])["ok"] is False  # stop it first
    env.r.stop_recording()
    row = env.row()
    assert env.r.delete(row["id"])["ok"]
    assert not Path(row["folder"]).exists() and not Path(row["notes_path"]).exists()
    assert env.svc.storage.query("SELECT * FROM recorder_recordings") == []
    assert env.r.search("more")["recordings"] == []


def test_status_lists_past_recordings_and_the_hud_search(env):
    env.stt.lines = ["the quarterly numbers look fine"]
    env.r.start_recording()
    env.talk()
    env.r.stop_recording()
    st = env.r.status()
    assert st["state"] == "idle" and st["total"] == 1 and st["recent"][0]["words"] == 5
    assert st["hotkey"] == "ctrl+alt+r" and st["notes_dir"].endswith("notes")
    hits = env.r.actions()["search"]({"q": "quarterly"})
    assert hits["recordings"][0]["lines"][0]["text"] == "the quarterly numbers look fine" and hits["answer"] is None


def test_selftest_never_fails_on_a_fresh_install(env):
    checks = env.r.selftest()
    assert {c["status"] for c in checks} <= {"pass", "skip", "warn"} and len(checks) == 3


def test_when_range_reads_day_words():
    now = datetime(2026, 10, 21, 15, 0)  # Wednesday
    tue = rmod.when_range("tuesday's call", now)
    assert datetime.fromtimestamp(tue[0]) == datetime(2026, 10, 20)
    assert datetime.fromtimestamp(rmod.when_range("yesterday", now)[0]) == datetime(2026, 10, 20)
    assert datetime.fromtimestamp(rmod.when_range("last week", now)[0]) == datetime(2026, 10, 12)
    assert rmod.when_range("the budget", now) is None
    assert rmod.keywords("what did we decide about the budget in tuesday's call") == ["budget"]
