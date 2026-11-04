import math
import threading
import time
from types import SimpleNamespace

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


def nudge_speaker(ago=2.0):
    sp = FakeSpeaker("Your D: drive is 99% full. Say “Friday, why is my drive full” and I'll find what's taking the space.",
                     ago=ago)
    sp.expects_reply = False  # a statement Vesper made on its own, not a question
    return sp


def test_saying_the_phrase_vesper_just_suggested_needs_no_name():
    sp = nudge_speaker()
    lst, commands = listener("Why is my drive full?", speaker=sp, cfg={"follow_up_any_seconds": 0})
    lst._handle_audio(AUDIO, started_at=sp.last_end + 1.0)  # J started after it had finished: not an echo
    assert commands == ["Why is my drive full?"]


def test_the_suggestion_coming_back_through_the_speakers_is_still_an_echo():
    sp = nudge_speaker()
    lst, commands = listener("why is my drive full", speaker=sp, cfg={"follow_up_any_seconds": 0})
    lst._handle_audio(AUDIO, started_at=sp.last_end - 1.5)  # began while Vesper was still talking
    assert commands == [] and lst.bus.heard()[-1]["ignored"] == "own voice"


def test_a_suggestion_window_closes_and_other_talk_still_needs_the_name():
    sp = nudge_speaker(ago=40)
    lst, commands = listener("why is my drive full", speaker=sp, cfg={"follow_up_any_seconds": 0})
    lst._handle_audio(AUDIO, started_at=time.time())
    assert commands == [] and lst.bus.heard()[-1]["wake"] is False
    sp = nudge_speaker()
    lst, commands = listener("what's for dinner", speaker=sp, cfg={"follow_up_any_seconds": 0})
    lst._handle_audio(AUDIO, started_at=sp.last_end + 1.0)
    assert commands == []


def test_the_suggested_phrase_still_needs_your_voice():
    sp = nudge_speaker()
    lst, commands = listener("why is my drive full", speaker=sp, verifier=FakeVerifier(accept=False),
                             cfg={"follow_up_any_seconds": 0})
    lst._handle_audio(AUDIO, started_at=sp.last_end + 1.0)
    assert commands == [] and lst.bus.heard()[-1]["ignored"] == "voice not recognised"


# ---- the echo guard on long replies, and speech said unprompted ----------------------------------------------------

LONG_REPLY = ("Tomorrow looks busy: you have the dentist at nine, a call with Dana at eleven, the sponsor sync at two "
              "and your stream at eight. The weather is cold and rainy with a high of six degrees. "
              "Do you want me to move the sponsor sync to Thursday?")


def test_a_misheard_piece_of_a_long_reply_is_still_its_own_echo():
    sp = FakeSpeaker(LONG_REPLY, ago=0.9)
    sp.expects_reply = True
    lst, commands = listener("do you want me to move the sponsor sink to thursday", speaker=sp)
    lst._handle_audio(AUDIO, started_at=sp.last_end - 2.0)  # began while Vesper was still saying it
    assert commands == [] and lst.bus.heard()[-1]["ignored"] == "own voice"


def test_an_answer_that_repeats_words_of_the_question_is_not_an_echo():
    sp = FakeSpeaker(LONG_REPLY, ago=0.9)
    sp.expects_reply = True
    lst, commands = listener("move the sponsor sync to friday", speaker=sp)
    lst._handle_audio(AUDIO, started_at=sp.last_end + 0.6)  # J started after Vesper had finished
    assert commands == ["move the sponsor sync to friday"]
    sp = FakeSpeaker(LONG_REPLY, ago=0.9)
    lst, commands = listener("Friday, move the sponsor sync to Thursday", speaker=sp)
    lst._handle_audio(AUDIO, started_at=sp.last_end - 1.0)  # over the reply, but with the name it never says
    assert commands == ["move the sponsor sync to Thursday"]


def test_the_reply_is_still_known_as_an_echo_after_an_alert_took_its_place():
    from collections import deque

    sp = FakeSpeaker("Raid incoming from Dana.", ago=0.5)
    sp.said_before = deque([(time.time() - 0.5, "Opening Discord now, and your stream key is set.")])
    lst, commands = listener("opening discord now and your stream key is set", speaker=sp)
    lst._handle_audio(AUDIO, started_at=sp.last_end - 2.0)
    assert commands == [] and lst.bus.heard()[-1]["ignored"] == "own voice"


def test_something_said_unprompted_opens_no_window_to_keep_going():
    sp = FakeSpeaker("Your pizza timer is done.", ago=1.0)
    sp.expects_reply, sp.unprompted = False, True  # a timer ring, half a minute after J's last command
    lst, commands = listener("and the other one", speaker=sp)
    lst.last_command = time.time() - 30
    lst._handle_audio(AUDIO, started_at=time.time())
    assert commands == [] and lst.bus.heard()[-1]["wake"] is False
    sp.unprompted = False  # the same words as a reply to J: he can keep going without the name
    lst._handle_audio(AUDIO, started_at=time.time())
    assert commands == ["and the other one"]


# ---- the speaker check scores J's voice, not the lead-in or Vesper's tail ------------------------------------------

class SizedVerifier(FakeVerifier):
    def __init__(self, accept=True):
        super().__init__(accept)
        self.sizes = []

    def check(self, samples):
        self.sizes.append(len(samples))
        return super().check(samples)


def test_the_voice_check_scores_the_speech_not_the_lead_in_before_it():
    v = SizedVerifier()
    lst, commands = listener("Friday, end the stream", verifier=v)
    lst._handle_audio(AUDIO, span=(9600, 32000))  # 0.6 s of pre-roll, then the speech
    assert v.sizes == [22400] and commands == ["end the stream"]


def test_the_end_of_vespers_own_sentence_is_not_scored_as_your_voice():
    v = SizedVerifier()
    now = time.time()
    sp = FakeSpeaker("Want me to end it?")
    sp.last_end = now - 0.5
    lst, commands = listener("yes, end it now please", speaker=sp, verifier=v)
    # a 2 s clip heard up to now: its first 1.5 s were still Vesper (plus 0.3 s of the sound card's buffer)
    lst._handle_audio(AUDIO, started_at=now - 0.1, ended_at=now)
    assert abs(v.sizes[0] - 3200) <= 16 and commands == ["yes, end it now please"]


class ShortVerifier:
    """Like the real one: a clip under 0.8 s of voice is let through, unjudged."""

    active = True
    profile = SimpleNamespace(threshold=0.5)

    def __init__(self):
        self.last_short = False

    def check(self, samples):
        self.last_short = len(samples) < 0.8 * 16000
        return (True, None) if self.last_short else (True, 0.9)


def test_a_yes_too_short_to_judge_is_marked_unsure_not_passed_as_yours():
    sp = FakeSpeaker("Delete the clip? Say yes or no.", ago=2)
    sp.expects_reply = True
    lst, commands = listener("Yes.", speaker=sp, verifier=ShortVerifier())
    lst._handle_audio(AUDIO, span=(9600, 14400), started_at=sp.last_end + 1)  # 0.3 s of "yes" after 0.6 s of lead-in
    assert commands == ["Yes."] and lst.last_owner == "unsure"
    lst.stt.text = "Friday, delete the clip"
    lst._handle_audio(AUDIO, started_at=time.time())  # 1.7 s of voice: judged
    assert lst.last_owner == "match"
