import io
import tarfile
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from assistant.voice import models, stt, vad
from assistant.voice.listener import EnergySegmenter
from assistant.voice.tts import Speaker, asks_something, clean_for_speech

# ---- Silero segmenter (with a scripted stand-in for sherpa-onnx's detector) ----


class ScriptedVAD:
    """Emits one segment covering [start, end) once `end + endpoint` samples have been fed."""

    def __init__(self, start, end, endpoint=6400):
        self.start, self.end, self.endpoint = start, end, endpoint
        self.fed, self.queue, self.resets, self.done = 0, [], 0, False

    def reset(self):
        self.fed, self.queue, self.done = 0, [], False
        self.resets += 1

    def accept_waveform(self, samples):
        self.fed += len(samples)
        if not self.done and self.fed >= self.end + self.endpoint:
            self.done = True
            self.queue.append(SimpleNamespace(start=self.start, samples=np.zeros(self.end - self.start, dtype=np.float32)))

    def is_speech_detected(self):
        return self.start <= self.fed < self.end + self.endpoint and not self.done

    def empty(self):
        return not self.queue

    @property
    def front(self):
        return self.queue[0]

    def pop(self):
        self.queue.pop(0)


def frames(n_samples, value=0):
    pcm = np.full(n_samples, value, dtype=np.int16)
    pcm[::7] = np.arange(len(pcm[::7])) % 1000  # distinct samples so slices are checkable
    return [pcm[i:i + 480] for i in range(0, n_samples, 480)], pcm


def test_silero_segmenter_returns_speech_plus_preroll():
    fake = ScriptedVAD(start=16000, end=32000)
    seg = vad.SileroSegmenter(None, vad=fake, preroll_ms=300)
    chunks, pcm = frames(16000 * 3)
    outs, speaking = [], []
    for c in chunks:
        r = seg.feed(c)
        speaking.append(seg.in_speech)
        if r:
            outs.append(np.concatenate(r))
    assert len(outs) == 1
    assert np.array_equal(outs[0], pcm[16000 - 4800:32000])  # 300 ms of lead-in, then the speech
    assert any(speaking) and not speaking[-1]


def test_silero_segmenter_reset_restarts_the_clock():
    fake = ScriptedVAD(start=8000, end=16000)
    seg = vad.SileroSegmenter(None, vad=fake)
    for c in frames(8000)[0]:
        seg.feed(c)
    seg.reset()
    assert fake.resets == 2 and seg._fed == 0 and len(seg._buf) == 0
    chunks, pcm = frames(16000 * 2)
    out = [np.concatenate(r) for r in (seg.feed(c) for c in chunks) if r]
    assert len(out) == 1 and np.array_equal(out[0], pcm[8000 - 4800:16000])


def test_make_segmenter_falls_back_to_energy(monkeypatch, tmp_path):
    def offline(*_a, **_k):
        raise OSError("no network")

    monkeypatch.setattr(models, "ensure", offline)
    assert isinstance(vad.make_segmenter({"vad": "auto"}, tmp_path), EnergySegmenter)
    assert isinstance(vad.make_segmenter({"vad": "energy"}, tmp_path), EnergySegmenter)
    with pytest.raises(OSError):
        vad.make_segmenter({"vad": "silero"}, tmp_path)  # asked for explicitly: don't hide the problem


# ---- engine choice -------------------------------------------------------------

def test_resolve_prefers_parakeet_then_whisper(monkeypatch):
    monkeypatch.setattr(stt, "available", lambda: {"parakeet": True, "parakeet-large": True, "moonshine": True, "whisper": True})
    assert stt.resolve({"stt_engine": "auto"}) == "parakeet"
    assert stt.resolve({"stt_engine": "whisper"}) == "whisper"
    assert stt.resolve({"stt_engine": "moonshine"}) == "moonshine"
    assert stt.resolve({"stt_engine": "nonsense"}) == "parakeet"
    monkeypatch.setattr(stt, "available", lambda: {"parakeet": False, "parakeet-large": False, "moonshine": False, "whisper": True})
    assert stt.resolve({"stt_engine": "parakeet"}) == "whisper"  # asked for but not installed
    monkeypatch.setattr(stt, "available", lambda: dict.fromkeys(stt.ENGINES, False))
    with pytest.raises(RuntimeError, match="requirements-voice"):
        stt.resolve({})


def test_whisper_engine_passes_hints_as_prompt():
    calls = {}

    class Model:
        def transcribe(self, samples, **kw):
            calls.update(kw)
            return [SimpleNamespace(text=" Vesper,"), SimpleNamespace(text=" open Discord.")], None

    engine = stt.WhisperSTT(loaded=Model())
    assert engine.transcribe(np.zeros(10, dtype=np.float32), ["Vesper", "Discord"]) == "Vesper, open Discord."
    assert calls["initial_prompt"] == "Vesper, Discord" and calls["language"] == "en"


def test_load_downloads_the_sherpa_model(monkeypatch, tmp_path):
    monkeypatch.setattr(stt, "available", lambda: dict.fromkeys(stt.ENGINES, True))
    fetched = []
    monkeypatch.setattr(models, "ensure", lambda key, d, cb=None: fetched.append(key) or tmp_path / key)
    built = []
    monkeypatch.setattr(stt, "SherpaSTT", lambda engine, path, threads: built.append((engine, path, threads)) or "engine")
    assert stt.load({"stt_engine": "auto", "stt_threads": 4}, tmp_path) == "engine"
    assert fetched == ["parakeet"] and built == [("parakeet", tmp_path / "parakeet", 4)]


# ---- model download ------------------------------------------------------------

def tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def serve(monkeypatch, payload: bytes):
    import httpx

    class Resp:
        headers = {"content-length": str(len(payload))}

        def raise_for_status(self):
            pass

        def iter_bytes(self, n):
            for i in range(0, len(payload), n):
                yield payload[i:i + n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    hits = []
    monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: hits.append(url) or Resp())
    return hits


def test_ensure_unpacks_once_and_marks_complete(monkeypatch, tmp_path):
    name = models.MODELS["parakeet"].name
    hits = serve(monkeypatch, tarball({f"{name}/tokens.txt": b"a 0\n", f"{name}/encoder.int8.onnx": b"x" * 1000}))
    progress = []
    path = models.ensure("parakeet", tmp_path, progress.append)
    assert (path / "tokens.txt").read_bytes() == b"a 0\n" and (path / models.COMPLETE).exists()
    assert progress[-1] == 1.0 and not list(tmp_path.glob("*.part"))
    assert models.ensure("parakeet", tmp_path) == path and len(hits) == 1  # cached: no second download


def test_ensure_rejects_archives_that_escape_the_models_folder(monkeypatch, tmp_path):
    serve(monkeypatch, tarball({"../evil.txt": b"pwned"}))
    with pytest.raises((tarfile.TarError, RuntimeError)):
        models.ensure("moonshine", tmp_path / "models")
    assert not (tmp_path / "evil.txt").exists()
    assert sorted(p.name for p in (tmp_path / "models").iterdir()) == []  # no .part or staging left behind
    assert not models.is_ready("moonshine", tmp_path / "models")


def test_two_callers_share_one_download(monkeypatch, tmp_path):
    """The health check asking for the voice while the speaker is still downloading it waits, not re-downloads."""
    import threading
    import time as _time

    name = models.MODELS["supertonic"].name
    payload = tarball({f"{name}/tts.json": b"{}"})
    hits = serve(monkeypatch, payload)
    import httpx
    slow = httpx.stream

    def stream(method, url, **kw):
        _time.sleep(0.2)  # long enough for the second caller to arrive mid-download
        return slow(method, url, **kw)
    monkeypatch.setattr(httpx, "stream", stream)
    paths = []
    threads = [threading.Thread(target=lambda: paths.append(models.ensure("supertonic", tmp_path))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(hits) == 1 and len(paths) == 2 and (paths[0] / "tts.json").exists()


def test_single_file_model(monkeypatch, tmp_path):
    serve(monkeypatch, b"onnx-bytes")
    path = models.ensure("silero-vad", tmp_path)
    assert path.read_bytes() == b"onnx-bytes" and models.is_ready("silero-vad", tmp_path)


# ---- speech output -------------------------------------------------------------

def test_clean_for_speech():
    assert clean_for_speech("**Done.** Opened [Twitch](https://twitch.tv) 🎮") == "Done. Opened Twitch"
    assert clean_for_speech("See https://example.com/x for more") == "See the link for more"
    assert clean_for_speech("- one\n- two") == "one two"


def test_expects_reply():
    assert asks_something("Want me to end it?")
    assert asks_something("brb will end the stream. Say yes to run it.")
    assert not asks_something("On Gameplay.")
    sp = Speaker(bus=SimpleNamespace(publish=lambda *a, **k: None, client_count=0), engine="browser")
    sp._q = SimpleNamespace(put=lambda _t: None)
    sp.speaking = threading.Event()
    sp.say("Done. Anything else?")
    assert sp.expects_reply is True
    sp.say("Done.", expects_reply=True)  # a pending confirmation keeps the mic open
    assert sp.expects_reply is True
    sp.say("**Done.**")
    assert sp.expects_reply is False and sp.last_text == "Done."


# ---- --bench-voice ---------------------------------------------------------------

def test_bench_picks_the_engine_that_hears_you(cfg, monkeypatch):
    import yaml

    from assistant.voice import bench

    monkeypatch.setattr(bench.time, "sleep", lambda _s: None)
    tone = (0.2 * np.sin(np.arange(16000) / 5)).astype(np.float32)
    quiet = np.zeros(32000, dtype=np.float32)
    calls = iter([quiet] + [np.concatenate([np.zeros(4000, np.float32), tone, np.zeros(4000, np.float32)])] * 10)
    record = lambda _sec, _cb: next(calls)  # noqa: E731
    said = [c.format(name="Friday") for c in bench.COMMANDS]

    class Echo:  # hears everything right, slowly
        name = "whisper"

        def __init__(self):
            self.i = -1

        def transcribe(self, _audio):
            self.i += 1
            return said[(self.i - 1) % len(said)] if self.i else ""

    class Mangler(Echo):  # fast, but mishears the name
        name = "parakeet"

        def transcribe(self, audio):
            return super().transcribe(audio).replace("Friday", "Fried egg")

    lines = []
    report = bench.run(cfg, record, loaders={"parakeet": Mangler, "whisper": Echo}, say=lines.append, apply=True)
    assert report["best"] == "whisper"
    assert report["results"]["whisper"]["woke"] == 6 and report["results"]["whisper"]["wer"] == 0
    assert report["results"]["parakeet"]["woke"] == 0
    assert any("← best" in line and "Whisper" in line for line in lines)
    layer = yaml.safe_load((cfg.data_dir / "settings.yaml").read_text())
    assert layer["voice"]["stt_engine"] == "whisper" and cfg["voice"]["stt_engine"] == "whisper"
    assert list((cfg.data_dir / "bench").glob("voice-*.json"))


# ---- push-to-talk hotkey -----------------------------------------------------

def test_hotkey_format_conversion():
    from assistant.voice.hotkey import to_pynput

    assert to_pynput("ctrl+alt+j") == "<ctrl>+<alt>+j"
    assert to_pynput("Ctrl + Shift + F13") == "<ctrl>+<shift>+<f13>"
    assert to_pynput("win+space") == "<cmd>+<space>"
    with pytest.raises(ValueError):
        to_pynput("ctrl+banana")


def test_hotkey_registers_with_pynput(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    from assistant.voice import hotkey

    started = []

    class GlobalHotKeys:
        def __init__(self, mapping):
            self.mapping = mapping

        def start(self):
            started.append(self.mapping)

        def stop(self):
            pass

    pynput = ModuleType("pynput")
    pynput.keyboard = SimpleNamespace(GlobalHotKeys=GlobalHotKeys)
    monkeypatch.setitem(sys.modules, "pynput", pynput)
    monkeypatch.setattr(hotkey, "_listener", None)
    monkeypatch.setattr(hotkey, "_bindings", {})
    cb = lambda: None  # noqa: E731
    assert hotkey.register_hotkey("ctrl+alt+j", cb) is True
    assert started == [{"<ctrl>+<alt>+j": cb}]
    kill = lambda: None  # noqa: E731
    assert hotkey.register_hotkey("ctrl+alt+k", kill) is True  # push-to-talk stays registered
    assert started[-1] == {"<ctrl>+<alt>+j": cb, "<ctrl>+<alt>+k": kill}
    assert hotkey.register_hotkey(None, cb) is False
