"""Natural end of turn: a pause mid-sentence doesn't end what you're saying, a finished sentence is answered sooner.

The voice detector alone ends an utterance after ``endpoint_ms`` of silence (400 ms), so "Vesper, remind me to…
call Dana" is cut at the pause, and a finished "open Discord" still waits the full 400 ms. With
``voice.end_of_turn: smart`` the detector reports every short pause (``turn_pause_ms``, 200 ms) and Smart Turn v3
(pipecat-ai/smart-turn, BSD-2-Clause: an 8 MB int8 ONNX model on Whisper Tiny, ~45 ms on one CPU core) listens to
the whole turn so far and says whether it sounds finished. Finished: the turn ends now. Not finished: keep listening,
up to ``turn_max_silence_ms`` (1500 ms) of silence, which always ends it. A held turn also ends ``turn_max_hold_ms``
(3500 ms) after you first paused, whatever is heard meanwhile: a friend on Discord or a video talking straight after
you is not your sentence going on (their speech becomes the next utterance). Without the model (not downloaded, no
onnxruntime, or it fails) the turn ends after ``endpoint_ms`` of silence, as before.

Measured on 70 synthetic commands (Kokoro, 11 voices): finished commands were called finished 97–99% of the time;
phrases that trail off ("Open Spotify and") were held open about half the time. Synthetic voices end every phrase like
a sentence, so the voice test (Setup → Voice test) measures it on your own voice and only then suggests turning it on.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
WINDOW = 8 * SAMPLE_RATE  # the model hears the last 8 s of the turn


# ---- Whisper log-mel features (the same numbers as transformers' WhisperFeatureExtractor(chunk_length=8)) -----------
def _mel_filters(n_fft: int = 400, n_mels: int = 80):
    import numpy as np

    def to_mel(f):  # Slaney scale
        f = np.asarray(f, dtype=np.float64)
        return np.where(f >= 1000, 15 + np.log(np.maximum(f, 1e-9) / 1000) * 27 / np.log(6.4), 3 * f / 200)

    def to_hz(m):
        return np.where(m >= 15, 1000 * np.exp(np.log(6.4) / 27 * (m - 15)), 200 * m / 3)

    edges = to_hz(np.linspace(to_mel(0.0), to_mel(SAMPLE_RATE / 2), n_mels + 2))
    fft = np.linspace(0, SAMPLE_RATE // 2, n_fft // 2 + 1)
    slopes = edges[None, :] - fft[:, None]
    down = -slopes[:, :-2] / np.diff(edges)[:-1]
    up = slopes[:, 2:] / np.diff(edges)[1:]
    return np.maximum(0, np.minimum(down, up)) * (2.0 / (edges[2:] - edges[:-2]))[None, :]


_FILTERS = None


def features(samples):
    """float32 16 kHz audio -> (80, 800) log-mel, the last 8 s of it, zero-padded in front."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    global _FILTERS
    if _FILTERS is None:
        _FILTERS = _mel_filters()
    x = np.asarray(samples, dtype=np.float32)[-WINDOW:]
    x = np.pad(x, (WINDOW - len(x), 0))
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    padded = np.pad(x.astype(np.float64), (200, 200), mode="reflect")
    frames = sliding_window_view(padded, 400)[::160] * np.hanning(401)[:-1]
    power = np.abs(np.fft.rfft(frames, axis=-1)) ** 2
    mel = np.log10(np.maximum(1e-10, power @ _FILTERS)).T[:, :-1]
    mel = np.maximum(mel, mel.max() - 8.0)
    return ((mel + 4.0) / 4.0).astype(np.float32)


class SmartTurn:
    """``model(samples) -> probability the turn is finished``."""

    def __init__(self, path: Path, threads: int = 1):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name

    def __call__(self, samples) -> float:
        out = self._session.run(None, {self._input: features(samples)[None]})[0]
        return float(out.reshape(-1)[0])


def load(models_dir: Path, on_progress=None) -> SmartTurn:
    from . import models

    return SmartTurn(models.ensure("smart-turn", models_dir, on_progress))


class TurnSegmenter:
    """Same contract as the segmenters in vad.py. ``inner`` is one of them, set to report pauses of ``pause_ms``;
    ``model(float32 samples) -> P(finished)``, or None for silence only. Time is counted in samples fed, so it
    behaves the same in a test, a replayed recording and the live mic."""

    def __init__(self, inner, model: Callable | None, pause_ms: int = 200, max_silence_ms: int = 1500,
                 fallback_ms: int = 400, threshold: float = 0.5, max_turn_s: float = 15, max_hold_ms: int = 3500):
        import numpy as np

        self._np = np
        self.inner, self.model = inner, model
        self.kind = f"{getattr(inner, 'kind', 'energy')}+turn"
        self.max_silence = int(max_silence_ms * SAMPLE_RATE / 1000)
        # Wall clock (samples fed) from the first pause, speech or not: someone else talking on can't hold it open.
        self.max_hold = int(max(max_hold_ms, max_silence_ms) * SAMPLE_RATE / 1000)
        self.fallback = int(max(fallback_ms, pause_ms) * SAMPLE_RATE / 1000)
        self.threshold = threshold
        self.max_turn = int(max_turn_s * SAMPLE_RATE)
        self.keep = self.max_turn + 3 * SAMPLE_RATE
        self.last_span: tuple[int, int, int] | None = None
        self.last_decision: dict | None = None  # why the last turn ended: finished | silence | long | no model
        self.error = ""
        self.reset()

    def reset(self) -> None:
        self.inner.reset()
        self._buf = self._np.zeros(0, dtype=self._np.int16)
        self._base = 0       # absolute sample index of _buf[0]
        self._fed = 0
        self._turn: list[int] | None = None  # [clip start, speech start, speech end] (absolute samples)
        self._wait = 0       # silence that ends the held turn (samples)
        self._held = 0       # where the turn's first pause began (absolute samples): the hold is timed from here

    @property
    def in_speech(self) -> bool:
        return self._turn is not None or bool(self.inner.in_speech)

    def feed(self, frame) -> list | None:
        np = self._np
        frame = np.asarray(frame, dtype=np.int16)
        self._buf = np.concatenate([self._buf, frame])
        self._fed += len(frame)
        if len(self._buf) > self.keep:
            drop = len(self._buf) - self.keep
            self._buf, self._base = self._buf[drop:], self._base + drop
        out = self.inner.feed(frame)
        if out is not None:
            clip = sum(len(f) for f in out)
            start, end, ago = self.inner.last_span or (0, clip, 0)
            clip_at = self._fed - ago - clip  # where the inner clip began (it ends ``ago`` samples back)
            seg = [clip_at, clip_at + start, clip_at + end]
            if self._turn is None:
                self._turn, self._held = seg, seg[2]
            else:
                self._turn[2] = max(self._turn[2], seg[2])
            return self._pause()
        if self._turn is not None and not self.inner.in_speech:
            silence = self._fed - self._turn[2]
            if silence >= self._wait:
                return self._emit("silence" if self._wait == self.max_silence else "no model")
        if self._turn is not None and self._fed - self._held >= self.max_hold:
            return self._emit("held")  # still talking: someone else (what they say comes next, on its own)
        return None

    def _pause(self) -> list | None:
        """A pause after speech: ask the model whether the turn sounds finished."""
        turn = self._turn
        if self._fed - turn[1] >= self.max_turn:
            return self._emit("long")
        if self.model is None or self.error:
            self._wait = self.fallback
        else:
            try:
                p = float(self.model(self._slice(turn[1], turn[2]).astype(self._np.float32) / 32768.0))
            except Exception as exc:  # a broken model never stops the mic: silence decides from now on
                self.error = f"{type(exc).__name__}: {exc}"
                log.warning("end-of-turn model failed (%s); ending turns on silence", self.error)
                self._wait = self.fallback
            else:
                self.last_decision = {"p": round(p, 3)}
                if p >= self.threshold:
                    return self._emit("finished", p)
                self._wait = self.max_silence
        if self._fed - turn[2] >= self._wait:
            return self._emit("silence")
        return None

    def _slice(self, a: int, b: int):
        return self._buf[max(a - self._base, 0):max(b - self._base, 0)]

    def _emit(self, why: str, p: float | None = None) -> list:
        clip_at, start, end = self._turn
        clip = self._slice(clip_at, end).copy()
        lead = max(0, min(start, end) - max(clip_at, self._base))
        self.last_span = (lead, len(clip), self._fed - end)
        self.last_decision = {"why": why, **({"p": round(p, 3)} if p is not None else {})}
        self._turn, self._wait = None, 0
        return [clip]

