"""Speech models, fetched once into ``data/models`` from the sherpa-onnx GitHub releases.

Everything here runs on sherpa-onnx (onnxruntime inside, no PyTorch), which the
voice profile already depends on: Silero VAD for "is someone talking",
Parakeet/Moonshine for speech-to-text, Supertonic/Kokoro for speaking. Smart Turn
(natural end of turn, voice/turn.py) comes from Hugging Face, pinned to one commit and checked by its hash.
Downloads are streamed to a ``.part`` file and only renamed into place once
complete, so a dropped connection never leaves a half-model that fails to load
forever.
"""

from __future__ import annotations

import logging
import shutil
import tarfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
COMPLETE = ".complete"
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass(frozen=True)
class ModelFile:
    name: str          # file (single model) or folder (archive) name under data/models
    url: str
    size_mb: int       # download size, for the progress message
    archive: bool = True
    sha256: str = ""   # checked after download when set (a file not from the sherpa-onnx releases)


MODELS = {
    "silero-vad": ModelFile("silero_vad.onnx", f"{RELEASES}/asr-models/silero_vad.onnx", 1, archive=False),
    "parakeet": ModelFile("sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8",
                          f"{RELEASES}/asr-models/sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8.tar.bz2", 100),
    "parakeet-large": ModelFile("sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
                                f"{RELEASES}/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2", 460),
    "moonshine": ModelFile("sherpa-onnx-moonshine-base-en-int8",
                           f"{RELEASES}/asr-models/sherpa-onnx-moonshine-base-en-int8.tar.bz2", 200),
    # speech output (assistant/voice/neural.py)
    "supertonic": ModelFile("sherpa-onnx-supertonic-tts-int8-2026-03-06",
                            f"{RELEASES}/tts-models/sherpa-onnx-supertonic-tts-int8-2026-03-06.tar.bz2", 85),
    "kokoro": ModelFile("kokoro-en-v0_19", f"{RELEASES}/tts-models/kokoro-en-v0_19.tar.bz2", 320),
    # end of turn (voice/turn.py): pipecat-ai/smart-turn v3.2, CPU int8, BSD-2-Clause; 8,679,182 bytes
    "smart-turn": ModelFile("smart-turn-v3.2-cpu.onnx",
                            "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/f766f81d3cfdf7737ac64aad813d91bbfd56bf93/"
                            "smart-turn-v3.2-cpu.onnx", 9, archive=False,
                            sha256="2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"),
}


def model_path(key: str, models_dir: Path) -> Path:
    return Path(models_dir) / MODELS[key].name


def is_ready(key: str, models_dir: Path) -> bool:
    path = model_path(key, models_dir)
    if MODELS[key].archive:
        return (path / COMPLETE).exists()
    return path.exists() and path.stat().st_size > 0


def ensure(key: str, models_dir: Path, on_progress: Callable[[float], None] | None = None) -> Path:
    """Path to the model, downloading and unpacking it first if needed.

    One download per model at a time: the health check or a voice switch asking for a model
    that's still downloading waits for it instead of writing the same ``.part`` file."""
    models_dir = Path(models_dir)
    if is_ready(key, models_dir):
        return models_dir / MODELS[key].name
    with _locks_guard:
        lock = _locks.setdefault(f"{models_dir.resolve()}/{key}", threading.Lock())
    with lock:
        return _download(key, models_dir, on_progress)


def _download(key: str, models_dir: Path, on_progress: Callable[[float], None] | None) -> Path:
    spec = MODELS[key]
    target = models_dir / spec.name
    if is_ready(key, models_dir):  # someone else finished it while we waited
        return target
    import httpx

    models_dir.mkdir(parents=True, exist_ok=True)
    part = models_dir / (spec.name + (".tar.bz2" if spec.archive else "") + ".part")
    log.info("downloading %s (~%d MB)", spec.name, spec.size_mb)
    with httpx.stream("GET", spec.url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0) or spec.size_mb * 1_000_000
        got = 0
        with part.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 16):
                fh.write(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(min(got / total, 1.0))
    if spec.sha256 and _sha256(part) != spec.sha256:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{spec.name}: the download doesn't match its published hash; try again later")
    if not spec.archive:
        part.replace(target)
        return target
    staging = models_dir / (spec.name + ".unpack")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        with tarfile.open(part, "r:bz2") as tar:
            _safe_extract(tar, staging)
        inner = staging / spec.name if (staging / spec.name).is_dir() else staging
        shutil.rmtree(target, ignore_errors=True)
        inner.replace(target)
    finally:  # a bad archive must not leave 100 MB of leftovers behind
        shutil.rmtree(staging, ignore_errors=True)
        part.unlink(missing_ok=True)
    (target / COMPLETE).write_text("ok")
    return target


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    try:
        tar.extractall(dest, filter="data")  # refuses absolute paths, "..", links outside dest
    except TypeError:  # Python without extraction filters
        root = dest.resolve()
        for member in tar.getmembers():
            if not (root / member.name).resolve().is_relative_to(root) or member.issym() or member.islnk():
                raise RuntimeError(f"unsafe path in model archive: {member.name}") from None
        tar.extractall(dest)
