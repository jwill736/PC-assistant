"""Spoken confirmations: asked once, ten seconds to answer without the wake word (your voice only), then the HUD
card and the toast wait two minutes in silence. A fake listener, speaker and clock drive the state machine."""

import threading
from types import SimpleNamespace

import pytest

from assistant.bus import EventBus
from assistant.voice.confirm_window import ConfirmWindow, answer, question


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class Listener:
    def __init__(self):
        self.armed, self.state, self.muted = [], "listening", False

    def arm(self, seconds, source="hotkey"):
        self.armed.append((seconds, source))


class Speaker:
    def __init__(self):
        self.speaking = threading.Event()
        self.said = []

    def say(self, text, **kw):
        self.said.append(text)


class Assistant:
    """Just what the window reads: the waiting action, and cancel."""

    def __init__(self, clock):
        self.clock, self.pending, self.cancelled = clock, None, []

    def set(self, text="post to Discord: “giveaway at 9”", tier=2):
        self.pending = {"id": "p1", "text": text, "tier": tier, "expires": self.clock() + 120}
        return {"id": "p1", "text": text, "tier": tier}

    def pending_view(self):
        p = self.pending
        return {"id": p["id"], "text": p["text"], "tier": p["tier"]} if p and p["expires"] > self.clock() else None

    def cancel(self, pid=None):
        self.cancelled.append(pid)
        self.pending = None


@pytest.fixture
def env():
    clock, bus = Clock(), EventBus()
    lst, sp = Listener(), Speaker()
    a = Assistant(clock)
    toasts = []
    w = ConfirmWindow(bus, sp, lambda: lst, a, toast=toasts.append, clock=clock, threads=False)
    events = []
    bus.on(lambda e: events.append(e) if e["type"] == "confirm_window" else None)
    return SimpleNamespace(w=w, clock=clock, bus=bus, lst=lst, sp=sp, a=a, toasts=toasts, events=events)


def ask(env):
    """Vesper asks the question out loud, then stops talking."""
    env.w.on_pending(env.a.set())
    env.sp.speaking.set()
    assert env.w.tick() == "asking"
    env.clock.t += 2.5
    env.sp.speaking.clear()
    return env.w.tick()


def test_the_question_is_asked_once_in_plain_words():
    assert question("post to Discord: “giveaway at 9”") == "Post to Discord: “giveaway at 9”? Say yes or no."
    assert question("") == "Should I go ahead? Say yes or no."


@pytest.mark.parametrize("said,want", [
    ("Yes.", "yes"), ("Yeah, go ahead.", "yes"), ("yes please", "yes"), ("Sure!", "yes"), ("uh, okay", "yes"),
    ("Do it.", "yes"), ("No.", "no"), ("Nope", "no"), ("no thanks", "no"), ("Cancel that.", "no"), ("Don't.", "no"),
    ("what time is it", None), ("yes and also open discord", None), ("I said no to him yesterday", None), ("", None),
])
def test_yes_and_no_are_recognised_and_nothing_else(said, want):
    assert answer(said) == want


def test_window_opens_after_the_question_is_spoken_and_a_yes_goes_through(env):
    assert ask(env) == "listening"
    assert env.lst.armed == [(10.0, "confirm")]  # no wake word needed, the speaker check still applies
    assert env.events[-1]["data"]["state"] == "listening"
    env.bus.publish("heard", {"text": "Yeah, do it.", "wake": False, "armed": True})
    assert env.w.tap("Yeah, do it.", "match") == "yes"  # handed on as a plain "yes"
    assert env.toasts == []  # answered in time: no toast
    env.a.pending = None  # the assistant ran it
    assert env.w.tick() == "done"


def test_a_no_cancels_through_the_assistant(env):
    ask(env)
    env.bus.publish("heard", {"text": "no", "wake": False, "armed": True})
    assert env.w.tap("No thanks.", "unknown") == "no"


def test_someone_elses_voice_and_other_chatter_are_ignored_in_the_window(env):
    ask(env)
    env.bus.publish("heard", {"text": "yes", "wake": False, "armed": True})
    assert env.w.tap("yes", "mismatch") is True  # dropped: not the owner
    assert env.w.tap("what's for dinner", "match") is True  # dropped: not a yes or no
    assert env.w.current["phase"] == "listening"
    env.bus.publish("heard", {"text": "Vesper, what time is it", "wake": True, "armed": True})
    assert env.w.tap("what time is it", "match") is None  # said with the name: a normal command


def test_no_answer_means_silence_then_the_card_and_toast_wait_two_minutes(env):
    ask(env)
    env.clock.t += 9.9
    assert env.w.tick() == "listening"
    env.clock.t += 0.2
    assert env.w.tick() == "waiting"
    assert env.toasts and env.toasts[0]["id"] == "p1" and env.sp.said == []  # Vesper says nothing more
    assert env.w.tap("yes", "match") is None  # the window closed: "Vesper, yes" still works the normal way
    env.clock.t += 60
    assert env.w.tick() == "waiting" and env.a.pending is not None
    env.clock.t += 60
    assert env.w.tick() == "done"
    assert env.a.cancelled == ["p1"] and env.events[-1]["data"]["state"] == "expired" and env.sp.said == []


def test_answered_on_the_hud_or_toast_ends_it(env):
    ask(env)
    env.a.pending = None
    assert env.w.tick() == "done" and env.toasts == []


def test_typed_commands_get_no_voice_window_just_the_card_and_toast(env):
    env.w.on_pending(env.a.set())
    env.clock.t += 3
    assert env.w.tick() == "asking"
    env.clock.t += 6  # nothing was said in 8 s
    assert env.w.tick() == "waiting"
    assert env.lst.armed == [] and len(env.toasts) == 1


def test_the_window_waits_while_the_model_is_still_writing_the_question(env):
    env.w.on_pending(env.a.set())
    env.bus.publish("thinking", {"active": True})
    env.clock.t += 12
    assert env.w.tick() == "asking"
    env.bus.publish("thinking", {"active": False})
    env.sp.speaking.set()
    env.w.tick()
    env.sp.speaking.clear()
    assert env.w.tick() == "listening"


def test_muted_mic_skips_the_window(env):
    env.lst.muted = True
    assert ask(env) == "waiting" and env.lst.armed == []


def test_a_newer_confirmation_replaces_the_old_one(env):
    ask(env)
    env.a.pending = {"id": "p2", "text": "close steam", "tier": 2, "expires": env.clock() + 120}
    assert env.w.tick() == "done"
    assert env.w.tap("yes", "match") is None


def test_runtime_wires_the_window_in_front_of_voice_commands(cfg, monkeypatch):
    from assistant.runtime import Runtime

    rt = Runtime(cfg)
    try:
        assert rt.assistant.on_pending == rt.confirm_window.on_pending and rt.confirm_window.tap in rt.voice_taps
        ran = []
        monkeypatch.setattr(rt, "_run_voice_command", lambda text, turn, owner="unknown": ran.append(text))
        rt._commands.submit = lambda fn, *a: fn(*a)
        rt.voice_taps.insert(0, lambda text, owner: "yes" if text == "yeah sure" else None)
        rt.voice_command("yeah sure")
        rt.voice_taps.insert(0, lambda text, owner: True if text == "swallow" else None)
        rt.voice_command("swallow")
        rt.voice_command("open discord")
        assert ran == ["yes", "open discord"]
    finally:
        rt.stop()


def test_pending_cards_last_two_minutes(svc):
    import time

    from assistant.brain.assistant import Assistant

    a = Assistant(svc)
    out = a.handle("close steam")
    assert out["reply"] == "Close steam? Say yes or no."
    assert 115 < a.pending["expires"] - time.time() <= 120
