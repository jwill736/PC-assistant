import math
import threading
import time

import numpy as np

from assistant.voice.listener import FRAME_SAMPLES, EnergySegmenter, VoiceListener, split_wake

WAKE = ["friday", "hey friday"]


def test_split_wake_variants():
    assert split_wake("Friday, open Discord.", WAKE) == (True, "open Discord")
    assert split_wake("Hey Friday switch to BRB", WAKE) == (True, "switch to BRB")
    assert split_wake("okay friday's gonna open chrome", WAKE)[0] is True
    assert split_wake("Fryday what time is it?", WAKE) == (True, "what time is it")
    assert split_wake("Friday?", WAKE) == (True, "")


def test_split_wake_ignores_background_speech():
    matched, rest = split_wake("so I told him the stream starts friday at nine", WAKE)
    assert matched is False and rest.startswith("so I told him")


def tone(amplitude):
    return [int(amplitude * math.sin(i / 3)) for i in range(FRAME_SAMPLES)]


def test_segmenter_emits_one_utterance():
    seg = EnergySegmenter(min_rms=300, silence_ms=300, max_utterance_s=5)
    quiet, loud = tone(50), tone(3000)
    outputs = [seg.feed(quiet) for _ in range(20)]
    outputs += [seg.feed(loud) for _ in range(30)]      # 900 ms of speech
    outputs += [seg.feed(quiet) for _ in range(15)]     # trailing silence
    utterances = [o for o in outputs if o]
    assert len(utterances) == 1
    assert len(utterances[0]) >= 30


def test_segmenter_drops_clicks():
    seg = EnergySegmenter(min_rms=300, silence_ms=300)
    quiet, loud = tone(50), tone(3000)
    outs = [seg.feed(quiet) for _ in range(10)] + [seg.feed(loud) for _ in range(4)] + [seg.feed(quiet) for _ in range(20)]
    assert not any(outs)  # 120 ms blip is not speech


# ---- listener: echo guard + speaker check ---------------------------------


class ListBus:
    def __init__(self):
        self.events = []

    def publish(self, type_, data, sticky=False):
        self.events.append((type_, data))

    def heard(self):
        return [d for t, d in self.events if t == "heard"]


class FakeSpeaker:
    def __init__(self, last_text="", ago=60.0):
        self.speaking = threading.Event()
        self.last_text = last_text
        self.last_end = time.time() - ago

    def chime(self):
        pass


class FakeSTT:
    name = "parakeet"

    def __init__(self, text):
        self.text = text
        self.hints = None

    def transcribe(self, _samples, hints=()):
        self.hints = list(hints)
        return self.text


class FakeVerifier:
    def __init__(self, accept):
        self.accept = accept
        self.checked = 0

    def check(self, _samples):
        self.checked += 1
        return self.accept, (0.7 if self.accept else 0.1)


def listener(text, speaker=None, verifier=None, cfg=None):
    commands = []
    lst = VoiceListener(ListBus(), speaker or FakeSpeaker(), list(WAKE), commands.append,
                        {"follow_up_seconds": 8, **(cfg or {})}, verifier=verifier)
    lst.stt = FakeSTT(text)
    return lst, commands


AUDIO = np.zeros(16000 * 2, dtype=np.int16)


def test_own_reply_is_not_a_command():
    lst, commands = listener("Friday, be right back is up.",
                             speaker=FakeSpeaker("Friday, be right back is up. Mic's muted.", ago=1))
    lst._handle_audio(AUDIO)
    assert commands == [] and lst.bus.heard()[-1]["ignored"] == "own voice"


def test_old_reply_is_not_treated_as_echo():
    lst, commands = listener("Friday, be right back", speaker=FakeSpeaker("be right back", ago=30))
    lst._handle_audio(AUDIO)
    assert commands == ["be right back"]


def test_stranger_voice_is_ignored():
    v = FakeVerifier(accept=False)
    lst, commands = listener("Friday, end the stream", verifier=v)
    lst._handle_audio(AUDIO)
    assert commands == [] and v.checked == 1
    heard = lst.bus.heard()[-1]
    assert (heard["text"], heard["ignored"], heard["score"]) == ("Friday, end the stream", "voice not recognised", 0.1)


def test_owner_voice_runs():
    lst, commands = listener("Friday, open discord", verifier=FakeVerifier(accept=True))
    lst._handle_audio(AUDIO)
    assert commands == ["open discord"]


def test_push_to_talk_skips_the_voice_check():
    v = FakeVerifier(accept=False)
    lst, commands = listener("open discord", verifier=v)
    lst.arm(source="hotkey")
    lst._handle_audio(AUDIO)
    assert commands == ["open discord"] and v.checked == 0


def test_wake_then_command_is_still_checked():
    v = FakeVerifier(accept=False)
    lst, commands = listener("open discord", verifier=v)
    lst.arm(source="wake")
    lst._handle_audio(AUDIO)
    assert commands == [] and v.checked == 1


def test_background_speech_never_reaches_the_verifier():
    v = FakeVerifier(accept=True)
    lst, commands = listener("so what are we doing tonight", verifier=v)
    lst._handle_audio(AUDIO)
    assert commands == [] and v.checked == 0


def test_fuzzy_wake_needs_nearly_the_whole_word():
    jarvis = ["jarvis", "hey jarvis"]
    assert split_wake("Jars, open the fridge", jarvis)[0] is False       # 4 of 6 letters: an everyday word
    assert split_wake("Jarvis's got this", jarvis)[0] is True
    assert split_wake("Fesper, open discord", ["vesper"]) == (True, "open discord")
    assert split_wake("Vespa open discord", ["vesper"])[0] is False     # calibration adds "vespa" if your voice needs it
    assert split_wake("Vespa open discord", ["vesper", "vespa"]) == (True, "open discord")


def test_short_replies_are_not_mistaken_for_echo():
    lst, commands = listener("Friday, yes", speaker=FakeSpeaker("Say yes to end the stream? yes", ago=1))
    lst._handle_audio(AUDIO)
    assert commands == ["yes"]
    lst, commands = listener("yes", speaker=FakeSpeaker("yes", ago=1))   # exactly what it just said: still echo
    lst._handle_audio(AUDIO)
    assert commands == []


def test_follow_up_window_only_after_a_question():
    sp = FakeSpeaker("On Gameplay.", ago=2)
    sp.expects_reply = False
    lst, commands = listener("and mute the mic", speaker=sp)
    lst._handle_audio(AUDIO)
    assert commands == []                                    # a statement doesn't leave the mic open
    sp.expects_reply = True
    lst._handle_audio(AUDIO)
    assert commands == ["and mute the mic"]


def test_corrections_latency_and_hints():
    lst, commands = listener("Friday, switch to the VRB scene", cfg={"corrections": {"vrb": "BRB"}})
    lst.hint_words = lambda: ["Gameplay", "BRB"]
    lst._handle_audio(AUDIO)
    assert commands == ["switch to the BRB scene"]
    heard = lst.bus.heard()[-1]
    assert heard["text"] == "Friday, switch to the BRB scene" and isinstance(heard["stt_ms"], int)
    assert lst.stt.hints[:3] == ["Friday", "Gameplay", "BRB"]
    status = lst.status()
    assert status["engine"] == "Parakeet 110M" and status["latency_ms"] is not None
