"""Dictation: hold the hotkey and talk, or "Vesper, dictate"; the text is cleaned up and typed into the app in front,
never into a password field. Mic, speech-to-text, keyboard, clipboard and windows are all fakes."""

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import dictation as dmod
from assistant.voice import dictate


# ---- the clean-up -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("said,typed", [
    ("um so I think comma we should go new line thanks", "So I think, we should go\nThanks."),
    ("Um, hello, comma, how are you question mark", "Hello, how are you?"),
    ("the trial period ends Friday period", "The trial period ends Friday."),
    ("i think i i will be there at 10:30 and it costs 3,000", "I think I will be there at 10:30 and it costs 3,000."),
    ("send it to Bob period then call Alice exclamation point", "Send it to Bob. Then call Alice!"),
    ("We met, uh, yesterday. um it was fine", "We met yesterday. It was fine."),
    ("dear Sam new paragraph thanks for the notes", "Dear Sam\n\nThanks for the notes."),
    ("lunch colon noon semicolon dinner colon eight", "Lunch: noon; dinner: eight."),
    ("ok", "Ok"),
    ("", ""),
])
def test_spoken_punctuation_fillers_and_capitals(said, typed):
    assert dictate.clean(said) == typed


def test_no_full_stop_when_turned_off():
    assert dictate.clean("see you at eight", end_period=False) == "See you at eight"


def test_polish_keeps_the_plain_text_when_the_model_does_more_than_tidy():
    text = "So I think we should we should move the meeting to Thursday because Sam is out on Wednesday."
    tidy = "So I think we should move the meeting to Thursday, because Sam is out on Wednesday."
    assert dictate.polish(text, lambda p, **kw: tidy) == tidy
    assert dictate.polish(text, lambda p, **kw: "Sure! Here's the cleaned text: " + tidy) is None
    assert dictate.polish(text, lambda p, **kw: "Moving it is a good idea. " * 6) is None  # it answered instead
    assert dictate.polish(text, lambda p, **kw: None) is None  # no model
    assert dictate.polish("line one\nline two is here and it goes on for a while longer than twelve words",
                          lambda p, **kw: "Line one. Line two is here and it goes on for a while longer than twelve words.") is None


def test_polish_gives_up_on_a_slow_model():
    gate = threading.Event()
    assert dictate.polish("a b c d e f g h i j k l m n", lambda p, **kw: gate.wait(1) or "x", timeout=0.05) is None
    gate.set()


def test_long_recordings_are_cut_at_quiet_moments():
    sr = dictate.SAMPLE_RATE
    loud = np.ones(sr * 20, np.float32) * 0.5
    audio = np.concatenate([loud, np.zeros(sr // 2, np.float32), loud, np.zeros(sr // 2, np.float32), loud])
    parts = dictate.chunks(audio, max_s=25)
    assert len(parts) == 3 and sum(len(p) for p in parts) == len(audio)
    assert all(len(p) <= 25 * sr for p in parts)
    assert dictate.chunks(np.ones(sr, np.float32)) and len(dictate.chunks(np.ones(sr, np.float32))) == 1


def test_capture_borrows_the_running_listener_and_gives_it_back():
    lst = SimpleNamespace(state="listening", paused=False, tap=None)
    cap = dictate.Capture(lst)
    assert cap.start() == "listener" and lst.paused and lst.tap is not None
    for _ in range(10):
        lst.tap(np.full(480, 16384, np.int16))
    audio = cap.stop()
    assert len(audio) == 4800 and abs(audio[0] - 0.5) < 1e-6 and lst.tap is None and lst.paused is False


def test_capture_opens_its_own_stream_without_a_listener():
    made = []

    class Stream:
        def __init__(self, push):
            self.push, self.on = push, False
            made.append(self)

        def start(self):
            self.on = True
            self.push(np.zeros(480, np.int16))

        def stop(self):
            self.on = False

        def close(self):
            pass
    cap = dictate.Capture(SimpleNamespace(state="unavailable", paused=False, tap=None), stream_factory=Stream)
    assert cap.start() == "own"
    assert len(cap.stop()) == 480 and not made[0].on


# ---- the skill ----------------------------------------------------------------------------------------------------

class Keys:
    def __init__(self):
        self.typed, self.chords, self.ok, self.held = [], [], True, False

    def type_text(self, text):
        self.typed.append(text)
        return self.ok

    def chord(self, *names):
        self.chords.append(names)
        return True

    def modifiers_down(self):
        return self.held


class Board:
    def __init__(self):
        self.text = "old clipboard"

    def sequence(self):
        return None

    def read(self):
        return self.text

    def write(self, text):
        self.text = text
        return True


class Cap:
    def __init__(self, seconds=2.0):
        self.seconds, self.started, self.stopped = seconds, 0, 0

    def start(self):
        self.started += 1
        return "listener"

    def stop(self):
        self.stopped += 1
        return np.zeros(int(self.seconds * dictate.SAMPLE_RATE), np.float32)


class Speaker:
    def __init__(self):
        self.speaking, self.interrupted, self.said = threading.Event(), 0, []

    def interrupt(self):
        self.interrupted += 1
        self.speaking.clear()

    def say(self, text, **kw):
        self.said.append(text)


class Listener:
    def __init__(self):
        self.armed = []

    def arm(self, seconds, source="hotkey"):
        self.armed.append((seconds, source))


@pytest.fixture
def env(svc):
    front = {"win": SimpleNamespace(app="slack", title="#general - Slack", pid=7, handle=70)}
    keys, board, cap = Keys(), Board(), Cap()
    heard = {"text": "um so the build is green comma ship it"}
    pw = {"v": False}
    d = dmod.Dictation(svc, keys=keys, board=board, window=lambda: front["win"], password=lambda: pw["v"],
                       capture=lambda: cap, transcribe=lambda samples: heard["text"], threads=False)
    d.speaker, d.listener = Speaker(), Listener()
    return SimpleNamespace(d=d, front=front, keys=keys, board=board, cap=cap, heard=heard, pw=pw, svc=svc)


def test_hold_talk_release_types_clean_text(env):
    env.d.key_down()
    assert env.d.state == "recording" and env.cap.started == 1
    assert env.svc.bus.latest["dictation"]["data"]["state"] == "recording"  # the HUD chip
    env.d.key_up()
    assert env.keys.typed == ["So the build is green, ship it."]
    st = env.d.status()
    assert st["state"] == "idle" and st["recent"][0]["text"] == "So the build is green, ship it." and st["recent"][0]["how"] == "typed"
    assert "_win" not in st["recent"][0]


def test_pressing_dictate_silences_vesper(env):
    env.d.speaker.speaking.set()
    env.d.key_down()
    assert env.d.speaker.interrupted == 1


def test_never_in_a_password_field_or_manager(env):
    env.pw["v"] = True
    env.d.key_down()
    assert env.d.state == "idle" and env.cap.started == 0 and "password" in env.d.note
    env.pw["v"] = False
    env.front["win"] = SimpleNamespace(app="1password", title="1Password", pid=1, handle=1)
    env.d.key_down()
    assert env.cap.started == 0 and env.keys.typed == []


def test_focus_moving_to_a_password_field_while_talking_keeps_the_text_out(env):
    env.d.key_down()
    env.pw["v"] = True
    env.d.key_up()
    assert env.keys.typed == [] and env.d.recent[0]["how"] == "kept" and "password" in env.d.note


def test_switching_windows_while_talking_keeps_it_on_the_hud(env):
    env.d.key_down()
    env.front["win"] = SimpleNamespace(app="discord", title="Discord", pid=9, handle=90)
    env.d.key_up()
    assert env.keys.typed == [] and env.d.recent[0]["how"] == "kept"
    assert env.d.actions()["copy"]({"id": env.d.recent[0]["id"]})["ok"]
    assert env.board.text == "So the build is green, ship it."


def test_too_short_or_silent_types_nothing(env):
    env.cap.seconds = 0.1
    env.d.key_down()
    env.d.key_up()
    assert env.keys.typed == [] and "too short" in env.d.note
    env.cap.seconds = 2
    env.heard["text"] = "  "
    env.d.key_down()
    env.d.key_up()
    assert env.keys.typed == [] and "didn't catch" in env.d.note and env.d.state == "idle"


def test_long_dictation_is_pasted_and_the_clipboard_put_back(env):
    env.heard["text"] = "word " * 120
    env.d.svc.cfg.setdefault("skills", {})
    env.d.key_down()
    env.d.key_up()
    assert env.keys.typed == [] and ("ctrl", "v") in env.keys.chords
    assert env.d.recent[0]["how"] == "pasted" and env.board.text == "old clipboard"


def test_the_model_polishes_longer_dictations(env):
    env.heard["text"] = "so um I think we should we should move the sync to Thursday because Sam is out Wednesday"
    tidy = "So I think we should move the sync to Thursday, because Sam is out Wednesday."
    env.svc.think = lambda prompt, **kw: tidy
    env.d.key_down()
    env.d.key_up()
    assert env.keys.typed == [tidy] and env.d.recent[0]["polished"] is True


def test_held_modifiers_block_typing_rather_than_firing_shortcuts(env, monkeypatch):
    monkeypatch.setattr(dmod.textio, "wait_modifiers_released", lambda keys, **kw: not keys.held)
    env.keys.held = True
    env.d.key_down()
    env.d.key_up()
    assert env.keys.typed == [] and "Ctrl" in env.d.note


def test_back_to_back_dictations_into_the_same_field_get_a_space(env):
    for said in ("first part", "second part"):
        env.heard["text"] = said
        env.d.key_down()
        env.d.key_up()
    assert env.keys.typed == ["First part", " Second part"]


def test_say_dictate_then_the_next_utterance_is_typed(env):
    out = env.d.dictate_next()
    assert out["waiting"] and env.d.listener.armed == [(dmod.AWAIT_S, "dictate")]
    assert env.d.say("dictate", {}, out) == "Go ahead."
    assert env.d.tap("Meet me at the Blue Door at eight", "match") is True
    assert env.keys.typed == ["Meet me at the Blue Door at eight."]
    assert env.d.tap("open discord", "match") is None  # only the one utterance


def test_inline_dictate_keeps_capitals_and_other_voices_are_ignored(env):
    assert env.d.tap("Dictate: Thanks, Priya. See you Friday", "match") is True
    assert env.keys.typed == ["Thanks, Priya. See you Friday."]
    env.d.dictate_next()
    assert env.d.tap("type this in", "mismatch") is True and len(env.keys.typed) == 1


def test_never_mind_cancels_a_waiting_dictation(env):
    env.d.dictate_next()
    assert env.d.tap("never mind", "match") is True
    assert env.keys.typed == [] and env.d.speaker.said == ["Okay."]


def test_waiting_runs_out(env):
    t = [1000.0]
    env.d.clock = lambda: t[0]
    env.d.dictate_next()
    t[0] += dmod.AWAIT_S + 1
    assert env.d.tap("hello there", "match") is None and env.d.awaiting is None


def test_not_from_the_hud_itself(env):
    env.front["win"] = SimpleNamespace(app="chrome", title="Friday HUD - Google Chrome", pid=3, handle=3)
    out = env.d.dictate_next()
    assert out["ok"] is False and "HUD" in out["error"]


def test_routes_and_tool(svc):
    sk = Skills(svc, ["dictation"])
    ctx = RouterContext(skill_route=sk.route)
    for said in ("dictate", "start dictation", "take dictation", "type what I say", "Friday dictate", "dictation mode"):
        intent = route(said.replace("Friday ", ""), ctx)
        assert intent and intent.tool == "dictate", said
    assert route("dictate a novel about whales", ctx) is None or route("dictate a novel about whales", ctx).tool != "dictate"
    tool = sk.tools()[0]
    assert tool.name == "dictate" and tool.tier_for({}) == 1


def test_skill_starts_with_a_runtime_and_registers_its_hooks(svc, monkeypatch):
    from assistant.voice import holdkey

    monkeypatch.setattr(holdkey.HoldKey, "start", lambda self: "hold")
    d = dmod.Dictation(svc, keys=Keys(), board=Board(), window=lambda: None, threads=False)
    rt = SimpleNamespace(listener=Listener(), speaker=Speaker(), voice_taps=[])
    d.start(rt)
    assert d.hotkey_mode == "hold" and rt.voice_taps == [d.tap] and d.status()["hotkey"] == "ctrl+alt+d"
