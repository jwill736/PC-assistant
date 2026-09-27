import threading
import time

import numpy as np
import yaml
from test_speaker import voice

from assistant.bus import EventBus
from assistant.config import load_config
from assistant.runtime import Runtime
from assistant.services import build_services
from assistant.storage import Storage
from assistant.voice import calibrate as calib
from assistant.voice import speaker_id

SR = 16000


def silence(seconds):
    return np.zeros(int(SR * seconds), dtype=np.float32)


def room(seconds, level=0.002):
    return (np.random.default_rng(0).normal(size=int(SR * seconds)) * level).astype(np.float32)


def speech(seconds, lead=0.0):
    t = np.arange(int(SR * seconds)) / SR
    tone = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    return np.concatenate([silence(lead), tone, silence(lead)])


class ListBus:
    def __init__(self):
        self.events = []

    def publish(self, type_, data, sticky=False):
        self.events.append((type_, data, sticky))

    def steps(self):
        return [d.get("step") for t, d, _ in self.events if t == "calibration"]


class FakeEmbedder:
    name = "fake"

    def __init__(self):
        self.pool = voice(1, n=20)

    def embed(self, _samples):
        return self.pool.pop(0)


def recorder(noise=None, wake=None, read=None):
    """Hands back room noise first, then a word per wake take, then read-aloud audio."""
    calls = {"n": 0}

    def record(seconds, on_level):
        calls["n"] += 1
        on_level(123.0)
        if calls["n"] == 1:
            return noise if noise is not None else room(seconds)
        if calls["n"] <= 6:
            return wake if wake is not None else speech(0.7, lead=0.5)
        return read if read is not None else speech(seconds)

    return record


def transcriber(texts):
    it = iter(texts)
    return lambda _samples: next(it)


def make(cfg, bus=None, **kw):
    kw.setdefault("record", recorder())
    kw.setdefault("transcribe", transcriber(["Freddy?", "Friday.", "Fried day", "Hey Friday", "Friday's"]))
    kw.setdefault("embedder", FakeEmbedder())
    return calib.Calibrator(cfg, bus if bus is not None else ListBus(), read_seconds=4, prompt_pause=0, **kw)


def test_wake_variants_keep_near_misses_only():
    heard = ["Travis.", "Jarvis's", "Hey Jarvis", "Jervis?", "service", "the day"]
    assert calib.wake_variants(heard, "Jarvis", ["jarvis", "hey jarvis"]) == ["travis", "jervis"]
    assert calib.wake_variants(["fri day"], "Friday", []) == []  # fragments never become wake words
    assert calib.wake_variants(["Desperate, open discord", "Fesper"], "Vesper", []) == ["fesper"]  # nor longer real words


def test_frame_rms_and_trim():
    x = np.concatenate([silence(1), speech(1), silence(1)])
    levels = calib.frame_rms(x)
    assert max(levels) > 4000 and min(levels) == 0
    trimmed = calib.trim(x, floor=500)
    assert SR <= len(trimmed) <= SR * 1.7
    assert len(calib.trim(silence(1), floor=500)) == 0


def test_full_calibration_writes_layer_and_profile(cfg):
    bus = ListBus()
    cal = make(cfg, bus)
    result = cal.run()
    assert 200 <= result["min_rms"] <= 3000
    assert result["wake_variants"] == ["freddy", "fried"]
    assert result["wake_takes"] == 5 and result["speech_chunks"] == 3
    assert result["profile"]["clips"] == 8
    assert speaker_id.load_profile(cfg.data_dir) is not None

    layer = yaml.safe_load((cfg.data_dir / calib.CALIBRATION_FILE).read_text())
    assert layer == {"assistant": {"wake_words": ["freddy", "fried"]},
                     "voice": {"min_rms": result["min_rms"], "speaker_check": "strict"}}

    steps = bus.steps()
    assert steps[0] == "noise" and "wake" in steps and "speech" in steps and "enroll" in steps
    assert steps[-1] == "done"
    assert bus.events[-1][2] is True  # the final result is sticky for a HUD that reconnects
    assert any(d.get("level") == 123 for _, d, _ in bus.events)


def test_not_enough_speech_skips_enrollment_but_keeps_mic_level(cfg):
    cal = make(cfg, record=recorder(noise=silence(1), wake=silence(2.5), read=silence(4)),
               transcribe=transcriber([]))
    result = cal.run()
    assert result["min_rms"] == 200
    assert "profile" not in result and "not enough clear speech" in result["profile_error"]
    assert speaker_id.load_profile(cfg.data_dir) is None
    layer = yaml.safe_load((cfg.data_dir / calib.CALIBRATION_FILE).read_text())
    assert layer == {"voice": {"min_rms": 200}}


def test_cancel_and_error(cfg):
    bus = ListBus()
    cal = make(cfg, bus)
    cal.cancelled.set()
    assert cal.run() == {"cancelled": True}
    assert bus.steps() == ["cancelled"]
    assert not (cfg.data_dir / calib.CALIBRATION_FILE).exists()

    def broken(_seconds, _on_level):
        raise RuntimeError("no microphone")

    bus = ListBus()
    result = make(cfg, bus, record=broken).run()
    assert "no microphone" in result["error"]
    assert bus.steps()[-1] == "error"


def write_config(tmp_path, extra=""):
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {tmp_path / 'data'}\nassistant:\n  name: Friday\n  wake_words: [fri]\n"
                    f"voice:\n  enabled: false\n{extra}", encoding="utf-8")
    return path


def test_calibration_layer_merges_under_user_config(tmp_path):
    path = write_config(tmp_path)
    (tmp_path / "data").mkdir()
    calib.write_layer({"voice": {"min_rms": 777, "speaker_check": "strict"},
                       "assistant": {"wake_words": ["freddy"]}}, tmp_path / "data")
    cfg = load_config(path)
    assert cfg["assistant"]["wake_words"] == ["friday", "hey friday", "fri", "freddy"]
    assert cfg["voice"]["min_rms"] == 777

    path = write_config(tmp_path, "  min_rms: 1500\n  speaker_check: log\n")
    cfg = load_config(path)
    assert cfg["voice"]["min_rms"] == 1500 and cfg["voice"]["speaker_check"] == "log"  # the user's own settings win


class FakeListener:
    def __init__(self, wake_words):
        self.paused = False
        self.paused_during_run = None
        self.wake_words = wake_words
        self.cfg = {}
        self._model = None
        self.restarts = 0

    def restart(self):
        self.restarts += 1

    def status(self):
        return {"engine": "Parakeet 110M", "vad": "silero", "latency_ms": 64}


class FakeCalibrator:
    def __init__(self, data_dir, listener):
        self.cancelled = threading.Event()
        self.release = threading.Event()  # the test decides when the "recording" ends
        self.data_dir, self.listener = data_dir, listener

    def run(self):
        self.listener.paused_during_run = self.listener.paused
        assert self.release.wait(5)
        calib.write_layer({"voice": {"min_rms": 1234, "speaker_check": "log"},
                           "assistant": {"wake_words": ["freddy"]}}, self.data_dir)
        return {"min_rms": 1234}


def test_runtime_applies_calibration_live(tmp_path):
    cfg = load_config(write_config(tmp_path))
    rt = Runtime(cfg, services=build_services(cfg, EventBus(), Storage(":memory:")))
    assert rt.start_calibration()["ok"] is False  # voice off: nothing to calibrate

    rt.listener = FakeListener(list(cfg["assistant"]["wake_words"]))
    rt.verifier = speaker_id.SpeakerVerifier(cfg.data_dir, "strict")
    first = FakeCalibrator(cfg.data_dir, rt.listener)
    assert rt.start_calibration(first)["ok"] is True
    assert rt.start_calibration(FakeCalibrator(cfg.data_dir, rt.listener))["ok"] is False  # one at a time
    first.release.set()
    deadline = time.time() + 5
    while rt.calibrator is not None and time.time() < deadline:
        time.sleep(0.01)

    assert rt.listener.paused_during_run is True and rt.listener.paused is False
    assert rt.listener.restarts == 1
    assert "freddy" in rt.listener.wake_words and rt.cfg["voice"]["min_rms"] == 1234
    assert rt.verifier.mode == "log"
    status = rt.bus.latest["voice_profile"]["data"]
    assert status["min_rms"] == 1234 and "freddy" in status["wake_words"] and not status["calibrating"]

    assert rt.set_speaker_check("off") == {"ok": True, "mode": "off"}
    assert rt.set_speaker_check("nope")["ok"] is False
