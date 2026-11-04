"""The assistant: fast-path router first, then a model with tools for everything else: Claude, or a Llama
(or other model) running on this PC.

Risky actions (closing apps, going live, shutting down) never run straight
from a single utterance — they're parked as a pending confirmation until you
say "yes" (or click Confirm on the dashboard).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx

from ..config import goal_lines
from ..integrations import desktop
from ..privacy import redact
from ..services import Services
from ..voice import neural
from ..voice.confirm_window import question
from . import briefing, conversation as convo_mod, local_llm, macros as macro_mod, model_bench
from .router import Intent, RouterContext, clean, route
from .speech import clock, summarize
from .tools import ToolBox, compact, urls

log = logging.getLogger(__name__)


class _LazySDK:
    """The Anthropic SDK, imported on first use: it is a second of every start and ~60 MB that a PC running only its
    own model never needs (PERF-5)."""

    def __getattr__(self, name):
        import anthropic as sdk

        globals()["anthropic"] = sdk
        return getattr(sdk, name)


anthropic = _LazySDK()

ADAPTIVE_THINKING_PREFIXES = ("claude-opus-5", "claude-fable-5", "claude-mythos-5", "claude-sonnet-5",
                              "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")
SERVER_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5")
ROUTER_HINT_KEYS = ("next_only", "when")

SYSTEM_PROMPT = """You are {name}, the personal operations assistant running on {user}'s Windows PC. You control the
machine through tools: apps and windows, Chrome, OBS, media keys, calendars, news, projects, tasks, background jobs.

How you operate:
- Time is money. Bias to action: if a tool can do it, call the tool instead of explaining how.
- Everything ladders up to {user}'s goals: {north_star}. This week: {this_week}.
  When {user} asks what to do, rank by leverage toward those goals, and say plainly when something is a distraction.
- Replies are spoken aloud: 1-3 short sentences, plain words, no markdown, no lists, no URLs, round numbers.
  If {user} asks for detail, you may go longer, still without markdown.
- Never invent data. If a tool fails or something isn't configured, say exactly what's missing.
- Some tools return status "awaiting_confirmation": the action has NOT happened. Ask one short yes/no question.
- Each user turn starts with a bracketed context line (time, active window, active profile). Use it; don't repeat it.
- {user}'s documents (Word, PDF, slides, notes in their folders) are searchable with search_library. For questions
  about their work, plans or anything they may have written down, check there before saying you don't know, and
  say which file the answer came from.
- Background work: for anything that takes more than a minute (research, coding in a repo), use start_job so
  {user} can keep working, then say it's running.
Profiles: {profiles}."""


LOCAL_ADDENDUM = """
Tools: call one only when the request needs it, using the exact names given; otherwise just answer. After a tool
returns, answer in one or two short spoken sentences from its result. Never make up a tool result."""
# Small models sometimes repeat the context line back, bracketed ("[Monday … | active window: …]") or not
# ("Thursday October 08 2026, 1:00 PM | active window: To Do List | profile: work", llama3.2:3b in CI): never say it.
CONTEXT_ECHO = re.compile(r"^\s*(?:Right now:\s*)?(?:\[[^\]\n]*\|\s*active window:[^\]\n]*\]"  # up to the bracket
                          r"|[^\[\]\n]*\|\s*active window:[^\n]*(?:\n|$))\s*")  # or the whole line
# A model Ollama says can't call tools still answers questions, but must not pretend it did something.
NO_ACTIONS = """
You can't take actions on this PC with the model that's answering now. If asked to do something, say so in one
sentence and that Setup > Brain can switch to a model that can."""
BENCH_FILE = local_llm.BENCH_FILE
OFFLINE = ("I can't answer that one without a model. Start Ollama or LM Studio with a Llama model, "
           "or add ANTHROPIC_API_KEY to the .env file.")
# Commands Vesper runs by itself (a routine, the planner): mid-game they're background work, not J asking (see
# local_llm.GAME_KEEP_ALIVE), so they never load a model onto the graphics card.
BACKGROUND_SOURCES = ("automation", "planner")
GAME_WAIT = "That needs the model, and you're in a game: it waits until the game is over."
# A connection that stalls (Wi-Fi gone, the socket left open) gives up within a minute, and is tried once more: the
# SDK's own default waits ten minutes, three times, while every voice command queues behind it.
COMMAND_TIMEOUT_S, COMMAND_RETRIES = 60.0, 1
SKILL_READ_S = 180.0  # a skill's question (a job's summary: 2,500 words) runs in the background
LONG_READ_S = 600.0  # the briefing and research write long answers with a lot of thinking first
CHANGED = "That question has changed: {question}"  # a yes or no for a question that was since replaced
AT_THE_PC = ("voice", "text")  # where the waiting question is in front of J (said aloud, or the HUD card)


def _clip_spoken(text: str, max_words: int = 90) -> str:
    """The spoken briefing, cut at a sentence end near ``max_words``; the HUD keeps the full plan."""
    if len(text.split()) <= max_words:
        return text
    out, count = [], 0
    for sentence in neural.split_sentences(text):
        n = len(sentence.split())
        if out and count + n > max_words:
            break
        out.append(sentence)
        count += n
    return " ".join(out)


def _supports(model: str, prefixes: tuple[str, ...]) -> bool:
    return model.startswith(prefixes)


class _Stopped(Exception):
    """The kill switch fired while Claude was still writing."""


class Assistant:
    def __init__(self, svc: Services, toolbox: ToolBox | None = None, client: anthropic.Anthropic | None = None):
        self.svc = svc
        self.cfg = svc.cfg["claude"]
        self.local = local_llm.LocalBrain((svc.cfg.get("brain") or {}).get("local") or {}, vram_mb=self.vram_mb,
                                          bench_path=svc.cfg.data_dir / BENCH_FILE)
        self.local_history: list[dict] = []  # OpenAI-style turns for the local model
        self.low_power = lambda: ""  # "game" while a fullscreen game runs (set by the runtime)
        # a fullscreen game is running, live or not (the runtime sets it): background model work then never loads a
        # model onto the graphics card (local_llm.GAME_KEEP_ALIVE)
        self.gaming = lambda: self.low_power() == "game"
        self._ctx: dict = {}  # the command being handled: source, utterance, turn, owner (for the audit log)
        self._tainted = False  # this command has read text other people wrote (see Tool.untrusted)
        self._untrusted_ids: dict[str, str] = {}  # tool results to scrub from history once the command ends
        self._tainted_by = "chat"
        self._known_urls: set[str] = set()  # links from J's own words or a search this command (see Tool.network)
        self.tools = toolbox or ToolBox(svc, context=lambda: self._ctx)
        svc.think = self.think  # skills ask the model through this
        svc.model_waits = self.model_waits
        svc.off_record = self.set_off_record
        self.convo = convo_mod.Conversation(svc)  # conversation mode (brain/conversation.py)
        self.wait = time.sleep  # a macro's "wait" step (a test swaps this, not the process-wide time.sleep)
        self.convo.brain = self.brain
        self.convo.off_record_check = lambda: time.time() < self.off_record_until
        self.convo.on_forget = self._forget_history  # "forget this conversation": the models' history goes too
        self._history_forgotten = False
        svc.conversation = self.convo
        self.on_kill = None  # the runtime adds what only it can stop: speech, background jobs
        self.on_pending = None  # e.g. a Windows toast with Yes/No buttons
        self.macros = macro_mod.load_macros(svc.cfg)
        for problem in macro_mod.validate(self.macros, set(self.tools.tools)):
            log.warning(problem)
        self.client = client if client is not None else self._make_client()
        self.history: list[dict] = []
        self.user_turns = 0
        self.last_turn = 0.0
        self.pending: dict | None = None
        self._queued: list[dict] = []  # questions waiting their turn: a new one never replaces the one J is answering
        self.replaced: str | None = None  # the question J's newest request just replaced (said in the reply)
        self._asked: dict[str, str] = {}  # source -> the question it was last shown, for a bare "yes" (see confirm)
        self._late_notes: list[dict] = []  # "[system note] User confirmed" from a yes that ran beside a request
        self._plock = threading.RLock()  # pending and _queued (an offer comes in on the EventSub thread)
        self.active_profile: str | None = None
        self.last_briefing: dict | None = None
        self._lock = threading.RLock()
        self._stream_on, self._speak_turn, self._streamed = False, None, False  # set per command in handle()
        self._early = False  # a fast-path reply is spoken the moment it exists (not inside a macro's steps)
        self.system_prompt = self._build_system_prompt()
        svc.bus.on(self._track_profile)

    def _build_system_prompt(self) -> str:
        cfg = self.svc.cfg
        a = cfg["assistant"]
        return SYSTEM_PROMPT.format(
            name=a["name"], user=a["user_name"],
            north_star="; ".join(goal_lines(cfg["goals"])) or "(not set: ask the user to set them in the HUD, Command tab, Goals & tasks)",
            this_week="; ".join(cfg["goals"].get("this_week") or []) or "(not set)",
            profiles=", ".join(f"{k} ({v.get('label', k)})" for k, v in cfg["profiles"].items()),
        )

    def refresh_goals(self) -> None:
        """The goals changed (HUD → Command → Goals & tasks): rank against the new ones from the next request on."""
        self.system_prompt = self._build_system_prompt()

    # ------------------------------------------------------------------
    def _make_client(self) -> anthropic.Anthropic | None:
        if not self.cfg.get("enabled", True):
            return None
        key = self.svc.cfg.secret(self.cfg.get("api_key_env"))
        # No key, no token and no `ant auth login` profile means no brain; say so up front (without the SDK)
        has_profile = (Path.home() / ".config" / "anthropic").exists()
        if not (key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or has_profile):
            log.warning("Claude API not configured; running on the local command router only")
            return None
        limits = {"timeout": anthropic.Timeout(COMMAND_TIMEOUT_S, connect=5.0), "max_retries": COMMAND_RETRIES}
        try:
            client = anthropic.Anthropic(api_key=key, **limits) if key else anthropic.Anthropic(**limits)
        except Exception:
            client = None
        if client is not None and not (client.api_key or getattr(client, "auth_token", None) or has_profile):
            client = None
        if client is None:
            log.warning("Claude API not configured; running on the local command router only")
        return client

    def _client_for(self, read_s: float):
        """The Claude client with a longer read timeout, for answers that take longer to write than a command's."""
        with_options = getattr(self.client, "with_options", None)
        return with_options(timeout=anthropic.Timeout(read_s, connect=5.0)) if with_options else self.client

    def set_off_record(self, minutes: float) -> float:
        self.off_record_until = time.time() + max(0.0, minutes) * 60
        return self.off_record_until

    def think(self, prompt: str, schema: dict | None = None, max_tokens: int = 1200, private: bool = True):
        """One question to the model for a skill (triage, summaries, plans): a dict when ``schema`` is given, else
        text; None when no model is connected. ``private``: the prompt holds email/documents, so the local model is
        preferred and Claude is only used when the user chose it (brain.provider: claude)."""
        pref = (self.svc.cfg.get("brain") or {}).get("provider", "auto")
        order = {"claude": ["claude", "local"], "local": ["local"]}.get(pref) or (
            ["local", "claude"] if private else ["claude", "local"])
        text = None
        game = self.gaming()
        for which in order:
            try:
                if which == "local" and self.local.ready:
                    # mid-game a skill's question gets only a model already loaded (J's own: the usual choice), and
                    # leaves it loaded briefly (local_llm.in_game)
                    llm = self._game_llm() if game else self.local.refresh()
                    if llm is None and game:
                        break  # nothing it may use: wait for the game to end (and keep private text local)
                    if llm is not None:
                        text = llm.chat([{"role": "user", "content": prompt}], json_schema=schema, temperature=0.2,
                                        max_tokens=max_tokens)["content"]
                elif which == "claude" and self.claude_ready:
                    ask = prompt + ("\n\nAnswer with JSON only, matching this schema: " + json.dumps(schema) if schema else "")
                    msg = self._client_for(SKILL_READ_S).messages.create(model=self.cfg["model"], max_tokens=max_tokens,
                                                                         messages=[{"role": "user", "content": ask}])
                    text = "".join(getattr(b, "text", "") for b in msg.content)
            except Exception:
                log.exception("think (%s) failed", which)
            if text:
                break
        if not text:
            return None
        if schema is None:
            return text.strip()
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            return json.loads(text[text.find("{"): text.rfind("}") + 1] if "{" in text else text)
        except ValueError:
            return None

    @property
    def claude_ready(self) -> bool:
        return self.client is not None

    def brain(self) -> str | None:
        """Which model answers what the router can't: "claude", "local", or None (router only)."""
        pref = (self.svc.cfg.get("brain") or {}).get("provider", "auto")
        if pref == "local":
            return "local" if self.local.ready else None  # "local" means never send it to the cloud
        if self.claude_ready:
            return "claude"
        if pref == "auto" and self.local.ready:
            return "local"
        return None

    def brain_status(self) -> dict:
        b = self.brain()
        return {"active": b, "provider": (self.svc.cfg.get("brain") or {}).get("provider", "auto"),
                "claude": {"ready": self.claude_ready, "model": self.cfg["model"]}, "local": self.local.status()}

    def _ask(self, text: str, convo: bool = False) -> str:
        """``convo``: a turn in conversation mode (persona, longer history, rolling summary)."""
        brain = self.brain()
        if brain == "claude":
            return self._ask_claude(text, convo=convo)
        if brain == "local":
            return self._ask_local(text, convo=convo)
        return OFFLINE

    # ---- conversation mode (brain/conversation.py) -----------------------------
    def _history_limits(self, convo: bool) -> tuple[float, int]:
        """(idle minutes, turns) before the history starts fresh: longer while talking."""
        if convo:
            return (float(self.convo.setting("history_idle_minutes", 30)), int(self.convo.setting("history_turns", 40)))
        return float(self.cfg.get("history_idle_minutes", 10)), int(self.cfg.get("history_turns", 12))

    def _conversation_prompt(self) -> str:
        user = self.svc.cfg["assistant"]["user_name"]
        out = convo_mod.PERSONA.format(user=user)
        if self.convo.on_stream:
            out += convo_mod.ON_STREAM.format(user=user)
        if self.convo.summary:
            out += f"\nEarlier in this conversation (summary): {self.convo.summary}"
        out += self.convo.asides()  # background jobs that just finished (skills/tasks_agent.py)
        return out + self.convo.prompt_extra()  # style, mode, the thread it continues (Vesper 2.2)

    def _fold(self, msgs: list, is_turn) -> list:
        """Drop all but the last few exchanges from ``msgs``; what's dropped goes into the rolling summary."""
        keep = max(2, int(self.convo.setting("keep_turns", 8)))
        starts = [i for i, m in enumerate(msgs) if is_turn(m)]
        if len(starts) <= keep:
            return msgs
        self.convo.fold(self.think, keep)
        return msgs[starts[-keep]:]

    def start_conversation(self, on_stream: bool = False, topic: str = "", source: str = "voice", mode: str = "",
                           resume: str | None = None, off_record: bool = False) -> dict:
        """"Let's talk" (or the HUD's Talk button): no wake word between turns until it ends. ``mode`` (brainstorm,
        coach…), ``resume`` (continue an earlier thread: its topic, or "" for the last one), ``off_record``."""
        ok, why = self.convo.begin(on_stream=on_stream, source=source, mode=mode, off_record=off_record,
                                   fresh=bool(topic or mode or resume is not None))
        if not ok:
            return {"reply": why, "kind": "conversation_off"}
        opener = self.convo.opener(topic=topic, resume=resume, mode=mode, already=why == "already")
        if opener:  # a recap, an offer to pick up an earlier talk, or a mode's hello: no model call
            return {"reply": opener, "kind": "conversation"}
        if topic:
            with self._lock:
                return {"reply": self._ask(f"Let's talk about {topic}.", convo=True), "kind": self.brain() or "offline"}
        if self.convo.on_stream:
            greet = convo_mod.ON_STREAM_GREETING
        elif why == "already":
            greet = "We're already talking. Go on."
        else:
            greet = convo_mod.GREETINGS[int(time.time()) % len(convo_mod.GREETINGS)]
        return {"reply": greet, "kind": "conversation"}

    def end_conversation(self, reason: str = "said") -> dict:
        ended = self.convo.end(reason)
        return {"reply": convo_mod.GOODBYE if ended else "We weren't in a conversation.", "kind": "conversation_end"}

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
            macro_match=lambda t: macro_mod.match(t, self.macros),
            skill_route=self.svc.skills.route if self.svc.skills is not None else (lambda t: None),
        )

    # ------------------------------------------------------------------
    def handle(self, text: str, source: str = "text", turn: int | None = None, owner: str | None = None,
               pending_id: str | None = None) -> dict:
        """``turn`` (voice commands) tags the spoken reply, so a newer command drops what's left of it.
        ``owner`` is how the speaker check judged the voice: match | mismatch | pressed | unknown.
        ``pending_id``: the question a yes or no answers (a phone button): one for a question since replaced runs
        nothing."""
        text = (text or "").strip()
        if not text:
            return {"reply": "", "kind": "empty"}
        # a command J gave (said, typed, the phone): its model calls are his own request, even mid-game
        with self._lock, local_llm.asking(source not in BACKGROUND_SOURCES):
            self._ctx = {"source": source, "utterance": text[:200], "turn": turn, "owner": owner, "pending_id": pending_id}
            asked_before = (self.pending_view() or {}).get("id")
            # the morning briefing (invite titles, email senders) stays in Claude's history for follow-ups
            self._tainted = any(isinstance(m.get("content"), str) and m["content"] in BRIEFING_MARKS for m in self.history)
            self._tainted_by = "your briefing" if self._tainted else "chat"
            self._known_urls = urls(text)
            self.tools.guard.new_command()
            convo = self.convo
            in_convo = convo.is_active()
            try:
                intent = route(text, self.router_context())
            except Exception:
                log.exception("routing failed")
                intent = None
            if (in_convo and intent is not None and intent.kind in ("confirm", "cancel") and not self.pending_view()
                    and not pending_id and clean(text) not in HARD_CANCEL):
                intent = None  # "yeah" / "nope" answers what Vesper asked; there's nothing to confirm
            # private words (a journal entry) never go into the conversation log or its search
            private = intent is not None and intent.tool in PRIVATE_TOOLS
            if not self.off_record() and not private:
                self.svc.storage.log("user", redact(text), source)  # a pasted key never lands in the log
            shown = (PRIVATE_TOOLS[intent.tool] or text) if private else text
            self.svc.bus.publish("user_said", {"text": shown, "source": source,
                                               **({"conversation": True} if in_convo else {})})
            self.svc.bus.publish("thinking", {"active": True})
            started = time.time()
            # In a conversation J is talking by voice: a line typed in between is answered out loud too.
            speak = (source == "voice" or self.svc.cfg["voice"].get("speak_typed")
                     or (in_convo and convo.last_source == "voice" and source != "automation"))
            if in_convo:
                convo.turn_started(shown, source, owner=owner)
            # Claude's reply is spoken sentence by sentence as it streams in, not after the last word.
            self._stream_on = bool(speak and self.svc.cfg["voice"]["tts"].get("stream", True))
            self._speak_turn, self._streamed, self._early = turn, False, self._stream_on
            try:
                if intent is not None:
                    out = self._run_intent(intent)
                else:
                    brain = self.brain()
                    if in_convo and brain is None:  # the model went away mid-conversation: say so once
                        convo.end("no model")
                        out = {"reply": convo_mod.LOST_MODEL, "kind": "offline"}
                    else:
                        out = {"reply": self._ask(text, convo=in_convo), "kind": brain or "offline"}
            except _Stopped:  # the kill switch, while the model was still on it
                self._streamed = False
                out = {"reply": "Stopped. PC control is paused.", "kind": "kill"}
            except Exception as exc:
                log.exception("command failed")
                out = {"reply": f"That failed: {type(exc).__name__}.", "kind": "error"}
            finally:
                self._stream_on = self._early = False
                self._scrub_untrusted()
                self.tools.guard.end_command()
                self.svc.bus.publish("thinking", {"active": False})
            out["ms"] = round((time.time() - started) * 1000)
            reply = out.get("reply") or ""
            if reply and speak and not self._streamed:  # spoken first: the log and the HUD can wait a few ms
                # A waiting confirmation keeps the mic open for the "yes"; otherwise only a question does.
                self.svc.speak(reply, expects_reply=True if self.pending_view() else None, turn=turn)
            if in_convo or convo.active:  # the HUD's conversation panel
                convo.turn_done(reply, source, said=None if in_convo else text, started=in_convo, owner=owner)
                out["conversation"] = convo.active
            if reply:
                if not self.off_record() and not private:
                    self.svc.storage.log("assistant", redact(reply), source)
            if private:
                out["private"] = True  # (the diagnostics report leaves its words out too)
            self._note_asked(source, asked_before)
            self.svc.bus.publish("assistant_said", {
                "text": reply, "kind": out.get("kind"), "source": source, "ms": out["ms"],
                "pending": self.pending_view(),
            })
            self._ctx = {}
            return out

    def _speak_sentence(self, sentence: str, final: bool) -> None:
        """One streamed sentence. The last one decides whether the mic stays open for an answer."""
        self._streamed = True
        expects = (True if self.pending_view() else None) if final else False
        self.svc.speak(sentence, expects_reply=expects, turn=self._speak_turn)

    def pending_view(self) -> dict | None:
        if self.pending and self.pending["expires"] > time.time():
            return {"text": self.pending["text"], "id": self.pending["id"], "tier": self.pending["tier"]}
        return None

    def _note_asked(self, source: str, before: str | None) -> None:
        """The question ``source`` has now been shown, for a bare "yes" from it later. A reply on a screen (the HUD,
        the phone) comes with the waiting card; a spoken one only asks a question this command raised."""
        view = self.pending_view()
        if view and source and (view["id"] != before or source != "voice"):
            self._asked[source] = view["id"]

    def _changed_since(self, pending: dict, pending_id: str | None, source: str | None) -> str | None:
        """Why a yes or no isn't for the question waiting now, or None. With an id (a card, a button, the voice
        window): it must be this one. Without (typed, or said with the wake word): the question its surface was last
        shown must still be the one waiting; away from the PC (the phone), it must have been shown at all."""
        if pending_id:
            stale = pending_id != pending["id"]
        elif source:
            seen = self._asked.get(source)
            stale = seen != pending["id"] if seen is not None or source not in AT_THE_PC else False
        else:
            stale = False
        if not stale:
            return None
        if source:
            self._asked[source] = pending["id"]  # the reply asks this one: a yes to it now is for it
        return CHANGED.format(question=question(pending["text"]))

    # ------------------------------------------------------------------
    def _run_intent(self, intent: Intent) -> dict:
        kind = intent.kind
        ctx = self._ctx
        if kind == "confirm":
            via = "voice" if ctx.get("source") == "voice" else "hud"
            return {"reply": self.confirm(ctx.get("pending_id"), via=via, owner=ctx.get("owner"), source=ctx.get("source")),
                    "kind": "confirm"}
        if kind == "cancel":
            return {"reply": self.decline(ctx.get("pending_id"), source=ctx.get("source")), "kind": "cancel"}
        if kind == "kill":
            self.stop_everything("voice")
            return {"reply": "Stopped. PC control is paused until you say resume control.", "kind": "kill"}
        if kind == "resume":
            was = self.tools.guard.hands_off
            self.resume_control()
            return {"reply": "Back in control." if was else "PC control wasn't paused.", "kind": "resume"}
        if kind == "reset":
            self.reset()
            self.convo.reset()
            return {"reply": "Fresh start.", "kind": "reset"}
        if kind == "conversation":
            return self.start_conversation(on_stream=bool(intent.args.get("on_stream")),
                                           topic=str(intent.args.get("topic") or ""),
                                           source=self._ctx.get("source") or "text", mode=str(intent.args.get("mode") or ""),
                                           resume=intent.args.get("resume"), off_record=bool(intent.args.get("off_record")))
        if kind == "conversation_end":
            return self.end_conversation("said")
        if kind == "time":
            now = datetime.now(self.svc.tz)
            return {"reply": f"It's {clock(now)}, {now.strftime('%A, %B')} {now.day}.", "kind": "time"}
        if kind == "briefing":
            b = self._fresh_morning_briefing() or self.briefing("morning")
            return {"reply": self._spoken_briefing(b, "morning"), "kind": "briefing", "data": b}
        if kind == "recap":
            b = self.briefing("recap")
            return {"reply": self._spoken_briefing(b, "recap"), "kind": "recap", "data": b}
        if kind == "next":
            if self.brain():
                return {"reply": self._ask("What should I work on next? Check my tasks, calendar and today's "
                                           "activity, then give me the single highest-leverage move."), "kind": self.brain()}
            tasks = self.svc.storage.list_tasks()
            return {"reply": f"Next: {tasks[0]['title']}." if tasks else "No open tasks. Add one and I'll line it up.",
                    "kind": "next"}
        if kind == "macro":
            return self.run_macro(intent.args["name"])
        args = dict(intent.args)
        hints = {k: args.pop(k) for k in ROUTER_HINT_KEYS if k in args}
        return self.run_tool(intent.tool or "", args, hints=hints)

    # ---- macros (trigger phrases) --------------------------------------
    def macro_risks(self, name: str) -> list[str]:
        """What in this macro needs a yes, worded for the question (see _macro_question)."""
        return self._macro_question(name)[0]

    def macro_needs_yes(self, name: str) -> bool:
        """For the HUD's macro buttons: only whether a step needs a yes, on throwaway copies (nothing is kept)."""
        macro = self.macros.get(name.lower())
        return any(tool and tool.needs_confirmation(args)
                   for tool, args in ((self.tools.tools.get(t), a) for t, a in (macro.tool_steps() if macro else [])))

    def _macro_question(self, name: str) -> tuple[list[str], list[dict]]:
        """(what needs a yes, each tool step's args as that question pinned them: a slot, a login). The pins travel
        with the question (``_pins`` in its call), so its yes runs exactly what it described."""
        macro = self.macros.get(name.lower())
        risks, pinned = [], []
        for tool_name, args in macro.tool_steps() if macro else []:
            tool = self.tools.tools.get(tool_name)
            if tool and tool.needs_confirmation(args):
                risks.append(tool.describe(args))  # may pin the exact target into this copy (a slot, a login)
            pinned.append(args)
        return risks, pinned

    def _tier_of(self, name: str, args: dict) -> int:
        if name == "run_macro":
            macro = self.macros.get(str(args.get("name", "")).lower())
            return max([self._tier_of(t, a) for t, a in macro.tool_steps()] or [0]) if macro else 0
        tool = self.tools.tools.get(name)
        return tool.tier_for(args) if tool else 0

    def run_macro(self, name: str, *, confirmed: bool | str = False, pins: list[dict] | None = None) -> dict:
        """``confirmed``: False, or how the yes came in (voice / hud / toast); True means the HUD. ``pins``: the step
        args the question that got this yes described (a HUD click without one works them out now)."""
        early, self._early = self._early, False  # its steps' replies aren't spoken one by one
        try:
            return self._run_macro(name, confirmed=confirmed, pins=pins)
        finally:
            self._early = early

    def _run_macro(self, name: str, *, confirmed: bool | str = False, pins: list[dict] | None = None) -> dict:
        via = confirmed if isinstance(confirmed, str) else ("hud" if confirmed else None)
        macro = self.macros.get(name.lower())
        if macro is None:
            return {"reply": f"I don't have a macro called {name}.", "kind": "error"}
        # at the yes, run what its question pinned; working the args out again could pick a different slot
        risks, fresh = self._macro_question(name) if not (via and pins is not None) else ([], pins)
        if risks and not via:
            if not self._set_pending([("run_macro", {"name": macro.name, "_pins": fresh})],
                                     f"run {macro.name} ({'; '.join(risks)})"):
                return {"reply": f"I'll ask you about running {macro.name} once you've answered me.", "kind": "pending"}
            return {"reply": f"{macro.name} will {', '.join(risks)}. Say yes to run it.{self._replaced_note()}",
                    "kind": "pending"}
        spoken, results = [], []
        pinned = iter(fresh)
        for step in macro.steps:
            if self.tools.guard.hands_off:
                spoken.append(f"{macro.name} stopped: PC control is paused.")
                break
            (kind, value), = step.items()
            if kind == "say":
                spoken.append(str(value))
            elif kind == "wait":
                self.wait(max(0.0, min(float(value or 0), 30.0)))
            elif kind == "command":
                intent = route(str(value), self.router_context())
                out = self._run_intent(intent) if intent and intent.kind not in ("macro",) else {
                    "reply": self._ask(str(value)) if self.brain() else ""}
                results.append({"command": value, "reply": out.get("reply")})
            else:
                args = next(pinned, None) or copy.deepcopy(value or {})
                result = self.tools.run(kind, args, confirmed_by=f"macro via {via}" if via else None)
                self.svc.bus.publish("tool", {"name": kind, "args": args, "result": result, "macro": macro.name})
                results.append({"tool": kind, "ok": result.get("ok", True), "error": result.get("error")})
                if result.get("ok") is False and result.get("error"):
                    spoken.append(result["error"])
        failed = sum(1 for r in results if r.get("ok") is False)
        reply = " ".join(spoken) or macro.reply or (f"{macro.name} done." if not failed else "")
        if failed and not spoken:
            reply = f"{macro.name}: {failed} step{'s' if failed != 1 else ''} failed."
        self.svc.bus.publish("macro", {"name": macro.name, "results": results})
        return {"reply": reply, "kind": "macro", "data": results}

    def macro_definition(self) -> dict | None:
        if not self.macros:
            return None
        listing = "; ".join(f"{m.name} ({', '.join(m.triggers[:3])})" for m in self.macros.values())
        return {"name": "run_macro", "description": f"Run one of the user's saved macros (trigger phrases): {listing}.",
                "input_schema": {"type": "object", "properties": {"name": {"type": "string", "enum": [m.name for m in self.macros.values()]}},
                                 "required": ["name"]}}

    def run_tool(self, name: str, args: dict, *, confirmed: bool | str = False, hints: dict | None = None,
                 beside: bool = False) -> dict:
        """``confirmed``: False, or how the yes came in (voice / hud / toast); True means the HUD. ``beside``: a yes
        that came in while another thread's command holds the model (see confirm): it runs with its own context and
        isn't spoken as part of that command's reply."""
        via = confirmed if isinstance(confirmed, str) else ("hud" if confirmed else None)
        if name == "run_macro":
            pins = args.get("_pins") if via else None  # only a confirmed question's own pins
            if beside:
                with self._lock:  # a macro's steps may ask the model: this one waits for the request in flight
                    return self.run_macro(args.get("name", ""), confirmed=via or False, pins=pins)
            return self.run_macro(args.get("name", ""), confirmed=via or False, pins=pins)
        tool = self.tools.tools.get(name)
        if tool is None:
            return {"reply": f"I don't have a {name} tool.", "kind": "error"}
        if tool.needs_confirmation(args) and not via:
            what = tool.describe(args)  # works out and pins the exact target (a raid's login, a slot, a job's items)
            if not self._set_pending([(name, args)], what):
                return {"reply": f"I'll ask you to {what} once you've answered me.", "kind": "pending"}
            return {"reply": question(what) + self._replaced_note(), "kind": "pending"}  # asked once (confirm_window.py)
        result = self.tools.run(name, args, confirmed_by=via, context={"source": via} if beside else None)
        said = self.svc.skills.say(name, args, result) if self.svc.skills is not None and not _failed(result) else None
        reply = said or summarize(name, args, result, self.svc.tz, hints)
        if name == "run_routine" and result.get("close_candidates"):
            names = result["close_candidates"]
            if self._set_pending([("close_app", {"name": n}) for n in names], f"close {', '.join(names)}"):
                reply += f" Want me to close {', '.join(names)}?{self._replaced_note()}"
            else:
                reply += f" I'll ask about closing {', '.join(names)} once you've answered me."
        if self._early and reply and not beside:  # say it now; the HUD's tool chip and panels follow
            self._speak_sentence(reply, final=True)
        self.svc.bus.publish("tool", {"name": name, "args": {} if name in PRIVATE_TOOLS else args, "result": result})
        return {"reply": reply, "kind": "tool", "tool": name, "data": result}

    def offer(self, calls: list[tuple[str, dict]], text: str) -> bool:
        """Something Vesper suggests on its own (a shoutout after a raid): waits for a yes like any T2 action. False:
        J is answering another question, so it waits its turn (asked when that one is settled) and isn't asked now."""
        return self._set_pending(calls, text, unprompted=True)

    def _wait_s(self) -> float:
        return 60 * float((self.svc.cfg.get("pc_control") or {}).get("confirm_minutes", 2))  # the HUD/toast card

    def _set_pending(self, calls: list[tuple[str, dict]], text: str, unprompted: bool = False) -> bool:
        """Ask for a yes. A yes only ever runs what J was asked about, so a question never silently replaces the one
        he's answering: one Vesper raises on its own (an offer, a routine) queues behind it; J's own newer request
        replaces it, and ``replaced`` holds the old one for the reply to say so (never re-asked: he moved on, and a
        second reflexive yes could end a stream he just started). True: this one is the question now."""
        tier = max([self._tier_of(n, a) for n, a in calls] or [0])
        new = {"id": uuid.uuid4().hex[:10], "calls": calls, "text": text, "tier": tier, "expires": time.time() + self._wait_s()}
        with self._plock:
            old = self.pending if self.pending_view() else None
            self.replaced = None
            if old is not None and old["calls"] != calls:
                if unprompted or self._ctx.get("source") == "automation":
                    self._queued.append(new)
                    log.info("question queued behind the one waiting: %s", text)
                    return False
                self.replaced = old["text"]
            self.pending = new
        self.svc.bus.publish("pending", self.pending_view())
        if self.on_pending:
            self.on_pending(self.pending_view())
        return True

    def _replaced_note(self) -> str:
        return f" I've dropped the earlier question: {self.replaced}." if self.replaced else ""

    def _next_question(self) -> str:
        """Once nothing waits on a yes: the question that was waiting its turn becomes the one asked ("" if none)."""
        with self._plock:
            if self.pending_view() is not None:
                return ""
            now = time.time()
            self._queued = [q for q in self._queued if q["expires"] > now]
            if not self._queued:
                return ""
            self.pending = nxt = {**self._queued.pop(0), "id": uuid.uuid4().hex[:10], "expires": now + self._wait_s()}
        self.svc.bus.publish("pending", self.pending_view())
        if self.on_pending:
            self.on_pending(self.pending_view())
        return f"Next: {question(nxt['text'])}"

    def confirm(self, pending_id: str | None = None, via: str | None = None, owner: str | None = None,
                source: str | None = None) -> str:
        """Run what's waiting on a yes. ``pending_id`` (HUD, toast, a phone button, the voice window) must match
        what's waiting now, so a slow click can never confirm a different action that replaced it; a yes without one
        (typed, or said with the wake word) answers what its ``source`` was last shown (see _changed_since).
        ``owner``: how the speaker check judged a spoken yes. A yes never waits for a model request in flight on
        another thread (a stalled connection): it runs beside it."""
        via = via or "hud"
        with self._plock:
            pending = self.pending
            live = bool(pending) and pending["expires"] >= time.time()
            why = None
            if not live:
                self.pending = None
            else:
                why = self._changed_since(pending, pending_id, source)
                if why is None and via == "voice" and pending["tier"] >= 3 and owner in ("mismatch", "unsure"):
                    # LOG mode lets other voices through, and a bare "yes" is too short to judge: an irreversible
                    # action still needs yours.
                    why = ("A bare yes is too short to know it's you: say \"yes, go ahead please\", or click Confirm."
                           if owner == "unsure" else
                           "That one needs your voice. Press the talk hotkey and say yes, or click Confirm.")
                if why is None:
                    self.pending = None  # taken: nothing can replace it or run it twice from here on
        if why:
            return why
        self.svc.bus.publish("pending", None)
        if not live:
            nxt = self._next_question()  # never this yes: J hasn't heard it yet
            return "Nothing waiting on a yes." + (f" {nxt}" if self._asked_now(source, nxt) else "")
        self._settled(pending)
        if self._lock.acquire(blocking=False):  # (a spoken or typed yes: this thread is the command holding it)
            try:
                return self._run_confirmed(pending, via, source)
            finally:
                self._lock.release()
        return self._run_confirmed(pending, via, source, beside=True)

    def _run_confirmed(self, pending: dict, via: str, source: str | None, beside: bool = False) -> str:
        own = not beside and not self._ctx  # a yes from outside any command (a button, the voice window, a toast)
        if own:  # who answered, for the action log and the skills watching what ran (a recording started by phone)
            self._ctx = {"source": source or via, "utterance": "yes"}
        try:
            with local_llm.asking((source or via) not in BACKGROUND_SOURCES):  # J said yes: it runs as his request
                replies = [self.run_tool(name, args, confirmed=via, beside=beside)["reply"]
                           for name, args in pending["calls"]]
        finally:
            if own:
                self._ctx = {}
        # let Claude know the parked action happened (after the request in flight, if one is)
        note = [{"role": "user", "content": f"[system note] User confirmed; executed: {pending['text']}. "
                                            f"Result: {' '.join(replies)}"},
                {"role": "assistant", "content": "Done."}]
        if beside:
            self._late_notes = (self._late_notes + note)[-8:]
        elif self.history:
            self.history.extend(note)
        nxt = self._next_question()
        return " ".join(r for r in [*replies, nxt if self._asked_now(source, nxt) else ""] if r) or "Done."

    def _settled(self, pending: dict) -> None:
        """Answered, wherever: no surface's next bare yes is for it any more."""
        self._asked = {k: v for k, v in self._asked.items() if v != pending["id"]}

    def _asked_now(self, source: str | None, nxt: str) -> str:
        """The question that waited its turn is asked in this reply: that surface has been shown it."""
        view = self.pending_view() if nxt else None
        if view and source:
            self._asked[source] = view["id"]
        return nxt

    def decline(self, pending_id: str | None = None, source: str | None = None) -> str:
        """A no: "Cancelled.", and the question waiting its turn is asked. ``pending_id``: the question it answers;
        a no for one that was since replaced cancels nothing."""
        with self._plock:
            pending = self.pending
            if pending and pending_id and pending["expires"] > time.time():
                why = self._changed_since(pending, pending_id, source)
                if why:
                    return why
            self.pending = None
        self.svc.bus.publish("pending", None)
        if pending:
            self._settled(pending)
        nxt = self._next_question()
        return ("Cancelled." if pending else "Okay.") + (f" {self._asked_now(source, nxt)}" if nxt else "")

    def cancel(self, pending_id: str | None = None) -> None:
        """A "No" from the HUD or the toast, or the card running out: the question waiting its turn is asked aloud."""
        if pending_id and (not self.pending or self.pending["id"] != pending_id):
            return  # a stale "No" must not cancel something newer
        self.pending = None
        self.svc.bus.publish("pending", None)
        nxt = self._next_question()
        if nxt:
            self.svc.speak(nxt, expects_reply=True)

    # ---- kill switch -------------------------------------------------------
    def stop_everything(self, source: str = "hotkey") -> None:
        """Hands off: no more actions (reads still work), nothing waiting on a yes, nothing still talking."""
        self.tools.guard.stop(source)
        with self._plock:
            self._queued.clear()
        self.cancel()
        self.convo.end("stopped")
        self.tools.audit.write(tool="kill_switch", tier=0, source=source, outcome="stopped")
        if self.on_kill:
            try:
                self.on_kill()
            except Exception:
                log.exception("kill switch hook failed")
        self.svc.bus.publish("pc_control", self.control_status(), sticky=True)

    def resume_control(self, source: str = "voice") -> None:
        if self.tools.guard.hands_off:
            self.tools.audit.write(tool="resume_control", tier=0, source=source, outcome="resumed")
        self.tools.guard.resume()
        self.svc.bus.publish("pc_control", self.control_status(), sticky=True)

    def control_status(self) -> dict:
        return {**self.tools.guard.status(), "recent": self.tools.audit.tail(40)}

    def reset(self) -> None:
        with self._lock:
            self.history, self.user_turns = [], 0

    def _forget_history(self) -> None:
        """"Forget this conversation": what the models were sent of it goes before their next request (not in the
        middle of one: a tool loop may be what's forgetting it)."""
        self._history_forgotten = True

    def _start_request(self) -> None:
        """Before a request to a model: drop a forgotten conversation; add what a yes beside the last one ran."""
        if self._history_forgotten:
            self._history_forgotten = False
            self.history, self.user_turns, self.local_history, self._late_notes = [], 0, [], []
        notes, self._late_notes = self._late_notes, []
        if notes and self.history:
            self.history.extend(notes)

    # ------------------------------------------------------------------
    off_record_until: float = 0.0

    def off_record(self) -> bool:
        """"Off the record": nothing said is written to the log or learned until it ends (skills/memory.py), and
        nothing from an off-the-record conversation either."""
        return time.time() < self.off_record_until or bool(self.convo.active and self.convo.off_record)

    def _memory_context(self, text: str, include_private: bool = True) -> str:
        """The core profile and the facts relevant to this question (skills/memory.py), or nothing."""
        note = self.convo.context_note()  # a follow-up Vesper just asked (skills/conversation.py)
        mem = self.svc.memory
        if mem is None:
            return note
        try:
            return "\n\n".join(x for x in (mem.context_for(text, include_private=include_private), note) if x)
        except Exception:
            log.exception("memory recall failed")
            return note

    def _context_line(self) -> str:
        now = datetime.now(self.svc.tz)
        win = desktop.active_window()
        where = f"{win.app} — {win.title[:100]}" if win else "unknown"
        return f"[{now:%A %B %d %Y, %I:%M %p} | active window: {where} | profile: {self.active_profile or 'none'}]"

    def _create(self, messages: list, *, tools: list | None = None, effort: str | None = None,
                max_tokens: int = 4096, fmt: dict | None = None, system: str | None = None, on_text=None,
                read_s: float | None = None):
        """One request to Claude. ``read_s``: a longer read timeout than a command's (the briefing, research)."""
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
        client = self._client_for(read_s) if read_s else self.client
        dropped = threading.Event()  # the kill switch let go of this request: nothing more of it is said

        def request():
            if on_text is not None:
                with client.beta.messages.stream(**kwargs) as stream:
                    for delta in stream.text_stream:
                        if self.tools.guard.hands_off or dropped.is_set():
                            raise _Stopped  # leaving the with-block closes the connection
                        on_text(delta)
                    return stream.get_final_message()
            return client.beta.messages.create(**kwargs)
        return self._unless_stopped(request, dropped)

    def _unless_stopped(self, request, dropped: threading.Event):
        """Run a model request so the kill switch never waits on it. A stalled connection sends nothing, so the
        check between streamed words never runs: the request waits on its own thread, and the kill switch lets go of
        it at once (this command ends; the request runs out on its own and is thrown away)."""
        box: dict = {}
        done = threading.Event()

        def run():
            try:
                box["out"] = request()
            except BaseException as exc:  # handed to the command, as if it had run here
                box["exc"] = exc
            finally:
                done.set()
        started, guard = time.time(), self.tools.guard
        threading.Thread(target=run, name="model-request", daemon=True).start()
        while not done.wait(0.1):
            if guard.hands_off and guard.since >= started:
                dropped.set()
                raise _Stopped
        if "exc" in box:
            raise box["exc"]
        return box["out"]

    def _ask_claude(self, text: str, convo: bool = False) -> str:
        if not self.claude_ready:
            return ("I don't know that one offline. Add ANTHROPIC_API_KEY to the .env file and I can handle anything.")
        idle_minutes, max_turns = self._history_limits(convo)
        idle = time.time() - self.last_turn > idle_minutes * 60
        self._start_request()
        if idle or self.user_turns >= max_turns:
            self.reset()  # start fresh rather than editing history, so the conversation stays append-only
        elif convo and self.user_turns >= int(self.convo.setting("summarize_after", 20)):
            # A long talk: the oldest turns become a summary in the system prompt, the last few stay word for word.
            self.history = self._fold(self.history, lambda m: m.get("role") == "user" and isinstance(m.get("content"), str))
            self.user_turns = sum(1 for m in self.history if m.get("role") == "user" and isinstance(m.get("content"), str))
        checkpoint = len(self.history)
        # Private facts never go to the cloud; on stream, nothing remembered at all.
        mem = "" if convo and self.convo.on_stream else self._memory_context(text, include_private=False)
        cut_in = f"[{self.svc.cfg['assistant']['user_name']} talked over your last reply, so they didn't hear all of it]\n" \
            if convo and self.convo.take_interrupted() else ""
        self.history.append({"role": "user", "content": f"{self._context_line()}\n{mem + chr(10) if mem else ''}{cut_in}{text}"})
        self.user_turns += 1
        self.last_turn = time.time()
        definitions = self.tools.definitions()
        if self.macro_definition():
            definitions = definitions + [self.macro_definition()]
        streaming = self._stream_on and hasattr(self.client.beta.messages, "stream")
        sentences = neural.SentenceStream(self._speak_sentence) if streaming else None
        system = f"{self.system_prompt}{self._conversation_prompt()}" if convo else None
        effort = (self.cfg.get("conversation_effort") or self.cfg.get("command_effort", "low")) if convo else \
            self.cfg.get("command_effort", "low")
        try:
            resp = None
            for _ in range(8):
                resp = self._create(self.history, tools=definitions, effort=effort, system=system,
                                    on_text=sentences.feed if sentences else None)
                if resp.stop_reason == "refusal":
                    del self.history[checkpoint:]
                    self.user_turns -= 1
                    self._streamed = False  # say the refusal, not a half-streamed answer
                    return "I can't help with that one."
                self.history.append({"role": "assistant", "content": resp.content})
                if sentences:  # "Checking your calendar." before a tool call is spoken right away
                    sentences.flush(final=resp.stop_reason != "tool_use")
                if resp.stop_reason != "tool_use":
                    break
                self.history.append({"role": "user", "content": self._run_tool_calls(resp.content)})
                guard = self.tools.guard
                if guard.hands_off or guard.exhausted:  # don't ask Claude to carry on
                    why = ("Stopped. PC control is paused." if guard.hands_off else
                           guard.check(1) or "Stopped.")
                    self.history.append({"role": "assistant", "content": why})
                    self._streamed = False
                    return why
            texts = [b.text for b in resp.content if getattr(b, "type", "") == "text"] if resp else []
            reply = " ".join(t.strip() for t in texts if t.strip())
            return reply or "Done."
        except anthropic.AuthenticationError:
            del self.history[checkpoint:]
            self._streamed = False
            return "Claude rejected the API key. Check ANTHROPIC_API_KEY in .env."
        except anthropic.RateLimitError:
            del self.history[checkpoint:]
            self._streamed = False
            return "I'm rate limited by the Claude API. Try again in a minute."
        except anthropic.APIStatusError as exc:
            del self.history[checkpoint:]
            self._streamed = False
            log.error("Claude API error %s: %s", exc.status_code, exc.message)
            return f"Claude returned an error, status {exc.status_code}."
        except anthropic.APIConnectionError:
            del self.history[checkpoint:]
            self._streamed = False
            return "I can't reach Claude right now. Local commands still work."
        except _Stopped:
            del self.history[checkpoint:]
            self._streamed = False
            return "Stopped. PC control is paused."

    # ---- a model on this PC -----------------------------------------------
    def _game_llm(self):
        """The local model for a question mid-game: J's own request, or background work (local_llm.in_game)."""
        return self.local.in_game(asked=True) if local_llm.asked() else self.local.in_game()

    def model_waits(self) -> bool:
        """Would a skill's background question get no model now? Mid-game with the local model not loaded: think()
        answers None rather than load it (and keeps private text off the cloud)."""
        try:
            if not self.gaming() or local_llm.asked():
                return False
            pref = (self.svc.cfg.get("brain") or {}).get("provider", "auto")
            if pref == "claude" and self.claude_ready:
                return False
            return bool(self.local.ready) and self._game_llm() is None
        except Exception:
            return False

    def gpu_keep_alive(self, held, usual):
        """The keep_alive for a model call to Ollama that doesn't go through the brain (memory's embeddings, the
        screen's vision model): ``usual`` at the desk; in a fullscreen game J's own request gets the short
        game_answer_keep_alive, and background work the shorter game_keep_alive, only for a model already loaded
        (``held()``). None: background work in a game with the model not loaded: it waits until the game is over."""
        if not self.gaming():
            return usual
        if local_llm.asked():
            return self.local.game_keep_alive(True)
        return self.local.game_keep_alive(False) if held() else None

    def vram_mb(self) -> float | None:
        """The graphics card's memory (the biggest card, from nvidia-smi), or None when it can't be read."""
        system = getattr(self.svc, "system", None)
        gpus = system.gpus() if system else []
        return max((g.get("mem_total_mb") or 0 for g in gpus), default=0) or None

    def bench_models(self, on_progress=None) -> dict:
        """Load each of your models and keep the strongest that takes the right actions and answers quickly."""
        self.local.refresh(force=True)
        lcfg = self.local.cfg
        result = model_bench.run(self.local.found, self._local_tools(), self.vram_mb(), http=self.local.http,
                                 system=f"{self.system_prompt}{LOCAL_ADDENDUM}", on_progress=on_progress,
                                 num_ctx=int(lcfg.get("context", 8192)))
        if not result.get("error"):
            result = model_bench.settle(result, self.local.bench)  # one bad test doesn't demote the last winner
            model_bench.save(result, self.local.bench_path)
            llm = self.local.refresh(force=True)
            if isinstance(llm, local_llm.OllamaLLM) and not self.local.streaming:
                try:  # the test unloaded it: load the winner now so your next question doesn't wait for it
                    local_llm.preload(llm.http, llm.url, llm.model, keep_alive=llm.keep_alive, num_ctx=llm.num_ctx)
                except httpx.HTTPError:
                    pass
        return result

    def _local_tools(self) -> list[dict]:
        names = ((self.svc.cfg.get("brain") or {}).get("local") or {}).get("tools") or list(self.tools.tools)
        out = []
        for name in names:
            tool = self.tools.tools.get(name)
            if tool:
                d = tool.definition()
                out.append({"type": "function", "function": {"name": d["name"], "description": d["description"],
                                                             "parameters": d["input_schema"]}})
        return out

    def _ask_local(self, text: str, convo: bool = False) -> str:
        if not self.gaming():
            llm = self.local.refresh()
        elif self._ctx.get("source") in BACKGROUND_SOURCES:  # a routine's question mid-game: a loaded model or later
            llm = self.local.in_game()
            if llm is None and self.local.ready:
                return GAME_WAIT
        else:
            llm = self.local.for_game_question()  # J asked: as at the desk, but it leaves the card soon after
        if llm is None:
            return OFFLINE
        lcfg = (self.svc.cfg.get("brain") or {}).get("local") or {}
        idle_minutes, max_turns = self._history_limits(convo)
        idle = time.time() - self.last_turn > idle_minutes * 60
        self._start_request()
        if idle or len(self.local_history) > 4 * max_turns:
            self.local_history = []
        elif convo and sum(1 for m in self.local_history if m.get("role") == "user") >= \
                int(self.convo.setting("summarize_after", 20)):
            self.local_history = self._fold(self.local_history, lambda m: m.get("role") == "user")
        self.last_turn = time.time()
        # The context line goes in the system message, not the user turn: a 3B model put in the user turn
        # repeated it as the first line of its answer (found by the CI run against llama3.2:3b).
        system = f"{self.system_prompt}{LOCAL_ADDENDUM if llm.can_act else NO_ACTIONS}"
        if convo:
            system += self._conversation_prompt()
        system += f"\nRight now: {self._context_line().strip('[]')}"
        mem = "" if convo and self.convo.on_stream else self._memory_context(text)
        if mem:
            system += f"\n\n{mem}"
        if convo and self.convo.take_interrupted():
            system += f"\n({self.svc.cfg['assistant']['user_name']} talked over your last reply, so they didn't hear all of it.)"
        messages = [{"role": "system", "content": system}, *self.local_history, {"role": "user", "content": text}]
        tools = self._local_tools() if llm.can_act else None

        def speak(sentence: str, final: bool) -> None:
            clean = CONTEXT_ECHO.sub("", sentence)
            if clean.strip():
                self._speak_sentence(clean, final)
        sentences = neural.SentenceStream(speak) if self._stream_on else None
        reply = ""
        try:
            for _ in range(6):
                temp = self.convo.setting("temperature", 0.7) if convo else lcfg.get("temperature", 0.3)
                out = llm.chat(messages, tools=tools, on_text=sentences.feed if sentences else None,
                               temperature=float(temp),
                               should_stop=lambda: self.tools.guard.hands_off)
                if self.tools.guard.hands_off:
                    raise _Stopped
                if not out["tool_calls"]:
                    reply = CONTEXT_ECHO.sub("", out["content"])
                    if sentences:
                        sentences.flush(final=True)
                    messages.append({"role": "assistant", "content": reply})
                    break
                if sentences:  # "Checking your calendar." before the call is spoken right away
                    sentences.flush(final=False)
                calls = out["tool_calls"]
                messages.append({"role": "assistant", "content": out["content"] or "", "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls]})
                blocks = [SimpleNamespace(type="tool_use", id=c["id"], name=c["name"],
                                          input=c["arguments"] if isinstance(c["arguments"], dict) else {})
                          for c in calls]
                for r in self._run_tool_calls(blocks):
                    messages.append({"role": "tool", "tool_call_id": r["tool_use_id"], "content": r["content"]})
                guard = self.tools.guard
                if guard.hands_off or guard.exhausted:
                    reply = "Stopped. PC control is paused." if guard.hands_off else (guard.check(1) or "Stopped.")
                    self._streamed = False
                    break
            else:
                reply = reply or "I went round in circles on that one. Try asking it another way."
        except _Stopped:
            self._streamed = False
            return "Stopped. PC control is paused."
        except httpx.ConnectError:
            self.local.lost()
            self._streamed = False
            return f"I can't reach {llm.label} anymore. Is it still running?"
        except httpx.TimeoutException:
            self._streamed = False
            return f"{llm.label} took too long to answer."
        except httpx.HTTPStatusError as exc:
            self._streamed = False
            log.error("local model error %s: %s", exc.response.status_code, exc.response.text[:300])
            try:
                detail = str(exc.response.json().get("error") or "")[:120]
            except ValueError:
                detail = ""
            return (f"{llm.label} returned an error: {detail}" if detail
                    else f"{llm.label} returned an error, status {exc.response.status_code}.")
        except local_llm.LocalModelError as exc:
            self._streamed = False
            log.error("local model error: %s", exc)
            return f"{llm.label} hit an error: {str(exc)[:120]}"
        self.local_history = [m for m in messages[1:]]  # the whole turn, tool calls and results included
        return reply.strip() or "Done."

    def _local_briefing(self, prompt: str) -> dict | None:
        # mid-game, as for think(): J's own "good morning" gets the loaded model or the light one; the pre-built one
        # only a model already loaded (None: the built-in summary)
        llm = self._game_llm() if self.gaming() else self.local.refresh()
        if llm is None:
            return None
        try:
            out = llm.chat([{"role": "user", "content": prompt}], json_schema=briefing.BRIEFING_SCHEMA,
                           temperature=0.2, max_tokens=1500)
            text = out["content"].strip().removeprefix("```json").removesuffix("```").strip()
            result = json.loads(text)
            if not isinstance(result, dict) or not result.get("spoken"):
                return None
            result["generated_by"] = "local"
            result["model"] = llm.model
            result["spoken"] = _clip_spoken(result["spoken"])  # a 3B model wrote ~230 words: 90 seconds of talking
            return result
        except (httpx.HTTPError, ValueError, KeyError):
            log.exception("local briefing failed; using the built-in summary")
            return None

    def _run_tool_calls(self, content) -> list[dict]:
        results = []
        pending_calls, pending_text = [], []
        for block in content:
            if getattr(block, "type", "") != "tool_use":
                continue
            args = block.input if isinstance(block.input, dict) else {}
            if block.name == "run_macro":
                name = str(args.get("name", ""))
                risks, pins = self._macro_question(name)
                if risks or self._tainted:
                    pending_calls.append(("run_macro", {"name": name, "_pins": pins}))  # never pins the model wrote
                    pending_text.append(f"run {name}")
                    payload = {"status": "awaiting_confirmation", "ask_user": f"run {name}"}
                else:
                    out = self.run_macro(name)
                    payload = {"ok": out["kind"] != "error", "reply": out["reply"], "steps": out.get("data")}
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": compact(payload),
                                **({"is_error": True} if payload.get("ok") is False else {})})
                continue
            tool = self.tools.tools.get(block.name)
            # after other people's text: any action, and any link that isn't from J or a search, waits for a yes
            after_chat = bool(tool and self._tainted and (tool.tier_for(args) >= 1
                                                          or (tool.network and urls(args) - self._known_urls)))
            if tool and (tool.needs_confirmation(args) or after_chat):
                what = tool.describe(args) + (f" (asked after reading {self._tainted_by})" if after_chat else "")
                pending_calls.append((block.name, args))
                pending_text.append(what)
                payload: dict = {"status": "awaiting_confirmation", "ask_user": what}
            else:
                payload = self.tools.run(block.name, args)
                self.svc.bus.publish("tool", {"name": block.name, "args": {} if block.name in PRIVATE_TOOLS else args,
                                              "result": payload})
                if payload.get("blocked") and self.tools.guard.hands_off:
                    payload = {**payload, "note": "The user stopped all actions. Don't retry; say you've stopped."}
                if tool and tool.network:  # a search's results: links the model may follow without a yes
                    self._known_urls |= urls(payload)
                if tool and tool.untrusted:
                    self._tainted = True
                    self._tainted_by = tool.untrusted_label
                    self._untrusted_ids[block.id] = tool.untrusted_label
                    payload = {**payload, "note": tool.untrusted_note}
            results.append({
                "type": "tool_result", "tool_use_id": block.id, "content": compact(payload),
                **({"is_error": True} if payload.get("ok") is False else {}),
            })
        if pending_calls:
            asked = self._set_pending(pending_calls, "; ".join(pending_text))
            for r in results:
                if '"awaiting_confirmation"' not in r["content"]:
                    continue
                if not asked:  # J is answering another question: the model mustn't ask this one now
                    r["content"] = compact({"status": "queued", "note": "The user is answering another yes/no question. "
                                            "Don't ask this one now: say you'll ask about it once they've answered."})
                elif self.replaced:
                    r["content"] = compact({**json.loads(r["content"]), "replaces": self.replaced,
                                            "note": "This replaces an earlier question; tell the user it was dropped."})
        return results

    def _scrub_untrusted(self) -> None:
        """Viewer messages and document text stay in the model's context for the request that read them, not for
        later ones."""
        if not self._untrusted_ids:
            return

        def gone(call_id: str) -> str:
            return "[viewer messages removed after use]" if self._untrusted_ids[call_id] == "chat" else \
                "[document text removed after use: search again to quote it]"
        for msg in self.local_history:
            if msg.get("role") == "tool" and msg.get("tool_call_id") in self._untrusted_ids:
                msg["content"] = gone(msg["tool_call_id"])
        for msg in self.history:
            if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                for item in msg["content"]:
                    if isinstance(item, dict) and item.get("tool_use_id") in self._untrusted_ids:
                        item["content"] = gone(item["tool_use_id"])
        self._untrusted_ids.clear()

    # ------------------------------------------------------------------
    def _fresh_morning_briefing(self, max_age_hours: float = 3) -> dict | None:
        """Reuse the briefing pre-built at first activity instead of paying for a second one."""
        b = self.last_briefing
        if not b or b.get("kind") != "morning" or time.time() - b["created"] > max_age_hours * 3600:
            return None
        built = datetime.fromtimestamp(b["created"], self.svc.tz).date()
        return b if built == datetime.now(self.svc.tz).date() else None

    def _spoken_briefing(self, b: dict, kind: str) -> str:
        """Live on stream, viewers hear it: times and counts only (event titles, tasks and senders are on the HUD)."""
        try:
            live = bool(self.svc.live and self.svc.live())
        except Exception:
            live = False
        return briefing.on_air(self.svc, kind) if live else b["spoken"]

    def briefing(self, kind: str = "morning") -> dict:
        data = briefing.gather_morning(self.svc) if kind == "morning" else briefing.gather_recap(self.svc)
        user = self.svc.cfg["assistant"]["user_name"]
        result = None
        if self.brain() == "claude":
            template = briefing.MORNING_INSTRUCTIONS if kind == "morning" else briefing.RECAP_INSTRUCTIONS
            prompt = template.format(user=user, data=json.dumps(data, default=str))
            try:
                resp = self._create([{"role": "user", "content": prompt}], effort=self.cfg.get("briefing_effort", "high"),
                                    max_tokens=16000, fmt={"type": "json_schema", "schema": briefing.BRIEFING_SCHEMA},
                                    read_s=LONG_READ_S)
                if resp.stop_reason not in {"refusal", "max_tokens"}:
                    text = next(b.text for b in resp.content if getattr(b, "type", "") == "text")
                    result = json.loads(text)
                    result["generated_by"] = "claude"
            except (anthropic.APIError, StopIteration, json.JSONDecodeError, _Stopped):
                log.exception("Claude briefing failed; using local version")
        elif self.brain() == "local":
            template = briefing.MORNING_INSTRUCTIONS if kind == "morning" else briefing.RECAP_INSTRUCTIONS
            result = self._local_briefing(template.format(user=user, data=json.dumps(data, default=str)))
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
                self.history.append({"role": "user", "content": f"[{kind} briefing requested]"})  # see BRIEFING_MARKS
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
                                effort="high", max_tokens=16000, system="You are a precise research analyst.",
                                read_s=LONG_READ_S)
            if resp.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": resp.content})
        if resp is None or resp.stop_reason == "refusal":
            raise RuntimeError("research request was declined")
        return "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


# What's said for these, and the reply, is kept only where the tool keeps it; the HUD shows the label ("": the words).
PRIVATE_TOOLS = {"journal_add": "(journal entry)", "recording_mark": "(recording note)", "recordings_search": ""}
# A briefing in Claude's history was written from other people's text: later commands start as if they'd read it.
BRIEFING_MARKS = {"[morning briefing requested]", "[recap briefing requested]"}
# In a conversation "yeah" / "no" answer a question; these still cancel.
HARD_CANCEL = {"stop", "cancel", "abort", "never mind", "nevermind", "forget it", "cancel that", "cancel it"}


def _failed(result) -> bool:
    return isinstance(result, dict) and result.get("ok") is False and bool(result.get("error"))
