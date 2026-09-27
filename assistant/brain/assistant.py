"""The assistant: fast-path router first, Claude with tools for everything else.

Risky actions (closing apps, going live, shutting down) never run straight
from a single utterance — they're parked as a pending confirmation until you
say "yes" (or click Confirm on the dashboard).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import anthropic

from ..integrations import desktop
from ..services import Services
from . import briefing
from .router import Intent, RouterContext, route
from .speech import clock, summarize
from .tools import ToolBox, compact

log = logging.getLogger(__name__)

ADAPTIVE_THINKING_PREFIXES = ("claude-opus-5", "claude-fable-5", "claude-mythos-5", "claude-sonnet-5",
                              "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")
SERVER_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5")
ROUTER_HINT_KEYS = ("next_only", "when")

SYSTEM_PROMPT = """You are {name}, the personal operations assistant running on {user}'s Windows PC. You control the
machine through tools: apps and windows, Chrome, OBS, media keys, calendars, news, projects, tasks, background jobs.

How you operate:
- Time is money. Bias to action: if a tool can do it, call the tool instead of explaining how.
- Everything ladders up to the goal. North star: {north_star}. This week: {this_week}.
  When {user} asks what to do, rank by leverage toward that goal, and say plainly when something is a distraction.
- Replies are spoken aloud: 1-3 short sentences, plain words, no markdown, no lists, no URLs, round numbers.
  If {user} asks for detail, you may go longer, still without markdown.
- Never invent data. If a tool fails or something isn't configured, say exactly what's missing.
- Some tools return status "awaiting_confirmation": the action has NOT happened. Ask one short yes/no question.
- Each user turn starts with a bracketed context line (time, active window, active profile). Use it; don't repeat it.
- Background work: for anything that takes more than a minute (research, coding in a repo), use start_job so
  {user} can keep working, then say it's running.
Profiles: {profiles}."""


def _supports(model: str, prefixes: tuple[str, ...]) -> bool:
    return model.startswith(prefixes)


class Assistant:
    def __init__(self, svc: Services, toolbox: ToolBox | None = None, client: anthropic.Anthropic | None = None):
        self.svc = svc
        self.cfg = svc.cfg["claude"]
        self.tools = toolbox or ToolBox(svc)
        self.client = client if client is not None else self._make_client()
        self.history: list[dict] = []
        self.user_turns = 0
        self.last_turn = 0.0
        self.pending: dict | None = None
        self.active_profile: str | None = None
        self.last_briefing: dict | None = None
        self._lock = threading.RLock()
        a = svc.cfg["assistant"]
        self.system_prompt = SYSTEM_PROMPT.format(
            name=a["name"], user=a["user_name"],
            north_star=svc.cfg["goals"].get("north_star") or "(not set — ask the user to set it in config.yaml)",
            this_week="; ".join(svc.cfg["goals"].get("this_week") or []) or "(not set)",
            profiles=", ".join(f"{k} ({v.get('label', k)})" for k, v in svc.cfg["profiles"].items()),
        )
        svc.bus.on(self._track_profile)

    # ------------------------------------------------------------------
    def _make_client(self) -> anthropic.Anthropic | None:
        if not self.cfg.get("enabled", True):
            return None
        key = self.svc.cfg.secret(self.cfg.get("api_key_env"))
        try:
            client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        except Exception:
            client = None
        # No key, no token and no `ant auth login` profile means no brain; say so up front.
        has_profile = (Path.home() / ".config" / "anthropic").exists()
        if client is not None and not (client.api_key or getattr(client, "auth_token", None) or has_profile):
            client = None
        if client is None:
            log.warning("Claude API not configured; running on the local command router only")
        return client

    @property
    def claude_ready(self) -> bool:
        return self.client is not None

    def _track_profile(self, event: dict) -> None:
        if event["type"] == "profile":
            self.active_profile = (event.get("data") or {}).get("active")

    def router_context(self) -> RouterContext:
        obs = self.svc.obs
        return RouterContext(
            is_site=lambda t: self.svc.browser.resolve(t) is not None,
            scene_match=lambda t: obs.match_scene(t) if obs.enabled and obs.scenes else None,
            profiles=list(self.svc.cfg["profiles"].keys()),
            profile_aliases={k.lower(): k for k in self.svc.cfg["profiles"]}
            | {str(v.get("label", "")).lower(): k for k, v in self.svc.cfg["profiles"].items() if v.get("label")},
        )

    # ------------------------------------------------------------------
    def handle(self, text: str, source: str = "text") -> dict:
        text = (text or "").strip()
        if not text:
            return {"reply": "", "kind": "empty"}
        with self._lock:
            self.svc.storage.log("user", text, source)
            self.svc.bus.publish("user_said", {"text": text, "source": source})
            self.svc.bus.publish("thinking", {"active": True})
            started = time.time()
            try:
                intent = route(text, self.router_context())
                if intent is not None:
                    out = self._run_intent(intent)
                else:
                    out = {"reply": self._ask_claude(text), "kind": "claude"}
            except Exception as exc:
                log.exception("command failed")
                out = {"reply": f"That failed: {type(exc).__name__}.", "kind": "error"}
            finally:
                self.svc.bus.publish("thinking", {"active": False})
            out["ms"] = round((time.time() - started) * 1000)
            reply = out.get("reply") or ""
            if reply:
                self.svc.storage.log("assistant", reply, source)
            self.svc.bus.publish("assistant_said", {
                "text": reply, "kind": out.get("kind"), "source": source, "ms": out["ms"],
                "pending": self.pending_view(),
            })
            if reply and (source == "voice" or self.svc.cfg["voice"].get("speak_typed")):
                self.svc.speak(reply)
            return out

    def pending_view(self) -> dict | None:
        if self.pending and self.pending["expires"] > time.time():
            return {"text": self.pending["text"]}
        return None

    # ------------------------------------------------------------------
    def _run_intent(self, intent: Intent) -> dict:
        kind = intent.kind
        if kind == "confirm":
            return {"reply": self.confirm(), "kind": "confirm"}
        if kind == "cancel":
            had = self.pending is not None
            self.pending = None
            self.svc.bus.publish("pending", None)
            return {"reply": "Cancelled." if had else "Okay.", "kind": "cancel"}
        if kind == "reset":
            self.reset()
            return {"reply": "Fresh start.", "kind": "reset"}
        if kind == "time":
            now = datetime.now(self.svc.tz)
            return {"reply": f"It's {clock(now)}, {now.strftime('%A, %B')} {now.day}.", "kind": "time"}
        if kind == "briefing":
            b = self._fresh_morning_briefing() or self.briefing("morning")
            return {"reply": b["spoken"], "kind": "briefing", "data": b}
        if kind == "recap":
            b = self.briefing("recap")
            return {"reply": b["spoken"], "kind": "recap", "data": b}
        if kind == "next":
            if self.claude_ready:
                return {"reply": self._ask_claude("What should I work on next? Check my tasks, calendar and today's "
                                                  "activity, then give me the single highest-leverage move."), "kind": "claude"}
            tasks = self.svc.storage.list_tasks()
            return {"reply": f"Next: {tasks[0]['title']}." if tasks else "No open tasks. Add one and I'll line it up.",
                    "kind": "next"}
        args = dict(intent.args)
        hints = {k: args.pop(k) for k in ROUTER_HINT_KEYS if k in args}
        return self.run_tool(intent.tool or "", args, hints=hints)

    def run_tool(self, name: str, args: dict, *, confirmed: bool = False, hints: dict | None = None) -> dict:
        tool = self.tools.tools.get(name)
        if tool is None:
            return {"reply": f"I don't have a {name} tool.", "kind": "error"}
        if tool.needs_confirmation(args) and not confirmed:
            self._set_pending([(name, args)], tool.describe(args))
            return {"reply": f"Confirm: {tool.describe(args)}? Say yes or cancel.", "kind": "pending"}
        result = self.tools.run(name, args)
        self.svc.bus.publish("tool", {"name": name, "args": args, "result": result})
        reply = summarize(name, args, result, self.svc.tz, hints)
        if name == "run_routine" and result.get("close_candidates"):
            names = result["close_candidates"]
            self._set_pending([("close_app", {"name": n}) for n in names], f"close {', '.join(names)}")
            reply += f" Want me to close {', '.join(names)}?"
        return {"reply": reply, "kind": "tool", "tool": name, "data": result}

    def _set_pending(self, calls: list[tuple[str, dict]], text: str) -> None:
        self.pending = {"calls": calls, "text": text, "expires": time.time() + 90}
        self.svc.bus.publish("pending", {"text": text})

    def confirm(self) -> str:
        with self._lock:
            pending, self.pending = self.pending, None
            self.svc.bus.publish("pending", None)
            if not pending or pending["expires"] < time.time():
                return "Nothing waiting on a yes."
            replies = [self.run_tool(name, args, confirmed=True)["reply"] for name, args in pending["calls"]]
            if self.history:  # let Claude know the parked action happened
                self.history.append({"role": "user", "content": f"[system note] User confirmed; executed: {pending['text']}. "
                                                                 f"Result: {' '.join(replies)}"})
                self.history.append({"role": "assistant", "content": "Done."})
            return " ".join(r for r in replies if r) or "Done."

    def cancel(self) -> None:
        self.pending = None
        self.svc.bus.publish("pending", None)

    def reset(self) -> None:
        with self._lock:
            self.history, self.user_turns = [], 0

    # ------------------------------------------------------------------
    def _context_line(self) -> str:
        now = datetime.now(self.svc.tz)
        win = desktop.active_window()
        where = f"{win.app} — {win.title[:100]}" if win else "unknown"
        return f"[{now:%A %B %d %Y, %I:%M %p} | active window: {where} | profile: {self.active_profile or 'none'}]"

    def _create(self, messages: list, *, tools: list | None = None, effort: str | None = None,
                max_tokens: int = 4096, fmt: dict | None = None, system: str | None = None):
        model = self.cfg["model"]
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system or self.system_prompt, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        output_config: dict = {}
        if _supports(model, ADAPTIVE_THINKING_PREFIXES):
            kwargs["thinking"] = {"type": "adaptive"}
            if effort:
                output_config["effort"] = effort
        if fmt:
            output_config["format"] = fmt
        if output_config:
            kwargs["output_config"] = output_config
        if self.cfg.get("server_fallbacks", True) and _supports(model, SERVER_FALLBACK_MODELS):
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        return self.client.beta.messages.create(**kwargs)

    def _ask_claude(self, text: str) -> str:
        if not self.claude_ready:
            return ("I don't know that one offline. Add ANTHROPIC_API_KEY to the .env file and I can handle anything.")
        idle = time.time() - self.last_turn > self.cfg.get("history_idle_minutes", 10) * 60
        if idle or self.user_turns >= self.cfg.get("history_turns", 12):
            self.reset()  # start fresh rather than editing history, so the conversation stays append-only
        checkpoint = len(self.history)
        self.history.append({"role": "user", "content": f"{self._context_line()}\n{text}"})
        self.user_turns += 1
        self.last_turn = time.time()
        definitions = self.tools.definitions()
        try:
            resp = None
            for _ in range(8):
                resp = self._create(self.history, tools=definitions, effort=self.cfg.get("command_effort", "low"))
                if resp.stop_reason == "refusal":
                    del self.history[checkpoint:]
                    self.user_turns -= 1
                    return "I can't help with that one."
                self.history.append({"role": "assistant", "content": resp.content})
                if resp.stop_reason != "tool_use":
                    break
                self.history.append({"role": "user", "content": self._run_tool_calls(resp.content)})
            texts = [b.text for b in resp.content if getattr(b, "type", "") == "text"] if resp else []
            reply = " ".join(t.strip() for t in texts if t.strip())
            return reply or "Done."
        except anthropic.AuthenticationError:
            del self.history[checkpoint:]
            return "Claude rejected the API key. Check ANTHROPIC_API_KEY in .env."
        except anthropic.RateLimitError:
            del self.history[checkpoint:]
            return "I'm rate limited by the Claude API. Try again in a minute."
        except anthropic.APIStatusError as exc:
            del self.history[checkpoint:]
            log.error("Claude API error %s: %s", exc.status_code, exc.message)
            return f"Claude returned an error, status {exc.status_code}."
        except anthropic.APIConnectionError:
            del self.history[checkpoint:]
            return "I can't reach Claude right now. Local commands still work."

    def _run_tool_calls(self, content) -> list[dict]:
        results = []
        pending_calls, pending_text = [], []
        for block in content:
            if getattr(block, "type", "") != "tool_use":
                continue
            args = block.input if isinstance(block.input, dict) else {}
            tool = self.tools.tools.get(block.name)
            if tool and tool.needs_confirmation(args):
                pending_calls.append((block.name, args))
                pending_text.append(tool.describe(args))
                payload: dict = {"status": "awaiting_confirmation", "ask_user": tool.describe(args)}
            else:
                payload = self.tools.run(block.name, args)
                self.svc.bus.publish("tool", {"name": block.name, "args": args, "result": payload})
            results.append({
                "type": "tool_result", "tool_use_id": block.id, "content": compact(payload),
                **({"is_error": True} if payload.get("ok") is False else {}),
            })
        if pending_calls:
            self._set_pending(pending_calls, "; ".join(pending_text))
        return results

    # ------------------------------------------------------------------
    def _fresh_morning_briefing(self, max_age_hours: float = 3) -> dict | None:
        """Reuse the briefing pre-built at first activity instead of paying for a second one."""
        b = self.last_briefing
        if not b or b.get("kind") != "morning" or time.time() - b["created"] > max_age_hours * 3600:
            return None
        built = datetime.fromtimestamp(b["created"], self.svc.tz).date()
        return b if built == datetime.now(self.svc.tz).date() else None

    def briefing(self, kind: str = "morning") -> dict:
        data = briefing.gather_morning(self.svc) if kind == "morning" else briefing.gather_recap(self.svc)
        user = self.svc.cfg["assistant"]["user_name"]
        result = None
        if self.claude_ready:
            template = briefing.MORNING_INSTRUCTIONS if kind == "morning" else briefing.RECAP_INSTRUCTIONS
            prompt = template.format(user=user, data=json.dumps(data, default=str))
            try:
                resp = self._create([{"role": "user", "content": prompt}], effort=self.cfg.get("briefing_effort", "high"),
                                    max_tokens=16000, fmt={"type": "json_schema", "schema": briefing.BRIEFING_SCHEMA})
                if resp.stop_reason not in {"refusal", "max_tokens"}:
                    text = next(b.text for b in resp.content if getattr(b, "type", "") == "text")
                    result = json.loads(text)
                    result["generated_by"] = "claude"
            except (anthropic.APIError, StopIteration, json.JSONDecodeError):
                log.exception("Claude briefing failed; using local version")
        if result is None:
            result = (briefing.fallback_morning if kind == "morning" else briefing.fallback_recap)(data, user)
        result.update(kind=kind, created=time.time())
        self.last_briefing = result
        self.svc.bus.publish("briefing", result, sticky=True)
        # Keep it in the conversation so follow-ups ("move the second one to 3pm") have context.
        # The lock matters: this can run on the morning pre-build thread while a command is mid tool-loop.
        if self.claude_ready:
            with self._lock:
                if time.time() - self.last_turn > self.cfg.get("history_idle_minutes", 10) * 60:
                    self.reset()
                self.history.append({"role": "user", "content": f"[{kind} briefing requested]"})
                self.history.append({"role": "assistant", "content": json.dumps(
                    {k: result[k] for k in ("headline", "top_moves", "risks") if k in result})})
                self.last_turn = time.time()
        return result

    def research(self, prompt: str) -> str:
        """Blocking web-research call for background jobs."""
        if not self.claude_ready:
            raise RuntimeError("Claude API key not configured")
        messages: list = [{"role": "user", "content": (
            "Research this and write a tight brief for a busy operator: key findings with numbers, what it means "
            f"for them, recommended next step, and sources.\n\nTopic: {prompt}")}]
        resp = None
        for _ in range(5):
            resp = self._create(messages, tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
                                effort="high", max_tokens=16000, system="You are a precise research analyst.")
            if resp.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": resp.content})
        if resp is None or resp.stop_reason == "refusal":
            raise RuntimeError("research request was declined")
        return "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
