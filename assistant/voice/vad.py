"""Speech segmentation: turn a stream of 30 ms mic frames into utterances.

``SileroSegmenter`` uses Silero VAD (via sherpa-onnx, <1 ms per frame on CPU).
Measured against the old energy gate on a stream of commands with game-style
sound effects between them, it produced 0 false segments (energy: 1–4) and
handed each utterance over ~0.4 s after speech ended instead of ~2.2 s,
because loud non-speech right after a command no longer gets glued onto it.

``EnergySegmenter`` (in listener.py) stays as the fallback when sherpa-onnx
or the model is unavailable.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class SileroSegmenter:
    """Same contract as EnergySegmenter: ``feed(int16 frame)`` returns the finished
    utterance as a list of int16 arrays when speech ends, else None."""

    kind = "silero"

    def __init__(self, model_path: Path, threshold: float = 0.6, endpoint_ms: int = 400, min_speech_ms: int = 250,
                 max_utterance_s: float = 15, preroll_ms: int = 300, vad=None):
        import numpy as np

        self._np = np
        self.preroll = int(preroll_ms * SAMPLE_RATE / 1000)
        self.keep = int((max_utterance_s + 2) * SAMPLE_RATE)
        if vad is None:
            import sherpa_onnx

            cfg = sherpa_onnx.VadModelConfig(
                silero_vad=sherpa_onnx.SileroVadModelConfig(
                    model=str(model_path), threshold=threshold, min_silence_duration=endpoint_ms / 1000,
                    min_speech_duration=min_speech_ms / 1000, window_size=512, max_speech_duration=max_utterance_s),
                sample_rate=SAMPLE_RATE, num_threads=1)
            vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=max_utterance_s + 5)
        self.vad = vad
        self.reset()

    def reset(self) -> None:
        self.vad.reset()
        self._buf = self._np.zeros(0, dtype=self._np.int16)
        self._buf_start = 0   # absolute sample index of _buf[0]
        self._fed = 0         # absolute samples fed since reset (the VAD's own clock)

    @property
    def in_speech(self) -> bool:
        return bool(self.vad.is_speech_detected())

    def feed(self, frame) -> list | None:
        np = self._np
        frame = np.asarray(frame, dtype=np.int16)
        self._buf = np.concatenate([self._buf, frame])
        self._fed += len(frame)
        if len(self._buf) > self.keep:
            drop = len(self._buf) - self.keep
            self._buf = self._buf[drop:]
            self._buf_start += drop
        self.vad.accept_waveform(frame.astype(np.float32) / 32768.0)
        out = None
        while not self.vad.empty():
            seg = self.vad.front
            start, n = int(seg.start), len(seg.samples)
            self.vad.pop()
            a = max(start - self.preroll, self._buf_start) - self._buf_start
            b = min(start + n, self._fed) - self._buf_start
            if b > a:
                out = [self._buf[a:b].copy()]  # keep the latest if two finish in one frame
        return out


def make_segmenter(cfg: dict, models_dir: Path, on_progress=None):
    """Silero when possible (``voice.vad: auto|silero``), else the energy gate."""
    from . import models
    from .listener import EnergySegmenter

    choice = cfg.get("vad", "auto")
    energy = lambda: EnergySegmenter(cfg.get("min_rms", 350), cfg.get("silence_ms", 800), cfg.get("max_utterance_s", 15))  # noqa: E731
    if choice == "energy":
        return energy()
    try:
        path = models.ensure("silero-vad", models_dir, on_progress)
        return SileroSegmenter(path, threshold=cfg.get("vad_threshold", 0.6), endpoint_ms=cfg.get("endpoint_ms", 400),
                               max_utterance_s=cfg.get("max_utterance_s", 15))
    except Exception as exc:
        if choice == "silero":
            raise
        log.warning("Silero VAD unavailable (%s); using the energy gate", exc)
        return energy()
