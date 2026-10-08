"""Your own model as the brain: a Llama (or Qwen, Mistral…) running on this PC.

Ollama, LM Studio, llama.cpp's server and Jan all speak the OpenAI-compatible
chat API, so one small client covers them. Vesper looks for them on their
default ports, lists the models, and picks one that can call tools (Ollama
reports this per model). It is used when there is no Claude key, or always
with ``brain.provider: local``.

Honest expectations: a 3-8B model on a gaming GPU answers questions and makes
single tool calls well; multi-step plans are where Claude is much stronger. The
fast-path router still handles everyday commands with no model at all, and the
same risk tiers, "yes" confirmations and kill switch apply to a local model's
tool calls as to Claude's.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger(__name__)

# (kind, base URL): the default ports. Anything else goes in brain.local.url.
SERVERS = (("ollama", "http://127.0.0.1:11434"), ("lmstudio", "http://127.0.0.1:1234"),
           ("llamacpp", "http://127.0.0.1:8080"), ("jan", "http://127.0.0.1:1337"))
LABELS = {"ollama": "Ollama", "lmstudio": "LM Studio", "llamacpp": "llama.cpp", "jan": "Jan", "custom": "Model server"}
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# Families whose chat templates handle tool calls, best first. Used when the server can't tell us.
TOOL_FAMILIES = ("llama3.3", "llama3.1", "qwen3", "qwen2.5", "llama3.2", "mistral-nemo", "mistral-small", "mistral",
                 "command-r", "hermes3", "granite3", "phi4-mini", "llama4", "gpt-oss")
SKIP = ("embed", "bge", "minilm", "rerank", "whisper", "clip", "llava", "moondream")


GIB = 1024 ** 3


def _size_b(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", text or "")
    return float(m.group(1)) if m else None


def _active_b(name: str) -> float | None:
    """A mixture-of-experts tag like 'qwen3.6:35b-a3b' names the parameters used per word: 3B of 35B."""
    m = re.search(r"-a(\d+(?:\.\d+)?)b\b", name.lower())
    return float(m.group(1)) if m else None


def strength(m: dict) -> float:
    """Rough capability in dense-model billions. A 35B model that uses 3B per word acts like a ~10B one (the usual
    square root of total x active), so a dense 31B ranks above it."""
    size = m.get("size_b") or _size_b(m["name"]) or 0.0
    active = _active_b(m["name"])
    return (size * active) ** 0.5 if active and size else size


def needs_gib(m: dict) -> float | None:
    """Video memory a model wants: its weights (the file) plus about 1.5 GB for the conversation."""
    return m["bytes"] / GIB + 1.5 if m.get("bytes") else None


def fits(m: dict, vram_mb: float | None) -> bool | None:
    """Runs entirely on the graphics card while leaving the larger of 4 GB or a fifth of it for Windows, OBS's
    encoder and a game. None when the card or the model's size is unknown."""
    need = needs_gib(m)
    if not vram_mb or need is None:
        return None
    vram = vram_mb / 1024
    return need <= vram - max(4.0, vram * 0.2)


def _usable(models: list[dict]) -> list[dict]:
    return [m for m in models if not any(s in m["name"].lower() for s in SKIP)]


BENCH_FILE = "brain_bench.json"  # in data_dir: which of your models passed the test on this PC


def pick_model(models: list[dict], vram_mb: float | None = None) -> str | None:
    """The strongest model that can call tools and fits the graphics card. With no card known (or nothing that
    fits), a tools-capable 3-14B model, which is fast enough to talk to almost anywhere."""
    usable = _usable(models)
    if not usable:
        return None
    fitting = [m for m in usable if m.get("tools") is not False and fits(m, vram_mb)]
    if fitting:
        return max(fitting, key=lambda m: (m.get("tools") is True, strength(m), -(m.get("bytes") or 0)))["name"]

    def score(m: dict) -> tuple:
        name = m["name"].lower()
        tools = m.get("tools")
        fam = next((i for i, f in enumerate(TOOL_FAMILIES) if f in name.replace("-", "").replace("_", "")
                    or f in name), len(TOOL_FAMILIES))
        size = m.get("size_b") or _size_b(name) or 0
        sweet = 0 if 3 <= size <= 14 else 1 if size < 3 else 2
        return (0 if tools else 1 if tools is None else 2, sweet, fam, -size)
    return min(usable, key=score)["name"]


def pick_light_model(models: list[dict]) -> str | None:
    """For stream nights: the smallest model that can still act (3B or more), so the game and the encoder keep
    the video memory."""
    acting = [m for m in _usable(models) if m.get("tools") is not False and strength(m) >= 3]
    if not acting:
        return None
    return min(acting, key=lambda m: (m.get("tools") is not True, m.get("bytes") or strength(m) * 7e8))["name"]


def detect(http: httpx.Client | None = None, extra_url: str | None = None, timeout: float = 0.8,
           defaults: bool = True) -> dict | None:
    """The first model server that answers, with its models: {kind, url, models: [{name, size_b, bytes, tools,
    caps}]}.

    ``extra_url`` (brain.local.url) is tried first: a non-default port, or the model on another PC on the
    home network. ``defaults=False`` checks only that one.
    """
    http = http or httpx.Client(timeout=timeout, trust_env=False)  # never send home-network calls through a proxy
    servers = ([("custom", extra_url.rstrip("/"))] if extra_url else []) + (list(SERVERS) if defaults else [])
    for kind, url in servers:
        try:
            if kind in ("ollama", "custom"):
                r = http.get(f"{url}/api/tags", timeout=timeout)
                if r.status_code == 200 and "models" in r.json():
                    models = []
                    for m in r.json()["models"]:
                        details = m.get("details") or {}
                        caps = _ollama_caps(http, url, m["name"])
                        models.append({"name": m["name"], "size_b": _size_b(details.get("parameter_size", "")),
                                       "bytes": m.get("size"), "family": details.get("family"),
                                       "tools": None if caps is None else "tools" in caps, "caps": caps})
                    return {"kind": "ollama", "url": url, "models": models}  # /api/tags answered: it's Ollama
            r = http.get(f"{url}/v1/models", timeout=timeout)
            if r.status_code == 200:
                data = r.json().get("data") or []
                return {"kind": kind, "url": url, "models": [{"name": m["id"], "size_b": _size_b(m["id"]), "tools": None}
                                                             for m in data if m.get("id")]}
        except (httpx.HTTPError, ValueError):
            continue
    return None


def server_label(kind: str | None, url: str | None) -> str | None:
    """'Ollama', or 'Ollama at GAMING-PC' when the server is another PC on the network."""
    if not kind:
        return None
    label = LABELS.get(kind, kind)
    host = httpx.URL(url).host if url else ""
    if not host or host in LOOPBACK:
        return label
    # Windows shows PC names in capitals (GAMING-PC); IP addresses stay as they are
    return f"{label} at {host.upper() if re.fullmatch(r'[A-Za-z0-9-]+', host) else host}"


def _ollama_caps(http: httpx.Client, url: str, name: str) -> list[str] | None:
    """Ollama 0.6+ lists what a model can do: "tools" (take actions), "thinking", "vision"…"""
    try:
        r = http.post(f"{url}/api/show", json={"model": name}, timeout=2)
        caps = r.json().get("capabilities") if r.status_code == 200 else None
        return list(caps) if isinstance(caps, list) else None
    except (httpx.HTTPError, ValueError):
        return None


def loaded(http: httpx.Client, url: str) -> list[dict]:
    """What Ollama holds in memory right now, and how much of each model sits in RAM instead of on the GPU."""
    try:
        r = http.get(f"{url}/api/ps", timeout=2)
        rows = (r.json().get("models") or []) if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    for m in rows:
        size, vram = m.get("size") or 0, m.get("size_vram") or 0
        out.append({"name": m.get("name") or m.get("model"), "size_gb": round(size / GIB, 1),
                    "vram_gb": round(vram / GIB, 1), "ram_gb": round(max(size - vram, 0) / GIB, 1)})
    return out


def unload(http: httpx.Client, url: str, model: str) -> bool:
    """Tell Ollama to drop a model from memory now instead of after its keep-alive."""
    try:
        return http.post(f"{url}/api/generate", json={"model": model, "keep_alive": 0}, timeout=15).status_code == 200
    except httpx.HTTPError:
        return False


def preload(http: httpx.Client, url: str, model: str, keep_alive: str | int = "15m", timeout: float = 300,
            num_ctx: int = 8192) -> float:
    """Load a model without asking it anything; returns the seconds it took. Same context size as the questions
    that follow, or Ollama loads it a second time for them."""
    t0 = time.perf_counter()
    r = http.post(f"{url}/api/generate", json={"model": model, "keep_alive": keep_alive, "options": {"num_ctx": num_ctx}},
                  timeout=timeout)
    r.raise_for_status()
    return time.perf_counter() - t0


THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.S)


def strip_thinking(text: str) -> str:
    """Drop <think>...</think> reasoning that some models write into the answer itself."""
    return THINK.sub("", text)


def visible_so_far(text: str) -> str:
    """What may be spoken of a reply still arriving: no reasoning, and not a half-written '<think' at the end."""
    shown = strip_thinking(text)
    for k in range(min(len(shown), len("<think>") - 1), 0, -1):
        if "<think>".startswith(shown[-k:]):
            return shown[:-k]
    return shown


class LocalModelError(httpx.HTTPError):
    """The model server reported an error in the middle of an answer (out of memory, a broken model…)."""


class LocalLLM:
    """A chat call against an OpenAI-compatible server. ``chat`` returns {"content", "tool_calls"}."""

    def __init__(self, url: str, model: str, kind: str = "ollama", http: httpx.Client | None = None,
                 timeout: float = 120):
        self.url, self.model, self.kind = url.rstrip("/"), model, kind
        self.http = http or httpx.Client(timeout=timeout, trust_env=False)

    @property
    def label(self) -> str:
        return f"{self.model} on {server_label(self.kind, self.url)}"

    @property
    def can_act(self) -> bool:
        return True  # an OpenAI-compatible server doesn't say; its model gets the tools and tries

    def chat(self, messages: list[dict], tools: list[dict] | None = None, on_text: Callable[[str], None] | None = None,
             temperature: float = 0.3, max_tokens: int = 700, json_schema: dict | None = None,
             should_stop: Callable[[], bool] | None = None) -> dict:
        body: dict = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                      "stream": on_text is not None}
        if tools:
            body["tools"] = tools
        if json_schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "reply", "schema": json_schema}}
        endpoint = f"{self.url}/v1/chat/completions"
        if on_text is None:
            r = self.http.post(endpoint, json=body)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            calls = [_call(c.get("id"), (c.get("function") or {}).get("name"), (c.get("function") or {}).get("arguments"))
                     for c in msg.get("tool_calls") or []]
            return _finish(msg.get("content") or "", calls, bool(tools))
        text, parts, emitted = [], {}, 0
        with self.http.stream("POST", endpoint, json=body) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if should_stop and should_stop():
                    break
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta") or {}
                except (ValueError, KeyError, IndexError):
                    continue
                if delta.get("content"):  # reasoning arrives in its own field ("reasoning"), never spoken
                    text.append(delta["content"])
                    emitted = _emit("".join(text), emitted, on_text)
                for tc in delta.get("tool_calls") or []:
                    p = parts.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                    p["id"] = tc.get("id") or p["id"]
                    fn = tc.get("function") or {}
                    p["name"] += fn.get("name") or ""
                    p["args"] += fn.get("arguments") or ""
        calls = [_call(p["id"], p["name"], p["args"]) for _, p in sorted(parts.items())]
        return _finish("".join(text), calls, bool(tools))


class OllamaLLM(LocalLLM):
    """Ollama's own /api/chat. Unlike its OpenAI-compatible endpoint it lets Vesper turn a thinking model's
    reasoning off (seconds saved, and never read aloud), size the context so the instructions and the tool list
    aren't cut off, and say how long the model stays loaded."""

    def __init__(self, url: str, model: str, http: httpx.Client | None = None, timeout: float = 120,
                 caps: list[str] | None = None, num_ctx: int = 8192, keep_alive: str | int = "15m"):
        super().__init__(url, model, "ollama", http=http, timeout=timeout)
        self.caps = set(caps) if caps is not None else None
        self.num_ctx, self.keep_alive = num_ctx, keep_alive
        self.no_tools = self.caps is not None and "tools" not in self.caps

    @property
    def can_act(self) -> bool:
        return not self.no_tools

    def chat(self, messages: list[dict], tools: list[dict] | None = None, on_text: Callable[[str], None] | None = None,
             temperature: float = 0.3, max_tokens: int = 700, json_schema: dict | None = None,
             should_stop: Callable[[], bool] | None = None) -> dict:
        body: dict = {"model": self.model, "messages": to_ollama(messages), "stream": on_text is not None,
                      "keep_alive": self.keep_alive,
                      "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": self.num_ctx}}
        if tools and not self.no_tools:
            body["tools"] = tools
        if json_schema:
            body["format"] = json_schema
        if self.caps and "thinking" in self.caps:
            body["think"] = False
        try:
            return self._send(body, on_text, should_stop)
        except httpx.HTTPStatusError as exc:
            if not (body.get("tools") and "does not support tools" in exc.response.text):
                raise
            self.no_tools = True  # answer without actions rather than not at all
            body.pop("tools")
            return self._send(body, on_text, should_stop)

    def _send(self, body: dict, on_text, should_stop) -> dict:
        endpoint, acting = f"{self.url}/api/chat", bool(body.get("tools"))
        if on_text is None:
            r = self.http.post(endpoint, json=body)
            r.raise_for_status()
            msg = r.json().get("message") or {}
            return _finish(msg.get("content") or "", _ollama_calls(msg), acting)
        text, calls, emitted = [], [], 0
        with self.http.stream("POST", endpoint, json=body) as r:
            if r.status_code >= 400:
                r.read()  # so the error text can be read
            r.raise_for_status()
            for line in r.iter_lines():
                if should_stop and should_stop():
                    break
                try:
                    chunk = json.loads(line) if line.strip() else {}
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise LocalModelError(str(chunk["error"]))
                msg = chunk.get("message") or {}
                if msg.get("content"):  # "thinking", if a model sends it anyway, is never read
                    text.append(msg["content"])
                    emitted = _emit("".join(text), emitted, on_text)
                calls += _ollama_calls(msg)
                if chunk.get("done"):
                    break
        return _finish("".join(text), calls, acting)


def to_ollama(messages: list[dict]) -> list[dict]:
    """Vesper's OpenAI-style turns in Ollama's own shape: tool-call arguments as objects, and each tool result
    tagged with the tool's name."""
    names: dict = {}
    out = []
    for m in messages:
        turn = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            turn["tool_calls"] = []
            for c in m["tool_calls"]:
                fn = c.get("function") or {}
                names[c.get("id")] = fn.get("name")
                turn["tool_calls"].append({"id": c.get("id"), "function": {
                    "name": fn.get("name"), "arguments": _call(None, None, fn.get("arguments"))["arguments"]}})
        if m["role"] == "tool":
            turn["tool_call_id"] = m.get("tool_call_id")
            if names.get(m.get("tool_call_id")):
                turn["tool_name"] = names[m["tool_call_id"]]
        out.append(turn)
    return out


def _ollama_calls(msg: dict) -> list[dict]:
    return [_call(c.get("id"), (c.get("function") or {}).get("name"), (c.get("function") or {}).get("arguments"))
            for c in msg.get("tool_calls") or []]


def _emit(text: str, emitted: int, on_text: Callable[[str], None]) -> int:
    """Pass on what's new of the reply so far: never reasoning, and nothing while it could still be a tool call
    written out as JSON."""
    shown = visible_so_far(text)
    if len(shown) > emitted and not _looks_like_json_call(shown):
        on_text(shown[emitted:])
        return len(shown)
    return emitted


def _call(call_id, name, arguments) -> dict:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            arguments = {}
    return {"id": call_id or f"call_{uuid.uuid4().hex[:8]}", "name": name or "", "arguments": arguments or {}}


def _looks_like_json_call(text: str) -> bool:
    """A tool call written as text starts with {, [{, <tool_call> or a code fence. A lone "[" is undecided."""
    s = text.lstrip()
    if s.startswith(("{", "<tool_call>", "```")):
        return True
    if s.startswith("["):
        rest = s[1:].lstrip()
        return not rest or rest.startswith("{")
    return not s  # nothing but whitespace yet


def _finish(content: str, calls: list[dict], tools_offered: bool) -> dict:
    """Some servers hand a tool call back as text ('{"name": "add_task", "parameters": {...}}'): recover it."""
    content = strip_thinking(content)
    if not calls and tools_offered and _looks_like_json_call(content):
        raw = re.sub(r"^```(?:json)?|```$|</?tool_call>", "", content.strip()).strip()
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
        for item in (obj if isinstance(obj, list) else [obj]):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                calls.append(_call(None, item["name"], item.get("arguments", item.get("parameters", {}))))
        if calls:
            content = ""
    return {"content": content.strip(), "tool_calls": [c for c in calls if c["name"]]}


class LocalBrain:
    """Finds the local model and keeps looking (every minute) if it isn't running yet.

    Which model: the one you pinned (brain.local.model or Setup → Brain), else the strongest that passed the
    test on this PC (model_bench), else the strongest that fits the graphics card. While you stream it moves to
    the light model and unloads the big one, so the game and OBS's encoder keep the video memory.
    """

    def __init__(self, cfg: dict, http: httpx.Client | None = None, clock=time.time,
                 vram_mb: Callable[[], float | None] | None = None, bench_path=None):
        self.cfg = cfg or {}
        self.http = http
        self.clock = clock
        self.vram_mb = vram_mb or (lambda: None)
        self.bench_path = bench_path
        self.found: dict | None = None
        self.llm: LocalLLM | None = None
        self.streaming = False
        self._probed = -1e9
        self._lock = threading.Lock()

    @property
    def bench(self) -> dict:
        """The last model test on this PC (data/brain_bench.json), or {}."""
        if not self.bench_path:
            return {}
        try:
            return json.loads(Path(self.bench_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _vram(self) -> float | None:
        try:
            return self.vram_mb()
        except Exception:  # a GPU query must never take the brain down
            return None

    def choose(self, models: list[dict]) -> str | None:
        names = [m["name"] for m in models]

        def match(want) -> str | None:
            if not want:
                return None
            return next((n for n in names if n == want), None) or next((n for n in names if n.startswith(want)), None)
        bench = self.bench
        if self.streaming and (self.cfg.get("stream_model") or "auto") != "same":
            want = self.cfg.get("stream_model")
            return (match(want if want != "auto" else None) or match(bench.get("light"))
                    or pick_light_model(models) or self._main(models, match, bench))
        return self._main(models, match, bench)

    def _main(self, models, match, bench) -> str | None:
        return match(self.cfg.get("model")) or match(bench.get("best")) or pick_model(models, self._vram())

    def _make(self, found: dict, model: str) -> LocalLLM:
        if found["kind"] == "ollama":
            info = next((m for m in found["models"] if m["name"] == model), {})
            return OllamaLLM(found["url"], model, http=self.http, caps=info.get("caps"),
                             num_ctx=int(self.cfg.get("context", 8192)), keep_alive=self.cfg.get("keep_alive", "15m"))
        return LocalLLM(found["url"], model, found["kind"], http=self.http)

    def refresh(self, force: bool = False) -> LocalLLM | None:
        with self._lock:
            if not self.cfg.get("enabled", True):
                self.llm, self.found = None, None
                return None
            if not force and (self.llm is not None or self.clock() - self._probed < 60):
                return self.llm
            self._probed = self.clock()
            found = detect(self.http, self.cfg.get("url") or None)
            self.found = found
            if not found or not found["models"]:
                self.llm = None
                return None
            model = self.choose(found["models"])
            self.llm = self._make(found, model) if model else None
            if self.llm:
                log.info("local model: %s", self.llm.label)
            return self.llm

    @property
    def ready(self) -> bool:
        return self.refresh() is not None

    def lost(self) -> None:
        """The server stopped answering: look again next time."""
        with self._lock:
            self.llm, self._probed = None, -1e9

    def set_streaming(self, on: bool) -> dict | None:
        """OBS went live (or stopped). Live: answer with the light model and unload the big one now rather than
        after its keep-alive. After: back to the strongest, which loads on the next question. Returns what
        changed, or None."""
        with self._lock:
            if on == self.streaming:
                return None
            self.streaming = on
            old, found = self.llm, self.found
            if not found or not found.get("models"):
                return None
            model = self.choose(found["models"])
            if old is not None and old.model == model:
                return None
            self.llm = self._make(found, model) if model else None
        freed = None
        if on and old is not None and old.kind == "ollama":
            gone = next((m for m in loaded(old.http, old.url) if m["name"] == old.model), None)
            if unload(old.http, old.url, old.model) and gone:
                freed = gone["vram_gb"]
        change = {"streaming": on, "from": old.model if old else None, "to": model, "freed_gb": freed}
        log.info("stream %s: answering with %s", "live" if on else "over", model)
        return change

    def loaded(self) -> list[dict]:
        f = self.found or {}
        if f.get("kind") != "ollama":
            return []
        return loaded(self.http or httpx.Client(timeout=3, trust_env=False), f["url"])

    def status(self) -> dict:
        f = self.found or {}
        bench, vram = self.bench, self._vram()
        tested = {r["name"]: r for r in bench.get("results") or []}
        choices = []
        for m in f.get("models") or []:
            if any(s in m["name"].lower() for s in SKIP):
                continue
            row = tested.get(m["name"]) or {}
            choices.append({"name": m["name"], "size_gb": round(m["bytes"] / GIB, 1) if m.get("bytes") else None,
                            "tools": m.get("tools"), "fits": fits(m, vram), "result": row.get("result"),
                            "first_word_s": row.get("first_word_s")})
        return {"ready": self.llm is not None, "server": server_label(f.get("kind"), f.get("url")),
                "url": f.get("url"), "model": self.llm.model if self.llm else None,
                "can_act": self.llm.can_act if self.llm else None,
                "models": [m["name"] for m in f.get("models") or []], "choices": choices,
                "pinned": self.cfg.get("model") or None, "streaming": self.streaming,
                "stream_model": self.cfg.get("stream_model") or "auto",
                "bench": {k: bench.get(k) for k in ("best", "light", "at")} if bench else None,
                "vram_gb": round(vram / 1024, 1) if vram else None,
                # brain.local.url (e.g. the main PC): shown when it can't be reached
                "configured_url": (self.cfg.get("url") or "").rstrip("/") or None}
