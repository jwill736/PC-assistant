"""Phase 4a: risk tiers, the kill switch, the step budget, confirmations that can't go stale, the audit log."""

import json
import threading
from types import SimpleNamespace

import pytest
from conftest import FakeClient, text_block, tool_block

from assistant.brain import policy
from assistant.brain.assistant import Assistant
from assistant.brain.router import route
from assistant.integrations import toast
from assistant.runtime import Runtime


@pytest.fixture
def launched(svc, monkeypatch):
    out = []
    monkeypatch.setattr(svc.launcher, "launch", lambda name: out.append(name) or {"ok": True, "launched": name})
    return out


@pytest.fixture
def obs_calls(svc, monkeypatch):
    out = []
    monkeypatch.setattr(svc.obs, "control", lambda action: out.append(action) or {"ok": True})
    return out


# ---- policy primitives ----------------------------------------------------------

def test_guard_kill_switch_blocks_actions_but_not_reads():
    g = policy.Guard(max_steps=3, max_failures=2)
    assert g.check(3) is None
    g.stop("hotkey")
    assert g.check(0) is None and "paused" in g.check(1)
    g.resume()
    assert g.check(1) is None


def test_step_and_failure_budget_only_counts_within_one_request():
    g = policy.Guard(max_steps=3, max_failures=2)
    for _ in range(10):  # HUD clicks between commands never use up the budget
        g.record(True)
    assert g.check(1) is None
    g.new_command()
    g.record(True), g.record(True), g.record(True)
    assert "3 actions" in g.check(1) and g.exhausted
    g.new_command()
    g.record(False), g.record(False)
    assert "2 failed actions" in g.check(0)
    g.end_command()
    assert g.check(1) is None


def test_audit_log_appends_redacts_and_survives_a_restart(tmp_path):
    log = policy.AuditLog(tmp_path, clock=lambda: 1_790_000_000.0)
    log.write(tool="open_urls", args={"targets": ["x" * 500], "api_key": "sk-123"}, tier=1, outcome="ok")
    line = json.loads(log.path().read_text(encoding="utf-8").splitlines()[0])  # Windows' default is cp1252
    assert line["args"]["api_key"] == "[redacted]" and line["args"]["targets"][0].endswith("…")
    assert len(line["args"]["targets"][0]) == 201
    again = policy.AuditLog(tmp_path, clock=lambda: 1_790_000_000.0)
    assert again.tail()[0]["tool"] == "open_urls"


# ---- one door for every action ----------------------------------------------------

def test_toolbox_refuses_an_unconfirmed_t2_and_logs_who_asked(svc, launched):
    a = Assistant(svc)
    tools = a.tools
    out = tools.run("close_app", {"name": "steam"})
    assert out["blocked"] and "needs a yes" in out["error"]
    assert tools.run("open_app", {"name": "discord"}, context={"source": "voice", "utterance": "open discord"})["ok"]
    recent = tools.audit.tail(2)
    assert recent[0]["tool"] == "open_app" and recent[0]["source"] == "voice" and recent[0]["confirmed_by"] == "auto"
    assert recent[1]["outcome"] == "blocked" and recent[1]["tier"] == 2


def test_new_tools_are_tiered_like_the_research_says(svc):
    t = Assistant(svc).tools.tools
    assert t["set_volume"].tier_for({"level": 5}) == 1 and t["where_am_i"].tier_for({}) == 0
    assert t["close_app"].tier_for({}) == 2 and t["clean_temp"].tier_for({}) == 3
    assert t["power"].tier_for({"action": "lock"}) == 1 and t["power"].tier_for({"action": "shutdown"}) == 3
    assert t["obs_control"].tier_for({"action": "save_replay"}) == 1
    assert t["obs_control"].tier_for({"action": "stop_stream"}) == 3


# ---- confirmations --------------------------------------------------------------

def test_pending_carries_an_id_and_tier_and_a_stale_click_does_nothing(svc, obs_calls):
    published = []
    real_publish = svc.bus.publish
    svc.bus.publish = lambda kind, data, **kw: (published.append((kind, data)), real_publish(kind, data, **kw))
    a = Assistant(svc)
    a.handle("end the stream")
    first = a.pending_view()
    assert first["tier"] == 3 and first["id"]
    assert ("pending", first) in published  # the HUD gets the id to send back
    a.handle("go live")  # replaces what's waiting
    assert a.confirm(first["id"], via="hud") == "That confirmation is out of date; nothing ran."
    assert obs_calls == [] and a.pending_view()
    a.cancel(first["id"])  # a stale "No" can't cancel the newer one either
    assert a.pending_view()
    assert a.confirm(a.pending_view()["id"], via="hud") == "You're going live."
    assert obs_calls == ["start_stream"]
    assert a.tools.audit.tail(1)[0]["confirmed_by"] == "hud"


def test_an_irreversible_yes_needs_the_owners_voice_in_log_mode(svc, obs_calls):
    a = Assistant(svc)
    a.handle("end the stream", "voice")
    out = a.handle("yes", "voice", owner="mismatch")  # LOG mode let a different voice through
    assert "needs your voice" in out["reply"] and obs_calls == []
    assert a.handle("yes", "voice", owner="match")["reply"] == "Stream ended."
    assert obs_calls == ["stop_stream"]
    assert a.tools.audit.tail(1)[0]["confirmed_by"] == "voice"


def test_a_t2_yes_from_log_mode_is_fine(svc, monkeypatch):
    closed = []
    monkeypatch.setattr("assistant.brain.tools.desktop.close_processes",
                        lambda names, protected: closed.append(names) or {"ok": True, "names": sorted(names)})
    a = Assistant(svc)
    a.handle("close steam", "voice")
    a.handle("yes", "voice", owner="mismatch")
    assert closed


# ---- kill switch ----------------------------------------------------------------

def test_stop_everything_by_voice_pauses_control_until_resumed(svc, launched, obs_calls):
    a = Assistant(svc)
    killed = []
    a.on_kill = lambda: killed.append(1)
    a.handle("end the stream")
    assert route("stop everything").kind == "kill" and route("Hands off!").kind == "kill"
    out = a.handle("stop everything", "voice")
    assert "paused" in out["reply"] and killed == [1] and a.pending_view() is None
    assert svc.bus.latest["pc_control"]["data"]["hands_off"] is True
    assert a.handle("open discord")["reply"].startswith("PC control is paused") and launched == []
    assert a.handle("what time is it")["kind"] == "time"  # reads and answers still work
    assert a.handle("resume control")["reply"] == "Back in control."
    a.handle("open discord")
    assert launched == ["discord"]
    kinds = [e["tool"] for e in a.tools.audit.tail(10)]
    assert "kill_switch" in kinds and "resume_control" in kinds


def test_kill_switch_stops_claudes_tool_loop_without_asking_it_again(svc, launched):
    script = [([tool_block("open_app", {"name": "photoshop"}, "t1"), tool_block("open_app", {"name": "obs"}, "t2")], "tool_use"),
              ([text_block("never reached")], "end_turn")]
    a = Assistant(svc, client=FakeClient(script))
    real_run = a.tools.run

    def run_then_kill(name, args=None, **kw):
        out = real_run(name, args, **kw)
        a.tools.guard.stop("hotkey")  # pressed right after the first action
        return out
    a.tools.run = run_then_kill
    out = a.handle("set up my thumbnail workspace")
    assert out["reply"] == "Stopped. PC control is paused." and launched == ["photoshop"]
    assert len(a.client.messages.calls) == 1  # Claude wasn't asked to carry on
    assert a.history[-1] == {"role": "assistant", "content": "Stopped. PC control is paused."}


def test_step_budget_ends_a_runaway_tool_loop(svc, launched):
    svc.cfg["pc_control"]["max_steps"] = 2
    script = [([tool_block("open_app", {"name": f"app{i}"}, f"t{i}") for i in range(4)], "tool_use"),
              ([text_block("never reached")], "end_turn")]
    a = Assistant(svc, client=FakeClient(script))
    out = a.handle("get my whole workspace ready")
    assert launched == ["app0", "app1"] and "2 actions" in out["reply"]
    assert len(a.client.messages.calls) == 1


def test_kill_switch_cuts_a_streaming_reply(svc):
    from test_streaming import streaming_assistant

    a, spoken = streaming_assistant(svc, [([text_block("Let me think about that. Here is a long answer about it.")], "end_turn")])
    real_speak = svc.speak

    def speak_then_kill(text, **kw):
        real_speak(text, **kw)
        a.tools.guard.stop("hotkey")
    svc.speak = speak_then_kill
    out = a.handle("explain something long", "voice", turn=4)
    assert out["reply"] == "Stopped. PC control is paused." and a.history == []


def test_macro_stops_between_steps_when_the_kill_switch_fires(svc, launched):
    from assistant.brain import macros as macro_mod

    svc.cfg["macros"] = {"setup": {"say": ["set up"], "steps": [{"open_app": {"name": "obs"}}, {"open_app": {"name": "discord"}}]}}
    a = Assistant(svc)
    a.macros = macro_mod.load_macros(svc.cfg)
    real_run = a.tools.run

    def run_then_kill(name, args=None, **kw):
        out = real_run(name, args, **kw)
        a.tools.guard.stop("hotkey")
        return out
    a.tools.run = run_then_kill
    out = a.run_macro("setup")
    assert launched == ["obs"] and "stopped" in out["reply"]


# ---- toast Yes/No ------------------------------------------------------------------

def test_toast_answers_only_its_own_pending_action():
    answers = []
    pending = {"id": "abc", "text": "end the stream", "tier": 3}
    for result, expected in (({"arguments": "http:Yes", "user_input": {}}, [("yes", "abc")]),
                             ({"arguments": "http:No"}, [("no", "abc")]), (None, [])):
        answers.clear()
        seen = {}
        conf = toast.ToastConfirmer(lambda pid: answers.append(("yes", pid)), lambda pid: answers.append(("no", pid)),
                                    toast_fn=lambda title, body, buttons: seen.update(title=title, body=body, buttons=buttons) or result)
        conf(pending).join(2)
        assert answers == expected
    assert seen["buttons"] == ["Yes", "No"] and "can't be undone" in seen["title"] and "end the stream" in seen["body"]
    assert toast.clicked({"arguments": "Yes"}) == "yes" and toast.clicked("dismissed") is None


# ---- runtime wiring ------------------------------------------------------------------

def test_runtime_kill_switch_silences_speech_and_cancels_jobs(cfg, svc):
    rt = Runtime(cfg, services=svc)
    interrupts, cancelled = [], []
    rt.speaker = SimpleNamespace(interrupt=lambda silence_turn=True: interrupts.append(silence_turn),
                                 say=lambda text, **kw: interrupts.append(text))
    svc.jobs = SimpleNamespace(cancel=lambda jid: cancelled.append(jid))
    jid = svc.storage.add_job("research", "t", "p", None) if hasattr(svc.storage, "add_job") else None
    if jid is not None:
        svc.storage.update_job(jid, "running")
    out = rt.kill_switch("hotkey")
    assert out["hands_off"] and interrupts[0] is False and "Stopped" in interrupts[1]
    if jid is not None:
        assert cancelled == [jid]
    assert rt.resume_control("hud")["hands_off"] is False


def test_runtime_passes_the_owner_verdict_with_voice_commands(cfg, svc):
    rt = Runtime(cfg, services=svc)
    seen = []
    done = threading.Event()
    rt.assistant = SimpleNamespace(handle=lambda text, source, turn=None, owner=None: seen.append(owner) or done.set())
    rt.listener = SimpleNamespace(last_owner="mismatch")
    rt.voice_command("yes")
    assert done.wait(2) and seen == ["mismatch"]
    rt._commands.shutdown()


def test_spoken_kill_switch_skips_the_queue_it_has_to_stop(cfg, svc):
    rt = Runtime(cfg, services=svc)
    busy = threading.Event()
    release = threading.Event()

    def slow_handle(text, source, turn=None, owner=None):
        busy.set()
        release.wait(3)
    rt.assistant.handle = slow_handle
    rt.voice_command("do a long thing")
    assert busy.wait(2)
    rt.voice_command("Stop everything.")  # must not wait for the long thing to finish
    assert rt.assistant.tools.guard.hands_off
    release.set()
    rt._commands.shutdown()


def test_kill_phrase_needs_no_wake_word():
    import numpy as np

    from assistant.voice.listener import VoiceListener

    commands = []
    speaker = SimpleNamespace(speaking=threading.Event(), last_text="", last_end=0, chime=lambda: None,
                              interrupt=lambda **k: None, expects_reply=False)
    lst = VoiceListener(SimpleNamespace(publish=lambda *a, **k: None), speaker, ["friday"], commands.append, {})
    lst.stt = SimpleNamespace(name="parakeet", transcribe=lambda s, hints=(): "Stop everything!")
    lst._handle_audio(np.zeros(32000, np.int16))
    lst.stt = SimpleNamespace(name="parakeet", transcribe=lambda s, hints=(): "hands off my snacks")
    lst._handle_audio(np.zeros(32000, np.int16))
    assert commands == ["stop everything"]  # the casual one still needs the name


def test_control_endpoints(cfg, svc, launched):
    from fastapi.testclient import TestClient

    from assistant.server import create_app

    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=False)
    with TestClient(app) as c:
        c.headers["X-Assistant-Token"] = app.state.token
        assert c.post("/api/tool/open_app", json={"name": "discord"}).json()["ok"]
        st = c.get("/api/control").json()
        assert st["hands_off"] is False and st["recent"][0]["source"] == "hud"
        assert c.post("/api/control/stop").json()["hands_off"] is True
        assert c.post("/api/tool/open_app", json={"name": "obs"}).json()["blocked"]
        assert c.get("/api/state").json()["pc_control"]["hands_off"] is True
        assert c.post("/api/control/resume").json()["hands_off"] is False
        c.post("/api/command", json={"text": "close steam"})
        assert "out of date" in c.post("/api/confirm", json={"id": "nope"}).json()["reply"]
    assert launched == ["discord"]


def test_listener_records_how_the_owner_was_verified():
    import numpy as np

    from assistant.voice.listener import VoiceListener

    class Verifier:
        def __init__(self, score):
            self.score, self.profile = score, SimpleNamespace(threshold=0.5)

        def check(self, _samples):
            return True, self.score  # LOG mode: always let it through

    commands = []
    speaker = SimpleNamespace(speaking=threading.Event(), last_text="", last_end=0, chime=lambda: None,
                              interrupt=lambda **k: None, expects_reply=False)
    bus = SimpleNamespace(publish=lambda *a, **k: None)
    lst = VoiceListener(bus, speaker, ["friday"], commands.append, {})
    lst.stt = SimpleNamespace(name="parakeet", transcribe=lambda s, hints=(): "Friday, open discord")
    audio = np.zeros(32000, np.int16)
    for score, expected in ((0.8, "match"), (0.2, "mismatch"), (None, "unknown")):
        lst.verifier = Verifier(score)
        lst._handle_audio(audio)
        assert lst.last_owner == expected
    assert commands == ["open discord"] * 3
