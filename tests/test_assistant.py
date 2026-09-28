import json

from conftest import FakeClient, text_block, tool_block

from assistant.brain.assistant import Assistant


def make(svc, script=None):
    a = Assistant(svc, client=FakeClient(script) if script is not None else None)
    a.client = a.client  # explicit for readability
    return a


def test_local_mode_handles_router_commands(svc):
    a = Assistant(svc)
    assert not a.claude_ready
    out = a.handle("add task Ship the HUD")
    assert out["reply"] == "Added: Ship the HUD." and out["kind"] == "tool"
    assert svc.storage.list_tasks()[0]["title"] == "Ship the HUD"
    out = a.handle("write me a poem")
    assert "ANTHROPIC_API_KEY" in out["reply"]


def test_risky_router_action_waits_for_yes(svc, monkeypatch):
    closed = []
    monkeypatch.setattr("assistant.brain.tools.desktop.close_processes", lambda names, protected: closed.append(names) or {"ok": True, "closed": 1, "still_running": 0, "names": sorted(names)})
    a = Assistant(svc)
    out = a.handle("close steam")
    assert out["kind"] == "pending" and "close steam" in out["reply"]
    assert closed == []
    assert a.handle("yes")["reply"].startswith("Closed")
    assert closed and "steam" in closed[0]
    assert a.handle("yes")["reply"] == "Nothing waiting on a yes."


def test_cancel_clears_pending(svc):
    a = Assistant(svc)
    a.handle("go live")
    assert a.pending is not None
    assert a.handle("cancel")["reply"] == "Cancelled."
    assert a.pending is None


def test_claude_tool_loop(svc, monkeypatch):
    monkeypatch.setattr(svc.launcher, "launch", lambda name: {"ok": True, "launched": name, "via": "alias"})
    script = [
        ([text_block("On it."), tool_block("open_app", {"name": "photoshop"})], "tool_use"),
        ([text_block("Photoshop's opening.")], "end_turn"),
    ]
    a = Assistant(svc, client=FakeClient(script))
    out = a.handle("can you get photoshop going so I can make a thumbnail")
    assert out["reply"] == "Photoshop's opening."
    calls = a.client.messages.calls
    assert calls[0]["model"] == "claude-opus-5"
    assert calls[0]["thinking"] == {"type": "adaptive"}
    assert calls[0]["output_config"] == {"effort": "low"}
    assert calls[0]["fallbacks"] == "default" and calls[0]["betas"] == ["server-side-fallback-2026-07-01"]
    assert calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in calls[0]["tools"]} >= {"open_app", "obs_switch_scene", "calendar", "start_job"}
    # second request carries the tool result for the tool_use id
    tool_result = calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"
    assert json.loads(tool_result["content"])["launched"] == "photoshop"
    # the user turn got the volatile context line prepended, not the system prompt
    assert calls[0]["messages"][0]["content"].startswith("[")


def test_claude_risky_tool_is_parked(svc, monkeypatch):
    ran = []
    monkeypatch.setattr(svc.obs, "control", lambda action: ran.append(action) or {"ok": True})
    script = [
        ([tool_block("obs_control", {"action": "start_stream"})], "tool_use"),
        ([text_block("Ready to go live — confirm?")], "end_turn"),
    ]
    a = Assistant(svc, client=FakeClient(script))
    out = a.handle("alright let's get this show on the road")
    assert out["reply"] == "Ready to go live — confirm?"
    assert ran == []
    parked = json.loads(a.client.messages.calls[1]["messages"][-1]["content"][0]["content"])
    assert parked["status"] == "awaiting_confirmation"
    assert a.confirm() == "You're going live."
    assert ran == ["start_stream"]


def test_api_error_rolls_back_history(svc):
    import anthropic
    import httpx

    class Boom:
        def create(self, **kw):
            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.APIConnectionError(request=req)

    a = Assistant(svc, client=FakeClient([]))
    a.client.beta.messages = Boom()
    out = a.handle("summarize my week")
    assert "can't reach Claude" in out["reply"]
    assert a.history == []


def test_briefing_uses_claude_json(svc):
    plan = {"spoken": "Morning J. Two things matter today.", "headline": "Ship + stream", "top_moves": [
        {"move": "Ship v1", "why": "north star", "when": "9-11"}], "sections": [{"title": "Schedule", "bullets": ["Clear"]}], "risks": []}
    a = Assistant(svc, client=FakeClient([([text_block(json.dumps(plan))], "end_turn")]))
    out = a.handle("good morning")
    assert out["kind"] == "briefing" and out["reply"] == plan["spoken"]
    call = a.client.messages.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "high"
    assert svc.bus.latest["briefing"]["data"]["generated_by"] == "claude"


def test_briefing_falls_back_locally(svc):
    svc.storage.add_task("Finish the overlay", "stream", 1)
    a = Assistant(svc)
    b = a.briefing("morning")
    assert b["generated_by"] == "local"
    assert b["spoken"].startswith("Good morning J")
    assert any("north-star" in r for r in b["risks"])  # goal unset in the test config
    assert b["top_moves"][0]["move"] == "Finish the overlay"
    recap = a.briefing("recap")
    assert recap["kind"] == "recap" and "recap" in recap["spoken"].lower()


def test_good_morning_reuses_prebuilt_briefing(svc):
    a = Assistant(svc)
    first = a.briefing("morning")
    out = a.handle("good morning")
    assert out["data"]["created"] == first["created"]  # not rebuilt (and not re-billed)
    a.briefing("recap")
    assert a.handle("good morning")["data"]["kind"] == "morning"  # a recap doesn't count


def test_tasks_added_by_voice_refresh_the_hud(cfg, svc):
    """The first live run: "1 open task" was spoken while the HUD's count still said 0."""
    from assistant.runtime import Runtime

    rt = Runtime(cfg, services=svc)
    seen = []
    rt.bus.on(lambda e: seen.append(e["data"]) if e["type"] == "tasks" else None)
    out = rt.assistant.handle("add a task to record the Twitch setup video")
    assert out["kind"] == "tool" and [t["title"] for t in seen[-1]] == ["record the Twitch setup video"]
    rt._commands.shutdown()

