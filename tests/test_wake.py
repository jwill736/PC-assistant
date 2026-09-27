import threading
import time
from types import SimpleNamespace

import numpy as np

from assistant.voice import tts as tts_mod
from assistant.voice import wakeword
from assistant.voice.listener import VoiceListener, strip_leading_name

WAKE = ["friday", "hey friday"]


# ---- streaming detector (scripted ONNX sessions) ---------------------------------

class Session:
    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def run(self, _out, feeds):
        self.calls += 1
        return [self.fn(next(iter(feeds.values())))]


def detector(scores, **kw):
    """Classifier returns the next scripted score per call."""
    it = iter(scores)
    mel = (Session(lambda a: np.zeros((1, 1, len(a[0]) // 160 - 3, 32), np.float32)), "x")
    emb = (Session(lambda m: np.ones((1, 1, 1, 96), np.float32)), "x")
    clf = Session(lambda x: np.array([[next(it, 0.0)]], np.float32))
    clock = kw.pop("clock", None) or (lambda: 0.0)
    return wakeword.WakeWords({}, sessions=(mel, emb, {"friday": (clf, "x")}), clock=clock, **kw), clf


def test_streams_one_embedding_per_80ms_hop():
    ww, clf = detector([0.1] * 100)
    frames = np.zeros(1280 * 30, dtype=np.int16)
    for i in range(0, len(frames), 480):
        ww.feed(frames[i:i + 480])
    mel_calls = ww._mel[0].calls
    assert mel_calls == 30                   # one mel call per hop, 8 frames each
    assert ww._emb[0].calls == 30 - 10 + 1   # embeddings start at hop 10, the first with >= 76 mel frames
    assert clf.calls == ww._emb[0].calls - 15  # classifier needs 16 embeddings


def test_threshold_and_refractory():
    t = [0.0]
    ww, _ = detector([0.2, 0.9, 0.95, 0.9, 0.1, 0.1, 0.9], threshold=0.5, refractory_s=1.5, clock=lambda: t[0])
    ww._mels = np.zeros((76, 32), np.float32)
    ww._embs.extend(np.zeros((15, 96), np.float32))
    hits = []
    for step in range(7):
        t[0] = step * 0.5
        hits += [(round(t[0], 1), h) for h in ww._hop(np.zeros(1280, np.float32))]
    assert [x[0] for x in hits] == [0.5, 3.0]  # 0.95 at 1.0 s is inside the refractory window
    assert ww.status()["peak"]["friday"] == 0.95


def test_per_model_thresholds():
    ww, _ = detector([0.7], threshold=0.5, thresholds={"Friday": 0.8})
    ww._mels = np.zeros((76, 32), np.float32)
    ww._embs.extend(np.zeros((15, 96), np.float32))
    assert ww._hop(np.zeros(1280, np.float32)) == []


def test_no_models_means_no_detector(tmp_path):
    assert wakeword.load({}, tmp_path) is None
    (tmp_path / "wakewords").mkdir()
    (tmp_path / "wakewords" / "Vesper.onnx").write_bytes(b"x")
    (tmp_path / "wakewords" / "stop.onnx").write_bytes(b"x")
    (tmp_path / "wakewords" / "Vesper.json").write_text('{"threshold": 0.68}')
    (tmp_path / "wakewords" / "stop.json").write_text("not json")
    assert list(wakeword.model_files(tmp_path)) == ["stop", "vesper"]
    assert wakeword.saved_thresholds(tmp_path) == {"vesper": 0.68}  # a broken sidecar is ignored


# ---- listener: anchoring, barge-in, hard triggers --------------------------------

class ListBus:
    def __init__(self):
        self.events = []

    def publish(self, type_, data, sticky=False):
        self.events.append((type_, data))

    def of(self, kind):
        return [d for t, d in self.events if t == kind]


class FakeSpeaker:
    def __init__(self, last_text="", speaking=False):
        self.speaking = threading.Event()
        if speaking:
            self.speaking.set()
        self.last_text = last_text
        self.last_end = time.time() - 60
        self.expects_reply = False
        self.interrupts = 0

    def chime(self):
        pass

    def interrupt(self):
        self.interrupts += 1
        self.speaking.clear()
        self.last_end = time.time()
        return True


class FakeSTT:
    name = "parakeet"

    def __init__(self, text):
        self.text, self.calls = text, 0

    def transcribe(self, _samples, hints=()):
        self.calls += 1
        return self.text


class FakeWake:
    def __init__(self, names):
        self.names = names

    def status(self):
        return {"models": self.names}


def listener(text, names=("friday",), speaker=None, cfg=None):
    commands = []
    lst = VoiceListener(ListBus(), speaker or FakeSpeaker(), list(WAKE), commands.append, {"follow_up_seconds": 8, **(cfg or {})})
    lst.stt = FakeSTT(text)
    lst.wake = FakeWake(list(names)) if names else None
    return lst, commands


AUDIO = np.zeros(16000 * 2, dtype=np.int16)


def test_trigger_kinds_and_mode():
    lst, _ = listener("x", names=("friday", "stop", "clip_that", "hey_friday"))
    assert [lst.trigger_kind(n) for n in lst.wake.names] == ["name", "stop", "command", "name"]
    assert lst.wake_mode() == "acoustic"
    lst.cfg["wake_mode"] = "hybrid"
    assert lst.wake_mode() == "hybrid"
    lst.wake = FakeWake(["stop"])  # no name model: the transcript still wakes it
    assert lst.wake_mode() == "transcript"


def test_acoustic_mode_skips_speech_to_text_without_the_name():
    lst, commands = listener("so anyway, open discord")
    lst._handle_audio(AUDIO, started_at=time.time())
    assert lst.stt.calls == 0 and commands == []


def test_acoustic_hit_at_the_start_wakes_even_if_the_name_is_misheard():
    lst, commands = listener("Fry day, open discord")
    start = time.time()
    lst._on_hits([("friday", 0.93)], speaking=False)
    lst._handle_audio(AUDIO, started_at=start)
    assert commands == ["open discord"]
    assert lst.bus.of("heard")[-1]["acoustic"] is True


def test_name_said_mid_sentence_does_not_wake():
    lst, commands = listener("so I told them Friday is great")
    lst._name_hit_at = time.time()
    lst._handle_audio(AUDIO, started_at=time.time() - 5)  # the utterance began 5 s before the name
    assert commands == [] and lst.stt.calls == 0


def test_stop_while_speaking_interrupts_without_a_command():
    sp = FakeSpeaker("Here's the full rundown of your afternoon", speaking=True)
    lst, commands = listener("Stop.", names=(), speaker=sp)
    lst._handle_audio(AUDIO, during_speech=True)
    assert sp.interrupts == 1 and commands == []
    assert lst.bus.of("heard")[-1]["interrupted"] is True


def test_cancel_while_speaking_also_cancels_the_pending_action():
    sp = FakeSpeaker("That will end the stream. Say yes to run it.", speaking=True)
    lst, commands = listener("cancel", names=(), speaker=sp)
    lst._handle_audio(AUDIO, during_speech=True)
    assert sp.interrupts == 1 and commands == ["cancel"]


def test_stop_inside_the_reply_itself_is_not_a_stop():
    sp = FakeSpeaker("I can stop the stream if you want", speaking=True)
    lst, commands = listener("stop", names=(), speaker=sp)
    lst._handle_audio(AUDIO, during_speech=True)
    assert sp.interrupts == 0 and commands == []


def test_name_over_a_reply_interrupts_and_runs_the_command():
    sp = FakeSpeaker("Here's the full rundown", speaking=True)
    lst, commands = listener("Friday, switch to BRB", names=(), speaker=sp)
    lst._handle_audio(AUDIO, during_speech=True)
    assert sp.interrupts == 1 and commands == ["switch to BRB"]


def test_other_talk_over_a_reply_is_ignored():
    sp = FakeSpeaker("Here's the full rundown", speaking=True)
    lst, commands = listener("lol chat look at that", names=(), speaker=sp)
    lst._handle_audio(AUDIO, during_speech=True)
    assert sp.interrupts == 0 and commands == []
    assert lst.bus.of("heard")[-1]["ignored"] == "while speaking"


def test_acoustic_hits_drive_stop_name_and_hard_triggers():
    sp = FakeSpeaker("talking", speaking=True)
    lst, commands = listener("", names=("friday", "stop", "clip_that"), speaker=sp,
                             cfg={"hard_triggers": {"clip_that": "save the replay"}})
    lst._on_hits([("stop", 0.9)], speaking=True)
    assert sp.interrupts == 1
    lst._on_hits([("clip_that", 0.9)], speaking=True)       # never while it's talking (could be its own voice)
    assert commands == []
    lst._on_hits([("stop", 0.9)], speaking=False)           # nothing to stop
    assert sp.interrupts == 1
    lst._on_hits([("clip_that", 0.95)], speaking=False)
    assert commands == ["save the replay"]
    lst._on_hits([("friday", 0.9)], speaking=True)
    assert sp.interrupts == 2 and lst._name_hit_at > 0
    assert [d["name"] for d in lst.bus.of("wake_word")] == ["stop", "clip_that", "stop", "clip_that", "friday"]


def test_strip_leading_name():
    assert strip_leading_name("Desperate, open Discord", "vesper") == "open Discord"
    assert strip_leading_name("Hey Vespa switch to BRB", "vesper") == "switch to BRB"
    assert strip_leading_name("open Discord", "vesper") == "open Discord"
    assert strip_leading_name("Best per, open Discord", "vesper") == "open Discord"  # split into two words


# ---- interruptible speech output -------------------------------------------------

def test_interrupt_drains_queue_and_stops_the_browser():
    events = []
    sp = tts_mod.Speaker(SimpleNamespace(publish=lambda t, d, **k: events.append(t), client_count=1), engine="browser")
    assert sp.interrupt() is False  # not speaking
    sp.say("one")
    sp.say("two")
    assert sp.interrupt() is True
    assert sp._q.empty() and sp._cancel.is_set()
    assert "speak_stop" in events and "interrupted" in events


def test_sapi_voice_purges_when_cancelled():
    calls = []

    class Voice:
        def __init__(self):
            self.polls = 0

        def Speak(self, text, flags=0):
            calls.append((text, flags))

        def WaitUntilDone(self, ms):
            self.polls += 1
            if self.polls == 2:
                cancel.set()
            return False

    cancel = threading.Event()
    tts_mod._SapiVoice(Voice()).say("A long reply", cancel=cancel)
    assert calls == [("A long reply", 1), ("", 3)]
