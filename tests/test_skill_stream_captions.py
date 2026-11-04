"""Live captions: J's words from the listener's ``heard`` event, never Vesper's own voice or commands to Vesper,
nothing while off the record, secrets blanked; the overlay's captions panel (whitelisted) and the OBS text source;
the listener transcribes everything in acoustic mode only while captions are showing; the switch and its phrases."""

import threading
import time

import numpy as np
import pytest

from assistant import overlay
from assistant.brain.router import RouterContext, route
from assistant.bus import EventBus
from assistant.integrations.obs import OBSController
from assistant.skills import Skills
from assistant.skills import stream_captions as sc
from assistant.voice.listener import VoiceListener


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


class FakeOBS:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def set_text(self, source, text):
        if self.fail:
            return {"ok": False, "error": self.fail}
        self.sent.append((source, text))
        return {"ok": True, "source": source}


@pytest.fixture
def env(svc, monkeypatch):
    monkeypatch.setattr("assistant.config.save_setting", lambda cfg, key, value: None)
    clock, state = Clock(), {"live": True, "off": False}
    svc.bus = EventBus()
    svc.live = lambda: state["live"]
    svc.obs = FakeOBS()
    cap = sc.StreamCaptions(svc, clock=clock)
    cap.threads = False
    cap.off_record = lambda: state["off"]
    cap.listener = object()  # the mic is there
    return cap, clock, state


def heard(cap, text, **kw):
    cap.svc.bus.publish("heard", {"text": text, "wake": False, "armed": False, **kw})


def texts(cap):
    return [x["text"] for x in cap.current()]


# ---- what becomes a caption ------------------------------------------------------------------------------------

WAKE = ["vesper", "hey vesper"]


@pytest.mark.parametrize("event,shown", [
    ({"text": "that boss was brutal"}, "that boss was brutal"),
    ({"text": "Vesper, clip that", "wake": True}, None),  # a command: the listener matched the name
    ({"text": "Vesper, mute the mic"}, None),  # the name, even when acoustic mode didn't take it as a command
    ({"text": "Your next meeting is at three", "ignored": "own voice"}, None),  # Vesper heard through the speakers
    ({"text": "okay so anyway", "ignored": "while speaking"}, None),
    ({"text": "yes", "armed": True}, None),  # an answer to Vesper's question
    ({"text": "open discord", "ignored": "voice not recognised"}, None),  # someone else's voice
    ({"text": "stop", "interrupted": True}, None),
    ({"text": "hmm"}, None),
    ({"text": "   "}, None),
    ({"text": "my api key is sk-abcdefghijklmnopqrstuvwx"}, "my api key is [secret]"),
])
def test_caption_text(event, shown):
    assert sc.caption_text({"wake": False, "armed": False, **event}, WAKE) == shown


def test_hidden_words_become_stars():
    assert sc.caption_text({"text": "my name is Jordan Smith"}, WAKE, hide=["Jordan", "smith"]) == "my name is *** ***"


def test_captions_follow_heard_events_only_while_on_and_live(env):
    cap, clock, state = env
    heard(cap, "off by default")
    assert texts(cap) == []
    cap.set_on(True)
    heard(cap, "hello chat")
    heard(cap, "Vesper, what's my uptime", wake=True)
    assert texts(cap) == ["hello chat"]
    st = cap.svc.bus.latest["stream_captions"]["data"]
    assert st["showing"] and [x["text"] for x in st["lines"]] == ["hello chat"]
    state["off"] = True  # "off the record"
    heard(cap, "this is private")
    assert texts(cap) == ["hello chat"]
    state["off"] = False
    cap.set_on(False)
    heard(cap, "not shown")
    assert texts(cap) == [] and not cap.showing()


def test_turned_on_offline_it_previews_for_three_minutes(env):
    cap, clock, state = env
    state["live"] = False
    cap.set_on(True)
    heard(cap, "testing one two")
    assert texts(cap) == ["testing one two"] and cap.status()["preview_s"] == 180
    clock.t += 181
    heard(cap, "gone now")
    assert not cap.showing() and "gone now" not in texts(cap)


def test_lines_age_out_and_the_obs_text_clears(env, monkeypatch):
    cap, clock, state = env
    monkeypatch.setitem(cap.cfg, "obs_source", "Captions")
    cap.set_on(True)
    heard(cap, "first line")
    clock.t += 2
    heard(cap, "second line")
    assert cap.svc.obs.sent[-1] == ("Captions", "first line\nsecond line")
    clock.t += 6  # the first is 8 s old, the second 6 s
    cap.tick()
    assert texts(cap) == ["second line"] and cap.svc.obs.sent[-1] == ("Captions", "second line")
    clock.t += 2
    cap.tick()
    assert texts(cap) == [] and cap.svc.obs.sent[-1] == ("Captions", "")
    sent = len(cap.svc.obs.sent)
    cap.tick()
    assert len(cap.svc.obs.sent) == sent  # nothing new to send


def test_the_stream_ending_takes_captions_down(env, monkeypatch):
    cap, clock, state = env
    monkeypatch.setitem(cap.cfg, "obs_source", "Captions")
    cap.set_on(True)
    heard(cap, "good night chat")
    clock.t += 200  # past the preview too
    state["live"] = False
    cap.tick()
    assert cap.svc.obs.sent[-1] == ("Captions", "") and cap.status()["lines"] == []


def test_obs_text_source_problems_show_on_the_hud(env, monkeypatch):
    cap, clock, state = env
    monkeypatch.setitem(cap.cfg, "obs_source", "Captions")
    cap.svc.obs = FakeOBS(fail="OBS has no source called “Captions”: add a Text source with that name, or fix the name.")
    cap.set_on(True)
    heard(cap, "hello")
    assert "no source called" in cap.status()["obs_error"]
    assert texts(cap) == ["hello"]  # the overlay still has it


def test_no_microphone_says_so(env):
    cap, clock, state = env
    cap.listener = None
    out = cap.set_on(True)
    assert out["voice"] and "voice is off" in cap.say("stream_captions", {"on": True}, out)


def test_obs_set_text_sends_only_the_text(monkeypatch):
    obs = OBSController(enabled=True)
    calls = []
    monkeypatch.setattr(obs, "_send", lambda req, data=None: calls.append((req, data)) or {})
    assert obs.set_text("Captions", "hi") == {"ok": True, "source": "Captions"}
    assert calls == [("SetInputSettings", {"inputName": "Captions", "inputSettings": {"text": "hi"}, "overlay": True})]

    def missing(req, data=None):
        raise RuntimeError("Request SetInputSettings returned code 600: No source was found by the name of Captions")
    monkeypatch.setattr(obs, "_send", missing)
    assert "no source called “Captions”" in obs.set_text("Captions", "hi")["error"]
    monkeypatch.setattr(obs, "_send", lambda req, data=None: (_ for _ in ()).throw(ConnectionRefusedError("refused")))
    assert obs.set_text("Captions", "hi")["error"] == "OBS isn't running or its WebSocket server is off."


# ---- the overlay ------------------------------------------------------------------------------------------------

def test_overlay_captions_are_whitelisted():
    status = {"showing": True, "keep_s": 7, "obs_error": "x", "voice": "y",
              "lines": [{"id": 1, "text": "a", "ts": 1.0, "extra": "no"}, {"id": 2, "text": "b " * 200, "ts": 2.0},
                        {"id": 3, "text": "c", "ts": 3.0}]}
    out = overlay.captions(status)
    assert out["showing"] and [x["id"] for x in out["lines"]] == [2, 3] and set(out["lines"][0]) == {"id", "text", "ts"}
    assert len(out["lines"][0]["text"]) <= 200 and out["keep_s"] == 7
    assert overlay.captions({"showing": False, "lines": [{"id": 1, "text": "a"}]}) == {"showing": False, "lines": [], "keep_s": 0}
    assert overlay.parts_for("stream_captions", status, {}, {}) == {"captions": out}
    assert "stream_captions" in overlay.WATCH and overlay.parse_panels("cc,music") == ["captions", "nowplaying"]
    assert "captions" not in overlay.parse_panels("")  # never on an overlay unless asked for


# ---- the switch -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,on", [("captions on", True), ("turn on captions", True), ("turn the captions off", False),
                                     ("start live captions", True), ("stop captions", False), ("subtitles off", False),
                                     ("enable closed captions", True)])
def test_routes(svc, text, on):
    intent = route(text, RouterContext(skill_route=Skills(svc, ["stream_captions"]).route))
    assert intent.tool == "stream_captions" and intent.args == {"on": on}


def test_say(env):
    cap, clock, state = env
    assert cap.say("stream_captions", {}, {"ok": True, "on": False}) == "Captions off."
    assert "captions overlay" in cap.say("stream_captions", {}, {"ok": True, "on": True, "live": True})
    assert "3 minutes" in cap.say("stream_captions", {}, {"ok": True, "on": True, "live": False})


def test_overlay_link_has_the_overlay_key_only(env):
    cap, clock, state = env
    url = cap.overlay_link()["url"]
    key = overlay.load_overlay_token(cap.svc.cfg.data_dir)
    assert url.endswith(f"panels=captions&theme=transparent&align=center&key={key}") and "/overlay?" in url


# ---- the listener: every sentence becomes text while captions are showing (acoustic mode) ----------------------

class Bus:
    def __init__(self):
        self.events = []

    def publish(self, type_, data=None, sticky=False):
        self.events.append((type_, data))


class Speaker:
    def __init__(self):
        self.speaking, self.last_end, self.last_text, self.expects_reply = threading.Event(), time.time() - 60, "", False


class STT:
    name = "parakeet"

    def __init__(self):
        self.calls = 0

    def transcribe(self, _samples, hints=()):
        self.calls += 1
        return "so anyway that boss fight was brutal"


class Wake:
    names = ["vesper"]

    def status(self):
        return {}


def test_acoustic_mode_hears_everything_out_only_while_captions_show():
    commands = []
    lst = VoiceListener(Bus(), Speaker(), ["vesper", "hey vesper"], commands.append, {"follow_up_seconds": 8})
    lst.stt, lst.wake = STT(), Wake()
    assert lst.wake_mode() == "acoustic"
    long = np.zeros(16000 * 4, dtype=np.int16)  # too long to be a no-name phrase
    lst._handle_audio(long, started_at=time.time())
    assert lst.stt.calls == 0  # as before: no speech-to-text without the name
    lst.hear_all = lambda: True
    lst._handle_audio(long, started_at=time.time())
    heard_ = [d for t, d in lst.bus.events if t == "heard"]
    assert lst.stt.calls == 1 and commands == []  # heard out, never run as a command
    assert heard_[-1]["text"] == "so anyway that boss fight was brutal" and heard_[-1]["wake"] is False


def test_start_wires_the_listener_and_off_the_record(env):
    cap, clock, state = env

    class Runtime:
        listener = VoiceListener(Bus(), Speaker(), ["vesper"], lambda t: None, {})
        assistant = type("A", (), {"off_record": staticmethod(lambda: True)})()
    cap.start(Runtime)
    assert Runtime.listener.hear_all == cap.showing and cap.off_record() is True
