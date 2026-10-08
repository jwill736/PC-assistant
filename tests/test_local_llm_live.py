"""The local brain against a real Llama served by Ollama (CI installs both).

Run locally with Ollama running and a model pulled:
    ASSISTANT_LLM_TESTS=1 OLLAMA_MODEL=llama3.2:3b pytest tests/test_local_llm_live.py -v -s
Temperature 0, so a run is repeatable on the same model; the assertions check what was done
(a task stored, a note found), not the exact wording.
"""

import os
import time

import httpx
import pytest

from assistant.brain import local_llm, model_bench
from assistant.brain.assistant import Assistant

pytestmark = pytest.mark.skipif(not os.environ.get("ASSISTANT_LLM_TESTS"), reason="needs Ollama with a model")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")


@pytest.fixture
def local(svc):
    svc.cfg["brain"] = {"provider": "local", "local": {
        "enabled": True, "model": MODEL, "temperature": 0.0,
        # a short list keeps CPU prompt processing to seconds on the CI runner
        "tools": ["add_task", "list_tasks", "remember", "recall", "calendar"]}}
    a = Assistant(svc)
    assert a.brain() == "local", "Ollama isn't answering on 127.0.0.1:11434"
    a.local.llm.http.timeout = 240  # per call; CPU-only runners are slow, but not this slow
    return a


def timed(fn):
    t = time.perf_counter()
    out = fn()
    print(f"  [{time.perf_counter() - t:.1f}s] {out['reply'] if isinstance(out, dict) and 'reply' in out else out}")
    return out


def test_ollama_and_the_model_are_found():
    found = local_llm.detect(timeout=5)
    assert found and found["kind"] == "ollama"
    model = next(m for m in found["models"] if m["name"].startswith(MODEL.split(":")[0]))
    assert model["tools"] is True, f"{model['name']} doesn't report tool support"
    print(f"  models: {[m['name'] for m in found['models']]}; picked {local_llm.pick_model(found['models'])}")


def test_it_answers_an_open_question(local):
    out = timed(lambda: local.handle("In one short sentence: why do streamers use a starting soon screen?"))
    assert out["kind"] == "local" and len(out["reply"].split()) >= 4


def test_it_uses_a_tool_to_do_what_was_asked(local, svc):
    out = timed(lambda: local.handle("I need to buy a new mic arm this week. Please put that on my task list."))
    titles = [t["title"].lower() for t in svc.storage.list_tasks()]
    assert any("mic arm" in t for t in titles), f"no task added; reply was {out['reply']!r}"
    assert "active window" not in out["reply"], "the context line would have been spoken"


def test_it_answers_from_memory(local, svc):
    svc.storage.add_note("the new overlay colours are orange and black")
    svc.storage.add_note("I stream on Wednesdays at eight")
    time.sleep(2.1)
    out = timed(lambda: local.handle("Which colours did I pick for my new overlay?"))
    assert "orange" in out["reply"].lower(), f"didn't use memory; reply was {out['reply']!r}"


def test_it_writes_the_morning_plan(local):
    b = timed(lambda: local.briefing("morning"))
    assert b["generated_by"] in ("local", None) or b.get("spoken")
    print(f"  generated_by={b.get('generated_by')}")
    assert b.get("spoken")


THINKER = os.environ.get("OLLAMA_THINKING_MODEL", "qwen3:0.6b")


def _found():
    found = local_llm.detect(timeout=5)
    assert found and found["kind"] == "ollama"
    return found


def test_a_thinking_model_answers_without_reasoning_aloud():
    """Qwen3 thinks before it answers by default: Vesper asks it not to, and nothing of a <think> block is heard."""
    found = _found()
    m = next((x for x in found["models"] if x["name"] == THINKER), None)
    if m is None:
        pytest.skip(f"{THINKER} not pulled")
    print(f"  {THINKER} capabilities: {m['caps']}")
    assert "thinking" in (m["caps"] or [])
    llm = local_llm.OllamaLLM(found["url"], THINKER, caps=m["caps"], timeout=240)
    heard = []
    t = time.perf_counter()
    out = llm.chat([{"role": "user", "content": "Say hello in five words."}], on_text=heard.append, max_tokens=60)
    print(f"  [{time.perf_counter() - t:.1f}s] heard {''.join(heard)!r}")
    assert out["content"] and "<think" not in "".join(heard) and "<think" not in out["content"]


def test_the_model_test_against_real_models():
    """The same test Vesper runs on your PC, here on the CI runner's CPU (so slowness is allowed)."""
    found = _found()
    found["models"] = [m for m in found["models"] if m["name"] in (MODEL, THINKER)]
    tools = [{"type": "function", "function": {"name": n, "description": d, "parameters": p}} for n, d, p in (
        ("set_volume", "Master volume: level 0-100 sets it exactly.", {"type": "object", "properties": {
            "level": {"type": "integer"}}}),
        ("add_task", "Add a task to the user's list.", {"type": "object", "properties": {
            "title": {"type": "string"}}, "required": ["title"]}))]
    t = time.perf_counter()
    result = model_bench.run(found, tools, None, first_word_s=120, timeout=240)
    print(f"  [{time.perf_counter() - t:.1f}s] best={result['best']} light={result['light']}")
    for r in result["results"]:
        print(f"  {r['name']}: {r['result']} actions={r.get('actions')} first_word={r.get('first_word_s')}s "
              f"{r.get('note')}\n    did: {r.get('did')}\n    answer: {r.get('answer')!r}")
    assert {r["name"] for r in result["results"]} == {m["name"] for m in found["models"]}
    for r in result["results"]:
        assert r["result"] in ("pass", "fail") and "<think" not in (r.get("answer") or "")
    assert result["best"], "no model passed: the test is too strict or the models regressed"
    assert local_llm.loaded(httpx.Client(trust_env=False), found["url"]) == []  # each one unloaded after its test
