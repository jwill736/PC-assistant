"""Acoustic wake words and hard triggers (livekit-wakeword / openWakeWord ONNX models).

Drop trained models into ``data/models/wakewords/``:

* ``vesper.onnx`` (the assistant's name, lower-case) - the wake word. The
  listener then only runs speech-to-text after it fires, and only when it fired
  at the *start* of the utterance, so saying the name to chat doesn't wake it.
* ``stop.onnx`` - interrupts a spoken reply.
* anything else, e.g. ``clip_that.onnx`` - runs that phrase as a command the
  moment it's heard, no speech-to-text ("clip that").

Train them on Colab with ``training/wake_words.ipynb``.

livekit-wakeword's own ``WakeWordModel.predict`` recomputes 2 s of features on
every call (~25 ms on a slow CPU, i.e. ~30% of a core at its 80 ms cadence).
This streams them instead, like openWakeWord: each 80 ms hop adds 8 mel frames
and one embedding, so the per-hop cost is one small CNN call plus the classifiers.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
HOP = 1280            # 80 ms: 8 mel frames
MEL_CONTEXT = 480     # mel model returns n/160 - 3 frames, so 1280 + 480 samples -> exactly 8
EMB_WINDOW = 76       # mel frames per embedding
N_EMB = 16            # embeddings per classifier input (~1.3 s)
FOLDER = "wakewords"


def available() -> bool:
    try:
        import livekit.wakeword  # noqa: F401  (bundles the mel + embedding models)
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def model_files(models_dir: Path) -> dict[str, Path]:
    folder = Path(models_dir) / FOLDER
    return {p.stem.lower(): p for p in sorted(folder.glob("*.onnx"), key=lambda p: p.stem.lower())} if folder.is_dir() else {}


class WakeWords:
    """Feed 16 kHz int16 frames; get (name, score) hits back."""

    def __init__(self, models: dict[str, Path], threshold: float = 0.5, thresholds: dict | None = None,
                 refractory_s: float = 1.5, sessions=None, clock=time.monotonic):
        import numpy as np

        self._np = np
        self.threshold = threshold
        self.thresholds = {k.lower(): float(v) for k, v in (thresholds or {}).items()}
        self.refractory_s = refractory_s
        self.clock = clock
        if sessions is None:
            sessions = self._load(models)
        self._mel, self._emb, self.classifiers = sessions
        self.last_scores: dict[str, float] = {}
        self.peak: dict[str, float] = {}
        self._last_hit: dict[str, float] = {}
        self.reset()

    @staticmethod
    def _load(models: dict[str, Path]):
        import onnxruntime as ort
        from livekit.wakeword.resources import get_embedding_model_path, get_mel_model_path

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1

        def session(path):
            s = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
            return s, s.get_inputs()[0].name

        classifiers = {}
        for name, path in models.items():
            classifiers[name] = session(path)
            log.info("wake word model '%s' loaded", name)
        return session(get_mel_model_path()), session(get_embedding_model_path()), classifiers

    @property
    def names(self) -> list[str]:
        return list(self.classifiers)

    def reset(self) -> None:
        np = self._np
        self._audio = np.zeros(0, dtype=np.float32)   # samples not yet turned into mel frames
        self._context = np.zeros(MEL_CONTEXT, dtype=np.float32)
        self._mels = np.zeros((0, 32), dtype=np.float32)
        self._embs: deque = deque(maxlen=N_EMB)

    def feed(self, frame) -> list[tuple[str, float]]:
        np = self._np
        frame = np.asarray(frame)
        if frame.dtype == np.int16:
            frame = frame.astype(np.float32) / 32768.0
        self._audio = np.concatenate([self._audio, frame.astype(np.float32)])
        hits = []
        while len(self._audio) >= HOP:
            chunk, self._audio = self._audio[:HOP], self._audio[HOP:]
            hits += self._hop(chunk)
        return hits

    def _hop(self, chunk) -> list[tuple[str, float]]:
        np = self._np
        (mel, mel_in), (emb, emb_in) = self._mel, self._emb
        audio = np.concatenate([self._context, chunk])
        self._context = audio[-MEL_CONTEXT:]
        frames = mel.run(None, {mel_in: audio[np.newaxis, :]})[0]
        frames = frames.reshape(-1, 32) / 10.0 + 2.0          # livekit's post-processing
        self._mels = np.concatenate([self._mels, frames])[-EMB_WINDOW:]
        if len(self._mels) < EMB_WINDOW:
            return []
        e = emb.run(None, {emb_in: self._mels[np.newaxis, :, :, np.newaxis].astype(np.float32)})[0]
        self._embs.append(e.reshape(96))
        if len(self._embs) < N_EMB:
            return []
        x = np.stack(self._embs)[np.newaxis].astype(np.float32)
        now = self.clock()
        hits = []
        for name, (session, inp) in self.classifiers.items():
            score = float(session.run(None, {inp: x})[0].reshape(-1)[0])
            self.last_scores[name] = score
            self.peak[name] = max(score, self.peak.get(name, 0.0))
            if score >= self.thresholds.get(name, self.threshold) and now - self._last_hit.get(name, -1e9) >= self.refractory_s:
                self._last_hit[name] = now
                hits.append((name, round(score, 3)))
        return hits

    def status(self) -> dict:
        return {"models": self.names, "threshold": self.threshold, "thresholds": self.thresholds,
                "peak": {k: round(v, 3) for k, v in self.peak.items()}}


def saved_thresholds(models_dir: Path) -> dict[str, float]:
    """Per-model thresholds saved next to the models (vesper.json: {"threshold": 0.68}),
    as recommended by the training notebook's evaluation."""
    import json

    out = {}
    for name, path in model_files(models_dir).items():
        side = path.with_suffix(".json")
        try:
            out[name] = float(json.loads(side.read_text(encoding="utf-8"))["threshold"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def load(cfg: dict, models_dir: Path) -> WakeWords | None:
    """The loaded models, or None when there are none (or the runtime isn't installed)."""
    files = model_files(models_dir)
    if not files:
        return None
    if not available():
        log.warning("wake word models found but livekit-wakeword isn't installed: pip install -r requirements-voice.txt")
        return None
    thresholds = {**saved_thresholds(models_dir), **{k.lower(): v for k, v in (cfg.get("wake_thresholds") or {}).items()}}
    return WakeWords(files, threshold=cfg.get("wake_threshold", 0.5), thresholds=thresholds,
                     refractory_s=cfg.get("wake_refractory_s", 1.5))
