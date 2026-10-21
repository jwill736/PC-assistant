"""Conversation: "Vesper, let's talk" and it keeps listening, no wake word between turns, until "that's all".

The state and the brain side live in brain/conversation.py and brain/assistant.py; the turn-taking (no wake word,
talking over a reply) in voice/listener.py. This skill is the glue: the phrases that start and end it, the HUD panel
(bubbles, a box to type into, Talk / End buttons), "how was your day", and the clock that ends it after a quiet
minute or when the stream goes live.

Vesper 2.2, conversations that evolve (brain/threads.py):
- threads: every talk is kept with a title, summary, J's views, what's open; "let's continue our talk about the move",
  "what were we saying about the new PC", "pick up where we left off"; "forget this conversation" takes it all back;
- modes: "let's brainstorm", "play devil's advocate", "coach me", "interview me", "teach me chess", "rubber duck this",
  "plan the move with me" (tasks proposed on a card, added only on a yes), "tell me a story", "switch to coach mode";
- style: "too long", "more detail", "be funnier", "stop asking me questions"… a small profile on the HUD;
- turn-taking: "hmm, let me think" holds the quiet-minute clock ("go on" / "wait, back up" are told to the model);
- follow-ups: an open decision comes back once, days later, at a good moment ("On Monday you were deciding between two
  GPUs: which did you pick?"); at most one a day, never live or mid-conversation, and off for good per thread;
- "look into that while we talk": a background job (the tasks engine when there is one, else a research job).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime

from ..brain import conversation as convo_mod
from ..brain.router import Intent
from ..brain.threads import STYLE_LABELS, first_sentence, style_words, when_words
from ..brain.tools import Tool, obj
from . import Skill

log = logging.getLogger(__name__)

MY_DAY = re.compile(r"(?:so )?how(?:'s| was| is| has| did)? your day(?: been| going| go)?(?: so far)?"
                    r"|tell me about your day|what (?:have you|did you|you) (?:been up to|do|get up to)(?: today)?"
                    r"|what'?s new with you|what'?ve you been up to(?: today)?")
THREADS_RE = re.compile(r"what (?:have|did) we (?:been )?talk(?:ed|ing) about(?: lately| recently| this week)?"
                        r"|(?:list|show)(?: me)? (?:our|my) (?:conversations|threads|talks)|what threads do (?:we|i) have")
STYLE_LINES = {
    ("length", -1): "Got it, shorter.", ("length", 1): "Sure, more detail from now on.",
    ("humour", 1): "Noted. I'll lighten up.", ("humour", -1): "Okay, fewer jokes.",
    ("questions", -1): "Fewer questions, got it.", ("questions", 1): "I'll ask more.",
    ("questions", "=0"): "Okay. No more questions from me unless I really need one.",
    ("formality", 1): "Certainly. A more formal register.", ("formality", -1): "Cool, I'll keep it casual.",
}
AT_LIMIT = {("length", -1): "That's already as short as I go.", ("length", 1): "That's already as much detail as I give.",
            ("humour", 1): "I'm already at full comedy.", ("humour", -1): "I'm already dead serious.",
            ("questions", -1): "I've already stopped asking.", ("questions", 1): "I'm already as curious as it gets.",
            ("formality", 1): "I'm already at my most formal.", ("formality", -1): "I'm already as casual as I get."}


class ConversationSkill(Skill):
    name, title = "conversation", "Conversation"
    poll_s = 5  # the quiet-minute and going-live checks (the mic loop checks too, when voice is on)
    game_essential = True  # a conversation ends the moment the stream goes live
    delay_s = 5

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.runtime = None
        self.clock = clock
        self._next_followup = 0.0
        self._awaiting: dict | None = None  # a follow-up that was just asked: {"thread_id", "text", "title", "at"}
        self._answer: str | None = None
        self._lock = threading.RLock()
        bus = getattr(svc, "bus", None)
        if bus is not None and hasattr(bus, "on"):
            bus.on(self._on_event)

    @property
    def convo(self):
        return getattr(self.svc, "conversation", None)

    @property
    def keeper(self):
        convo = self.convo
        return getattr(convo, "threads", None) if convo is not None else None

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
            listener.conversation_noise = convo.touch  # "hmm" mid-conversation: J's thinking, keep it open
            convo.on_change.append(lambda _data: listener.refresh_state())

    def tick(self) -> None:
        convo = self.convo
        if convo is not None and convo.active:
            why = convo.check()
            if why:
                log.info("conversation ended: %s", why)
        now = self.clock()
        if now >= self._next_followup:
            self._next_followup = now + float(self.setting("followup_check_s", 300))
            try:
                self.follow_up(now)
            except Exception:
                log.exception("conversation follow-up check failed")
        self.publish()

    # ---- follow-ups ----------------------------------------------------------------------------------
    def follow_up(self, now: float | None = None) -> dict | None:
        """One gentle question about an open decision from an earlier talk, at most once a day, never live or while
        talking, never for a thread J switched follow-ups off for. The nudge waits for a good moment."""
        now = self.clock() if now is None else now
        convo, keeper = self.convo, self.keeper
        if keeper is None or not self.setting("followups", True):
            return None
        if (self.svc.live and self.svc.live()) or (convo is not None and convo.active):
            return None
        t = keeper.followup_candidate(now)
        if t is None:
            return None
        text = keeper.followup_text(t, now)
        why = {"rule": "Conversation follow-up (an open decision from an earlier talk)",
               "evidence": [f"Thread: {t['title']}, last talked {when_words(t['updated'], now, self.svc.tz)}"]
               + [f"Still open: {q}" for q in (t.get("open") or [])[:2]],
               "note": "At most one a day. Say “don't follow up on that” to stop them for this talk."}
        self.nudge(text, priority="normal", key=f"conversation:followup:{t['id']}", expires_in=6 * 3600, why=why,
                   action={"label": "Pick it up", "command": f"let's continue our talk about {t['title']}"})
        keeper.mark_followed(t["id"], now)
        return {"thread_id": t["id"], "text": text}

    def _on_event(self, ev: dict) -> None:
        kind, data = ev.get("type"), ev.get("data") or {}
        if not isinstance(data, dict):
            return
        try:
            if kind == "notice" and str(data.get("key") or "").startswith("conversation:followup:"):
                tid = int(str(data["key"]).rsplit(":", 1)[-1])
                t = self.keeper.get(tid) if self.keeper is not None else None
                if t is not None:
                    note = {"thread_id": tid, "text": data.get("text") or "", "title": t["title"], "at": self.clock()}
                    with self._lock:
                        self._awaiting, self._answer = note, None
                    if self.convo is not None:
                        self.convo.followup_note = note
            elif kind == "user_said" and self._awaiting and not data.get("conversation"):
                with self._lock:
                    if self.clock() - self._awaiting["at"] > float(self.setting("followup_answer_s", 300)):
                        self._awaiting = None
                    else:
                        self._answer = str(data.get("text") or "")
            elif kind == "assistant_said" and self._awaiting and self._answer is not None:
                with self._lock:
                    note, answer, self._answer = self._awaiting, self._answer, None
                    if data.get("kind") not in ("claude", "local", "offline"):
                        return  # J did something else (a command): still waiting for the answer
                    self._awaiting = None
                if self.convo is not None:
                    self.convo.followup_note = None
                threading.Thread(target=self._fold_answer, args=(note, answer), daemon=True,
                                 name="conversation-followup").start()
        except Exception:
            log.exception("conversation event failed")

    def _fold_answer(self, note: dict, answer: str) -> None:
        a = self.svc.cfg["assistant"]
        self.keeper.answered(note["thread_id"], note["text"], answer, think=self.svc.think,
                             user=a.get("user_name") or "J", assistant=a.get("name") or "Vesper")
        self.publish()

    # ---- phrases -------------------------------------------------------------------------------------
    def route(self, t: str):
        convo = self.convo
        if convo is None:
            return None
        cfg = self.svc.cfg
        start = convo_mod.match_start(t, cfg)
        if start is not None:
            return Intent("conversation", args=start)
        resume = convo_mod.match_resume(t)
        if resume is not None and not (convo.active and not resume):  # mid-talk "where were we": the model recaps
            return Intent("conversation", args={"on_stream": False, "resume": resume})
        mode = convo_mod.match_mode(t, cfg)
        if mode is not None and (convo.active or mode["mode"] != "chat" or mode.get("topic")
                                 or re.fullmatch(r"(?:let'?s )?just (?:chat|talk)", t)):
            return Intent("conversation", args={"on_stream": False, **mode})
        if re.fullmatch(convo_mod.FORGET_RE, t):
            m = re.fullmatch(convo_mod.FORGET_RE, t)
            topic = (m.group("a") or m.group("b") or "").strip()
            last = "last" in t.split()
            return Intent("tool", "conversation_forget", {"topic": topic} if topic else {"current": not last})
        if convo.active:
            if convo_mod.match_end(t, cfg):
                return Intent("conversation_end")
            if re.fullmatch(convo_mod.HOLD_RE, t):
                return Intent("tool", "conversation_hold", {})
            if convo.link_offer and re.fullmatch(convo_mod.LINK_RE, t):
                return Intent("tool", "conversation_link", {"thread_id": convo.link_offer["id"]})
            style = convo_mod.match_style(t)
            if style is not None:
                dim, step = style
                return Intent("tool", "conversation_style", {"action": "adjust", "dim": dim, "step": str(step), "said": t})
            if convo.proposals and re.fullmatch(convo_mod.ADD_TASKS_RE, t):
                return Intent("tool", "conversation_add_tasks", {})
            m = re.fullmatch(convo_mod.LOOK_RE, t)
            if m:
                return Intent("tool", "conversation_look_into", {"what": (m.group("a") or "").strip()})
        if re.fullmatch(convo_mod.STYLE_RESET_RE, t):
            return Intent("tool", "conversation_style", {"action": "reset"})
        if re.fullmatch(convo_mod.STYLE_ASK_RE, t):
            return Intent("tool", "conversation_style", {"action": "show"})
        m = re.fullmatch(convo_mod.NO_FOLLOWUP_RE, t)
        if m:
            topic = (m.group("a") or "").strip()
            return Intent("tool", "conversation_followups", {"on": False, **({"topic": topic} if topic
                                                                              and topic not in ("it", "that", "this") else {})})
        if THREADS_RE.fullmatch(t):
            return Intent("tool", "conversation_threads", {})
        if MY_DAY.fullmatch(t):
            return Intent("tool", "conversation_my_day", {})
        return None

    # ---- tools ---------------------------------------------------------------------------------------
    def tools(self):
        modes = list(convo_mod.MODES)
        return [
            Tool("conversation_my_day", "What Vesper noticed and did today (updates it brought up, background jobs, "
                                        "requests handled, things it learned). Use for 'how was your day', 'what have "
                                        "you been up to'.", obj({}), self.my_day),
            Tool("end_conversation", "End conversation mode (back to the wake word) when the user signals they're "
                                     "done talking: 'gotta go', 'talk later', 'that's enough for now'.",
                 obj({}), self._end_tool),
            Tool("conversation_link", "In a conversation: the user said yes to picking up the earlier talk you offered "
                                      "(or named one): continue that thread.",
                 obj({"thread_id": {"type": "integer"}}), self._link, tier=1),
            Tool("conversation_mode", "Switch the conversation's mode: chat, brainstorm, devil (devil's advocate / "
                                      "debate), coach (asks, never answers), interview, teach (with check questions), "
                                      "duck (rubber duck), plan, story.",
                 obj({"mode": {"type": "string", "enum": modes}, "topic": {"type": "string"}}, ["mode"]),
                 self._mode, tier=1),
            Tool("conversation_hold", "The user is thinking ('hmm, let me think'): keep the conversation open a few "
                                      "minutes longer.", obj({}), self._hold, tier=1),
            Tool("conversation_style", "The user's feedback on how you talk in conversations ('too long', 'be funnier', "
                                       "'stop asking me questions'): adjust (dim length|humour|questions|formality, "
                                       "step -1, +1 or '=0'), reset, or show.",
                 obj({"action": {"type": "string", "enum": ["adjust", "reset", "show"]},
                      "dim": {"type": "string", "enum": list(STYLE_LABELS)}, "step": {"type": "string"}}, ["action"]),
                 self._style, tier=1),
            Tool("conversation_propose_tasks", "Plan mode: put the plan's concrete tasks on a card for the user to "
                                               "approve. Nothing is added until they say yes.",
                 obj({"tasks": {"type": "array", "items": {"type": "string"}}}, ["tasks"]), self._propose, tier=0),
            Tool("conversation_add_tasks", "Add the tasks proposed on the plan card, ONLY after the user said yes to them.",
                 obj({}), self._add_tasks, tier=1),
            Tool("conversation_forget", "Forget a conversation completely: the thread, its log and what it taught. "
                                        "current=true for the one going on now; else by topic, or the last one.",
                 obj({"topic": {"type": "string"}, "current": {"type": "boolean"}}), self._forget, tier=1),
            Tool("conversation_threads", "The conversations Vesper keeps (threads): titles, when, what's still open.",
                 obj({}), self._threads),
            Tool("conversation_followups", "Turn follow-ups for a conversation thread off (or back on): by topic, or the "
                                           "one just followed up.",
                 obj({"on": {"type": "boolean"}, "topic": {"type": "string"}}, ["on"]), self._followups, tier=1),
            Tool("conversation_look_into", "The user wants something looked into in the background while you keep "
                                           "talking ('look into that while we talk').",
                 obj({"what": {"type": "string"}}), self._look_into, tier=1),
        ]

    def _end_tool(self) -> dict:
        convo = self.convo
        return {"ok": True, "ended": bool(convo and convo.end("said"))}

    def _link(self, thread_id: int | None = None) -> dict:
        convo = self.convo
        if convo is None or not convo.active:
            return {"ok": False, "error": "We're not in a conversation."}
        t = convo.link(thread_id)
        if t is None:
            return {"ok": False, "error": "I couldn't find that earlier talk."}
        full = self.keeper.get(t["id"]) or t
        return {"ok": True, "title": t["title"], "recap": convo.recap_line(full)}

    def _mode(self, mode: str, topic: str = "") -> dict:
        convo = self.convo
        if convo is None or mode not in convo_mod.MODES:
            return {"ok": False, "error": f"I don't have a {mode} mode."}
        if not convo.active:
            return {"ok": False, "error": "We're not in a conversation. Say let's talk first."}
        convo.set_mode(mode, topic)
        return {"ok": True, "mode": mode, "label": convo_mod.MODES[mode]["label"]}

    def _hold(self) -> dict:
        convo = self.convo
        if convo is None or not convo.active:
            return {"ok": True, "held": False}
        until = convo.hold()
        return {"ok": True, "held": True, "minutes": round((until - self.clock()) / 60)}

    def _style(self, action: str = "show", dim: str = "", step: str = "", said: str = "") -> dict:
        keeper = self.keeper
        if keeper is None:
            return {"ok": False, "error": "Conversation mode isn't available."}
        if action == "reset":
            return {"ok": True, "action": "reset", "style": keeper.reset_style()}
        if action == "adjust":
            if dim not in STYLE_LABELS:
                return {"ok": False, "error": f"I don't know the style {dim}."}
            cur = keeper.style()
            step = str(step).strip()
            new = 0 if step == "=0" else cur[dim] + (1 if step.lstrip("+") == "1" else -1 if step == "-1" else 0)
            new = max(0, min(4, new))
            if new == cur[dim]:
                return {"ok": True, "action": "adjust", "dim": dim, "step": step, "changed": False, "style": cur}
            style = keeper.set_style({dim: new}, said=said)
            if self.convo is not None:
                self.convo._changed()
            return {"ok": True, "action": "adjust", "dim": dim, "step": step, "changed": True, "style": style,
                    "now": STYLE_LABELS[dim][new]}
        return {"ok": True, "action": "show", "style": keeper.style()}

    def _propose(self, tasks: list | None = None) -> dict:
        convo = self.convo
        items = [str(x).strip()[:100] for x in tasks or [] if str(x).strip()][:8]
        if convo is None or not items:
            return {"ok": False, "error": "No tasks to propose."}
        convo.proposals = [{"title": x, "keep": True} for x in items]
        convo._changed()
        return {"ok": True, "proposed": len(items), "note": "On a card for the user to approve; nothing added yet."}

    def _add_tasks(self, titles: list | None = None) -> dict:
        convo = self.convo
        if convo is None or not convo.proposals:
            return {"ok": False, "error": "There's nothing on the plan card."}
        chosen = [p["title"] for p in convo.proposals if p.get("keep", True)]
        if titles is None and not self._said_yes():  # the model asked to add them: only after J's own yes
            return {"ok": False, "error": "They're on the card. I'll add them once you say yes."}
        if titles is not None:
            chosen = [str(x).strip()[:100] for x in titles if str(x).strip()]
        added = [self.svc.storage.add_task(title, "personal" if self._personal() else "work") for title in chosen]
        convo.proposals = []
        convo._changed()
        self.svc.bus.publish("tasks", self.svc.storage.list_tasks())
        return {"ok": True, "added": [t["title"] for t in added]}

    def _said_yes(self) -> bool:
        """Did J's last words in the conversation agree to adding the tasks?"""
        convo = self.convo
        last = next((e["text"] for e in reversed(convo.transcript) if e["role"] == "user"), "") if convo else ""
        t = convo_mod._norm_t(last)
        return bool(re.fullmatch(convo_mod.ADD_TASKS_RE, t) or re.match(
            r"(?:yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|please do|sounds good|perfect|great|add)\b", t))

    def _personal(self) -> bool:
        return "personal" in (self.svc.cfg.get("profiles") or {})

    def _forget(self, topic: str = "", current: bool = False) -> dict:
        convo, keeper = self.convo, self.keeper
        if convo is None or keeper is None:
            return {"ok": False, "error": "Conversation mode isn't available."}
        if current and convo.active:
            out = convo.forget_current()
            return {**out, "ok": True, "current": True}
        if topic:
            hit = keeper.match(topic, strict=False)
            if hit is None:
                return {"ok": False, "error": f"I don't have a conversation about {topic}."}
            tid = hit[0]["id"]
        else:  # "forget the last conversation" / "forget this conversation" just after it ended
            tid = (convo.covered or {}).get("thread_id") or (convo.thread or {}).get("id") or \
                (keeper.latest() or {}).get("id")
        if not tid:
            return {"ok": False, "error": "There's no kept conversation to forget."}
        out = keeper.forget(int(tid))
        if (convo.thread or {}).get("id") == tid:
            convo.thread = None
        if (convo.covered or {}).get("thread_id") == tid:
            convo.covered = {**convo.covered, "state": "forgotten"}
        convo._changed()
        return out

    def _threads(self) -> dict:
        keeper = self.keeper
        if keeper is None:
            return {"ok": False, "error": "Conversation mode isn't available."}
        rows = [keeper.card(t) for t in keeper.list(8)]
        out = {"ok": True, "count": keeper.count(), "threads": rows}
        if self.svc.live and self.svc.live():
            out = {"ok": True, "count": out["count"], "threads": [], "live_note": "Live: say how many, not what about."}
        return out

    def _followups(self, on: bool = False, topic: str = "") -> dict:
        keeper = self.keeper
        if keeper is None:
            return {"ok": False, "error": "Conversation mode isn't available."}
        t = None
        if topic:
            hit = keeper.match(topic, strict=False)
            t = hit[0] if hit else None
        elif self._awaiting or (self.convo is not None and self.convo.followup_note):
            note = self._awaiting or self.convo.followup_note
            t = keeper.get(note["thread_id"])
        elif self.convo is not None and self.convo.thread:
            t = keeper.get(self.convo.thread["id"])
        if t is None:
            return {"ok": False, "error": "Which conversation? Say don't follow up on, then the topic."}
        keeper.set_followups(t["id"], bool(on))
        if not on:
            self._awaiting = None
        return {"ok": True, "title": t["title"], "on": bool(on)}

    def _look_into(self, what: str = "") -> dict:
        """A background job about what we're talking about. The tasks engine (``svc.tasks``) when Vesper has one,
        else the research job runner."""
        convo = self.convo
        what = (what or "").strip()
        if not what or what in ("that", "this", "it"):
            bits = []
            if convo is not None:
                if convo.thread and convo.thread.get("title"):
                    bits.append(convo.thread["title"])
                elif convo.topic:
                    bits.append(convo.topic)
                said = [e["text"] for e in convo.transcript if e["role"] == "user"
                        and not re.search(r"\b(?:look into|research|dig into)\b", e["text"].lower())]
                bits += said[-2:]
            what = " — ".join(b for b in bits if b)[:400]
        if not what:
            return {"ok": False, "error": "Look into what? Say look into, then the thing."}
        prompt = f"Look into this for J (from a conversation with Vesper): {what}"
        title = f"Look into: {first_sentence(what, 50)}"
        tasks = getattr(self.svc, "tasks", None)
        if tasks is not None:
            for fn in ("start", "submit", "add"):
                start = getattr(tasks, fn, None)
                if callable(start):
                    try:
                        out = start(prompt, title=title, source="conversation")
                    except TypeError:
                        out = start(prompt)
                    except Exception as exc:
                        return {"ok": False, "error": f"The task engine failed ({type(exc).__name__})."}
                    out = out if isinstance(out, dict) else {"ok": True}
                    return {"ok": out.get("ok", True), "what": what, "via": "tasks", **(
                        {"error": out["error"]} if out.get("error") else {})}
        jobs = getattr(self.svc, "jobs", None)
        if jobs is None:
            return {"ok": False, "error": "Background jobs aren't running, so I can't look into it while we talk."}
        out = jobs.submit("research", prompt, None, title)
        return {**out, "what": what, "via": "jobs"}

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
        if tool != "conversation_my_day" and not result.get("ok", True):
            return result.get("error") or "That didn't work."
        if tool == "conversation_link":
            return result["recap"]
        if tool == "conversation_mode":
            return convo_mod.MODES[result["mode"]]["switch"]
        if tool == "conversation_hold":
            return "Take your time." if result.get("held") else "Sure, take your time."
        if tool == "conversation_style":
            return self._say_style(result)
        if tool == "conversation_propose_tasks":
            n = result["proposed"]
            return f"I've put {n} task{'s' if n != 1 else ''} on the card. Say add them, or tap Add."
        if tool == "conversation_add_tasks":
            n = len(result["added"])
            return f"Added {n} task{'s' if n != 1 else ''}." if n else "Nothing added."
        if tool == "conversation_forget":
            if result.get("current"):
                return "Done. Nothing from this conversation is kept, and I'll keep it that way till we finish."
            n = len(result.get("facts") or [])
            return (f"Forgotten: our talk about {result['title']}" + (f", and the {n} thing{'s' if n != 1 else ''} "
                    "it taught me." if n else "."))
        if tool == "conversation_threads":
            n = result.get("count") or 0
            if not n:
                return "Nothing kept yet. Say let's talk and it starts."
            if result.get("live_note"):
                return f"{n} conversation{'s' if n != 1 else ''} kept. They're on the HUD."
            t = result["threads"][0]
            opn = f", {t['open']} thing{'s' if t['open'] != 1 else ''} still open" if t["open"] else ""
            others = ", ".join(x["title"] for x in result["threads"][1:3])
            return (f"{n} thread{'s' if n != 1 else ''}. Latest: {t['title']}, "
                    f"{when_words(t['updated'], self.clock(), self.svc.tz)}{opn}." + (f" Also {others}." if others else ""))
        if tool == "conversation_followups":
            return (f"Okay, I won't follow up on {result['title']}." if not result["on"]
                    else f"Follow-ups for {result['title']} are back on.")
        if tool == "conversation_look_into":
            return "On it: I'm looking into that in the background. Keep going."
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

    def _say_style(self, result: dict) -> str:
        if result["action"] == "reset":
            return "Back to my usual style."
        if result["action"] == "show":
            return f"Right now I keep it {style_words(result['style'])}. You can change it on the HUD or just tell me."
        key = (result["dim"], result["step"] if result["step"] == "=0" else int(result["step"].lstrip("+") or 0))
        if not result.get("changed"):
            return AT_LIMIT.get(key, "That's where it already is.")
        return STYLE_LINES.get(key, "Got it.")

    # ---- the HUD -------------------------------------------------------------------------------------
    def status(self) -> dict:
        convo = self.convo
        if convo is None:
            return {"active": False, "available": False, "transcript": []}
        return {**convo.status(), "available": True,
                "voice": bool(self.runtime is not None and getattr(self.runtime, "listener", None) is not None),
                "followups": bool(self.setting("followups", True))}

    def actions(self):
        return {"start": self._start, "end": self._end, "resume": self._resume, "forget": self._forget_action,
                "followups": self._followups_by_id, "rename": self._rename,
                "mode": self._mode_action, "link": self._link_action, "unlink": self._unlink,
                "style": self._style_action, "style_reset": lambda b: {"ok": True, "style": self.keeper.reset_style(),
                                                                       "_": self._published()},
                "add_tasks": lambda b: self._add_tasks(b.get("titles") if isinstance(b.get("titles"), list) else None),
                "dismiss_tasks": self._dismiss_tasks, "dismiss_covered": self._dismiss_covered}

    def _published(self) -> bool:
        if self.convo is not None:
            self.convo._changed()
        return True

    def _start(self, body: dict) -> dict:
        on_stream = bool(body.get("on_stream"))
        mode = str(body.get("mode") or "")
        assistant = getattr(self.runtime, "assistant", None)
        if assistant is not None:
            out = assistant.start_conversation(on_stream=on_stream, source="hud", mode=mode)
        elif self.convo is not None:
            ok, why = self.convo.begin(on_stream=on_stream, source="hud", mode=mode)
            reply = (self.convo.opener(mode=mode, already=why == "already") or convo_mod.GREETINGS[0]) if ok else why
            out = {"reply": reply, "kind": "conversation" if ok else "conversation_off"}
        else:
            return {"ok": False, "error": "Conversation mode isn't available."}
        return self._opened(out, body)

    def _opened(self, out: dict, body: dict) -> dict:
        ok = out["kind"] != "conversation_off"
        if ok and self.convo is not None:
            self.convo.turn_done(out["reply"], "hud", started=False)  # the greeting opens the transcript
        self.svc.bus.publish("assistant_said", {"text": out["reply"], "kind": out["kind"], "source": "hud", "ms": 0})
        if self.status().get("voice"):  # the mic is open: say hello (or why not) out loud
            self.svc.speak(out["reply"], expects_reply=False)
        return {"ok": ok, "reply": out["reply"], **({} if ok else {"error": out["reply"]})}

    def _resume(self, body: dict) -> dict:
        convo, keeper = self.convo, self.keeper
        if convo is None or keeper is None:
            return {"ok": False, "error": "Conversation mode isn't available."}
        t = keeper.get(int(body.get("id") or 0))
        if t is None:
            return {"ok": False, "error": "That conversation is gone."}
        assistant = getattr(self.runtime, "assistant", None)
        if assistant is not None:
            out = assistant.start_conversation(source="hud", resume="")  # a fresh start, then this thread
        else:
            ok, why = convo.begin(source="hud")
            out = {"reply": why, "kind": "conversation" if ok else "conversation_off"}
        if out["kind"] != "conversation_off":
            convo.attach(t)
            out = {"reply": convo.recap_line(t), "kind": "conversation"}
        return self._opened(out, body)

    def _forget_action(self, body: dict) -> dict:
        convo, keeper = self.convo, self.keeper
        if keeper is None:
            return {"ok": False, "error": "Conversation mode isn't available."}
        if body.get("current") and convo is not None and convo.active:
            return {**convo.forget_current(), "ok": True}
        tid = int(body.get("id") or 0)
        out = keeper.forget(tid)
        if convo is not None:
            if (convo.thread or {}).get("id") == tid:
                convo.thread = None
            if (convo.covered or {}).get("thread_id") == tid:
                convo.covered = {**convo.covered, "state": "forgotten"}
            convo._changed()
        return out

    def _followups_by_id(self, body: dict) -> dict:
        tid = int(body.get("id") or 0)
        ok = self.keeper.set_followups(tid, bool(body.get("on")))
        self._published()
        return {"ok": ok}

    def _rename(self, body: dict) -> dict:
        ok = self.keeper.rename(int(body.get("id") or 0), str(body.get("title") or ""))
        self._published()
        return {"ok": ok}

    def _mode_action(self, body: dict) -> dict:
        mode = str(body.get("mode") or "")
        convo = self.convo
        if convo is None or mode not in convo_mod.MODES:
            return {"ok": False, "error": "Unknown mode."}
        if convo.active:
            convo.set_mode(mode)
            return {"ok": True, "mode": mode}
        return self._start({"mode": mode})

    def _link_action(self, body: dict) -> dict:
        out = self._link(int(body["id"]) if body.get("id") else None)
        if out.get("ok") and self.convo is not None:
            self.convo.turn_done(out["recap"], "hud", started=False)
            self.svc.bus.publish("assistant_said", {"text": out["recap"], "kind": "conversation", "source": "hud", "ms": 0})
            if self.status().get("voice"):
                self.svc.speak(out["recap"], expects_reply=False)
        return out

    def _unlink(self, _body: dict) -> dict:
        if self.convo is not None:
            self.convo.link_offer = None
            self.convo._changed()
        return {"ok": True}

    def _style_action(self, body: dict) -> dict:
        dim = str(body.get("dim") or "")
        if dim not in STYLE_LABELS:
            return {"ok": False, "error": "Unknown style."}
        style = self.keeper.set_style({dim: int(body.get("value") or 0)}, said="(HUD)", by="hud")
        self._published()
        return {"ok": True, "style": style}

    def _dismiss_tasks(self, _body: dict) -> dict:
        if self.convo is not None:
            self.convo.proposals = []
            self.convo._changed()
        return {"ok": True}

    def _dismiss_covered(self, _body: dict) -> dict:
        if self.convo is not None:
            self.convo.covered = None
            self.convo._changed()
        return {"ok": True}

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
