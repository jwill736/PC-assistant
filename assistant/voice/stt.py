"""Speech-to-text engines behind one ``transcribe(samples, hints) -> str`` call.

Measured on 80 synthetic commands (3 voices; clean and with a second voice
talking underneath at -10 dB), 2 CPU threads, wake word checked with the
app's own matcher:

=====================  ==========  =============  ==============  =========
engine                 WER clean   WER + chatter  wake (clean/noisy)  latency
=====================  ==========  =============  ==============  =========
parakeet (110M)         8.2 %       22.5 %        16/20 · 12/20      64 ms
moonshine (base)       13.2 %       60.4 %        15/20 ·  8/20      62 ms
parakeet-large (0.6B)   9.3 %       72.5 %        16/20 ·  5/20     219 ms
whisper base.en        16.5 %       36.3 %         8/20 ·  9/20     273 ms
=====================  ==========  =============  ==============  =========

So ``auto`` picks Parakeet 110M. ``whisper`` (faster-whisper) stays for people
who already run it on CUDA.

Names: the hint words (scenes, apps, the game, chatters, people, words you taught it) bias recognition. Whisper
takes them as a prompt; Parakeet as hotwords (beam search, ``voice.hotwords_score``, 0 turns it off). On 34
synthetic commands full of names (Balatro, Zentreya, Siobhan…) that cut word errors from 22.9% to 14.9% and
exact transcripts from 9 to 16, with no change on 36 everyday commands and ~12 ms more per sentence.
Run the voice test (Setup → Voice test) to measure both on your own voice and PC.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from . import models

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
ENGINES = ("parakeet", "parakeet-large", "moonshine", "whisper")
LABELS = {"parakeet": "Parakeet 110M", "parakeet-large": "Parakeet 0.6B", "moonshine": "Moonshine base",
          "whisper": "Whisper"}


MAX_HOTWORDS = 60


def hotword_list(hints) -> list[str]:
    """Hint words as Parakeet hotwords: lower case (its word pieces are; title case measured worse), letters only,
    3+ letters, no repeats."""
    import re

    out: list[str] = []
    for hint in hints:
        phrase = " ".join(re.sub(r"[^a-z' ]+", " ", str(hint).lower()).split())
        if len(phrase.replace(" ", "")) >= 3 and phrase not in out:
            out.append(phrase)
    return out[:MAX_HOTWORDS]


def recase(text: str, hints) -> str:
    """Hotword hits come out lower case ("balatro"): give them the hint's own spelling back ("Balatro")."""
    import re

    for hint in hints:
        hint = str(hint).strip()
        if hint != hint.lower() and re.fullmatch(r"[A-Za-z' ]{3,}", hint):
            text = re.sub(rf"(?<![\w']){re.escape(hint)}(?![\w'])", hint, text, flags=re.IGNORECASE)
    return text


def _bpe_vocab(model_dir: Path) -> Path:
    """sherpa-onnx encodes hotwords with a sentencepiece vocab, which the Parakeet download doesn't ship: build one
    from tokens.txt. Every piece scores -1, so a word splits into the fewest pieces (all-equal scores split names
    into stray letters and made things worse)."""
    path = Path(model_dir) / "bpe.vocab"
    if not path.exists():
        lines = []
        for line in (Path(model_dir) / "tokens.txt").read_text(encoding="utf-8").splitlines():
            token = line.rsplit(" ", 1)[0]
            if token and not token.startswith("<"):
                lines.append(f"{token}\t-1")
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(path)
    return path


class SherpaSTT:
    """Parakeet / Moonshine through sherpa-onnx (onnxruntime, CPU, no PyTorch)."""

    def __init__(self, engine: str, model_dir: Path, threads: int = 2, hotwords_score: float = 0.0):
        import sherpa_onnx

        self.name = engine
        self.hotwords = False   # Parakeet with beam search and hotwords
        self.hotwords_error = ""
        self._last_hints = 0
        d = Path(model_dir)
        if engine.startswith("parakeet"):
            common = dict(encoder=str(d / "encoder.int8.onnx"), decoder=str(d / "decoder.int8.onnx"),
                          joiner=str(d / "joiner.int8.onnx"), tokens=str(d / "tokens.txt"), num_threads=threads,
                          model_type="nemo_transducer")
            if hotwords_score > 0:
                try:
                    self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(
                        **common, decoding_method="modified_beam_search", modeling_unit="bpe",
                        bpe_vocab=str(_bpe_vocab(d)), hotwords_score=float(hotwords_score))
                    self.hotwords = True
                except Exception as exc:  # an older sherpa-onnx, a read-only folder: names unbiased, still works
                    self.hotwords_error = f"{type(exc).__name__}: {exc}"
                    log.warning("speech hotwords unavailable (%s); names won't be biased", self.hotwords_error)
            if not self.hotwords:
                self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(**common)
        elif engine == "moonshine":
            self.rec = sherpa_onnx.OfflineRecognizer.from_moonshine(
                preprocessor=str(d / "preprocess.onnx"), encoder=str(d / "encode.int8.onnx"),
                uncached_decoder=str(d / "uncached_decode.int8.onnx"), cached_decoder=str(d / "cached_decode.int8.onnx"),
                tokens=str(d / "tokens.txt"), num_threads=threads)
        else:
            raise ValueError(f"not a sherpa engine: {engine}")

    def transcribe(self, samples, hints=()) -> str:
        words = hotword_list(hints) if self.hotwords else []
        self._last_hints = len(words)
        stream = self.rec.create_stream(hotwords="/".join(words)) if words else self.rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self.rec.decode_stream(stream)
        text = stream.result.text.strip()
        return recase(text, hints) if words else text

    def hotwords_status(self) -> dict | None:
        if not self.name.startswith("parakeet"):
            return None
        return {"on": self.hotwords, "words": self._last_hints, "error": self.hotwords_error}


class WhisperSTT:
    """faster-whisper (CTranslate2). Slower on CPU; takes vocabulary hints as a prompt."""

    name = "whisper"

    def __init__(self, model: str = "base.en", device: str = "auto", loaded=None):
        if loaded is None:
            from faster_whisper import WhisperModel

            loaded = WhisperModel(model, device=device, compute_type="int8" if device in ("cpu", "auto") else "float16")
        self.model = loaded

    def transcribe(self, samples, hints=()) -> str:
        segments, _ = self.model.transcribe(samples, language="en", beam_size=1, vad_filter=True,
                                            condition_on_previous_text=False,
                                            initial_prompt=", ".join(list(hints)[:40]) or None)
        return " ".join(s.text.strip() for s in segments if s.text.strip())


def available() -> dict[str, bool]:
    def has(mod: str) -> bool:
        try:
            __import__(mod)
            return True
        except ImportError:
            return False

    sherpa = has("sherpa_onnx")
    return {"parakeet": sherpa, "parakeet-large": sherpa, "moonshine": sherpa, "whisper": has("faster_whisper")}


def resolve(cfg: dict) -> str:
    """Engine to use: ``voice.stt_engine`` if installed, else the best installed one."""
    want = cfg.get("stt_engine", "auto")
    have = available()
    if want in ENGINES and have.get(want):
        return want
    if want not in ("auto", None) and want not in ENGINES:
        log.warning("unknown voice.stt_engine %r; choosing automatically", want)
    elif want != "auto":
        log.warning("voice.stt_engine %r is not installed; choosing automatically", want)
    for engine in ("parakeet", "whisper"):
        if have[engine]:
            return engine
    raise RuntimeError("No speech-to-text engine installed: pip install -r requirements-voice.txt")


def load(cfg: dict, models_dir: Path, on_progress: Callable[[float], None] | None = None):
    engine = resolve(cfg)
    if engine == "whisper":
        return WhisperSTT(cfg.get("stt_model", "base.en"), cfg.get("stt_device", "auto"))
    path = models.ensure(engine, models_dir, on_progress)
    return SherpaSTT(engine, path, threads=int(cfg.get("stt_threads", 2)),
                     hotwords_score=float(cfg.get("hotwords_score", 2.0) or 0))
