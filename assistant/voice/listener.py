"""Always-on voice input: mic -> speech segments -> Whisper -> wake word -> command.

The wake word is matched on the transcript, so it can be any name you choose
(no custom model training). After the assistant answers, a short follow-up
window accepts the next sentence without the name. Push-to-talk (hotkey or the
dashboard mic button) skips the wake word for one utterance.
"""

from __future__ import annotations

import difflib
import logging
import math
import queue
import re
import threading
import time
from collections import deque
from typing import Callable

from ..bus import EventBus
from .tts import Speaker

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000

# Whisper's favourite things to "hear" in silence or fan noise.
HALLUCINATIONS = {"you", "thank you", "thanks for watching", "thank you for watching", "bye", "so", "okay", ""}


def _norm(token: str) -> str:
    return re.sub(r"[^\w']", "", token.lower())


def split_wake(transcript: str, wake_words: list[str], search_words: int = 4, cutoff: float = 0.8) -> tuple[bool, str]:
    """Find a wake word near the start of ``transcript``.

    Returns (matched, command_after_wake_word). Fuzzy so "Jarvis," / "jarvis's" /
    "Hey, Jarvis" all match, and the command keeps its original casing.
    """
    tokens = re.findall(r"\S+", transcript)
    normed = [_norm(t) for t in tokens]
    for wake in sorted(wake_words, key=lambda w: -len(w.split())):
        wake_tokens = [_norm(w) for w in wake.split()]
        n = len(wake_tokens)
        target = " ".join(wake_tokens)
        for i in range(0, max(0, min(search_words, len(tokens) - n + 1))):
            cand = " ".join(normed[i:i + n])
            if cand.endswith("'s"):
                cand = cand[:-2]
            if cand == target or difflib.SequenceMatcher(None, cand, target).ratio() >= cutoff:
                rest = " ".join(tokens[i + n:]).strip(" ,.!?;:-")
                return True, rest
    return False, transcript.strip()


class EnergySegmenter:
    """Adaptive-threshold voice activity detection over 30 ms int16 frames."""

    def __init__(self, min_rms: float = 350, silence_ms: int = 800, max_utterance_s: float = 15,
                 start_frames: int = 3, preroll_frames: int = 10, min_voiced_ms: int = 250):
        self.min_rms = min_rms
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.max_frames = int(max_utterance_s * 1000 / FRAME_MS)
        self.start_frames = start_frames
        self.min_voiced_frames = max(1, min_voiced_ms // FRAME_MS)
        self.noise = min_rms / 2
        self.preroll: deque = deque(maxlen=preroll_frames)
        self.reset()

    def reset(self) -> None:
        self.in_speech = False
        self.voiced = 0
        self.silent = 0
        self.voiced_total = 0
        self.buffer: list = []
        self.preroll.clear()

    @staticmethod
    def rms(frame) -> float:
        try:
            import numpy as np

            return float(np.sqrt(np.mean(np.asarray(frame, dtype=np.float32) ** 2)))
        except ImportError:  # tests without numpy
            return math.sqrt(sum(float(x) * float(x) for x in frame) / max(len(frame), 1))

    @property
    def threshold(self) -> float:
        return max(self.min_rms, self.noise * 2.5)

    def feed(self, frame) -> list | None:
        """Feed one frame; returns the finished utterance's frames when speech ends."""
        level = self.rms(frame)
        if not self.in_speech:
            self.preroll.append(frame)
            if level > self.threshold:
                self.voiced += 1
                if self.voiced >= self.start_frames:
                    self.in_speech = True
                    self.buffer = list(self.preroll)
                    self.silent = 0
                    self.voiced_total = self.voiced
            else:
                self.voiced = 0
                self.noise = 0.97 * self.noise + 0.03 * level
            return None
        self.buffer.append(frame)
        if level > self.threshold * 0.7:
            self.silent = 0
            self.voiced_total += 1
        else:
            self.silent += 1
        if self.silent >= self.silence_frames or len(self.buffer) >= self.max_frames:
            frames, voiced = self.buffer, self.voiced_total
            self.reset()
            # Clicks, coughs and key clacks are loud but short: require real voiced time.
            return frames if voiced >= self.min_voiced_frames else None
        return None


class VoiceListener:
    def __init__(self, bus: EventBus, speaker: Speaker, wake_words: list[str], on_command: Callable[[str], None],
                 cfg: dict, hint_words: Callable[[], list[str]] | None = None):
        self.bus, self.speaker = bus, speaker
        self.wake_words = wake_words
        self.on_command = on_command
        self.cfg = cfg
        self.hint_words = hint_words or (lambda: [])
        self.state = "off"
        self.error = ""
        self.muted = False
        self.armed_until = 0.0
        self.last_command = 0.0
        self._audio: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._model = None

    # ---- control ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="voice", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def arm(self, seconds: float = 8) -> None:
        """Accept the next utterance without the wake word (push-to-talk)."""
        self.armed_until = time.time() + seconds
        self.speaker.chime()
        self._set_state("armed")

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        self._set_state("muted" if muted else "listening")

    def _set_state(self, state: str, **extra) -> None:
        self.state = state
        self.bus.publish("voice_state", {"state": state, "error": self.error, **extra}, sticky=True)

    def status(self) -> dict:
        return {"state": self.state, "error": self.error, "muted": self.muted, "wake_words": self.wake_words}

    # ---- main loop --------------------------------------------------------
    def _load_model(self):
        from faster_whisper import WhisperModel

        device = self.cfg.get("stt_device", "auto")
        compute = "int8" if device in ("cpu", "auto") else "float16"
        return WhisperModel(self.cfg.get("stt_model", "base.en"), device=device, compute_type=compute)

    def _run(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            self.error = "Voice extras not installed: pip install -r requirements-voice.txt"
            self._set_state("unavailable")
            return
        self._set_state("loading")
        try:
            self._model = self._load_model()
        except Exception as exc:
            self.error = f"Speech model failed to load: {exc}"
            log.exception("whisper load failed")
            self._set_state("error")
            return

        segmenter = EnergySegmenter(self.cfg.get("min_rms", 350), self.cfg.get("silence_ms", 800),
                                    self.cfg.get("max_utterance_s", 15))

        def callback(indata, _frames, _time, status):
            if status:
                log.debug("audio status: %s", status)
            self._audio.put(indata[:, 0].copy())

        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME_SAMPLES,
                                    device=self.cfg.get("input_device"), callback=callback)
        except Exception as exc:
            self.error = f"Microphone unavailable: {exc}"
            self._set_state("error")
            return
        with stream:
            self._set_state("muted" if self.muted else "listening")
            while not self._stop.is_set():
                try:
                    frame = self._audio.get(timeout=0.5)
                except queue.Empty:
                    continue
                if self.muted or self.speaker.speaking.is_set():
                    segmenter.reset()
                    continue
                frames = segmenter.feed(frame)
                if frames is None:
                    if segmenter.in_speech and self.state != "hearing":
                        self._set_state("hearing")
                    continue
                self._set_state("transcribing")
                try:
                    self._handle_audio(np.concatenate(frames))
                except Exception:
                    log.exception("voice pipeline error")
                self._drain()
                if not self.muted:
                    self._set_state("armed" if self._armed() else "listening")

    def _drain(self) -> None:
        """Drop audio captured while we were busy (it's mostly our own voice)."""
        while True:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                return

    def _armed(self) -> bool:
        now = time.time()
        if now < self.armed_until:
            return True
        follow = self.cfg.get("follow_up_seconds", 8)
        return self.speaker.last_end > self.last_command and now - self.speaker.last_end < follow

    def _handle_audio(self, audio) -> None:
        import numpy as np

        samples = audio.astype(np.float32) / 32768.0
        hints = ", ".join([self.wake_words[0].title(), *self.hint_words()][:40])
        segments, _info = self._model.transcribe(samples, language="en", beam_size=1, vad_filter=True,
                                                 condition_on_previous_text=False, initial_prompt=hints)
        text = " ".join(s.text for s in segments).strip()
        if _norm(text.replace(" ", "")) in {h.replace(" ", "") for h in HALLUCINATIONS}:
            return
        matched, command = split_wake(text, self.wake_words)
        armed = self._armed()
        self.bus.publish("heard", {"text": text, "wake": matched, "armed": armed})
        if matched and not command:
            self.arm()  # "Jarvis?" -> listen for the actual command
            return
        if matched or armed:
            self.armed_until = 0.0
            self.last_command = time.time()
            self.on_command(command if matched else text)
