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
