"""Faster replies: wake-to-first-words measured per turn, fast-path replies spoken before the HUD hears about them,
the voice made ready while you talk, and the common short replies played from ready-made audio. Measured with fakes."""

import threading
import time

import numpy as np
import pytest

from assistant.voice import neural, tts
from assistant.voice.latency import Prewarm, TurnLatency
from assistant.voice.neural import AudioOut
from assistant.voice.phrases import COMMON, PhraseCache


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def ev(kind, data, ts=None):
    return {"type": kind, "data": data, "ts": ts}


# ---- the per-turn measurement --------------------------------------------------------------------------------------

def test_wake_to_first_audio_is_measured_from_when_you_stopped_talking():
    clock, turn = Clock(), [0]
    lat = TurnLatency(lambda: turn[0], clock=clock)
    lat.on_event(ev("heard", {"text": "open discord", "wake": True, "armed": False, "stt_ms": 80}, ts=100.0))
    turn[0] = 1
    lat.on_event(ev("assistant_said", {"text": "Opening Discord.", "kind": "tool", "source": "voice"}))
    lat.first_audio(1, at=100.35)
    r = lat.report()
    assert r["n"] == 1 and r["p50_ms"] == 430 and r["fast_p50_ms"] == 430 and r["stt_p50_ms"] == 80


def test_a_streamed_model_reply_is_labelled_when_its_text_arrives_after_the_first_words():
    clock, turn = Clock(), [0]
    lat = TurnLatency(lambda: turn[0], clock=clock)
    lat.on_event(ev("heard", {"text": "plan my day", "wake": True, "stt_ms": 60}, ts=100.0))
    turn[0] = 1
    lat.first_audio(1, at=101.2)  # the first sentence plays while the model still writes
    clock.t = 103
    lat.on_event(ev("assistant_said", {"text": "...", "kind": "claude", "source": "voice"}))
    assert lat.report()["model_p50_ms"] == 1260 and lat.report()["fast_p50_ms"] is None


def test_ignored_speech_typed_commands_and_old_turns_are_not_counted():
    clock, turn = Clock(), [3]
    lat = TurnLatency(lambda: turn[0], clock=clock)
    lat.on_event(ev("heard", {"text": "chatter", "wake": False, "armed": False}, ts=100.0))
    lat.first_audio(4, at=100.5)
    lat.on_event(ev("heard", {"text": "yes", "ignored": "own voice"}, ts=100.0))
    lat.first_audio(5, at=100.5)
    lat.on_event(ev("assistant_said", {"text": "typed", "kind": "tool", "source": "text"}))
    lat.on_event(ev("heard", {"text": "next", "armed": True}, ts=100.0))
    lat.first_audio(3, at=100.4)  # the end of the previous reply, not this turn's
    assert lat.report() is None
    lat.on_event(ev("heard", {"text": "clip that", "trigger": "clip_that"}, ts=100.0))
    lat.first_audio(4, at=100.2)
    assert lat.report()["n"] == 1


def test_the_report_is_the_median_of_the_last_twenty_turns():
    turn = [0]
    lat = TurnLatency(lambda: turn[0], clock=Clock())
    for i in range(25):
        lat.on_event(ev("heard", {"text": "x", "wake": True}, ts=100.0))
        turn[0] += 1
        lat.first_audio(turn[0], at=100 + (i + 1) / 100)
    r = lat.report()
    assert r["n"] == 20 and r["p50_ms"] == round(np.median([(i + 1) * 10 for i in range(5, 25)]))
    assert len(r["recent"]) == 20 and r["last_ms"] == 250


def test_prewarm_on_wake_is_rate_limited_and_skipped_while_talking():
    calls, t = [], [0.0]
    sp = type("S", (), {"speaking": threading.Event(), "prewarm": lambda self: calls.append(1)})()
    p = Prewarm(sp, gap_s=5, clock=lambda: t[0])
    p.on_event(ev("wake", {}))
    p.on_event(ev("voice_state", {"state": "hearing"}))
    t[0] = 6
    p.on_event(ev("voice_state", {"state": "listening"}))
    assert len(calls) == 1
    sp.speaking.set()
    p.on_event(ev("voice_state", {"state": "armed"}))
    sp.speaking.clear()
    p.on_event(ev("voice_state", {"state": "armed"}))
    assert len(calls) == 2


def test_phrase_cache_keeps_short_sentences_per_voice():
    c = PhraseCache(max_items=3)
    c.put(("s", "m1", 1.0), "Done.", "A")
    assert c.get(("s", "m1", 1.0), " Done. ") == "A" and c.get(("s", "f1", 1.0), "Done.") is None
    c.put(("s", "m1", 1.0), "This sentence is far too long to keep around.", "B")
    assert len(c) == 1
    for p in ("One.", "Two.", "Three."):
        c.put("k", p, p)
    assert c.get(("s", "m1", 1.0), "Done.") is None and len(c) == 3  # least recently used went
    assert "Done." in c.missing("k") and c.hits >= 1


# ---- the speaker: cache, prewarm, first-audio hook -----------------------------------------------------------------

class Bus:
    client_count = 0

    def __init__(self):
        self.events = []

    def publish(self, kind, data=None, sticky=False):
        self.events.append((kind, data))


class SlowVoice:
    sample_rate = 1000
    engine = "supertonic"

    def __init__(self, delay):
        self.voice, self.speed, self.delay, self.said = neural.voice("supertonic", "m1"), 1.0, delay, []

    def synth(self, text):
        time.sleep(self.delay)
        self.said.append(text)
        return np.ones(60, np.float32)

    def warm_up(self):
        return 0.0


class Card:
    def __init__(self, callback):
        self.callback, self.run, self.starts = callback, False, 0

    def start(self):
        self.run, self.starts = True, self.starts + 1

        def loop():
            buf = np.zeros((20, 1), np.float32)
            while self.run:
                self.callback(buf, 20)
                time.sleep(0.01)
        threading.Thread(target=loop, daemon=True).start()

    def stop(self):
        self.run = False

    def close(self):
        self.run = False


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    raise AssertionError("timed out")


@pytest.fixture
def speaker(monkeypatch):
    monkeypatch.setattr(neural, "available", lambda: True)
    voices, cards = [], []

    def make(delay=0.0):
        def loader(*_a, **_kw):
            voices.append(SlowVoice(delay))
            return voices[-1]
        sp = tts.Speaker(Bus(), "supertonic", loader=loader,
                         audio_factory=lambda sr: AudioOut(sr, stream_factory=lambda cb: cards.append(Card(cb)) or cards[-1]))
        sp.start()
        wait_for(lambda: sp.state == "ready")
        return sp, voices[0], cards
    yield make


def test_a_short_reply_said_once_plays_from_ready_audio_the_next_time(speaker):
    sp, voice, _ = speaker()
    sp.say("Done.")
    wait_for(lambda: not sp.speaking.is_set())
    sp.say("Done.")
    wait_for(lambda: not sp.speaking.is_set())
    assert voice.said == ["Done."] and sp.phrases.hits == 1


def test_prewarm_opens_the_device_and_makes_the_common_replies_ahead(speaker):
    sp, voice, cards = speaker()
    assert not cards  # nothing opened until it's needed
    sp.prewarm()
    wait_for(lambda: len(voice.said) == len(COMMON))
    assert cards and cards[0].starts == 1
    sp.say("Okay.")
    wait_for(lambda: not sp.speaking.is_set())
    assert voice.said.count("Okay.") == 1  # made once ahead, never again


def test_first_audio_hook_fires_once_per_turn(speaker):
    sp, _voice, _ = speaker()
    seen = []
    sp.on_first_audio = lambda turn, at: seen.append(turn)
    turn = sp.new_turn()
    sp.say("First. Second. Third.", turn=turn)
    wait_for(lambda: not sp.speaking.is_set())
    sp.say("More from the same turn.", turn=turn)
    wait_for(lambda: not sp.speaking.is_set())
    turn2 = sp.new_turn()
    sp.say("Next.", turn=turn2)
    wait_for(lambda: not sp.speaking.is_set())
    assert seen == [turn, turn2]


def test_measured_end_to_end_a_cached_reply_starts_sooner(speaker):
    """You speak, the fast path answers "Done.": wake-to-first-audio with a slow synthesiser, then from the cache."""
    sp, _voice, _ = speaker(delay=0.25)
    lat = TurnLatency(lambda: sp.turn)
    sp.on_first_audio, sp.latency = lat.first_audio, lat.report
    for _ in range(2):
        lat.on_event({"type": "heard", "data": {"text": "do it", "wake": True, "stt_ms": 0}, "ts": time.time()})
        turn = sp.new_turn()
        sp.say("Done.", turn=turn)
        wait_for(lambda: not sp.speaking.is_set())
    first, cached = [s["ms"] for s in lat.samples]
    assert first >= 240 and cached < first - 150
    assert sp.status()["turns"]["n"] == 2


# ---- the assistant speaks first, the HUD hears after ---------------------------------------------------------------

def test_fast_path_reply_is_spoken_before_the_tool_event_and_the_log(svc):
    from assistant.brain.assistant import Assistant

    order = []
    svc.bus.on(lambda e: order.append(e["type"]) if e["type"] in ("tool", "assistant_said") else None)
    svc.speak = lambda text, expects_reply=None, turn=None: order.append(f"speak:{text}")
    a = Assistant(svc)
    out = a.handle("what time is it", "voice", turn=1)  # built-in answer
    assert order[0].startswith("speak:It's") and order[-1] == "assistant_said"
    order.clear()
    svc.storage.add_task("thing")
    out = a.handle("what are my tasks", "voice", turn=2)
    assert out["kind"] == "tool"
    assert order.index(next(o for o in order if o.startswith("speak:"))) < order.index("tool") < order.index("assistant_said")
    assert sum(o.startswith("speak:") for o in order) == 1  # said once, not again at the end


def test_typed_commands_and_macro_steps_stay_quiet(svc):
    from assistant.brain.assistant import Assistant

    spoken = []
    svc.speak = lambda text, expects_reply=None, turn=None: spoken.append(text)
    a = Assistant(svc)
    a.handle("what are my tasks", "text")
    assert spoken == []


def test_runtime_shows_the_latency_with_the_voice_settings(cfg):
    from assistant.runtime import Runtime

    rt = Runtime(cfg)
    try:
        assert rt.speaker.on_first_audio == rt.latency.first_audio
        rt.bus.publish("heard", {"text": "x", "wake": True, "stt_ms": 50})
        rt.latency.first_audio(rt.speaker.turn + 1)
        assert rt.speaker.status()["turns"]["n"] == 1
        assert rt.bus.latest["tts"]["data"]["turns"]["n"] == 1
    finally:
        rt.stop()
