"""Natural end of turn (voice/turn.py): a pause mid-sentence doesn't end J's turn, a finished sentence ends sooner,
a long silence always ends it, and without the model it's the old silence rule. The model is a fake; time is samples
fed, never the wall clock."""

import math

import numpy as np
import pytest

from assistant.voice import turn, vad
from assistant.voice.listener import FRAME_SAMPLES, EnergySegmenter

MS = FRAME_SAMPLES * 1000 // 16000  # 30 ms per frame


def tone(amplitude):
    return np.array([int(amplitude * math.sin(i / 3)) for i in range(FRAME_SAMPLES)], dtype=np.int16)


LOUD, QUIET = tone(3000), tone(40)


class Model:
    """Says 'finished' with the probabilities given, one per pause, and keeps what it heard."""

    def __init__(self, *probs):
        self.probs, self.heard = list(probs), []

    def __call__(self, samples):
        self.heard.append(len(samples))
        return self.probs.pop(0) if self.probs else 0.0


def segmenter(model, **kw):
    inner = EnergySegmenter(min_rms=300, silence_ms=210, max_utterance_s=15)  # reports a pause after 7 quiet frames
    return turn.TurnSegmenter(inner, model, pause_ms=210, **{"max_silence_ms": 1500, "fallback_ms": 400, **kw})


def feed(seg, pattern):
    """pattern: [(frame, count)]; returns [(frame index, utterance)] for every utterance handed over."""
    out, i = [], 0
    for frame, count in pattern:
        for _ in range(count):
            got = seg.feed(frame)
            if got is not None:
                out.append((i, got))
            i += 1
    return out


def test_a_finished_sentence_ends_at_the_first_short_pause():
    model = Model(0.93)
    seg = segmenter(model)
    got = feed(seg, [(QUIET, 10), (LOUD, 30), (QUIET, 60)])
    assert len(got) == 1
    ended_at, clip = got[0]
    assert (ended_at - 40) * MS <= 240  # ~210 ms after the speech, not the 400 ms silence rule
    assert seg.last_decision == {"why": "finished", "p": 0.93}
    assert model.heard and model.heard[0] >= 30 * FRAME_SAMPLES  # it heard the whole sentence


def test_a_pause_mid_sentence_is_held_and_the_turn_comes_out_whole():
    model = Model(0.12, 0.88)  # "remind me to …" (not finished), then "… call Dana" (finished)
    seg = segmenter(model)
    got = feed(seg, [(QUIET, 10), (LOUD, 20), (QUIET, 25), (LOUD, 20), (QUIET, 40)])
    assert len(got) == 1, "the pause split the sentence"
    _at, [clip] = got[0]
    start, end, ago = seg.last_span
    assert end - start >= (20 + 25 + 20) * FRAME_SAMPLES - 3 * FRAME_SAMPLES  # both halves and the pause between
    assert end == len(clip) and ago > 0
    assert len(model.heard) == 2 and model.heard[1] > model.heard[0]  # the second look heard the whole turn again
    assert seg.last_decision["why"] == "finished"


def test_while_held_it_still_counts_as_speech():
    seg = segmenter(Model(0.1))
    feed(seg, [(QUIET, 10), (LOUD, 20), (QUIET, 10)])
    assert seg.in_speech  # the listener keeps showing "hearing", and the follow-up silence clock doesn't start


def test_a_long_silence_always_ends_the_turn():
    seg = segmenter(Model(0.05))  # never sounds finished
    got = feed(seg, [(QUIET, 10), (LOUD, 20), (QUIET, 80)])
    assert len(got) == 1
    ended_at = got[0][0]
    silence_ms = (ended_at - 29) * MS
    assert 1450 <= silence_ms <= 1600
    assert seg.last_decision["why"] == "silence"


def test_no_model_ends_on_the_old_silence_rule():
    seg = segmenter(None)
    got = feed(seg, [(QUIET, 10), (LOUD, 20), (QUIET, 40)])
    assert len(got) == 1
    assert 380 <= (got[0][0] - 29) * MS <= 470
    assert seg.last_decision["why"] == "no model"


def test_a_failing_model_never_stops_the_mic():
    def broken(_samples):
        raise RuntimeError("onnx went away")

    seg = segmenter(broken)
    first = feed(seg, [(QUIET, 10), (LOUD, 20), (QUIET, 40)])
    assert len(first) == 1 and "onnx went away" in seg.error
    again = feed(seg, [(LOUD, 20), (QUIET, 40)])  # and the next turn too, on silence
    assert len(again) == 1


def test_reset_drops_a_held_turn():
    seg = segmenter(Model(0.1))
    feed(seg, [(QUIET, 10), (LOUD, 20), (QUIET, 10)])
    seg.reset()
    assert not seg.in_speech
    assert feed(seg, [(QUIET, 60)]) == []


def test_kind_says_what_ends_a_turn():
    assert segmenter(Model()).kind == "energy+turn"


# ---- make_segmenter: opt-in, and the fallbacks ---------------------------------------------------------------------

def test_silence_is_the_default_even_with_a_model_at_hand():
    seg = vad.make_segmenter({"vad": "energy"}, None, turn_model=Model())
    assert isinstance(seg, EnergySegmenter)


def test_smart_wraps_the_detector_with_short_pauses():
    seg = vad.make_segmenter({"vad": "energy", "end_of_turn": "smart", "turn_pause_ms": 240, "silence_ms": 800,
                              "turn_max_silence_ms": 1200}, None, turn_model=Model())
    assert isinstance(seg, turn.TurnSegmenter) and seg.kind == "energy+turn"
    assert seg.inner.silence_frames == 240 // 30  # the detector reports pauses, the model decides
    assert seg.max_silence == 1200 * 16 and seg.fallback == 800 * 16


def test_smart_without_the_model_falls_back_and_says_why(monkeypatch, tmp_path):
    def missing(*_a, **_k):
        raise RuntimeError("offline")

    monkeypatch.setattr(turn, "load", missing)
    seg = vad.make_segmenter({"vad": "energy", "end_of_turn": "smart"}, tmp_path)
    assert isinstance(seg, EnergySegmenter) and seg.silence_frames == 800 // 30  # the old rule, untouched
    assert "offline" in seg.turn_error


def test_features_are_whisper_shaped():
    f = turn.features(np.sin(np.arange(16000 * 3) / 7).astype(np.float32) * 0.1)
    assert f.shape == (80, 800) and np.isfinite(f).all() and f.dtype == np.float32


def test_smart_turn_model_is_pinned_and_hash_checked(tmp_path, monkeypatch):
    from assistant.voice import models

    spec = models.MODELS["smart-turn"]
    assert "/resolve/f766f81d3cfdf7737ac64aad813d91bbfd56bf93/" in spec.url and len(spec.sha256) == 64

    class Stream:
        headers = {"content-length": "4"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, _n):
            yield b"evil"

    import httpx

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: Stream())
    with pytest.raises(RuntimeError, match="hash"):
        models.ensure("smart-turn", tmp_path)
    assert not list(tmp_path.iterdir())  # nothing half-downloaded left behind


def test_the_diagnostic_report_says_how_turns_end_and_whether_names_are_expected():
    from types import SimpleNamespace

    from assistant import diagnostics as diag

    lst = SimpleNamespace(recent_heard=[], wake_words=["vesper"], status=lambda: {
        "engine": "Parakeet 110M", "vad": "silero", "end_of_turn": "smart", "turn_error": "RuntimeError: offline",
        "hotwords": {"on": True, "words": 23, "error": ""}})
    md = diag.to_markdown({"summary": {}, "checks": [], "voice": diag.voice_report(SimpleNamespace(listener=lst))})
    assert ("Hearing: Parakeet 110M, silero voice detection, end of turn smart (unavailable: RuntimeError: offline), "
            "names expected (23).") in md
