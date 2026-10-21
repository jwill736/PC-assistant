"""Conversation mode: talking with Vesper, not just giving it commands.

"Vesper, let's talk" (or the HUD's Talk button) starts it. Until it ends, every sentence in J's voice is a turn: no
wake word, the mic stays open between turns, and talking over a reply stops it and becomes the next turn
(voice/listener.py). Commands still run instantly on the fast path; everything else goes to the model with a
conversational persona and a longer memory of what was said (a rolling summary keeps long talks small).

It ends on "that's all" / "thanks Vesper, that's it" / "stop talking", after a minute of silence, from the HUD, or when
the stream goes live (unless J said "let's talk on stream", which also keeps private things out of it).

This object holds the state; the brain (assistant.py) and the skill (skills/conversation.py) both use it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

PERSONA = """
Right now you're in a conversation with {user}, not just taking commands. Be good company:
- Warm, direct and a bit witty. Never sycophantic: no "great question", no flattery, no gushing. Disagree when you
  disagree, and say why. Have opinions when asked for them.
- Speak like a person across a desk: 1-3 short sentences unless {user} asks for more. No lists, no markdown.
- Ask {user} a question back when it moves things along (not every turn). Remember what was said earlier in this
  conversation and build on it.
- You know {user}: use what you know naturally, never recite it.
- Brainstorming: offer concrete ideas and build on {user}'s. "Help me think through X": one sharp question or
  observation at a time. "Quiz me": one question at a time, wait for the answer, keep score.
- "How was your day" or "what have you been up to": call conversation_my_day and tell it like a person would.
- You can still use tools mid-conversation when something needs doing. If {user} signals they're done talking
  ("gotta go", "talk later"), call end_conversation and say a short goodbye."""

ON_STREAM = """
You're live on stream: viewers hear everything you say. Never mention anything private: nothing you remember about
{user}, no email, calendar details, documents, money, health or other people's details. Keep it fun and friendly."""

SUMMARY_PROMPT = """Update the running summary of a spoken conversation between {user} and {assistant}.
Keep what matters for carrying on: topics, what {user} said about themselves and their plans, opinions given,
questions still open, any quiz score. At most 120 words, plain sentences, no lists.

Summary so far: {previous}

Newer turns:
{turns}

The updated summary:"""

NO_MODEL = ("Conversation mode needs a model to talk with, and none is connected. Set up the local model in Setup, "
            "then Brain, and we can talk. Until then I'm on commands.")
LOST_MODEL = "I've lost my model, so I'm back to commands. You can reconnect it in Setup, then Brain."
LIVE_REFUSE = ("You're live, so I'll stick to commands. Say let's talk on stream if you want to chat on air, "
               "and I'll keep anything private out of it.")
GREETINGS = ("I'm listening. What's on your mind?", "Sure. What are we talking about?",
             "Go for it. I'm all ears.", "Happy to. What's up?")
ON_STREAM_GREETING = "We're on air, so I'll keep it public. What's up?"
GOODBYE = "Okay. I'm here when you need me."
ASIDE = """
Background jobs that just finished (mention one in a short line at a natural pause, not mid-thought, and only once):
{items}"""

START_RE = (r"(?:let'?s|can we|could we|i (?:want|wanna|would like|'d like) to|i'?d like to|wanna) "
            r"(?:have a |do some |have a little )?(?:talk|chat|conversation|catch up)"
            r"|talk (?:to|with) me|chat with me|keep me company|let'?s have a (?:chat|talk|conversation)"
            r"|(?:start|begin|enter|turn on|switch to) (?:a )?(?:conversation|chat)(?: mode)?"
            r"|conversation mode(?: on)?")
# (the router's clean() already dropped a trailing "now" / "thanks": "that's all for now" arrives as "that's all for")
END_RE = (r"that'?s (?:all|it|everything|enough)(?: for(?: now| today| tonight)?)?|that'?ll be all|that will be all"
          r"|stop talking|end (?:the |this |our )?(?:conversation|chat)|(?:exit|leave|stop|end|turn off) (?:the )?"
          r"conversation mode|conversation mode off|(?:we'?re|i'?m) done(?: talking)?(?: for(?: now| today)?)?"
          r"|(?:good ?)?bye(?: for now)?|talk (?:to you )?later|catch you later|over and out|nothing else")
LEAD = r"(?:(?:thanks|thank you|cheers|ok|okay|alright|all right|right|cool|great|perfect|anyway)\s+)*"


def _names(cfg) -> str:
    """The assistant's name and wake words, as a regex alternation."""
    a = cfg["assistant"]
    words = {str(a.get("name") or "").lower(), *[str(w).lower() for w in a.get("wake_words") or []]}
    return "|".join(sorted((re.escape(w) for w in words if w), key=len, reverse=True)) or "vesper"


def match_start(t: str, cfg) -> dict | None:
    """'let's talk', 'Vesper let's chat on stream', "let's talk about my week" -> {on_stream, topic}."""
    name = rf"(?:(?:{_names(cfg)})\s+)?"  # typed lines keep the name
    m = re.fullmatch(rf"{LEAD}{name}(?:{START_RE})(?P<stream> (?:on|with) (?:the )?(?:stream|air|chat))?"
                     rf"(?: for a (?:bit|while|minute|second|few minutes))?(?: about (?P<topic>.+))?", t)
    if not m:
        return None
    topic = (m.group("topic") or "").strip()
    return {"on_stream": bool(m.group("stream")), **({"topic": topic} if topic else {})}


def match_end(t: str, cfg) -> bool:
    """'that's all', 'thanks Vesper that's it', 'stop talking', 'bye Vesper' (only checked while talking)."""
    names = _names(cfg)
    return bool(re.fullmatch(rf"{LEAD}(?:(?:{names})\s+)?{LEAD}(?:{END_RE})(?:\s+(?:{names}))?", t))


class Conversation:
    """Is J talking with Vesper right now, and what's been said."""

    def __init__(self, svc: Any, clock: Callable[[], float] = time.time):
        self.svc, self.clock = svc, clock
        self.active = False
        self.on_stream = False
        self.started = 0.0
        self.last_activity = 0.0
        self.last_source = ""
        self.busy = 0  # turns being answered right now (never time out mid-reply)
        self.turns = 0
        self.summary = ""
        self.summarised_until = 0.0  # transcript entries up to this time are folded into the summary
        self.transcript: list[dict] = []  # {"role": "user"|"assistant", "text", "ts", "source"}
        self.interrupted = False  # J cut the last reply short: the model is told on the next turn
        self.ended: dict | None = None  # {"reason", "ts"} of the last one
        self.asides_seen = 0.0  # background jobs (skills/tasks_agent.py) finished before this were already mentioned
        self.speaker = None  # the runtime's Speaker: talking now counts as activity
        self.brain: Callable[[], str | None] = lambda: None  # set by the assistant: which model answers, if any
        self.on_change: list[Callable[[dict], None]] = []
        self._lock = threading.RLock()
        self._fold: threading.Thread | None = None
        bus = getattr(svc, "bus", None)
        if bus is not None and hasattr(bus, "on"):
            bus.on(self._on_event)

    # ---- settings -------------------------------------------------------------------------------------
    def setting(self, key: str, default: Any) -> Any:
        return (self.svc.cfg.get("conversation") or {}).get(key, default)

    @property
    def timeout_s(self) -> float:
        return float(self.setting("timeout_seconds", 60))

    # ---- life cycle -----------------------------------------------------------------------------------
    def begin(self, *, on_stream: bool = False, source: str = "voice") -> tuple[bool, str]:
        """Start (or keep going). (False, why) when there's no model, or the stream is live and J didn't say
        "on stream"."""
        if self.brain() is None:
            return False, NO_MODEL
        if self._live() and not on_stream:
            return False, LIVE_REFUSE
        with self._lock:
            already = self.active
            now = self.clock()
            if not already:
                self.started, self.turns, self.interrupted = now, 0, False
                self.asides_seen = now
                if self.ended is None or now - self.ended["ts"] > float(self.setting("summary_keep_minutes", 30)) * 60:
                    self.summary, self.summarised_until = "", 0.0
                    self.transcript = []
            self.active, self.on_stream = True, bool(on_stream or (already and self.on_stream))
            self.last_activity, self.last_source = now, source
            self.ended = None
        self._changed()
        return True, "already" if already else "started"

    def end(self, reason: str = "said") -> bool:
        with self._lock:
            if not self.active:
                return False
            self.active, self.on_stream = False, False
            self.ended = {"reason": reason, "ts": self.clock()}
        log.info("conversation ended (%s)", reason)
        self._changed()
        return True

    def is_active(self) -> bool:
        self.check()
        return self.active

    def check(self, now: float | None = None) -> str | None:
        """End it if the stream went live (and it wasn't started on stream) or J went quiet. Returns why it ended."""
        if not self.active:
            return None
        if not self.on_stream and self._live():
            self.end("live")
            return "live"
        if self.idle_for(now) > self.timeout_s:
            self.end("timeout")
            return "timeout"
        return None

    def idle_for(self, now: float | None = None) -> float:
        now = self.clock() if now is None else now
        sp = self.speaker
        if self.busy or (sp is not None and sp.speaking.is_set()):
            return 0.0
        last = max(self.last_activity, getattr(sp, "last_end", 0.0) if sp is not None else 0.0)
        return max(0.0, now - last)

    def touch(self) -> None:
        self.last_activity = self.clock()

    # ---- turns ----------------------------------------------------------------------------------------
    def turn_started(self, said: str, source: str) -> None:
        """J said something in the conversation: on the HUD at once, with Vesper "thinking" until the reply."""
        with self._lock:
            self.busy += 1
            if source == "voice":  # J is talking out loud: typed lines in between get spoken replies too
                self.last_source = source
            self._add("user", said, source)
            self.touch()
        self._changed()

    def turn_done(self, reply: str, source: str, said: str | None = None, started: bool = True) -> None:
        """The reply (``said``: J's words, when they weren't recorded at the start: the "let's talk" turn)."""
        with self._lock:
            if started:
                self.busy = max(0, self.busy - 1)
            if said:
                self._add("user", said, source)
            if reply:
                self._add("assistant", reply, source)
            self.touch()
        self._changed()

    def _add(self, role: str, text: str, source: str) -> None:
        if not text:
            return
        now = max(self.clock(), (self.transcript[-1]["ts"] + 0.001) if self.transcript else 0.0)
        self.transcript.append({"role": role, "text": text[:1200 if role == "assistant" else 600], "ts": now,
                                "source": source})
        self.transcript = self.transcript[-200:]
        if role == "user":
            self.turns += 1

    def asides(self) -> str:
        """Jobs that finished since the last turn (svc.tasks.done_since), for the model to mention at a pause. Each is
        told once, and its nudge (held until the conversation ends) is dropped. Never on stream: results are private."""
        tasks = getattr(self.svc, "tasks", None)
        if tasks is None or self.on_stream:
            return ""
        try:
            done = tasks.done_since(self.asides_seen, mark=True)
        except Exception:
            log.exception("job asides failed")
            return ""
        self.asides_seen = self.clock()
        user = self.svc.cfg["assistant"].get("user_name") or "J"
        lines = [f"- {d['title']}: " + (f"didn't work ({d['line']})" if d["state"] == "failed" else
                                        f"waiting on {user}'s OK to {d['waiting']}" if d.get("waiting") else
                                        d["line"] or "done") for d in done[-3:]]
        return ASIDE.format(items="\n".join(lines)) if lines else ""

    def take_interrupted(self) -> bool:
        was, self.interrupted = self.interrupted, False
        return was

    def _on_event(self, event: dict) -> None:
        if event.get("type") == "interrupted" and self.active:
            self.interrupted = True

    # ---- the rolling summary --------------------------------------------------------------------------
    def fold(self, think: Callable[..., Any], keep_turns: int, *, wait: bool = False) -> None:
        """Fold what's older than the last ``keep_turns`` exchanges into the summary (in the background: the
        next reply shouldn't wait for it). Without a model the old turns just drop out."""
        with self._lock:
            fresh = [e for e in self.transcript if e["ts"] > self.summarised_until]
            old = fresh[:max(0, len(fresh) - 2 * keep_turns)]
            if not old:
                return
            self.summarised_until = old[-1]["ts"]
        a = self.svc.cfg["assistant"]
        prompt = SUMMARY_PROMPT.format(
            user=a.get("user_name") or "J", assistant=a.get("name") or "Vesper", previous=self.summary or "(nothing yet)",
            turns="\n".join(f"{a.get('user_name') or 'J' if e['role'] == 'user' else a.get('name') or 'Vesper'}: {e['text']}"
                            for e in old)[-6000:])

        def run() -> None:
            try:
                text = think(prompt, max_tokens=400)
            except Exception:
                log.exception("conversation summary failed")
                text = None
            if isinstance(text, str) and text.strip():
                self.summary = _clip(text.strip(), 1200)
        if wait:
            run()
            return
        self._fold = threading.Thread(target=run, name="conversation-summary", daemon=True)
        self._fold.start()

    def wait_summary(self, timeout: float = 10) -> None:
        if self._fold is not None:
            self._fold.join(timeout)

    def reset(self) -> None:
        self.summary, self.summarised_until = "", self.clock()

    # ---- the HUD --------------------------------------------------------------------------------------
    def status(self) -> dict:
        now = self.clock()
        return {"active": self.active, "on_stream": self.on_stream, "started": self.started or None,
                "turns": self.turns, "thinking": bool(self.busy), "idle_s": round(self.idle_for(now), 1) if self.active else None,
                "timeout_s": self.timeout_s, "summarised": bool(self.summary), "ended": self.ended,
                "model": self.brain() is not None, "live": self._live(),
                "transcript": [dict(e) for e in self.transcript[-40:]]}

    def _changed(self) -> None:
        data = self.status()
        bus = getattr(self.svc, "bus", None)
        if bus is not None:
            bus.publish("conversation", data, sticky=True)
        for fn in list(self.on_change):
            try:
                fn(data)
            except Exception:
                log.exception("conversation listener failed")

    def _live(self) -> bool:
        try:
            return bool(self.svc.live and self.svc.live())
        except Exception:
            return False


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"
