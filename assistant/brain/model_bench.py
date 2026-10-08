"""Find the strongest model that works on this PC.

A model's name says little about whether it can drive Vesper: some can't take actions at all, some think out
loud for seconds, some don't fit the graphics card and crawl along half on the CPU. So every model that could
work is loaded in turn and given the checks Vesper depends on, with Vesper's real instructions and tool list:

- two spoken commands that must become the right action (set the volume to 20; put a mic arm on the task list)
- one question it must start answering quickly, without any reasoning text

Nothing is executed: the checks only read which action the model chose. The strongest model that passes wins
(``best``); the smallest one that passes is kept for stream nights (``light``). Results go to
data/brain_bench.json, which LocalBrain reads.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from . import local_llm

log = logging.getLogger(__name__)

PROBES = (
    ("Set the volume to 20.", "set_volume", lambda a: _num(a.get("level")) == 20),
    ("Put buy a new mic arm on my task list.", "add_task", lambda a: "mic arm" in str(a.get("title", "")).lower()),
)
QUESTION = "In one short sentence: why do streamers use a starting soon screen?"
SYSTEM = ("You are Vesper, a voice assistant that controls this Windows PC. When the user asks for something a tool "
          "can do, call the tool. Answer questions in one or two short spoken sentences.")
FIRST_WORD_S = 3.0  # longer than this before the first word, and talking to it feels broken


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def run(found: dict, tools: list[dict], vram_mb: float | None = None, system: str = SYSTEM,
        http: httpx.Client | None = None, first_word_s: float = FIRST_WORD_S,
        on_progress: Callable[[dict], None] | None = None, timeout: float = 300, num_ctx: int = 8192) -> dict:
    """Test every model on the server; returns {best, light, results, models, vram_gb, at}."""
    if not found or found.get("kind") != "ollama":
        return {"error": "Testing models needs Ollama (it reports what each model can do and loads them on request)."}
    http = http or httpx.Client(timeout=timeout, trust_env=False)
    url = found["url"]
    models = [m for m in found["models"] if not any(s in m["name"].lower() for s in local_llm.SKIP)]
    models.sort(key=local_llm.strength, reverse=True)
    results = []

    def note(row: dict) -> None:
        results.append(row)
        if on_progress:
            on_progress({"done": len(results), "total": len(models), "row": row})

    for m in models:
        name = m["name"]
        row = {"name": name, "size_gb": round(m["bytes"] / local_llm.GIB, 1) if m.get("bytes") else None,
               "strength": round(local_llm.strength(m), 1)}
        if m.get("tools") is False:
            note({**row, "result": "no_actions", "note": "can't take actions (Ollama says so)"})
            continue
        if local_llm.fits(m, vram_mb) is False:
            vram = vram_mb / 1024
            note({**row, "result": "too_big", "note": f"needs about {local_llm.needs_gib(m):.0f} GB of video memory; "
                                                    f"{vram - max(4.0, vram * 0.2):.0f} GB of your {vram:.0f} GB is free for it"})
            continue
        if on_progress:
            on_progress({"done": len(results), "total": len(models), "testing": name})
        note(_test(http, url, m, tools, system, first_word_s, timeout, row, vram_mb, num_ctx))

    passed = [r for r in results if r["result"] == "pass"]
    best = max(passed, key=lambda r: r["strength"], default=None)
    light = min((r for r in passed if r["strength"] >= 3), key=lambda r: (r.get("size_gb") or 0), default=None)
    return {"best": best["name"] if best else None, "light": (light or best or {}).get("name"),
            "results": results, "models": [m["name"] for m in found["models"]],
            "vram_gb": round(vram_mb / 1024, 1) if vram_mb else None, "at": datetime.now().isoformat(timespec="seconds")}


def _test(http, url, m, tools, system, first_word_s, timeout, row, vram_mb, num_ctx) -> dict:
    name = m["name"]
    llm = local_llm.OllamaLLM(url, name, http=http, caps=m.get("caps"), keep_alive="2m", timeout=timeout,
                              num_ctx=num_ctx)
    try:
        row["load_s"] = round(local_llm.preload(http, url, name, keep_alive="2m", timeout=timeout, num_ctx=num_ctx), 1)
        held = next((x for x in local_llm.loaded(http, url) if x["name"] == name), None)
        if held and held["size_gb"]:
            row["on_gpu"] = round(held["vram_gb"] / held["size_gb"], 2)
        acted, did = 0, []
        for said, want, ok in PROBES:
            out = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": said}],
                           tools=tools, temperature=0, max_tokens=200)
            call = next((c for c in out["tool_calls"] if c["name"] == want), None)
            good = bool(call and ok(call["arguments"]))
            acted += good
            did.append(f"{said} -> " + (f"{call['name']}({json.dumps(call['arguments'])})" if call else
                                        ", ".join(c["name"] for c in out["tool_calls"]) or repr(out["content"][:60])))
        row["actions"] = f"{acted}/{len(PROBES)}"
        row["did"] = did
        first, words = [None], []
        t0 = time.perf_counter()

        def heard(text: str) -> None:
            if first[0] is None and text.strip():
                first[0] = time.perf_counter() - t0
            words.append(text)
        # With the tools, as every real question has them: the model must answer, not reach for one.
        out = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": QUESTION}],
                       tools=tools, on_text=heard, temperature=0, max_tokens=120)
        row["answer_s"] = round(time.perf_counter() - t0, 1)
        row["first_word_s"] = round(first[0], 1) if first[0] is not None else None
        row["answer"] = out["content"][:200]
    except httpx.HTTPError as exc:
        local_llm.unload(http, url, name)
        return {**row, "result": "error", "note": f"{type(exc).__name__}: {str(exc)[:120]}"}
    local_llm.unload(http, url, name)  # free the card for the next one
    problems = []
    if acted < len(PROBES):
        problems.append(f"took the right action {acted} of {len(PROBES)} times")
    if not out["content"].strip():
        problems.append("gave no answer")
    if row["first_word_s"] is None or row["first_word_s"] > first_word_s:
        problems.append(f"first word after {row['first_word_s'] or row['answer_s']} s")
    if vram_mb and row.get("on_gpu") is not None and row["on_gpu"] < 0.98:
        problems.append(f"only {row['on_gpu']:.0%} of it fit on the GPU")
    return {**row, "result": "fail" if problems else "pass", "note": "; ".join(problems) or "passed"}


def save(result: dict, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
    tmp.replace(path)


def summary(result: dict) -> str:
    """One spoken/printed line: what won and why."""
    if result.get("error"):
        return result["error"]
    if not result.get("best"):
        return "None of your models passed: " + "; ".join(f"{r['name']} {r['note']}" for r in result["results"][:4])
    best = next(r for r in result["results"] if r["name"] == result["best"])
    line = f"Using {best['name']}: it took the right actions and starts answering in {best['first_word_s']} seconds."
    if result.get("light") and result["light"] != result["best"]:
        line += f" On stream nights I'll switch to {result['light']} to leave the GPU to the game."
    return line
