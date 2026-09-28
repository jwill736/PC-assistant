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
from typing import Callable

import httpx

log = logging.getLogger(__name__)

# (kind, base URL): the default ports. Anything else goes in brain.local.url.
SERVERS = (("ollama", "http://127.0.0.1:11434"), ("lmstudio", "http://127.0.0.1:1234"),
           ("llamacpp", "http://127.0.0.1:8080"), ("jan", "http://127.0.0.1:1337"))
LABELS = {"ollama": "Ollama", "lmstudio": "LM Studio", "llamacpp": "llama.cpp", "jan": "Jan", "custom": "Local server"}
# Families whose chat templates handle tool calls, best first. Used when the server can't tell us.
TOOL_FAMILIES = ("llama3.3", "llama3.1", "qwen3", "qwen2.5", "llama3.2", "mistral-nemo", "mistral-small", "mistral",
                 "command-r", "hermes3", "granite3", "phi4-mini", "llama4", "gpt-oss")
SKIP = ("embed", "bge", "minilm", "rerank", "whisper", "clip", "llava", "moondream")


def _size_b(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", text or "")
    return float(m.group(1)) if m else None


def pick_model(models: list[dict]) -> str | None:
    """A tools-capable chat model, preferring 3-14B (fast enough to talk to), then the best-known family."""
    usable = [m for m in models if not any(s in m["name"].lower() for s in SKIP)]
    if not usable:
        return None

    def score(m: dict) -> tuple:
        name = m["name"].lower()
        tools = m.get("tools")
        fam = next((i for i, f in enumerate(TOOL_FAMILIES) if f in name.replace("-", "").replace("_", "")
                    or f in name), len(TOOL_FAMILIES))
        size = m.get("size_b") or _size_b(name) or 0
        sweet = 0 if 3 <= size <= 14 else 1 if size < 3 else 2
        return (0 if tools else 1 if tools is None else 2, sweet, fam, -size)
    return min(usable, key=score)["name"]


def detect(http: httpx.Client | None = None, extra_url: str | None = None, timeout: float = 0.8) -> dict | None:
    """The first local model server that answers, with its models: {kind, url, models: [{name, size_b, tools}]}."""
    http = http or httpx.Client(timeout=timeout, trust_env=False)  # never send localhost through a proxy
    servers = ([("custom", extra_url.rstrip("/"))] if extra_url else []) + list(SERVERS)
    for kind, url in servers:
        try:
            if kind in ("ollama", "custom"):
                r = http.get(f"{url}/api/tags", timeout=timeout)
                if r.status_code == 200 and "models" in r.json():
                    models = []
                    for m in r.json()["models"]:
                        details = m.get("details") or {}
                        models.append({"name": m["name"], "size_b": _size_b(details.get("parameter_size", "")),
                                       "family": details.get("family"), "tools": _ollama_tools(http, url, m["name"])})
                    return {"kind": "ollama" if kind == "ollama" else kind, "url": url, "models": models}
            r = http.get(f"{url}/v1/models", timeout=timeout)
            if r.status_code == 200:
                data = r.json().get("data") or []
                return {"kind": kind, "url": url, "models": [{"name": m["id"], "size_b": _size_b(m["id"]), "tools": None}
                                                             for m in data if m.get("id")]}
        except (httpx.HTTPError, ValueError):
            continue
    return None


def _ollama_tools(http: httpx.Client, url: str, name: str) -> bool | None:
    """Ollama 0.6+ lists a model's capabilities ("tools" among them)."""
    try:
        r = http.post(f"{url}/api/show", json={"model": name}, timeout=2)
        caps = r.json().get("capabilities") if r.status_code == 200 else None
        return None if caps is None else "tools" in caps
    except (httpx.HTTPError, ValueError):
        return None


class LocalLLM:
    """A chat call against an OpenAI-compatible server. ``chat`` returns {"content", "tool_calls"}."""

    def __init__(self, url: str, model: str, kind: str = "ollama", http: httpx.Client | None = None,
                 timeout: float = 120):
        self.url, self.model, self.kind = url.rstrip("/"), model, kind
        self.http = http or httpx.Client(timeout=timeout, trust_env=False)

    @property
    def label(self) -> str:
        return f"{self.model} on {LABELS.get(self.kind, self.kind)}"

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
        text, parts = [], {}
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
                if delta.get("content"):
                    text.append(delta["content"])
                    if not _looks_like_json_call("".join(text)):
                        on_text(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    p = parts.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                    p["id"] = tc.get("id") or p["id"]
                    fn = tc.get("function") or {}
                    p["name"] += fn.get("name") or ""
                    p["args"] += fn.get("arguments") or ""
        calls = [_call(p["id"], p["name"], p["args"]) for _, p in sorted(parts.items())]
        return _finish("".join(text), calls, bool(tools))


def _call(call_id, name, arguments) -> dict:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            arguments = {}
    return {"id": call_id or f"call_{uuid.uuid4().hex[:8]}", "name": name or "", "arguments": arguments or {}}


def _looks_like_json_call(text: str) -> bool:
    return text.lstrip().startswith(("{", "[", "<tool_call>", "```"))


def _finish(content: str, calls: list[dict], tools_offered: bool) -> dict:
    """Some servers hand a tool call back as text ('{"name": "add_task", "parameters": {...}}'): recover it."""
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
    """Finds the local model and keeps looking (every minute) if it isn't running yet."""

    def __init__(self, cfg: dict, http: httpx.Client | None = None, clock=time.time):
        self.cfg = cfg or {}
        self.http = http
        self.clock = clock
        self.found: dict | None = None
        self.llm: LocalLLM | None = None
        self._probed = -1e9
        self._lock = threading.Lock()

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
            wanted = self.cfg.get("model") or ""
            names = [m["name"] for m in found["models"]]
            model = next((n for n in names if n == wanted), None) or next(
                (n for n in names if wanted and n.startswith(wanted)), None) or pick_model(found["models"])
            self.llm = LocalLLM(found["url"], model, found["kind"], http=self.http) if model else None
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

    def status(self) -> dict:
        f = self.found or {}
        return {"ready": self.llm is not None, "server": LABELS.get(f.get("kind", ""), f.get("kind")),
                "url": f.get("url"), "model": self.llm.model if self.llm else None,
                "models": [m["name"] for m in f.get("models") or []]}
