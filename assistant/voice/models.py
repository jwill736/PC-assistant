"""Speech models, fetched once into ``data/models`` from the sherpa-onnx GitHub releases.

Everything here runs on sherpa-onnx (onnxruntime inside, no PyTorch), which the
voice profile already depends on: Silero VAD for "is someone talking",
Parakeet/Moonshine for speech-to-text. Downloads are streamed to a ``.part``
file and only renamed into place once complete, so a dropped connection never
leaves a half-model that fails to load forever.
"""

from __future__ import annotations

import logging
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
COMPLETE = ".complete"


@dataclass(frozen=True)
class ModelFile:
    name: str          # file (single model) or folder (archive) name under data/models
    url: str
    size_mb: int       # download size, for the progress message
    archive: bool = True


MODELS = {
    "silero-vad": ModelFile("silero_vad.onnx", f"{RELEASES}/asr-models/silero_vad.onnx", 1, archive=False),
    "parakeet": ModelFile("sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8",
                          f"{RELEASES}/asr-models/sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8.tar.bz2", 100),
    "parakeet-large": ModelFile("sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
                                f"{RELEASES}/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2", 460),
    "moonshine": ModelFile("sherpa-onnx-moonshine-base-en-int8",
                           f"{RELEASES}/asr-models/sherpa-onnx-moonshine-base-en-int8.tar.bz2", 200),
}


def model_path(key: str, models_dir: Path) -> Path:
    return Path(models_dir) / MODELS[key].name


def is_ready(key: str, models_dir: Path) -> bool:
    path = model_path(key, models_dir)
    if MODELS[key].archive:
        return (path / COMPLETE).exists()
    return path.exists() and path.stat().st_size > 0


def ensure(key: str, models_dir: Path, on_progress: Callable[[float], None] | None = None) -> Path:
    """Path to the model, downloading and unpacking it first if needed."""
    spec = MODELS[key]
    models_dir = Path(models_dir)
    target = models_dir / spec.name
    if is_ready(key, models_dir):
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


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    try:
        tar.extractall(dest, filter="data")  # refuses absolute paths, "..", links outside dest
    except TypeError:  # Python without extraction filters
        root = dest.resolve()
        for member in tar.getmembers():
            if not (root / member.name).resolve().is_relative_to(root) or member.issym() or member.islnk():
                raise RuntimeError(f"unsafe path in model archive: {member.name}") from None
        tar.extractall(dest)
