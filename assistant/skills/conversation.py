"""Conversation: "Vesper, let's talk" and it keeps listening, no wake word between turns, until "that's all".

The state and the brain side live in brain/conversation.py and brain/assistant.py; the turn-taking (no wake word,
talking over a reply) in voice/listener.py. This skill is the glue: the phrases that start and end it, the HUD panel
(bubbles, a box to type into, Talk / End buttons), "how was your day", and the clock that ends it after a quiet
minute or when the stream goes live.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from ..brain import conversation as convo_mod
from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill

log = logging.getLogger(__name__)

MY_DAY = re.compile(r"(?:so )?how(?:'s| was| is| has| did)? your day(?: been| going| go)?(?: so far)?"
                    r"|tell me about your day|what (?:have you|did you|you) (?:been up to|do|get up to)(?: today)?"
                    r"|what'?s new with you|what'?ve you been up to(?: today)?")


class ConversationSkill(Skill):
    name, title = "conversation", "Conversation"
    poll_s = 5  # the quiet-minute and going-live checks (the mic loop checks too, when voice is on)
    game_essential = True  # a conversation ends the moment the stream goes live
    delay_s = 5

    def __init__(self, svc):
        super().__init__(svc)
        self.runtime = None

    @property
    def convo(self):
        return getattr(self.svc, "conversation", None)

    # ---- wiring --------------------------------------------------------------------------------------
    def start(self, runtime) -> None:
        self.runtime = runtime
        convo = self.convo
        if convo is None:
            return
        convo.speaker = getattr(runtime, "speaker", None)
        listener = getattr(runtime, "listener", None)
        if listener is not None:
            listener.in_conversation = convo.is_active
            listener.is_live = lambda: bool(self.svc.live and self.svc.live())
            convo.on_change.append(lambda _data: listener.refresh_state())

    def tick(self) -> None:
        convo = self.convo
        if convo is not None and convo.active:
            why = convo.check()
            if why:
                log.info("conversation ended: %s", why)
        self.publish()

    # ---- phrases -------------------------------------------------------------------------------------
    def route(self, t: str):
        convo = self.convo
        if convo is None:
            return None
        start = convo_mod.match_start(t, self.svc.cfg)
        if start is not None:
            return Intent("conversation", args=start)
        if convo.active and convo_mod.match_end(t, self.svc.cfg):
            return Intent("conversation_end")
        if MY_DAY.fullmatch(t):
            return Intent("tool", "conversation_my_day", {})
        return None

    # ---- tools ---------------------------------------------------------------------------------------
    def tools(self):
        return [
            Tool("conversation_my_day", "What Vesper noticed and did today (updates it brought up, background jobs, "
                                        "requests handled, things it learned). Use for 'how was your day', 'what have "
                                        "you been up to'.", obj({}), self.my_day),
            Tool("end_conversation", "End conversation mode (back to the wake word) when the user signals they're "
                                     "done talking: 'gotta go', 'talk later', 'that's enough for now'.",
                 obj({}), self._end_tool),
        ]

    def _end_tool(self) -> dict:
        convo = self.convo
        return {"ok": True, "ended": bool(convo and convo.end("said"))}

    def my_day(self) -> dict:
        """Today, from Vesper's side: what it brought up, what ran in the background, what it learned."""
        tz = self.svc.tz
        start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        out: dict = {"ok": True}
        try:
            rows = self.svc.storage.query("SELECT COUNT(*) AS n FROM log WHERE role = 'user' AND ts >= ?", (start,))
            out["requests"] = int(rows[0]["n"]) if rows else 0
        except Exception:
            out["requests"] = 0
        nudges = self.svc.skills.get("nudges") if self.svc.skills is not None else None
        feed = [n for n in (getattr(nudges, "feed", None) or []) if (n.delivered or n.created) >= start]
        out["brought_up"] = [{"text": n.text, "from": n.source, "said": n.spoken} for n in feed[:8]]
        try:
            jobs = [j for j in self.svc.storage.list_jobs(30) if (j.get("created") or 0) >= start]
        except Exception:
            jobs = []
        out["jobs"] = [{"title": j.get("title"), "status": j.get("status")} for j in jobs[:6]]
        mem = self.svc.memory
        learned = [f for f in (getattr(mem, "learned", None) or []) if (f.get("learned_at") or 0) >= start]
        out["learned"] = [f["statement"] for f in learned[:6]]
        if self.svc.live and self.svc.live():
            # On air: how much, not what (updates and memories are J's business).
            out = {"ok": True, "requests": out["requests"], "brought_up": len(out["brought_up"]),
                   "jobs": len(out["jobs"]), "learned": len(out["learned"]),
                   "live_note": "Live on stream: say how much, never what it was about."}
        return out

    def say(self, tool, args, result):
        if tool == "end_conversation":
            return convo_mod.GOODBYE if result.get("ended") else "We weren't in a conversation."
        if tool != "conversation_my_day":
            return None
        n = int(result.get("requests") or 0)
        if result.get("live_note"):
            return (f"Busy day behind the scenes: {n} requests, {result['brought_up']} updates and "
                    f"{result['jobs']} background jobs. The details stay off air.")
        parts = []
        if n:
            parts.append(f"you've asked me for {n} thing{'s' if n != 1 else ''}")
        jobs = result.get("jobs") or []
        if jobs:
            done = [j for j in jobs if j.get("status") == "done"]
            parts.append(f"I ran {len(jobs)} background job{'s' if len(jobs) != 1 else ''}"
                         + (f", {len(done)} finished" if done and len(done) != len(jobs) else ""))
        brought = result.get("brought_up") or []
        if brought:
            parts.append(f"I brought up {len(brought)} thing{'s' if len(brought) != 1 else ''}, "
                         f"the latest: {brought[0]['text'].rstrip('.')}")
        learned = result.get("learned") or []
        if not parts and not learned:
            return "Quiet so far. Nothing's needed me yet, which I'm choosing to take as a compliment."
        line = ("Pretty full. " if n >= 15 else "Not bad. ") + (_join(parts) + "." if parts else "")
        if learned:
            line += f" And I learned something: {learned[0].rstrip('.')}."
        return line.strip()

    # ---- the HUD -------------------------------------------------------------------------------------
    def status(self) -> dict:
        convo = self.convo
        if convo is None:
            return {"active": False, "available": False, "transcript": []}
        return {**convo.status(), "available": True,
                "voice": bool(self.runtime is not None and getattr(self.runtime, "listener", None) is not None)}

    def actions(self):
        return {"start": self._start, "end": self._end}

    def _start(self, body: dict) -> dict:
        on_stream = bool(body.get("on_stream"))
        assistant = getattr(self.runtime, "assistant", None)
        if assistant is not None:
            out = assistant.start_conversation(on_stream=on_stream, source="hud")
        elif self.convo is not None:
            ok, why = self.convo.begin(on_stream=on_stream, source="hud")
            out = {"reply": convo_mod.GREETINGS[0] if ok else why, "kind": "conversation" if ok else "conversation_off"}
        else:
            return {"ok": False, "error": "Conversation mode isn't available."}
        ok = out["kind"] != "conversation_off"
        if ok and self.convo is not None:
            self.convo.turn_done(out["reply"], "hud", started=False)  # the greeting opens the transcript
        self.svc.bus.publish("assistant_said", {"text": out["reply"], "kind": out["kind"], "source": "hud", "ms": 0})
        if self.status().get("voice"):  # the mic is open: say hello (or why not) out loud
            self.svc.speak(out["reply"], expects_reply=False)
        return {"ok": ok, "reply": out["reply"], **({} if ok else {"error": out["reply"]})}

    def _end(self, _body: dict) -> dict:
        convo = self.convo
        if convo is None or not convo.active:
            return {"ok": True, "ended": False}
        convo.end("hud")
        if self.runtime is not None and getattr(self.runtime, "speaker", None) is not None:
            self.runtime.speaker.interrupt()  # "End" means stop talking now too
        return {"ok": True, "ended": True}


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


__all__ = ["ConversationSkill"]
