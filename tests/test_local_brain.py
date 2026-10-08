"""Phase 6: a model on this PC as the brain, and the memory that makes it a second brain.
The local server is faked with httpx.MockTransport, answering the way Ollama and LM Studio do."""

import json
import sqlite3
import time

import httpx
import pytest

from assistant.brain import briefing, local_llm
from assistant.brain.assistant import OFFLINE, Assistant
from assistant.brain.router import route
from assistant.storage import SCHEMA, Storage


class FakeOllama:
    """/api/tags, /api/show, /api/ps, /api/generate (load/unload), Ollama's own /api/chat and the OpenAI-compatible
    /v1/chat/completions, scripted per request. Script steps are written OpenAI-style (a message, or SSE text);
    /api/chat answers the same step in Ollama's shape. A step {"native": [chunks]} is sent as written."""

    def __init__(self, models=("llama3.1:8b",), tools=("llama3.1:8b",), script=(), lmstudio=False, sizes=None,
                 thinking=(), host="127.0.0.1"):
        self.models, self.tools, self.script, self.lmstudio = list(models), set(tools), list(script), lmstudio
        self.sizes, self.thinking, self.host = dict(sizes or {}), set(thinking), host
        self.bodies: list[dict] = []
        self.loaded: dict[str, int] = {}  # name -> bytes on the GPU
        self.unloaded: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if self.lmstudio:
            if req.url.port != 1234:
                raise httpx.ConnectError("refused", request=req)
            if path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        elif req.url.port != 11434 or req.url.host != self.host:
            raise httpx.ConnectError("refused", request=req)
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": m, "size": self.sizes.get(m), "details": {"parameter_size": m.split(":")[-1].split("-")[0].upper()
                                                                   if ":" in m else ""}} for m in self.models]})
        if path == "/api/show":
            name = json.loads(req.content)["model"]
            caps = ["completion"] + (["tools"] if name in self.tools else []) + (["thinking"] if name in self.thinking else [])
            return httpx.Response(200, json={"capabilities": caps})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": n, "size": b, "size_vram": b} for n, b in self.loaded.items()]})
        if path == "/api/generate":
            body = json.loads(req.content)
            if body.get("keep_alive") == 0:
                self.loaded.pop(body["model"], None)
                self.unloaded.append(body["model"])
            else:
                self.loaded[body["model"]] = self.sizes.get(body["model"]) or 1
            return httpx.Response(200, json={"done": True})
        if path in ("/v1/chat/completions", "/api/chat"):
            body = json.loads(req.content)
            self.bodies.append(body)
            if path == "/api/chat":
                self.loaded.setdefault(body["model"], self.sizes.get(body["model"]) or 1)
            step = self.script.pop(0)
            if path == "/api/chat":
                return self._native(step, body.get("stream", True))
            if body.get("stream"):
                return httpx.Response(200, text=step, headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"choices": [{"message": step}]})
        return httpx.Response(404)

    @staticmethod
    def _native(step, stream: bool) -> httpx.Response:
        if isinstance(step, dict) and "native" in step:
            chunks = step["native"]
        elif isinstance(step, str):  # SSE: one chunk per text delta, tool-call fragments joined into whole calls
            chunks, parts = [], {}
            for line in step.splitlines():
                if not line.startswith("data:") or line.strip() == "data: [DONE]":
                    continue
                delta = json.loads(line[5:])["choices"][0].get("delta") or {}
                if delta.get("content"):
                    chunks.append({"message": {"role": "assistant", "content": delta["content"]}, "done": False})
                for tc in delta.get("tool_calls") or []:
                    p = parts.setdefault(tc.get("index", 0), {"name": "", "args": ""})
                    p["name"] += (tc.get("function") or {}).get("name") or ""
                    p["args"] += (tc.get("function") or {}).get("arguments") or ""
            if parts:
                chunks.append({"message": {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": p["name"], "arguments": json.loads(p["args"] or "{}")}} for p in parts.values()]},
                    "done": False})
        else:
            msg = {"role": "assistant", "content": step.get("content") or ""}
            if step.get("tool_calls"):
                msg["tool_calls"] = [{"id": c.get("id"), "function": {"name": c["function"]["name"],
                                                                      "arguments": json.loads(c["function"]["arguments"])}}
                                     for c in step["tool_calls"]]
            chunks = [{"message": msg, "done": False}]
        if not stream:
            errors = [c["error"] for c in chunks if c.get("error")]
            if errors:  # Ollama answers a failed non-streamed request with an error status
                return httpx.Response(500, json={"error": errors[0]})
            content = "".join((c.get("message") or {}).get("content") or "" for c in chunks)
            calls = [tc for c in chunks for tc in (c.get("message") or {}).get("tool_calls") or []]
            msg = {"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})}
            return httpx.Response(200, json={"message": msg, "done": True})
        lines = chunks + [{"message": {"role": "assistant", "content": ""}, "done": True}]
        return httpx.Response(200, text="".join(json.dumps(c) + "\n" for c in lines))


def http(fake) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake))


def tool_msg(name, args, content=""):
    return {"role": "assistant", "content": content,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def local_assistant(svc, fake, provider="auto") -> Assistant:
    svc.cfg["brain"] = {"provider": provider, "local": {**svc.cfg["brain"]["local"], "enabled": True}}
    a = Assistant(svc)
    a.local = local_llm.LocalBrain(svc.cfg["brain"]["local"], http=http(fake))
    return a


# ---- finding the model ----------------------------------------------------------------

def test_detect_picks_a_tools_capable_mid_size_model_and_skips_embeddings():
    fake = FakeOllama(models=["nomic-embed-text:latest", "llama3.2:1b", "llama3.1:8b", "qwen2.5:32b", "gemma2:9b"],
                      tools=["llama3.2:1b", "llama3.1:8b", "qwen2.5:32b"])
    found = local_llm.detect(http(fake))
    assert found["kind"] == "ollama" and found["url"] == "http://127.0.0.1:11434"
    assert local_llm.pick_model(found["models"]) == "llama3.1:8b"  # tools, and 3-14B: fast enough to talk to
    by = {m["name"]: m for m in found["models"]}
    assert by["gemma2:9b"]["tools"] is False and by["llama3.1:8b"]["size_b"] == 8.0


def test_detect_finds_lm_studio_and_the_brain_keeps_looking(monkeypatch):
    fake = FakeOllama(models=["meta-llama-3.1-8b-instruct"], lmstudio=True)
    found = local_llm.detect(http(fake))
    assert found["kind"] == "lmstudio" and found["models"][0]["tools"] is None
    clock = [0.0]
    brain = local_llm.LocalBrain({"enabled": True}, http=http(FakeOllama(models=[], lmstudio=True)), clock=lambda: clock[0])
    assert brain.ready is False
    brain.http = http(fake)
    assert brain.ready is False           # looked less than a minute ago
    clock[0] = 61
    assert brain.ready and brain.status()["model"] == "meta-llama-3.1-8b-instruct" and brain.status()["server"] == "LM Studio"


def test_a_named_model_wins_and_disabled_means_never():
    fake = FakeOllama(models=["llama3.2:3b", "llama3.1:8b"], tools=["llama3.2:3b", "llama3.1:8b"])
    assert local_llm.LocalBrain({"model": "llama3.2"}, http=http(fake)).refresh().model == "llama3.2:3b"
    assert local_llm.LocalBrain({"enabled": False}, http=http(fake)).refresh() is None


# ---- talking to it -------------------------------------------------------------------

def test_streamed_text_and_tool_call_fragments_are_reassembled():
    sse = "".join(f"data: {json.dumps(x)}\n\n" for x in [
        {"choices": [{"delta": {"content": "Adding "}}]},
        {"choices": [{"delta": {"content": "it."}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c9", "function": {"name": "add_", "arguments": '{"ti'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "task", "arguments": 'tle": "mic arm"}'}}]}}]},
    ]) + "data: [DONE]\n\n"
    llm = local_llm.LocalLLM("http://127.0.0.1:11434", "llama3.1:8b", http=http(FakeOllama(script=[sse])))
    heard = []
    out = llm.chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}], on_text=heard.append)
    assert heard == ["Adding ", "it."] and out["content"] == "Adding it."
    assert out["tool_calls"] == [{"id": "c9", "name": "add_task", "arguments": {"title": "mic arm"}}]


def test_a_tool_call_written_as_text_is_recovered_and_not_spoken():
    raw = '{"name": "add_task", "parameters": {"title": "buy a mic arm"}}'
    llm = local_llm.LocalLLM("http://127.0.0.1:11434", "llama3.2:3b", http=http(FakeOllama(script=[{"role": "assistant", "content": raw}])))
    out = llm.chat([], tools=[{"type": "function"}])
    assert out == {"content": "", "tool_calls": [{"id": out["tool_calls"][0]["id"], "name": "add_task",
                                                   "arguments": {"title": "buy a mic arm"}}]}


# ---- the assistant with a local brain ----------------------------------------------------

def test_open_question_goes_to_the_local_model_which_uses_tools(svc):
    fake = FakeOllama(script=[tool_msg("add_task", {"title": "buy a mic arm"}), {"role": "assistant", "content": "Added it to your list."}])
    a = local_assistant(svc, fake)
    out = a.handle("I really need to remember to get a mic arm this week, can you put that somewhere")
    assert out == {**out, "reply": "Added it to your list.", "kind": "local"}
    assert [t["title"] for t in svc.storage.list_tasks()] == ["buy a mic arm"]
    first, second = fake.bodies
    assert first["messages"][-1] == {"role": "user", "content": "I really need to remember to get a mic arm this week, "
                                                                 "can you put that somewhere"}  # context lives in the system message
    assert "Right now: " in first["messages"][0]["content"] and "active window" in first["messages"][0]["content"]
    names = {t["function"]["name"] for t in first["tools"]}
    assert {"add_task", "recall", "calendar"} <= names and "close_app" in names and "twitch_chat_recent" not in names
    assert first["messages"][0]["role"] == "system" and "exact names" in first["messages"][0]["content"]
    tool_result = second["messages"][-1]
    assert tool_result["role"] == "tool" and tool_result["tool_call_id"] == "call_1" and "mic arm" in tool_result["content"]
    assert a.local_history[-1] == {"role": "assistant", "content": "Added it to your list."}


def test_a_risky_local_tool_call_still_waits_for_yes(svc, monkeypatch):
    closed = []
    monkeypatch.setattr("assistant.brain.tools.desktop.close_processes",
                        lambda names, protected: closed.append(names) or {"ok": True, "closed": 1, "still_running": 0, "names": []})
    fake = FakeOllama(script=[tool_msg("close_app", {"name": "steam"}), {"role": "assistant", "content": "Close Steam?"}])
    a = local_assistant(svc, fake)
    assert a.handle("steam is eating my ram, get rid of it")["reply"] == "Close Steam?"
    assert closed == [] and a.pending and "close" in a.pending["text"]
    parked = json.loads(fake.bodies[1]["messages"][-1]["content"])
    assert parked["status"] == "awaiting_confirmation"


def test_which_brain_answers(svc, fake_client):
    fake = FakeOllama(script=[{"role": "assistant", "content": "local"}])
    a = local_assistant(svc, fake)
    assert a.brain() == "local"
    a.client = fake_client([])  # a Claude key appears: auto prefers Claude
    assert a.brain() == "claude"
    svc.cfg["brain"]["provider"] = "local"  # "local" means never the cloud
    assert a.brain() == "local"
    a.local = local_llm.LocalBrain({"enabled": True}, http=http(FakeOllama(models=[], lmstudio=True)))
    assert a.brain() is None and a._ask("anything") == OFFLINE
    assert a.brain_status()["provider"] == "local" and a.brain_status()["claude"]["ready"] is True


def test_no_model_at_all_says_how_to_get_one(svc):
    a = Assistant(svc)
    out = a.handle("write me a stream title for tonight")
    assert out["kind"] == "offline" and "Ollama" in out["reply"] and "ANTHROPIC_API_KEY" in out["reply"]


def test_local_model_down_mid_session_is_said_plainly(svc):
    class Down(FakeOllama):
        def __call__(self, req):
            if req.url.path in ("/v1/chat/completions", "/api/chat"):
                raise httpx.ConnectError("refused", request=req)
            return super().__call__(req)
    a = local_assistant(svc, Down())
    reply = a.handle("what's a good name for my overlay pack")["reply"]
    assert reply == "I can't reach llama3.1:8b on Ollama anymore. Is it still running?"


def test_morning_plan_from_the_local_model(svc):
    plan = {"headline": "Ship the overlay", "spoken": "Morning. Ship the overlay first.", "top_moves": [], "risks": []}
    fake = FakeOllama(script=[{"role": "assistant", "content": json.dumps(plan)}])
    a = local_assistant(svc, fake)
    b = a.briefing("morning")
    assert b["generated_by"] == "local" and b["model"] == "llama3.1:8b" and b["spoken"] == plan["spoken"]
    assert fake.bodies[0]["format"]["required"] == briefing.BRIEFING_SCHEMA["required"]  # Ollama's JSON-schema output


# ---- memory ------------------------------------------------------------------------------------

def test_recall_by_voice_finds_notes_tasks_and_conversation(svc):
    a = Assistant(svc)
    a.handle("remember that the new overlay colours are orange and black")
    a.handle("add a task to record the Twitch setup video")
    time.sleep(2.1)  # recall hides the last two seconds (the question itself is logged)
    out = a.handle("what did I say about the overlay colors")
    assert out["tool"] == "recall" and "you noted: the new overlay colours are orange and black" in out["reply"]
    hits = out["data"]["hits"]
    assert hits[0]["kind"] == "note" and all("what did i say" not in h["text"].lower() for h in hits)
    assert "record the twitch setup video" in a.handle("do you remember anything about the twitch video")["reply"].lower()
    assert a.handle("what do I know about submarines")["reply"] == "I don't have anything about submarines yet."


def test_memory_indexes_what_an_older_install_already_has(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO notes(text, created) VALUES ('stream on Wednesdays at eight', ?)", (time.time() - 99,))
    db.commit()
    db.close()
    s = Storage(path)
    assert [h["text"] for h in s.search_memory("when do I stream")] == ["stream on Wednesdays at eight"]
    s.add_note("new mic arm arrives Friday")
    assert s.search_memory("mic")[0]["text"] == "new mic arm arrives Friday"  # kept in step by the trigger


@pytest.mark.parametrize("heard,query", [
    ("what did I say about the overlay", "the overlay"),
    ("do you remember when I stream", "i stream"),
    ("search my notes for mic arm", "mic arm"),
    ("what are my notes", ""),
])
def test_recall_phrases_route_locally(heard, query):
    intent = route(heard)
    assert (intent.tool, intent.args) == ("recall", {"query": query})


# ---- getting connected ---------------------------------------------------------------------

def test_the_pc_scan_finds_local_ai_running_installed_or_absent(tmp_path, cfg):
    from assistant import discovery

    cfg["brain"]["local"]["enabled"] = True
    paths = discovery.SystemPaths(env={"LOCALAPPDATA": str(tmp_path)}, home=tmp_path)
    running = {"kind": "ollama", "url": "http://127.0.0.1:11434",
               "models": [{"name": "llama3.1:8b", "size_b": 8.0, "tools": True}, {"name": "nomic-embed-text", "tools": False}]}
    f = discovery.scan_local_ai(paths, cfg, detector=lambda extra_url=None: running)[0]
    assert (f.status, f.name) == ("connected", "Local AI (Ollama)") and "Answers with llama3.1:8b" in f.detail
    f = discovery.scan_local_ai(paths, cfg, detector=lambda extra_url=None: {**running, "models": []})[0]
    assert f.status == "action" and "ollama pull" in f.fix
    exe = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    f = discovery.scan_local_ai(paths, cfg, detector=lambda extra_url=None: None)[0]
    assert f.status == "action" and f.detail == "Ollama is installed but not running."
    exe.unlink()
    assert discovery.scan_local_ai(paths, cfg, detector=lambda extra_url=None: None)[0].status == "missing"
    # no Claude key is only a gap when nothing else can answer
    assert discovery.scan_accounts(cfg, [], local_ai=True)[0].status == "found"
    assert discovery.scan_accounts(cfg, [], local_ai=False)[0].status == "action"


def test_health_check_names_the_brain(cfg):
    from assistant import doctor

    found = local_llm.LocalBrain({"enabled": True}, http=http(FakeOllama()))
    cfg["brain"]["local"]["enabled"] = True
    assert doctor.check_brain(cfg, found).detail == "local: llama3.1:8b on Ollama (no Claude key)"
    none = local_llm.LocalBrain({"enabled": True}, http=http(FakeOllama(models=[], lmstudio=True)))
    assert doctor.check_brain(cfg, none).status == doctor.WARN
    cfg["brain"]["provider"] = "local"
    assert doctor.check_brain(cfg, none).status == doctor.FAIL


def test_whats_connected_by_voice(svc):
    svc.connections = lambda: {"brain": {"active": "local", "local": {"model": "llama3.1:8b", "server": "Ollama"}},
                               "findings": [{"name": "OBS", "status": "connected", "detail": "", "fix": ""},
                                            {"name": "Local AI (Ollama)", "status": "connected", "detail": "", "fix": ""},
                                            {"name": "Calendars", "status": "action", "detail": "0 connected.",
                                             "fix": "Paste your iCal link into .env."}]}
    reply = Assistant(svc).handle("what's connected")["reply"]
    assert reply == ("I'm thinking with llama3.1:8b on Ollama. Connected: OBS, Local AI (Ollama). "
                     "Needs you: Calendars, Paste your iCal link into .env.")


def test_brain_panel_endpoints(cfg, svc):
    from fastapi.testclient import TestClient

    from assistant.runtime import Runtime
    from assistant.server import create_app

    rt = Runtime(cfg, services=svc)
    fake = FakeOllama(models=["llama3.2:3b", "llama3.1:8b"], tools=["llama3.2:3b", "llama3.1:8b"],
                      script=[{"role": "assistant", "content": "ready"}, tool_msg("set_volume", {"level": 20})])
    cfg["brain"]["local"]["enabled"] = True
    rt.assistant.local = local_llm.LocalBrain(cfg["brain"]["local"], http=http(fake))
    app = create_app(rt, start_background=False)
    with TestClient(app) as c:
        h = {"x-assistant-token": app.state.token}
        st = c.get("/api/brain", headers=h).json()
        assert st["active"] == "local" and st["local"]["model"] == "llama3.1:8b" and st["local"]["models"] == ["llama3.2:3b", "llama3.1:8b"]
        st = c.post("/api/brain", json={"provider": "local", "model": "llama3.2:3b"}, headers=h).json()
        assert st["ok"] and st["provider"] == "local" and st["local"]["model"] == "llama3.2:3b"
        assert "provider: local" in (cfg.data_dir / "settings.yaml").read_text()  # remembered across restarts
        t = c.post("/api/brain/test", headers=h).json()
        assert t["ok"] and t["reply"] == "ready" and t["ms"] >= 0
        assert t["acts"] is True  # it also picked the right action for "set the volume to 20" (never run)
        assert c.post("/api/brain", json={"provider": "gpt"}, headers=h).json()["ok"] is False
    rt._commands.shutdown()


@pytest.mark.parametrize("echo", [
    "[Monday September 28 2026, 01:18 AM | active window: unknown | profile: work]\nTask added: Buy a new mic arm.",
    # llama3.2:3b on CI, once the context moved to the system message without brackets
    "Thursday October 08 2026, 1:00 PM | active window: To Do List | profile: work\n\nYou have a new task: Buy new mic arm.",
    "Right now: Thursday October 08 2026, 1:00 PM | active window: To Do List | profile: work\nYou have a new task: Buy new mic arm.",
])
def test_every_form_of_the_context_echo_is_dropped(echo):
    from assistant.brain.assistant import CONTEXT_ECHO

    said = CONTEXT_ECHO.sub("", echo)
    assert "active window" not in said and said.startswith(("Task added", "You have a new task"))
    assert CONTEXT_ECHO.sub("", "[Monday 01:18 AM | active window: unknown | profile: work] Done. It's on your list.") == \
        "Done. It's on your list."  # the words after a bracketed echo are kept
    assert CONTEXT_ECHO.sub("", "Your profile: work stays on. The active window is OBS.") == \
        "Your profile: work stays on. The active window is OBS."  # ordinary sentences are left alone


def test_a_repeated_context_line_is_never_said(svc):
    """llama3.2:3b on the CI run answered "[Monday September 28 2026, 01:18 AM | active window: unknown | profile: work]
    Task added: Buy a new mic arm." That first line would have been spoken."""
    echo = "[Monday September 28 2026, 01:18 AM | active window: unknown | profile: work]\nTask added: Buy a new mic arm."
    fake = FakeOllama(script=[{"role": "assistant", "content": echo}])
    a = local_assistant(svc, fake)
    assert a.handle("please sort out the mic arm thing")["reply"] == "Task added: Buy a new mic arm."
    said = []
    a._speak_sentence = lambda s, final: said.append(s)
    a._stream_on = True
    sse = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n"
                  for c in ["[Monday 01:18 AM | active window: unknown | profile: work] ", "Done. ", "It's on your list."]) + "data: [DONE]\n\n"
    a.local.llm.http = http(FakeOllama(script=[sse]))
    a._ask_local("and the other thing")
    assert said and not any("active window" in s for s in said)



def test_the_local_model_is_not_a_memory_hog_to_close(monkeypatch):
    """The first local morning plan (llama3.2:3b) flagged llama-server 2.5 GB as a heavy app to deal with."""
    from assistant.integrations.system import SystemMonitor

    mon = SystemMonitor(heavy_process_mb=1500)
    gb = 1024
    monkeypatch.setattr(mon, "snapshot", lambda include_processes=True: {
        "memory": {"percent": 60}, "cpu": {"percent": 10}, "disks": [], "gpus": [], "power_plan": None,
        "processes": {"by_mem": [{"name": "llama-server", "mem_mb": 2.5 * gb, "count": 1},
                                 {"name": "ollama_llama_server", "mem_mb": 5 * gb, "count": 1},
                                 {"name": "chrome", "mem_mb": 2 * gb, "count": 30}], "by_cpu": []}})
    heavy = [f for f in mon.analyze()["findings"] if f["title"] == "Heavy apps running"]
    assert heavy and "chrome" in heavy[0]["detail"] and "llama" not in heavy[0]["detail"]


def test_a_rambling_local_plan_is_cut_to_about_ninety_words():
    from assistant.brain.assistant import _clip_spoken

    long = " ".join(f"Sentence number {i} has exactly seven words here." for i in range(40))
    clipped = _clip_spoken(long)
    assert len(clipped.split()) <= 90 and clipped.endswith(".") and clipped.startswith("Sentence number 0")
    assert _clip_spoken("Short and sweet.") == "Short and sweet."
