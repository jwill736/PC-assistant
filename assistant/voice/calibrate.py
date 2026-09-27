"""Voice calibration: tune the mic threshold, learn how Whisper hears the wake
word in *your* voice, and enroll your voice so only you can command it.

Three steps, ~1 minute, run from the HUD Setup tab or ``--calibrate``:

1. **Room noise** — 5 s of quiet sets ``voice.min_rms`` to ~3x the noise floor.
2. **Wake word** — say the name 5 times; every way Whisper spells it becomes an
   extra wake word ("Vesper" → "vespa", "vesper's"…).
3. **Your voice** — read three short lines (~30 s). Those takes plus the wake
   words become the speaker profile used to ignore other voices.

Results land in ``data/calibration.yaml`` (merged under config.yaml) and the
encrypted voice profile. Everything is injectable so it tests without a mic.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable

import yaml

from . import speaker_id

log = logging.getLogger(__name__)

CALIBRATION_FILE = "calibration.yaml"
SAMPLE_RATE = 16000
READ_ALOUD = [
    "Good morning. Pull up my calendar, check the stream stats, and tell me what matters today.",
    "Switch to the gameplay scene, mute the music, and start recording when I say go.",
    "Remind me to send the invoice after lunch, then open the project I was working on yesterday.",
]
COMMON_WORDS = {"the", "a", "an", "hey", "hi", "yes", "you", "is", "it", "and", "i", "ok", "okay", "so", "oh"}


def _np():
    import numpy as np

    return np


def frame_rms(samples, frame: int = 480) -> list[float]:
    """RMS per 30 ms frame on the int16 scale the live segmenter uses."""
    np = _np()
    x = np.asarray(samples, dtype=np.float32) * 32768.0
    n = len(x) // frame
    if n == 0:
        return [0.0]
    x = x[: n * frame].reshape(n, frame)
    return np.sqrt((x ** 2).mean(axis=1)).tolist()


def voiced_chunks(samples, floor: float, seconds: float = 3.0, min_voiced: float = 0.4) -> list:
    """Split a recording into ~3 s pieces, keeping those that are mostly speech."""
    size = int(seconds * SAMPLE_RATE)
    out = []
    for start in range(0, max(len(samples) - size // 2, 1), size):
        piece = samples[start:start + size]
        levels = frame_rms(piece)
        if len(piece) >= SAMPLE_RATE and sum(lv > floor for lv in levels) / len(levels) >= min_voiced:
            out.append(piece)
    return out


def trim(samples, floor: float, pad: int = 4800):
    """Cut leading/trailing silence so the fingerprint is all voice."""
    levels = frame_rms(samples)
    voiced = [i for i, lv in enumerate(levels) if lv > floor]
    if not voiced:
        return samples[:0]
    return samples[max(0, voiced[0] * 480 - pad): min(len(samples), (voiced[-1] + 1) * 480 + pad)]


def wake_variants(transcripts: list[str], wake_word: str, known: list[str]) -> list[str]:
    """Spellings of the wake word Whisper produced for this voice that we don't already accept."""
    target = wake_word.lower()
    shortest = max(3, round(len(target) * 0.7))  # "day" is not a way of saying "Friday"
    longest = len(target) + 2                     # nor is "desperate" a way of saying "Vesper"
    found: list[str] = []
    for text in transcripts:
        for token in re.findall(r"[a-z']+", text.lower()):
            token = token.strip("'")
            if token.endswith("'s"):
                token = token[:-2]
            if not shortest <= len(token) <= longest or token in COMMON_WORDS or token == target or token in known or token in found:
                continue
            if difflib.SequenceMatcher(None, token, target).ratio() >= 0.6:
                found.append(token)
    return found


class CalibrationCancelled(Exception):
    pass


class Calibrator:
    STEPS = ("noise", "wake", "speech")

    def __init__(self, cfg, bus, record: Callable, transcribe: Callable, embedder=None,
                 takes: int = 5, noise_seconds: float = 5.0, read_seconds: float = 10.0, prompt_pause: float = 0.8):
        self.cfg = cfg
        self.bus = bus
        self.record = record          # (seconds, on_level) -> float32 samples @16 kHz
        self.transcribe = transcribe  # (samples) -> text
        self.embedder = embedder      # object with .embed(samples) -> vector; None = skip enrollment
        self.takes, self.noise_seconds, self.read_seconds = takes, noise_seconds, read_seconds
        self.prompt_pause = prompt_pause
        self.cancelled = threading.Event()
        self.result: dict = {}

    # ---- plumbing -------------------------------------------------------
    def emit(self, **data) -> None:
        if self.bus is not None:
            self.bus.publish("calibration", data, sticky=data.get("step") in ("done", "error", "cancelled"))

    def _rec(self, step: str, prompt: str, seconds: float, index: int = 0, total: int = 1):
        if self.cancelled.is_set():
            raise CalibrationCancelled()
        self.emit(step=step, prompt=prompt, index=index, total=total, seconds=seconds, recording=False)
        time.sleep(self.prompt_pause)  # a beat to read the prompt
        self.emit(step=step, prompt=prompt, index=index, total=total, seconds=seconds, recording=True)
        samples = self.record(seconds, lambda level: self.emit(step=step, level=round(level), recording=True,
                                                               prompt=prompt, index=index, total=total))
        if self.cancelled.is_set():
            raise CalibrationCancelled()
        return samples

    # ---- steps ----------------------------------------------------------
    def run(self) -> dict:
        try:
            noise = self.step_noise()
            wake_clips = self.step_wake(noise)
            speech_clips = self.step_speech(noise)
            self.finish(wake_clips + speech_clips)
            self.emit(step="done", summary=self.result)
            return self.result
        except CalibrationCancelled:
            self.emit(step="cancelled")
            return {"cancelled": True}
        except Exception as exc:
            log.exception("calibration failed")
            self.emit(step="error", error=f"{type(exc).__name__}: {exc}")
            return {"error": str(exc)}

    def step_noise(self) -> float:
        np = _np()
        samples = self._rec("noise", "Stay quiet for a moment — measuring room noise.", self.noise_seconds)
        levels = frame_rms(samples)
        p90 = float(np.percentile(levels, 90))
        min_rms = int(min(3000, max(200, p90 * 3)))
        self.result.update(noise_rms=round(p90, 1), min_rms=min_rms)
        return max(p90 * 2, 150.0)

    def step_wake(self, floor: float) -> list:
        name = self.cfg["assistant"]["name"]
        transcripts, clips = [], []
        for i in range(self.takes):
            samples = self._rec("wake", f"Say “{name}” — normally, like you'd call it.", 2.5, i + 1, self.takes)
            voiced = trim(samples, floor)
            if len(voiced) < SAMPLE_RATE * 0.25:
                continue
            clips.append(voiced)
            transcripts.append(self.transcribe(voiced))
        variants = wake_variants(transcripts, name, [w.lower() for w in self.cfg["assistant"]["wake_words"]])
        self.result.update(wake_heard=transcripts, wake_variants=variants, wake_takes=len(clips))
        return clips

    def step_speech(self, floor: float) -> list:
        chunks = []
        for i, line in enumerate(READ_ALOUD):
            samples = self._rec("speech", f"Read aloud: “{line}”", self.read_seconds, i + 1, len(READ_ALOUD))
            chunks += voiced_chunks(samples, floor)
        self.result["speech_chunks"] = len(chunks)
        return chunks

    def finish(self, clips: list) -> None:
        data_dir = Path(self.cfg.data_dir)
        layer: dict = {"voice": {"min_rms": self.result["min_rms"]}}
        if self.result.get("wake_variants"):
            layer["assistant"] = {"wake_words": self.result["wake_variants"]}
        if self.embedder is not None and len(clips) >= 3:
            self.emit(step="enroll", prompt="Building your voice profile…")
            embeddings = [self.embedder.embed(c) for c in clips]
            profile = speaker_id.build_profile(embeddings, getattr(self.embedder, "name", speaker_id.MODEL_NAME))
            speaker_id.save_profile(profile, data_dir)
            layer["voice"]["speaker_check"] = "strict"
            self.result["profile"] = profile.summary()
        elif self.embedder is not None:
            self.result["profile_error"] = "not enough clear speech to enroll — try again somewhere quieter"
        write_layer(layer, data_dir)
        self.result["saved_to"] = str(data_dir / CALIBRATION_FILE)


def write_layer(layer: dict, data_dir: Path) -> Path:
    path = Path(data_dir) / CALIBRATION_FILE
    header = ("# Written by voice calibration. Sits under config.yaml (your settings win).\n"
              "# Re-run calibration from the Setup tab or with --calibrate.\n")
    path.write_text(header + yaml.safe_dump(layer, sort_keys=True, allow_unicode=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Real microphone + Whisper hooks
# ---------------------------------------------------------------------------

def mic_recorder(device=None) -> Callable:
    def record(seconds: float, on_level: Callable[[float], None]):
        import numpy as np
        import sounddevice as sd

        frames: list = []
        last = [0.0]

        def cb(indata, _n, _t, _status):
            frames.append(indata[:, 0].copy())
            now = time.time()
            if now - last[0] > 0.1:
                last[0] = now
                on_level(float(np.sqrt(np.mean(indata[:, 0].astype(np.float32) ** 2))))

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=480, device=device, callback=cb):
            time.sleep(seconds)
        audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
        return audio.astype(np.float32) / 32768.0

    return record


def engine_transcriber(cfg, listener=None) -> Callable:
    """Transcribe with the same speech engine the live listener uses."""
    if listener is not None:
        return listener.transcribe
    from . import stt

    state = {"engine": None}

    def transcribe(samples) -> str:
        if state["engine"] is None:
            state["engine"] = stt.load(cfg["voice"], Path(cfg.data_dir) / "models")
        return state["engine"].transcribe(samples)

    return transcribe


def default_embedder(data_dir: Path):
    try:
        return speaker_id.SherpaEmbedder(speaker_id.ensure_model(Path(data_dir) / "models"))
    except Exception as exc:
        log.warning("speaker enrollment skipped: %s", exc)
        return None
