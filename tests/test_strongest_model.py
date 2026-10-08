"""The strongest model that works on this PC: chosen by what fits the graphics card, then proven by a test that
loads each model and checks it takes the right actions and answers quickly. Thinking models never read their
reasoning aloud, a model that can't take actions answers without pretending to, and while you're live the light
model answers and the big one is unloaded.

The model list is the real one from J's PC (`ollama list`, RTX 5090 with 32 GB)."""

import json
import time

import httpx
import pytest

from assistant.brain import local_llm, model_bench
from assistant.brain.assistant import Assistant
from test_local_brain import FakeOllama, http

RTX_5090_MB = 32607
J_MODELS = {  # name: bytes, from `ollama list`
    "gemma4:31b-it-qat": 18e9, "qwen3.6:27b-q8_0": 29e9, "qwen3.6:35b-a3b": 23e9, "elite-coach:latest": 42e9,
    "gemma4:latest": 9.6e9, "llama3.3:70b": 42e9, "llama3.1:8b": 4.9e9,
}
QWEN = ("qwen3.6:27b-q8_0", "qwen3.6:35b-a3b")


def j_models(gemma_tools: bool) -> list[dict]:
    tools = {"llama3.1:8b", "llama3.3:70b", *QWEN} | ({"gemma4:31b-it-qat", "gemma4:latest"} if gemma_tools else set())
    return [{"name": n, "bytes": b, "size_b": local_llm._size_b(n.split(":")[1].split("-")[0]), "tools": n in tools,
             "caps": ["completion"] + (["tools"] if n in tools else []) + (["thinking"] if n in QWEN else [])}
            for n, b in J_MODELS.items()]


# ---- choosing without a test ---------------------------------------------------------------------------

def test_on_a_5090_the_strongest_model_that_fits_is_chosen_not_the_8b():
    # gemma4 31B (18 GB) fits and is dense; qwen3.6 27B at q8 (29 GB) leaves no room; the 70Bs never fit.
    assert local_llm.pick_model(j_models(gemma_tools=True), RTX_5090_MB) == "gemma4:31b-it-qat"
    # if gemma4 can't take actions, the 35B mixture-of-experts model is next (it acts like ~10B, above the 8B)
    assert local_llm.pick_model(j_models(gemma_tools=False), RTX_5090_MB) == "qwen3.6:35b-a3b"
    # unknown card (no nvidia-smi): the old, safe choice
    assert local_llm.pick_model(j_models(gemma_tools=True)) == "llama3.1:8b"
    # a small card fits nothing with room to spare: also the safe choice
    assert local_llm.pick_model(j_models(gemma_tools=True), 8192) == "llama3.1:8b"


def test_what_fits_and_how_strong():
    by = {m["name"]: m for m in j_models(True)}
    assert local_llm.fits(by["qwen3.6:35b-a3b"], RTX_5090_MB) is True
    assert local_llm.fits(by["qwen3.6:27b-q8_0"], RTX_5090_MB) is False
    assert local_llm.fits(by["llama3.3:70b"], RTX_5090_MB) is False
    assert local_llm.fits(by["llama3.1:8b"], None) is None
    assert round(local_llm.strength(by["qwen3.6:35b-a3b"]), 1) == 10.2 and local_llm.strength(by["gemma4:31b-it-qat"]) == 31
    assert local_llm.pick_light_model(j_models(True)) == "llama3.1:8b"


# ---- the test that proves it ------------------------------------------------------------------------------

class JsPC(FakeOllama):
    """Ollama on J's PC: each model answers the test the way it's set up to."""

    def __init__(self, acts=(), slow=(), no_tools=(), thinks_aloud=()):
        super().__init__(models=list(J_MODELS), tools=[n for n in J_MODELS if n not in no_tools],
                         sizes={n: int(b) for n, b in J_MODELS.items()}, thinking=QWEN)
        self.acts, self.slow, self.no_tools, self.thinks_aloud = set(acts), set(slow), set(no_tools), set(thinks_aloud)

    def __call__(self, req):
        if req.url.path == "/api/tags":
            r = super().__call__(req)
            data = r.json()
            for m in data["models"]:  # what Ollama reports as parameter_size
                m["details"]["parameter_size"] = {"gemma4:31b-it-qat": "31.3B", "qwen3.6:27b-q8_0": "27.8B",
                                                  "qwen3.6:35b-a3b": "35.1B", "elite-coach:latest": "70.6B",
                                                  "gemma4:latest": "8.0B", "llama3.3:70b": "70.6B",
                                                  "llama3.1:8b": "8.0B"}.get(m["name"], "14B")
            return httpx.Response(200, json=data)
        if req.url.path != "/api/chat":
            return super().__call__(req)
        body = json.loads(req.content)
        self.bodies.append(body)
        name, said = body["model"], (body["messages"] or [{"content": ""}])[-1]["content"]
        self.loaded.setdefault(name, self.sizes.get(name) or 1)
        if body.get("tools") and name in self.no_tools:
            return httpx.Response(400, json={"error": f"registry.ollama.ai/library/{name} does not support tools"})
        if name in self.slow:
            time.sleep(0.05)
        if said in [p[0] for p in model_bench.PROBES] and name in self.acts:
            call = {"function": {"name": "set_volume", "arguments": {"level": 20}}} if "volume" in said else \
                {"function": {"name": "add_task", "arguments": {"title": "buy a new mic arm"}}}
            msg = {"role": "assistant", "content": "", "tool_calls": [call]}
        elif said in [p[0] for p in model_bench.PROBES]:
            msg = {"role": "assistant", "content": "Sure, done!"}  # says it, doesn't do it
        else:
            text = "So viewers have something to watch while the streamer gets ready."
            msg = {"role": "assistant", "content": (f"<think>{'hmm ' * 50}</think>" if name in self.thinks_aloud else "") + text}
        if not body.get("stream"):
            return httpx.Response(200, json={"message": msg, "done": True})
        words = msg["content"].split(" ")
        lines = [{"message": {"role": "assistant", "content": w + " "}, "done": False} for w in words]
        lines.append({"message": {"role": "assistant", "content": ""}, "done": True})
        return httpx.Response(200, text="".join(json.dumps(x) + "\n" for x in lines))


def bench(fake, vram=RTX_5090_MB, **kw):
    found = local_llm.detect(http(fake))
    tools = [{"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object"}}}
             for n in ("set_volume", "add_task", "open_app")]
    return model_bench.run(found, tools, vram, http=http(fake), **kw)


def test_the_test_picks_the_strongest_that_passes_and_the_lightest_for_stream_nights():
    fake = JsPC(acts={"qwen3.6:35b-a3b", "llama3.1:8b"})  # gemma4 31B answers but says "done" without acting
    progress = []
    result = bench(fake, on_progress=progress.append)
    rows = {r["name"]: r for r in result["results"]}
    assert result["best"] == "qwen3.6:35b-a3b" and result["light"] == "llama3.1:8b"
    assert rows["gemma4:31b-it-qat"]["result"] == "fail" and "right action 0 of 2" in rows["gemma4:31b-it-qat"]["note"]
    assert rows["llama3.3:70b"]["result"] == "too_big" and "GB of video memory" in rows["llama3.3:70b"]["note"]
    assert rows["qwen3.6:27b-q8_0"]["result"] == "too_big"
    assert rows["qwen3.6:35b-a3b"]["actions"] == "2/2" and rows["qwen3.6:35b-a3b"]["first_word_s"] is not None
    # tested strongest first, each loaded then unloaded so the next has the card to itself
    tested = [r["name"] for r in result["results"] if "load_s" in r]
    assert tested == ["gemma4:31b-it-qat", "qwen3.6:35b-a3b", "gemma4:latest", "llama3.1:8b"]
    assert fake.unloaded == tested and fake.loaded == {}
    # thinking models were asked not to think; nothing was ever executed
    assert all(b.get("think") is False for b in fake.bodies if b["model"] in QWEN)
    assert any(p.get("testing") == "qwen3.6:35b-a3b" for p in progress)
    assert "qwen3.6:35b-a3b" in model_bench.summary(result) and "llama3.1:8b" in model_bench.summary(result)


def test_reasoning_written_into_the_answer_is_never_heard_or_kept():
    fake = JsPC(acts=set(J_MODELS), thinks_aloud=set(J_MODELS))
    result = bench(fake)
    winner = next(r for r in result["results"] if r["name"] == result["best"])
    assert "hmm" not in winner["answer"] and winner["answer"].startswith("So viewers")


def test_a_model_that_cant_take_actions_is_skipped_or_answers_without_pretending():
    fake = JsPC(acts=set(J_MODELS), no_tools={"gemma4:31b-it-qat", "gemma4:latest"})
    result = bench(fake)
    rows = {r["name"]: r for r in result["results"]}
    assert rows["gemma4:31b-it-qat"]["result"] == "no_actions" and result["best"] == "qwen3.6:35b-a3b"
    # older Ollama that doesn't list capabilities: the 400 is caught and it answers without the tools
    llm = local_llm.OllamaLLM("http://127.0.0.1:11434", "gemma4:latest", http=http(fake), caps=None)
    out = llm.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    assert out["content"].startswith("So viewers") and llm.can_act is False


def test_nothing_passes_says_why():
    result = bench(JsPC(acts=()))
    assert result["best"] is None and "None of your models passed" in model_bench.summary(result)


def test_lm_studio_cant_be_tested():
    assert "needs Ollama" in model_bench.run({"kind": "lmstudio", "url": "x", "models": []}, [])["error"]


# ---- what the brain uses, and stream nights ----------------------------------------------------------------

def brain_on_js_pc(tmp_path, fake, cfg=None, best="qwen3.6:35b-a3b", light="llama3.1:8b"):
    path = tmp_path / f"bench-{best}.json"
    if best:
        path.write_text(json.dumps({"best": best, "light": light, "models": list(J_MODELS), "results": []}))
    return local_llm.LocalBrain({"enabled": True, **(cfg or {})}, http=http(fake), vram_mb=lambda: RTX_5090_MB,
                                bench_path=path)


def test_the_tested_winner_answers_and_a_pinned_model_still_wins(tmp_path):
    fake = JsPC(acts=set(J_MODELS))
    assert brain_on_js_pc(tmp_path, fake).refresh().model == "qwen3.6:35b-a3b"
    assert brain_on_js_pc(tmp_path, fake, {"model": "llama3.1:8b"}).refresh().model == "llama3.1:8b"
    # never tested: the strongest that fits
    assert brain_on_js_pc(tmp_path, fake, best=None).refresh().model == "gemma4:31b-it-qat"


def test_going_live_switches_to_the_light_model_and_frees_the_big_ones_memory(tmp_path):
    fake = JsPC(acts=set(J_MODELS))
    brain = brain_on_js_pc(tmp_path, fake)
    llm = brain.refresh()
    llm.chat([{"role": "user", "content": "hi"}])  # loads it
    assert "qwen3.6:35b-a3b" in fake.loaded
    change = brain.set_streaming(True)
    assert change == {"streaming": True, "from": "qwen3.6:35b-a3b", "to": "llama3.1:8b", "freed_gb": 21.4}
    assert brain.llm.model == "llama3.1:8b" and "qwen3.6:35b-a3b" not in fake.loaded
    assert brain.set_streaming(True) is None  # already live
    assert brain.status()["streaming"] is True
    back = brain.set_streaming(False)
    assert back["to"] == "qwen3.6:35b-a3b" and back["freed_gb"] is None and brain.llm.model == "qwen3.6:35b-a3b"
    same = brain_on_js_pc(tmp_path, fake, {"stream_model": "same"})
    same.refresh()
    assert same.set_streaming(True) is None and same.llm.model == "qwen3.6:35b-a3b"


def test_the_status_lists_each_model_with_its_test_result(tmp_path):
    fake = JsPC(acts={"qwen3.6:35b-a3b", "llama3.1:8b"})
    result = bench(fake)
    path = tmp_path / "brain_bench.json"
    model_bench.save(result, path)
    brain = local_llm.LocalBrain({"enabled": True}, http=http(fake), vram_mb=lambda: RTX_5090_MB, bench_path=path)
    brain.refresh()
    st = brain.status()
    by = {c["name"]: c for c in st["choices"]}
    assert st["model"] == "qwen3.6:35b-a3b" and st["bench"]["light"] == "llama3.1:8b" and st["vram_gb"] == 31.8
    assert by["qwen3.6:35b-a3b"]["result"] == "pass" and by["llama3.3:70b"]["result"] == "too_big"
    assert by["llama3.3:70b"]["fits"] is False and st["can_act"] is True and st["pinned"] is None


# ---- thinking text --------------------------------------------------------------------------------------

def test_visible_text_never_includes_reasoning_even_mid_stream():
    seen = [local_llm.visible_so_far(t) for t in
            ["Hi", "Hi <", "Hi <thi", "Hi <think>let me", "Hi <think>let me see</think>", "Hi <think>x</think> there"]]
    assert seen == ["Hi", "Hi ", "Hi ", "Hi ", "Hi ", "Hi  there"]
    assert local_llm.visible_so_far("a < b") == "a < b"
    assert local_llm.strip_thinking("<think>\n\n</think>\n\nReady.").strip() == "Ready."


def test_thinking_models_are_asked_not_to_think_and_their_thinking_field_is_ignored():
    native = {"native": [{"message": {"role": "assistant", "content": "", "thinking": "The user wants..."}},
                         {"message": {"role": "assistant", "content": "Twenty it is."}}]}
    fake = FakeOllama(models=["qwen3.6:35b-a3b"], tools=["qwen3.6:35b-a3b"], thinking=["qwen3.6:35b-a3b"], script=[native])
    llm = local_llm.OllamaLLM("http://127.0.0.1:11434", "qwen3.6:35b-a3b", http=http(fake),
                              caps=["completion", "tools", "thinking"], num_ctx=16384)
    heard = []
    out = llm.chat([{"role": "user", "content": "x"}], on_text=heard.append)
    assert heard == ["Twenty it is."] and out["content"] == "Twenty it is."
    body = fake.bodies[0]
    assert body["think"] is False and body["options"]["num_ctx"] == 16384 and body["keep_alive"] == "15m"
    plain = FakeOllama(script=[{"role": "assistant", "content": "ok"}])
    local_llm.OllamaLLM("http://127.0.0.1:11434", "llama3.1:8b", http=http(plain), caps=["completion", "tools"]).chat([])
    assert "think" not in plain.bodies[0]  # only thinking models get it: older Ollama rejects it elsewhere


def test_an_error_in_the_middle_of_an_answer_is_said_plainly(svc):
    fake = FakeOllama(script=[{"native": [{"error": "model requires more system memory (24 GiB) than is available"}]}])
    svc.cfg["brain"] = {"provider": "local", "local": {**svc.cfg["brain"]["local"], "enabled": True}}
    a = Assistant(svc)
    a.local = local_llm.LocalBrain(svc.cfg["brain"]["local"], http=http(fake))
    a._stream_on = True
    a._speak_sentence = lambda s, final: None
    assert "more system memory" in a.handle("tell me something")["reply"]


def test_a_model_that_cant_act_is_told_so_and_gets_no_tools(svc):
    fake = FakeOllama(models=["gemma4:latest"], tools=[], script=[{"role": "assistant", "content": "I can't do that with this model."}])
    svc.cfg["brain"] = {"provider": "local", "local": {**svc.cfg["brain"]["local"], "enabled": True}}
    a = Assistant(svc)
    a.local = local_llm.LocalBrain(svc.cfg["brain"]["local"], http=http(fake))
    a.handle("write me a stream title for tonight")
    body = fake.bodies[0]
    assert "tools" not in body and "can't take actions" in body["messages"][0]["content"]


# ---- the runtime: test once by itself, switch when live -------------------------------------------------------

@pytest.fixture
def runtime(cfg, svc):
    from assistant.runtime import Runtime

    cfg["brain"]["local"]["enabled"] = True
    rt = Runtime(cfg, services=svc)
    yield rt
    rt._commands.shutdown()


def test_the_model_test_runs_by_itself_once_and_again_when_models_change(runtime, monkeypatch):
    fake = JsPC(acts={"qwen3.6:35b-a3b", "llama3.1:8b"})
    local = runtime.assistant.local
    local.http, local.vram_mb = http(fake), lambda: RTX_5090_MB
    events = []
    runtime.bus.on(events.append)
    runtime._auto_bench()
    for _ in range(200):
        if not runtime._bench_running and local.bench:
            break
        time.sleep(0.02)
    assert local.bench["best"] == "qwen3.6:35b-a3b" and local.refresh(force=True).model == "qwen3.6:35b-a3b"
    assert any(e["type"] == "announce" and "qwen3.6:35b-a3b" in e["data"]["text"] for e in events)
    started = []
    monkeypatch.setattr(runtime, "bench_brain", lambda: started.append(1))
    runtime._auto_bench()
    assert started == []  # same models: not again
    fake.models.append("qwen3.7:14b")
    runtime._auto_bench()
    assert started == [1]  # a new model: test again
    runtime.cfg["brain"]["local"]["model"] = "llama3.1:8b"
    runtime._auto_bench()
    assert started == [1]  # you picked one yourself: left alone


def test_going_live_switches_models_and_says_so(runtime, tmp_path):
    fake = JsPC(acts=set(J_MODELS))
    local = runtime.assistant.local
    local.http, local.vram_mb = http(fake), lambda: RTX_5090_MB
    model_bench.save({"best": "qwen3.6:35b-a3b", "light": "llama3.1:8b", "models": list(J_MODELS), "results": []},
                     local.bench_path)
    local.refresh(force=True).chat([{"role": "user", "content": "hi"}])
    events = []
    runtime.bus.on(events.append)
    runtime._stream_brain(True)
    runtime._stream_brain(True)
    runtime._stream_brain(False)
    said = [e["data"]["text"] for e in events if e["type"] == "announce"]
    assert said == ["You're live: answering with llama3.1:8b, freed 21 GB of video memory.",
                    "Stream over: back to qwen3.6:35b-a3b for your next question."]


def test_the_test_button_checks_actions_not_just_a_reply(runtime):
    fake = JsPC(acts=set())  # answers, never acts
    local = runtime.assistant.local
    local.http, local.vram_mb = http(fake), lambda: RTX_5090_MB
    local.cfg["model"] = "gemma4:31b-it-qat"
    local.refresh(force=True)
    out = runtime.test_brain()
    assert out["ok"] and out["acts"] is False


# ---- memory --------------------------------------------------------------------------------------------------

def test_a_model_spilling_into_ram_is_named_and_can_be_unloaded(runtime):
    fake = JsPC(acts=set(J_MODELS))
    fake.loaded = {"llama3.3:70b": int(42e9)}

    def ps(req):  # 70B: about 30 GB on the 5090, the rest in RAM
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": n, "size": b, "size_vram": int(min(b, 30e9))}
                                                        for n, b in fake.loaded.items()]})
        return fake(req)
    local = runtime.assistant.local
    local.http, local.vram_mb = httpx.Client(transport=httpx.MockTransport(ps)), lambda: RTX_5090_MB
    local.refresh(force=True)
    found = runtime._model_memory_findings()
    assert found[0]["title"] == "llama3.3:70b holds 11 GB of RAM" and found[0]["severity"] == "high"
    assert found[0]["action"] == {"tool": "free_model_memory", "args": {"keep_current": True}}
    out = runtime.assistant.tools.run("free_model_memory", {"keep_current": True})
    assert out["unloaded"] == ["llama3.3:70b"] and out["freed_gb"] == 39.1 and fake.loaded == {}
    assert runtime._model_memory_findings() == []


def test_per_app_memory_is_private_bytes_not_summed_working_sets(monkeypatch):
    from types import SimpleNamespace

    from assistant.integrations import system

    gb = 1024 ** 3
    procs = [SimpleNamespace(info={"pid": i, "name": "chrome.exe", "cpu_percent": 0.0,
                                   "memory_info": SimpleNamespace(rss=int(1.5 * gb), private=int(0.2 * gb))})
             for i in range(40)]
    monkeypatch.setattr(system.psutil, "process_iter", lambda attrs: procs)
    top = system.SystemMonitor().top_processes()
    chrome = top["by_mem"][0]
    assert chrome["name"] == "chrome" and chrome["count"] == 40
    assert round(chrome["mem_mb"] / 1024) == 8  # 40 x 0.2 GB private, not 40 x 1.5 GB = 60 GB of shared pages


def test_a_browser_eating_memory_gets_the_find_the_tab_tip(monkeypatch):
    from assistant.integrations import system

    mon = system.SystemMonitor()
    snap = {"memory": {"percent": 96}, "cpu": {"percent": 10}, "disks": [], "gpus": [],
            "processes": {"by_mem": [{"name": "chrome", "mem_mb": 30000, "count": 60}], "by_cpu": []}}
    monkeypatch.setattr(mon, "snapshot", lambda include_processes=True: snap)
    monkeypatch.setattr(mon, "temp_dir_size", lambda: (0, 0))
    monkeypatch.setattr(mon, "active_power_plan", lambda: None)
    finding = mon.analyze()["findings"][0]
    assert finding["title"] == "Memory at 96%" and "Shift+Esc" in finding["detail"] and "Memory Saver" in finding["detail"]
