"""Only answer the owner's voice.

After the wake word fires, the utterance is turned into a 256-number voice
"fingerprint" (WeSpeaker ResNet34 via sherpa-onnx: ~26 MB ONNX, CPU-only,
~50 ms per clip, CC-BY-4.0) and compared with the fingerprints recorded during
calibration. Strangers — Discord friends, stream audio, the TV — are ignored.

This is a convenience filter, not security: a recording of the owner's voice
can pass it. Risky actions keep their spoken "yes" regardless of the score.
The profile is biometric-derived, so it stays on this PC (encrypted with
Windows DPAPI when available) and can be deleted from the Setup tab.
"""

from __future__ import annotations

import base64
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_NAME = "wespeaker_en_voxceleb_resnet34_LM"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
             f"speaker-recongition-models/{MODEL_NAME}.onnx")  # (sic) upstream tag spelling
PROFILE_FILE = "voice_profile.json"
# Cosine thresholds for this model live around 0.4-0.6; never auto-tune outside this band.
MIN_THRESHOLD, MAX_THRESHOLD = 0.35, 0.72
SHORTEST_CLIP_S = 0.8  # below this the fingerprint is too noisy to judge
SAMPLE_RATE = 16000


def _np():
    import numpy as np

    return np


def cosine(a, b) -> float:
    np = _np()
    a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b)) or 1e-9
    return float(a @ b / denom)


# ---------------------------------------------------------------------------
# Embedding model
# ---------------------------------------------------------------------------

def ensure_model(models_dir: Path, url: str = MODEL_URL) -> Path:
    """Download the speaker model once (≈26 MB) into data/models/."""
    import httpx

    models_dir.mkdir(parents=True, exist_ok=True)
    path = models_dir / f"{MODEL_NAME}.onnx"
    if path.exists() and path.stat().st_size > 1_000_000:
        return path
    tmp = path.with_suffix(".part")
    log.info("downloading speaker model (%s)…", url)
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 16):
                fh.write(chunk)
    if tmp.stat().st_size < 1_000_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("speaker model download was truncated")
    tmp.replace(path)
    return path


class SherpaEmbedder:
    """sherpa-onnx speaker embedding extractor (onnxruntime only — no PyTorch)."""

    def __init__(self, model_path: Path, threads: int = 1):
        import sherpa_onnx

        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model_path), num_threads=threads, provider="cpu")
        if not cfg.validate():
            raise RuntimeError(f"invalid speaker model config: {model_path}")
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.name = MODEL_NAME

    def embed(self, samples) -> list[float]:
        np = _np()
        stream = self._ex.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=np.asarray(samples, dtype=np.float32))
        stream.input_finished()
        vec = np.asarray(self._ex.compute(stream), dtype=np.float32)
        return (vec / (np.linalg.norm(vec) or 1.0)).tolist()


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@dataclass
class VoiceProfile:
    embeddings: list[list[float]]
    threshold: float
    model: str = MODEL_NAME
    created: float = field(default_factory=time.time)
    owner_scores: list[float] = field(default_factory=list)

    def centroid(self) -> list[float]:
        np = _np()
        c = np.mean(np.asarray(self.embeddings, dtype=np.float32), axis=0)
        return (c / (np.linalg.norm(c) or 1.0)).tolist()

    def score(self, embedding) -> float:
        """Blend of similarity to the average voice and to the closest single take."""
        best = max(cosine(embedding, e) for e in self.embeddings)
        return round(0.5 * cosine(embedding, self.centroid()) + 0.5 * best, 4)

    def summary(self) -> dict:
        return {"enrolled": True, "clips": len(self.embeddings), "threshold": self.threshold, "model": self.model,
                "created": self.created,
                "owner_score_min": min(self.owner_scores) if self.owner_scores else None}


def build_profile(embeddings: list[list[float]], model: str = MODEL_NAME) -> VoiceProfile:
    """Tune the threshold to this user: score each take against the others (leave-one-out)."""
    if len(embeddings) < 3:
        raise ValueError("need at least 3 voice samples to enroll")
    scores = []
    for i, emb in enumerate(embeddings):
        others = VoiceProfile(embeddings[:i] + embeddings[i + 1:], 0.0, model)
        scores.append(others.score(emb))
    scores.sort()
    # Sit just under the owner's weaker takes (10th percentile), inside the model's sane band.
    floor = scores[max(0, int(len(scores) * 0.1) - 1)] if len(scores) >= 10 else scores[0]
    threshold = round(min(MAX_THRESHOLD, max(MIN_THRESHOLD, floor - 0.08)), 3)
    return VoiceProfile(embeddings, threshold, model, owner_scores=[round(s, 4) for s in scores])


def _protect(data: bytes) -> tuple[bytes, bool]:
    if sys.platform == "win32":
        try:
            import win32crypt

            return win32crypt.CryptProtectData(data, "pc-assistant voice profile", None, None, None, 0), True
        except Exception:  # pywin32 missing: fall back to plain file in the private data folder
            log.warning("pywin32 unavailable; voice profile stored unencrypted")
    return data, False


def _unprotect(data: bytes, protected: bool) -> bytes:
    if not protected:
        return data
    import win32crypt

    return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]


def save_profile(profile: VoiceProfile, data_dir: Path) -> Path:
    blob, protected = _protect(json.dumps(asdict(profile)).encode("utf-8"))
    path = Path(data_dir) / PROFILE_FILE
    path.write_text(json.dumps({"protected": protected, "data": base64.b64encode(blob).decode("ascii")}), encoding="utf-8")
    return path


def load_profile(data_dir: Path) -> VoiceProfile | None:
    path = Path(data_dir) / PROFILE_FILE
    if not path.exists():
        return None
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        raw = _unprotect(base64.b64decode(wrapper["data"]), wrapper.get("protected", False))
        return VoiceProfile(**json.loads(raw))
    except Exception:
        log.exception("voice profile unreadable; re-run calibration")
        return None


def delete_profile(data_dir: Path) -> bool:
    path = Path(data_dir) / PROFILE_FILE
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


# ---------------------------------------------------------------------------
# Runtime check
# ---------------------------------------------------------------------------

class SpeakerVerifier:
    """mode: off (never check) | log (score but never block) | strict (ignore strangers)."""

    MODES = ("off", "log", "strict")

    def __init__(self, data_dir: Path, mode: str = "strict", embedder=None):
        self.data_dir = Path(data_dir)
        self.mode = mode if mode in self.MODES else "strict"
        self.profile = load_profile(self.data_dir)
        self._embedder = embedder
        self._embedder_error = ""
        self.last_score: float | None = None

    @property
    def active(self) -> bool:
        return self.mode != "off" and self.profile is not None

    def embedder(self):
        if self._embedder is None and not self._embedder_error:
            try:
                self._embedder = SherpaEmbedder(ensure_model(self.data_dir / "models"))
            except Exception as exc:
                self._embedder_error = f"{type(exc).__name__}: {exc}"
                log.warning("speaker check unavailable: %s", self._embedder_error)
        return self._embedder

    def reload(self) -> None:
        self.profile = load_profile(self.data_dir)

    def check(self, samples) -> tuple[bool, float | None]:
        """(accepted, score). Unknown/short/unavailable → accepted, so it never locks the owner out."""
        if not self.active or len(samples) < SHORTEST_CLIP_S * SAMPLE_RATE:
            return True, None
        emb = self.embedder()
        if emb is None:
            return True, None
        score = self.profile.score(emb.embed(samples))
        self.last_score = score
        return (score >= self.profile.threshold or self.mode == "log"), score

    def status(self) -> dict:
        base = self.profile.summary() if self.profile else {"enrolled": False}
        return {**base, "mode": self.mode, "last_score": self.last_score, "error": self._embedder_error}
