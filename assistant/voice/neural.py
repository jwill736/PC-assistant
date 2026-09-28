"""Local neural voices (sherpa-onnx) and an output stream that can stop mid-word.

Measured on a 2-thread budget (Xeon, AVX-512) with the replies the assistant
actually gives, time until the first word plays:

=====================  ===============  ============  =========================
engine                 first audio      real-time     notes
=====================  ===============  ============  =========================
Supertonic (int8)      140-220 ms       0.09          default: 85 MB, 10 voices
Kokoro v0.19 (fp32)    410-1200 ms      0.45          320 MB, British voices;
                                                      high fixed cost per chunk
Kokoro (int8)          1000-3100 ms     1.13          slower than fp32: not used
=====================  ===============  ============  =========================

So replies are cut into sentences and each one plays while the next is being
synthesised; an interrupt stops the sound within one audio block (~20 ms).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

ENGINES = ("supertonic", "kokoro")


@dataclass(frozen=True)
class Voice:
    id: str
    label: str
    sid: int


# Supertonic's 10 styles: 0-4 measure 165-242 Hz (female), 5-9 88-138 Hz (male).
# Kokoro v0.19's speaker ids, in sherpa-onnx's order (pitch checked the same way).
VOICES: dict[str, list[Voice]] = {
    "supertonic": [Voice(f"m{i}", f"Male {i}", 4 + i) for i in range(1, 6)]
    + [Voice(f"f{i}", f"Female {i}", i - 1) for i in range(1, 6)],
    "kokoro": [
        Voice("bm_george", "George · British", 9), Voice("bm_lewis", "Lewis · British", 10),
        Voice("am_adam", "Adam · American", 5), Voice("am_michael", "Michael · American", 6),
        Voice("bf_emma", "Emma · British", 7), Voice("bf_isabella", "Isabella · British", 8),
        Voice("af", "Default · American", 0), Voice("af_bella", "Bella · American", 1),
        Voice("af_nicole", "Nicole · American", 2), Voice("af_sarah", "Sarah · American", 3),
        Voice("af_sky", "Sky · American", 4),
    ],
}
LABELS = {"supertonic": "Supertonic", "kokoro": "Kokoro"}


def available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
        import sounddevice  # noqa: F401
        return True
    except (ImportError, OSError):  # OSError: sounddevice without a PortAudio library
        return False


def voice(engine: str, voice_id: str | None) -> Voice:
    voices = VOICES[engine]
    return next((v for v in voices if v.id == (voice_id or "").lower()), voices[0])


# ---- text -> speakable chunks ------------------------------------------------

_ABBREV = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "approx", "no", "jan", "feb", "aug", "sept",
           "oct", "nov", "dec"}
_BOUNDARY = re.compile(r"[.!?…]+[\"')\]]*\s+|\n+")
_KEEP_CAPS = {"I", "A"}
_SAY = {"OK": "okay", "%": " percent", "&": " and ", "w/": "with "}


def speakable(text: str) -> str:
    """Spell out what synthesisers mangle: 'BRB' -> 'B R B', '91%' -> '91 percent'."""
    text = re.sub(r"\bOK\b", _SAY["OK"], text)
    text = text.replace("%", _SAY["%"]).replace(" & ", _SAY["&"])
    return re.sub(r"\b[A-Z]{2,4}s?\b", lambda m: m.group(0) if m.group(0) in _KEEP_CAPS
                  else " ".join(m.group(0).rstrip("s")) + ("s" if m.group(0).endswith("s") else ""), text)


class SentenceStream:
    """Turns streamed text into whole sentences as soon as each one is complete.

    A sentence is handed out when the *next* one starts, so the last sentence
    of a reply is always the one ``flush(final=True)`` emits: that's where we
    learn whether the reply asked a question (and should keep the mic open).
    """

    def __init__(self, emit, max_words: int = 28):
        self.emit, self.max_words = emit, max_words
        self._buf = ""
        self._held: str | None = None

    def feed(self, delta: str) -> None:
        self._buf += delta
        while True:
            cut = self._next_cut()
            if cut is None:
                break
            sentence, self._buf = self._buf[:cut].strip(), self._buf[cut:]
            if sentence:
                self._hold(sentence)
        if self._held is not None and self._buf.strip():
            self.emit(self._held, False)
            self._held = None

    def flush(self, final: bool = True) -> None:
        tail = self._buf.strip()
        self._buf = ""
        if tail:
            self._hold(tail)
        if self._held is not None:
            self.emit(self._held, final)
            self._held = None

    def _hold(self, sentence: str) -> None:
        if self._held is not None:
            self.emit(self._held, False)
        self._held = sentence

    def _next_cut(self) -> int | None:
        for m in _BOUNDARY.finditer(self._buf):
            before = self._buf[:m.start()].split()
            last = before[-1].lower().rstrip(".") if before else ""
            if m.group(0).startswith(".") and (last in _ABBREV or last.isdigit() and len(before) == 1):
                continue  # "Dr. Lee", "1. Open Discord"
            return m.end()
        words = self._buf.split()
        if len(words) > self.max_words:  # a run-on sentence: break at a comma rather than wait for the end
            comma = self._buf.rfind(", ", 0, len(self._buf) - 20)
            if comma > 40:
                return comma + 2
        return None


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    stream = SentenceStream(lambda s, _final: out.append(s))
    stream.feed(text)
    stream.flush()
    return out


# ---- synthesis -----------------------------------------------------------------

class NeuralVoice:
    """One loaded sherpa-onnx model; ``synth(text)`` returns float32 samples."""

    def __init__(self, engine: str, model_dir: Path, voice_id: str | None = None, speed: float = 1.0,
                 threads: int = 2, tts=None):
        if engine not in ENGINES:
            raise ValueError(f"unknown voice engine {engine!r}")
        self.engine = engine
        self.voice = voice(engine, voice_id)
        self.speed = speed
        self._tts = tts if tts is not None else self._load(engine, Path(model_dir), threads)
        self.sample_rate = self._tts.sample_rate
        self._lock = threading.Lock()  # one synthesis at a time (preview vs. a reply)

    @staticmethod
    def _load(engine: str, d: Path, threads: int):
        import sherpa_onnx as so

        if engine == "supertonic":
            model = so.OfflineTtsModelConfig(supertonic=so.OfflineTtsSupertonicModelConfig(
                duration_predictor=str(d / "duration_predictor.int8.onnx"), text_encoder=str(d / "text_encoder.int8.onnx"),
                vector_estimator=str(d / "vector_estimator.int8.onnx"), vocoder=str(d / "vocoder.int8.onnx"),
                tts_json=str(d / "tts.json"), unicode_indexer=str(d / "unicode_indexer.bin"),
                voice_style=str(d / "voice.bin")), num_threads=threads, provider="cpu")
        else:
            model = so.OfflineTtsModelConfig(kokoro=so.OfflineTtsKokoroModelConfig(
                model=str(d / "model.onnx"), voices=str(d / "voices.bin"), tokens=str(d / "tokens.txt"),
                data_dir=str(d / "espeak-ng-data")), num_threads=threads, provider="cpu")
        return so.OfflineTts(so.OfflineTtsConfig(model=model, max_num_sentences=1))

    def synth(self, text: str):
        import numpy as np

        with self._lock:
            audio = self._tts.generate(speakable(text), sid=self.voice.sid, speed=self.speed)
        return np.asarray(audio.samples, dtype=np.float32)

    def warm_up(self) -> float:
        """The first synthesis is several times slower than the rest; pay it at startup."""
        t0 = time.perf_counter()
        for text in ("Ready.", "Okay, that's done."):
            self.synth(text)
        return time.perf_counter() - t0


def load(engine: str, models_dir: Path, voice_id: str | None = None, speed: float = 1.0, threads: int = 2,
         on_progress=None) -> NeuralVoice:
    from . import models

    return NeuralVoice(engine, models.ensure(engine, models_dir, on_progress), voice_id, speed, threads)


# ---- playback ------------------------------------------------------------------

class AudioOut:
    """A mono output stream fed with chunks; ``abort()`` silences it at once.

    The stream stays open between sentences (reopening a device costs 50-100 ms
    and can click) and is closed after ``idle_close_s`` of silence.
    """

    FADE_S = 0.01  # ramp down instead of cutting mid-waveform (a cut clicks)

    def __init__(self, sample_rate: int, device=None, stream_factory=None, idle_close_s: float = 15.0):
        self.sample_rate = sample_rate
        self.device = device
        self.idle_close_s = idle_close_s
        self._factory = stream_factory
        self._stream = None
        self._chunks: deque = deque()
        self._pos = 0
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._last_active = time.monotonic()

    def _open(self):
        if self._factory is not None:
            return self._factory(self._callback)
        import sounddevice as sd

        return sd.OutputStream(samplerate=self.sample_rate, channels=1, dtype="float32", device=self.device,
                               latency="low", callback=self._callback)

    def play(self, samples) -> None:
        import numpy as np

        samples = np.asarray(samples, dtype=np.float32)
        if not len(samples):
            return
        with self._lock:
            self._chunks.append(samples)
            self._idle.clear()
            self._last_active = time.monotonic()
        if self._stream is None:
            self._stream = self._open()
            self._stream.start()

    def abort(self) -> None:
        import numpy as np

        with self._lock:
            if self._chunks:
                head = self._chunks[0][self._pos:self._pos + int(self.sample_rate * self.FADE_S)]
                self._chunks.clear()
                self._pos = 0
                if len(head):
                    self._chunks.append(head * np.linspace(1.0, 0.0, len(head), dtype=np.float32))
                else:
                    self._idle.set()

    @property
    def playing(self) -> bool:
        return not self._idle.is_set()

    def wait(self, cancel: threading.Event | None = None, timeout: float = 120.0) -> bool:
        """Block until everything queued has played. False if cancelled or timed out."""
        end = time.monotonic() + timeout
        while not self._idle.wait(0.02):
            if (cancel is not None and cancel.is_set()) or time.monotonic() > end:
                return False
        return True

    def maybe_close(self) -> None:
        if self._stream is not None and not self.playing and time.monotonic() - self._last_active > self.idle_close_s:
            self.close()

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.debug("closing the output stream failed", exc_info=True)

    def _callback(self, outdata, frames, _time=None, _status=None) -> None:
        out = outdata[:, 0] if getattr(outdata, "ndim", 1) == 2 else outdata
        filled = 0
        with self._lock:
            while filled < frames and self._chunks:
                chunk = self._chunks[0]
                n = min(frames - filled, len(chunk) - self._pos)
                out[filled:filled + n] = chunk[self._pos:self._pos + n]
                filled += n
                self._pos += n
                if self._pos >= len(chunk):
                    self._chunks.popleft()
                    self._pos = 0
            if filled < frames:
                out[filled:] = 0
            if not self._chunks:
                self._idle.set()
                self._last_active = time.monotonic()
