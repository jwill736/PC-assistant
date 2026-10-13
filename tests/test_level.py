"""The HUD orb's audio level: throttled, transient, and silent when nobody is watching."""

import asyncio
import json

import numpy as np

from assistant.bus import EventBus
from assistant.voice.level import LevelMeter, rms01
from assistant.voice.neural import AudioOut


class Bus:
    def __init__(self, clients=1):
        self.client_count = clients
        self.events = []

    def publish(self, type_, data, sticky=False, transient=False):
        self.events.append((type_, data, sticky, transient))


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_rms_maps_silence_and_loud_speech_to_0_and_1():
    assert rms01(np.zeros(480, dtype=np.int16)) == 0.0
    loud = (np.sin(np.linspace(0, 60, 480)) * 30000).astype(np.int16)
    assert rms01(loud) == 1.0
    quiet = (np.sin(np.linspace(0, 60, 480)) * 300).astype(np.int16)
    assert 0.1 < rms01(quiet) < 0.6
    as_float = (np.sin(np.linspace(0, 60, 480)) * 0.01).astype(np.float32)
    assert abs(rms01(as_float) - rms01((as_float * 32768).astype(np.int16))) < 0.02
    assert rms01([]) == 0.0


def test_meter_throttles_to_20_per_second_and_is_transient():
    bus, clock = Bus(), Clock()
    meter = LevelMeter(bus, "mic", clock=clock)
    frame = (np.ones(480) * 3000).astype(np.int16)
    for _ in range(10):  # ten 30 ms frames = 300 ms -> at most 6 samples at 20/s
        meter.feed(frame)
        clock.t += 0.03
    assert 5 <= len(bus.events) <= 7
    type_, data, sticky, transient = bus.events[0]
    assert type_ == "level" and data["src"] == "mic" and 0 < data["rms"] <= 1 and not sticky and transient
    meter.end()
    assert bus.events[-1][1]["rms"] == 0.0
    meter.end()  # only once
    assert sum(1 for e in bus.events if e[1]["rms"] == 0.0) == 1


def test_no_hud_no_work():
    bus = Bus(clients=0)
    meter = LevelMeter(bus, "tts")
    assert not meter.wanted()
    meter.feed(np.ones(480, dtype=np.int16))
    meter.end()
    assert bus.events == []


def test_busy_meter_drops_the_sample():
    bus = Bus()
    meter = LevelMeter(bus, "tts")
    with meter._busy:
        meter.send(lambda: 0.5)
    assert bus.events == []
    meter.send(lambda: None)  # nothing to measure (e.g. the audio callback held its lock)
    assert bus.events == []


def test_transient_events_skip_history_and_listeners_but_reach_the_hud():
    bus = EventBus()
    heard = []
    bus.on(heard.append)

    async def run():
        bus.bind_loop(asyncio.get_running_loop())
        q = bus.subscribe()
        bus.publish("level", {"src": "tts", "rms": 0.4}, transient=True)
        return json.loads(await asyncio.wait_for(q.get(), 1))

    got = asyncio.run(run())
    assert got["type"] == "level" and got["data"]["rms"] == 0.4
    assert heard == [] and not bus.recent and "level" not in bus.latest


def test_audio_out_level_reads_what_plays_next():
    out = AudioOut(16000, stream_factory=lambda cb: type("S", (), {"start": lambda self: None})())
    assert out.level() == 0.0
    out.play(np.ones(1600, dtype=np.float32) * 0.5)
    assert out.level() > 0.8
    with out._lock:  # the audio callback is mid-buffer: drop rather than wait
        assert out.level() is None


def test_speaker_pumps_levels_only_while_speaking_with_a_hud_open():
    from assistant.voice.tts import Speaker

    class Out:
        def level(self):
            return 0.5

    bus = Bus()
    sp = Speaker(bus, engine="none")
    sp.out = Out()
    sp.speaking.set()
    sp._start_level_pump()
    sp._level_thread.join(0.2)
    sp.speaking.clear()
    sp._level_thread.join(1)
    levels = [e[1]["rms"] for e in bus.events if e[0] == "level"]
    assert levels[0] == 0.5 and levels[-1] == 0.0 and len(levels) <= 7

    quiet = Bus(clients=0)
    sp = Speaker(quiet, engine="none")
    sp.out = Out()
    sp.speaking.set()
    sp._start_level_pump()
    assert sp._level_thread is None
