"""Claude's reply is spoken sentence by sentence while it streams; voice commands run off the mic thread."""

import re
import threading
import time

import anthropic
import httpx
from conftest import FakeClient, FakeMessages, text_block, tool_block

from assistant.brain.assistant import Assistant
from assistant.runtime import Runtime


class StreamingMessages(FakeMessages):
    """``messages.stream()`` that yields each scripted text block word by word."""

    def __init__(self, script, fail_after=None):
        super().__init__(script)
        self.streamed, self.fail_after = 0, fail_after

    def stream(self, **kwargs):
        resp = self.create(**kwargs)
        self.streamed += 1
        fail_after = self.fail_after

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            @property
            def text_stream(self):
                sent = 0
                for block in resp.content:
                    if block.type == "text":
                        for word in re.findall(r"\S+\s*", block.text):
                            if fail_after is not None and sent == fail_after:
                                raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api"))
                            sent += 1
                            yield word

            def get_final_message(self):
                return resp
        return Stream()


def streaming_assistant(svc, script, **kw):
    client = FakeClient([])
    client.messages = client.beta.messages = StreamingMessages(script, **kw)
    spoken = []
    svc.speak = lambda text, expects_reply=None, turn=None: spoken.append((text, expects_reply, turn))
    return Assistant(svc, client=client), spoken


def test_voice_reply_is_spoken_as_it_streams_with_the_last_sentence_deciding_the_follow_up(svc, monkeypatch):
    monkeypatch.setattr(svc.launcher, "launch", lambda name: {"ok": True, "launched": name, "via": "alias"})
    a, spoken = streaming_assistant(svc, [
        ([text_block("On it."), tool_block("open_app", {"name": "photoshop"})], "tool_use"),
        ([text_block("Photoshop's opening. Want the thumbnail template too?")], "end_turn"),
    ])
    out = a.handle("get photoshop going for a thumbnail", "voice", turn=7)
    assert out["reply"] == "Photoshop's opening. Want the thumbnail template too?"
    assert spoken == [("On it.", False, 7), ("Photoshop's opening.", False, 7),
                      ("Want the thumbnail template too?", None, 7)]  # None: the speaker sees the "?"
    assert a.client.messages.streamed == 2


def test_typed_commands_are_not_streamed_or_spoken(svc):
    a, spoken = streaming_assistant(svc, [([text_block("Sure.")], "end_turn")])
    assert a.handle("what's up", "text")["reply"] == "Sure."
    assert spoken == [] and a.client.messages.streamed == 0


def test_a_dropped_connection_mid_reply_still_says_what_happened(svc):
    a, spoken = streaming_assistant(svc, [([text_block("Let me check. Your calendar has three things.")], "end_turn")],
                                    fail_after=5)  # dies after "Let me check. Your calendar"
    out = a.handle("what's on today", "voice", turn=2)
    assert out["reply"] == "I can't reach Claude right now. Local commands still work."
    assert spoken == [("Let me check.", False, 2), (out["reply"], None, 2)]
    assert a.history == []  # the failed exchange isn't kept


def test_fast_path_replies_are_spoken_once_with_the_turn(svc):
    a, spoken = streaming_assistant(svc, [])
    a.handle("add task Record the intro", "voice", turn=3)
    assert spoken == [("Added: Record the intro.", None, 3)]


# ---- runtime: commands off the mic thread, voice settings --------------------------

class SlowAssistant:
    def __init__(self):
        self.calls, self.started, self.release = [], threading.Event(), threading.Event()
        self.claude_ready = False

    def handle(self, text, source="text", turn=None, owner=None):
        self.calls.append((text, source, turn, threading.current_thread().name))
        self.started.set()
        self.release.wait(2)
        return {"reply": ""}


def test_voice_commands_run_off_the_mic_thread_one_at_a_time(cfg, svc):
    rt = Runtime(cfg, services=svc)
    rt.assistant = slow = SlowAssistant()
    t0 = time.time()
    rt.voice_command("open discord")
    rt.voice_command("never mind")
    assert time.time() - t0 < 0.5  # returned at once: the mic keeps listening while Claude works
    assert slow.started.wait(2)
    assert len(slow.calls) == 1  # the second waits its turn
    slow.release.set()
    deadline = time.time() + 2
    while len(slow.calls) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert [(c[0], c[1], c[2]) for c in slow.calls] == [("open discord", "voice", 1), ("never mind", "voice", 2)]
    assert slow.calls[0][3].startswith("command")
    rt.stop()


def test_set_voice_validates_applies_and_remembers(cfg, svc):
    rt = Runtime(cfg, services=svc)
    assert rt.set_voice("klingon")["ok"] is False
    assert rt.set_voice("supertonic", voice="bm_george")["ok"] is False  # a Kokoro voice
    assert rt.set_voice(speed="fast")["ok"] is False
    r = rt.set_voice("none", speed=9)
    assert r["ok"] and r["engine"] == "none" and r["speed"] == 1.6
    saved = (cfg.data_dir / "settings.yaml").read_text()
    assert "engine: none" in saved and "speed: 1.6" in saved
    assert rt.preview_voice()["ok"] is False  # nothing to preview with speech off
    assert rt.bus.latest["tts"]["data"]["engine"] == "none"


def test_tts_endpoints(cfg, svc):
    from fastapi.testclient import TestClient

    from assistant.server import create_app

    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=False)
    with TestClient(app) as c:
        c.headers["X-Assistant-Token"] = app.state.token
        st = c.get("/api/tts").json()
        assert {"engine", "voices", "first_audio_ms", "neural_available"} <= set(st)
        assert [v["id"] for v in st["voices"]["kokoro"]][:2] == ["bm_george", "bm_lewis"]
        assert c.post("/api/tts", json={"engine": "nope"}).json()["ok"] is False
        assert c.post("/api/tts", json={"engine": "none"}).json()["engine"] == "none"
        assert c.post("/api/tts/preview").json()["ok"] is False
        assert c.get("/api/state").json()["tts"]["engine"] == "none"
