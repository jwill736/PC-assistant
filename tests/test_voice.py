import math

from assistant.voice.listener import FRAME_SAMPLES, EnergySegmenter, split_wake

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
