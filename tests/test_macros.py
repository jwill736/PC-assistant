import json
from pathlib import Path

import yaml
from conftest import FakeClient, text_block, tool_block

from assistant.brain import macros as macro_mod
from assistant.brain.assistant import Assistant

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"

MACROS = {
    "brb": {"say": ["brb", "be right back", "taking a break"],
            "steps": [{"obs_switch_scene": {"scene": "BRB"}},
                      {"obs_set_mute": {"source": "mic", "muted": True}},
                      {"say": "Be right back is up."}]},
    "wrap up stream": {"say": ["wrap it up", "end of stream"],
                       "steps": [{"obs_switch_scene": {"scene": "Ending"}}, {"wait": 5},
                                 {"obs_control": {"action": "stop_stream"}}]},
    "note": {"say": "log it", "steps": [{"command": "add task Edit the highlight"}]},
}


def with_macros(svc, spec=MACROS, client=None):
    svc.cfg["macros"] = spec
    return Assistant(svc, client=client)


def obs_spy(svc, monkeypatch):
    calls = []
    monkeypatch.setattr(svc.obs, "switch_scene", lambda scene: calls.append(("scene", scene)) or {"ok": True, "scene": scene})
    monkeypatch.setattr(svc.obs, "set_mute", lambda source, muted=None: calls.append(("mute", source, muted)) or {"ok": True})
    monkeypatch.setattr(svc.obs, "control", lambda action: calls.append(("control", action)) or {"ok": True})
    return calls


def test_example_config_macros_use_real_tools(svc):
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    a = with_macros(svc, example["macros"])
    assert set(a.macros) == {"brb", "back", "wrap up stream", "focus"}
    assert macro_mod.validate(a.macros, set(a.tools.tools)) == []
    assert a.macro_risks("wrap up stream") and not a.macro_risks("brb")


def test_validate_flags_unknown_steps(svc):
    a = with_macros(svc, {"oops": {"steps": [{"obs_teleport": {}}, {"say": "hi"}]}})
    assert macro_mod.validate(a.macros, set(a.tools.tools)) == ["macro 'oops': unknown step 'obs_teleport'"]


def test_match_phrases():
    m = macro_mod.load_macros({"macros": MACROS})
    assert macro_mod.match("BRB.", m) == "brb"
    assert macro_mod.match("be right back", m) == "brb"
    assert macro_mod.match("be right bak", m) == "brb"            # a Whisper slip still counts
    assert macro_mod.match("run wrap up stream", m) == "wrap up stream"
    assert macro_mod.match("log it", m) == "note"                # a single string trigger works too
    assert macro_mod.match("open discord", m) is None
    assert macro_mod.match("be right back in five minutes with the new overlay", m) is None


def test_spoken_trigger_runs_every_step(svc, monkeypatch):
    calls = obs_spy(svc, monkeypatch)
    out = with_macros(svc).handle("be right back")
    assert out["kind"] == "macro" and out["reply"] == "Be right back is up."
    assert calls == [("scene", "BRB"), ("mute", "mic", True)]
    assert [r["tool"] for r in out["data"]] == ["obs_switch_scene", "obs_set_mute"]


def test_command_steps_run_like_speech(svc):
    out = with_macros(svc).handle("log it")
    assert out["kind"] == "macro"
    assert svc.storage.list_tasks()[0]["title"] == "Edit the highlight"


def test_risky_macro_asks_once_then_runs(svc, monkeypatch):
    calls = obs_spy(svc, monkeypatch)
    waits = []
    monkeypatch.setattr("assistant.brain.assistant.time.sleep", waits.append)
    a = with_macros(svc)
    out = a.handle("wrap it up")
    assert out["kind"] == "pending" and "end the stream" in out["reply"]
    assert calls == []
    out = a.handle("yes")
    assert calls == [("scene", "Ending"), ("control", "stop_stream")]
    assert waits == [5.0]


def test_failed_step_is_reported(svc, monkeypatch):
    monkeypatch.setattr(svc.obs, "switch_scene", lambda scene: {"ok": False, "error": "OBS isn't running."})
    monkeypatch.setattr(svc.obs, "set_mute", lambda source, muted=None: {"ok": True})
    out = with_macros(svc).run_macro("brb")
    assert "OBS isn't running." in out["reply"]
    assert out["data"][0] == {"tool": "obs_switch_scene", "ok": False, "error": "OBS isn't running."}


def test_unknown_macro(svc):
    assert with_macros(svc).run_macro("nope")["kind"] == "error"


def test_claude_can_run_a_macro(svc, monkeypatch):
    calls = obs_spy(svc, monkeypatch)
    script = [
        ([tool_block("run_macro", {"name": "brb"})], "tool_use"),
        ([text_block("BRB scene is up.")], "end_turn"),
    ]
    a = with_macros(svc, client=FakeClient(script))
    out = a.handle("I need to step away for a sec, set things up")
    assert out["reply"] == "BRB scene is up."
    tool = next(t for t in a.client.messages.calls[0]["tools"] if t["name"] == "run_macro")
    assert set(tool["input_schema"]["properties"]["name"]["enum"]) == {"brb", "wrap up stream", "note"}
    assert calls[0] == ("scene", "BRB")
    result = json.loads(a.client.messages.calls[1]["messages"][-1]["content"][0]["content"])
    assert result["ok"] is True and result["reply"] == "Be right back is up." and len(result["steps"]) == 2


def test_claude_risky_macro_is_parked(svc, monkeypatch):
    calls = obs_spy(svc, monkeypatch)
    script = [
        ([tool_block("run_macro", {"name": "wrap up stream"})], "tool_use"),
        ([text_block("Want me to end the stream?")], "end_turn"),
    ]
    a = with_macros(svc, client=FakeClient(script))
    a.handle("ok chat that's it for tonight")
    assert calls == [] and a.pending is not None


def test_no_macros_no_tool(svc):
    assert Assistant(svc).macro_definition() is None
