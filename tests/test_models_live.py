"""Real models, real audio. Opt-in (downloads ~100 MB): ASSISTANT_MODEL_TESTS=1.

CI runs this on Windows because sherpa-onnx has an open report of Parakeet
returning empty text on Windows 11 — the default engine must be proven there.
"""

import os
import wave
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("ASSISTANT_MODEL_TESTS") != "1", reason="set ASSISTANT_MODEL_TESTS=1")


@pytest.fixture(scope="module")
def models_dir():
    return Path(os.environ.get("ASSISTANT_MODELS_DIR") or Path(__file__).parent / ".models")


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def test_parakeet_transcribes_real_speech(models_dir):
    from assistant.voice import stt

    engine = stt.load({"stt_engine": "parakeet"}, models_dir)
    assert engine.name == "parakeet"
    wav = next((models_dir / "sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8" / "test_wavs").glob("0.wav"))
    text = engine.transcribe(read_wav(wav).astype(np.float32) / 32768).lower()
    assert "phoebe" in text and "portrait" in text, text


def test_silero_segments_real_speech(models_dir):
    from assistant.voice import vad

    seg = vad.make_segmenter({"vad": "silero"}, models_dir)
    assert seg.kind == "silero"
    speech = read_wav(next((models_dir / "sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8" / "test_wavs").glob("0.wav")))
    silence = np.zeros(16000, dtype=np.int16)
    pcm = np.concatenate([silence, speech, silence])
    out = [np.concatenate(r) for r in (seg.feed(pcm[i:i + 480]) for i in range(0, len(pcm) - 480, 480)) if r]
    assert out, "no speech found"
    total = sum(len(o) for o in out) / 16000
    assert 5.0 < total < len(pcm) / 16000, total  # the 7.4 s sentence, not the silence around it


OWW = "https://github.com/dscripka/openWakeWord"


def fetch(url: str, dest: Path) -> Path:
    import httpx

    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = httpx.get(url, follow_redirects=True, timeout=60)
        r.raise_for_status()
        dest.write_bytes(r.content)
    return dest


def test_wake_words_fire_only_on_their_own_phrase(models_dir):
    """openWakeWord's released 'alexa' / 'hey mycroft' models and its real test recordings.
    (Those models are CC BY-NC-SA: downloaded here as test fixtures only, never shipped.)"""
    from assistant.voice.wakeword import WakeWords

    d = models_dir / "oww-fixtures"
    models = {name: fetch(f"{OWW}/releases/download/v0.5.1/{name}_v0.1.onnx", d / f"{name}.onnx")
              for name in ("alexa", "hey_mycroft")}
    ww = WakeWords(models, threshold=0.5)
    for clip, expected in (("alexa_test.wav", "alexa"), ("hey_mycroft_test.wav", "hey_mycroft"), ("hey_jane.wav", None)):
        wav = fetch(f"{OWW}/raw/main/tests/data/{clip}", d / clip)
        pcm = np.concatenate([np.zeros(32000, np.int16), read_wav(wav), np.zeros(24000, np.int16)])
        ww.reset()
        hits = [h for i in range(0, len(pcm) - 480, 480) for h in ww.feed(pcm[i:i + 480])]
        assert [name for name, _ in hits] == ([expected] if expected else []), (clip, hits)
