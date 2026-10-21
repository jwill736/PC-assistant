"""Meeting debrief: right after a meeting with other people, one question, and J's answer becomes notes and tasks.

- Within 30 minutes after a meeting with attendees ends, a nudge: "Anything to note from Contract review?"
- Whatever J says next (within 3 minutes of hearing it), or types in the HUD box, becomes:
  a note ("Debrief · Contract review: …"), tasks from lines like "I'll send the deck by Friday" / "we need to book
  the venue" / "send Dana the numbers tomorrow" (with due dates), and People notes for the attendees it mentions.
- "debrief my last meeting" asks the question for the meeting that ended last (any time today);
  "debrief my last meeting: I'll send the deck by Friday" saves it at once. "nothing to note" closes it.

A connected model tidies the tasks; without one they're found by pattern. Attendees come from Google calendars
(iCal links don't list them). It replaces the meetings skill's "any follow-ups?" nudge when this skill is on.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill
from .people import display_name, first_name, on_air

WINDOW_MIN = 30  # ask within this long after the meeting ends
LISTEN_S = 180  # J's next words within 3 minutes of the question are the debrief
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DUE = re.compile(r"\s*,?\s*\b(?:(?:by|before|on|due|for|until)\s+)?(?:the\s+)?(today|tonight|tomorrow|eod|eow|end of (?:the )?(?:day|week)|"
                 r"this week|next week|(?:next |this )?(?:" + "|".join(DAYS) + r")|(?:" + "|".join(MONTHS) +
                 r")[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?|\d{1,2}/\d{1,2})\b", re.I)
MINE = re.compile(r"^(?:so |and |also |then |ok(?:ay)? |um |uh )*(?:i'?ll|i will|i'm going to|i am going to|i need to|i have to|"
                  r"i should|i must|i've got to|i gotta|let me|we need to|we should|we have to|we'll|we will|we must|"
                  r"need to|have to|must|remember to|don't forget to|todo|to do|action items?)\b[:,]?\s*(.+)", re.I)
VERB = re.compile(r"^(?:send|email|call|book|schedule|share|write|draft|review|update|fix|prepare|ping|check|finish|ask|"
                  r"set up|follow up|circle back|get|order|pay|sign|submit|file|loop in|invite|confirm|chase|text)\b", re.I)
SKIP = re.compile(r"(?:nothing|no(?:pe)?|nah|not really|all good|skip(?: it)?|none)(?: (?:to note|really|thanks|thank you|for now|"
                  r"from (?:that|it)))*")
NOT_DEBRIEF = re.compile(r"^(?:open|close|play|pause|skip|next|switch|turn|set|start|stop|launch|mute|unmute|what|what's|"
                         r"who|who's|when|where|how|why|is|are|can|could|remind|search|check my|show|volume|plan|focus|lights?)\b")
SCHEMA = {"type": "object", "required": ["tasks"], "properties": {
    "summary": {"type": "string"},
    "tasks": {"type": "array", "items": {"type": "object", "required": ["title"], "properties": {
        "title": {"type": "string"}, "due": {"type": ["string", "null"]}}}},
    "people": {"type": "array", "items": {"type": "object", "required": ["email", "note"], "properties": {
        "email": {"type": "string"}, "note": {"type": "string"}}}}}}


def due_of(text: str, base: date) -> tuple[str | None, str]:
    """(YYYY-MM-DD or None, the text without the date words) for "send the deck by Friday"."""
    m = DUE.search(text)
    if not m:
        return None, text
    w, wd = m.group(1).lower(), base.weekday()
    if w in ("today", "tonight", "eod") or w.startswith("end of") and w.endswith("day"):
        d = base
    elif w == "tomorrow":
        d = base + timedelta(days=1)
    elif w.split()[-1] in DAYS:  # "friday": the coming one (today if it's Friday); "next friday": never today
        ahead = (DAYS.index(w.split()[-1]) - wd) % 7
        d = base + timedelta(days=ahead or (7 if w.startswith("next ") else 0))
    elif w == "next week":
        d = base + timedelta(days=(7 - wd) or 7)
    elif w in ("eow", "this week") or w.startswith("end of"):
        d = base + timedelta(days=(4 - wd) % 7)
    elif "/" in w:
        mo, dd = (int(x) for x in w.split("/"))
        d = _date_after(base, mo, dd)
    else:
        mo = MONTHS.index(w[:3]) + 1
        d = _date_after(base, mo, int(re.search(r"\d+", w).group(0)))
    if d is None:
        return None, text
    rest = (text[:m.start()] + text[m.end():]).strip(" ,.;")
    return d.isoformat(), re.sub(r"\s{2,}", " ", rest)


def _date_after(base: date, month: int, day: int) -> date | None:
    try:
        d = date(base.year, month, day)
        return d if d >= base - timedelta(days=1) else date(base.year + 1, month, day)
    except ValueError:
        return None


def find_tasks(text: str, base: date) -> list[dict]:
    """J's own commitments in what he said, each {title, due}."""
    out = []
    lead = r"(?:i'?ll|i will|i'm going to|i need to|i have to|we need to|we should|we have to|we'll|we will|let me)\b"
    for s in re.split(rf"(?<=[.!?;])\s+|\n+|,? (?:and|also|plus|then) (?={lead}|(?:send|call|book|email)\b)|\s+(?={lead})",
                      text or "", flags=re.I):
        s = s.strip(" .,;!")
        if len(s) < 6:
            continue
        m = MINE.match(s)
        what = m.group(1) if m else (s if VERB.match(s) else None)
        if not what:
            continue
        due, what = due_of(what, base)
        what = re.sub(r"^(?:to |go ahead and |make sure (?:to |we |i )?)", "", what.strip(), flags=re.I)
        if len(what.split()) < 2:
            continue
        what = what[:90].rsplit(" ", 1)[0] + "…" if len(what) > 90 else what
        out.append({"title": what[:1].upper() + what[1:], "due": due})
    return out[:8]


class Debrief(Skill):
    name, title = "debrief", "Meeting debrief"
    poll_s = 60
    delay_s = 50

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        self.error: str | None = None
        self._asking: dict | None = None  # the meeting the question is about (HUD box open for WINDOW_MIN)
        self._listen_until = 0.0  # J's next words before this are the answer
        svc.storage.execute("CREATE TABLE IF NOT EXISTS debrief_log (key TEXT PRIMARY KEY, title TEXT NOT NULL, start TEXT, "
                            "end_at TEXT, profile TEXT, attendees TEXT, state TEXT NOT NULL, asked REAL, done REAL, "
                            "text TEXT, note_id INTEGER, tasks TEXT, people TEXT)")
        svc.bus.on(self._on_event)

    # ---- the calendar ----------------------------------------------------------------------------------------
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.svc.tz)

    def ended(self, hours: float = 12) -> list[dict]:
        """Meetings with other people that ended in the last ``hours``, the latest first."""
        now = self.now()
        try:
            evs = self.svc.calendars.events(now - timedelta(hours=hours + 8), now)
        except Exception as exc:
            self.error = f"Couldn't read the calendar ({type(exc).__name__})."
            return []
        out = []
        for e in evs:
            others = [{"name": display_name(a.get("name", ""), a["email"]), "email": a["email"].lower()}
                      for a in e.get("attendees") or [] if not a.get("self") and a.get("email")]
            end = datetime.fromisoformat(e["end"])
            if e.get("all_day") or not others or not (now - timedelta(hours=hours) <= end <= now):
                continue
            out.append({"key": f"{e['title']}|{e['start']}", "title": e["title"], "start": e["start"], "end": e["end"],
                        "profile": e.get("profile") or "work", "others": others})
        return sorted(out, key=lambda e: e["end"], reverse=True)

    def _row(self, key: str) -> dict | None:
        rows = self.svc.storage.query("SELECT * FROM debrief_log WHERE key=?", (key,))
        return rows[0] if rows else None

    # ---- asking -------------------------------------------------------------------------------------------
    def check(self) -> None:
        now = self.clock()
        if self._asking and now - datetime.fromisoformat(self._asking["end"]).timestamp() > WINDOW_MIN * 60 + LISTEN_S:
            self._asking = None
        for e in self.ended(hours=1):
            mins = (now - datetime.fromisoformat(e["end"]).timestamp()) / 60
            if mins > WINDOW_MIN or self._row(e["key"]):
                continue
            self.svc.storage.execute("INSERT OR IGNORE INTO debrief_log(key, title, start, end_at, profile, attendees, state, "
                                     "asked) VALUES (?,?,?,?,?,?,?,?)", (e["key"], e["title"], e["start"], e["end"],
                                                                         e["profile"], json.dumps(e["others"]), "asked", now))
            self._asking = e
            self.nudge(f"Anything to note from {e['title']}?", priority="normal", key=f"debrief:{e['key']}",
                       expires_in=max(60.0, (WINDOW_MIN - mins) * 60),
                       action={"label": "Debrief", "command": "debrief my last meeting"})
            if self.svc.nudge is None:  # no nudge queue: the question showed now
                self._listen_until = now + LISTEN_S
            break

    def _on_event(self, ev: dict) -> None:
        """The question went out (spoken or on the HUD): listen for the answer from now. And keep the last thing J
        said as he said it (the fast path only sees it lower-cased, without the full stops between sentences)."""
        d = ev.get("data") or {}
        if ev.get("type") == "user_said" and isinstance(d.get("text"), str):
            self._said = d["text"]
        if ev.get("type") != "notice":
            return
        if self._asking and d.get("key") == f"debrief:{self._asking['key']}":
            self._listen_until = self.clock() + LISTEN_S

    _said = ""

    def _original(self, text: str) -> str:
        """The words as J said them, when ``text`` is (the end of) the cleaned-up utterance."""
        from ..brain.router import clean

        raw = self._said or ""
        if not raw or not text or not clean(raw).endswith(text.lower()):
            return text
        n = len(text.split())
        words = raw.split()
        return " ".join(words[-n:]) if len(words) >= n else text

    def listening(self) -> bool:
        return bool(self._asking) and self.clock() < self._listen_until

    # ---- tools -------------------------------------------------------------------------------------------
    def _meeting(self, key: str = "") -> dict | None:
        if key:
            if self._asking and self._asking["key"] == key:
                return self._asking
            row = self._row(key)
            if row:
                return {"key": key, "title": row["title"], "start": row["start"], "end": row["end_at"],
                        "profile": row["profile"], "others": json.loads(row["attendees"] or "[]")}
            return next((e for e in self.ended(hours=24) if e["key"] == key), None)
        if self._asking:
            return self._asking
        return next(iter(self.ended(hours=float(self.setting("lookback_hours", 12)))), None)

    def start_debrief(self) -> dict:
        e = self._meeting()
        if e is None:
            return {"ok": False, "error": "No meeting with other people has ended today." + self._hint()}
        self._asking = e
        self._listen_until = self.clock() + LISTEN_S
        if not self._row(e["key"]):
            self.svc.storage.execute("INSERT OR IGNORE INTO debrief_log(key, title, start, end_at, profile, attendees, state, "
                                     "asked) VALUES (?,?,?,?,?,?,?,?)", (e["key"], e["title"], e["start"], e["end"],
                                                                         e["profile"], json.dumps(e["others"]), "asked", self.clock()))
        self.publish()
        return {"ok": True, "title": e["title"], "key": e["key"], "with": [p["name"] for p in e["others"]],
                "question": f"What should I note from {e['title']}?"}

    def save(self, text: str, key: str = "", source: str = "voice") -> dict:
        text = re.sub(r"\s+", " ", self._original((text or "").strip())).strip()
        if len(text) < 3:
            return {"ok": False, "error": "Say what to note, e.g. “I'll send Dana the deck by Friday”."}
        e = self._meeting(key)
        if e is None:
            return {"ok": False, "error": "Which meeting? None with other people ended today." + self._hint()}
        text = text[0].upper() + text[1:]
        for p in e["others"]:  # spoken names come in lower case
            for w in p["name"].split():
                if len(w) > 2:
                    text = re.sub(rf"\b{re.escape(w.lower())}\b", w, text)
        end_day = datetime.fromisoformat(e["end"]).astimezone(self.svc.tz).date()
        tasks, people, by = self._understand(text, e, end_day)
        profile = e.get("profile") if e.get("profile") in (self.svc.cfg.get("profiles") or {}) or e.get("profile") == "personal" else "work"
        made = [self.svc.storage.add_task(t["title"], profile, 2, t.get("due")) for t in tasks]
        note = self.svc.storage.add_note(f"Debrief · {e['title']} ({end_day.strftime('%b')} {end_day.day}): {text}")
        noted = self._people_notes(people, e)
        self.svc.storage.execute(
            "INSERT INTO debrief_log(key, title, start, end_at, profile, attendees, state, asked, done, text, note_id, tasks, "
            "people) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET state='done', done=excluded.done, "
            "text=excluded.text, note_id=excluded.note_id, tasks=excluded.tasks, people=excluded.people",
            (e["key"], e["title"], e["start"], e["end"], profile, json.dumps(e["others"]), "done", self.clock(), self.clock(),
             text, note["id"], json.dumps([{"id": t["id"], "title": t["title"], "due": t["due"]} for t in made]), json.dumps(noted)))
        if self._asking and self._asking["key"] == e["key"]:
            self._asking, self._listen_until = None, 0.0
        if made:
            self.svc.bus.publish("tasks", self.svc.storage.list_tasks())
        self.publish()
        return {"ok": True, "title": e["title"], "note": note["text"], "by": by,
                "tasks": [{"id": t["id"], "title": t["title"], "due": t["due"]} for t in made], "people": noted}

    def skip(self, key: str = "") -> dict:
        e = self._meeting(key)
        if e is not None:
            self.svc.storage.execute("UPDATE debrief_log SET state='skipped', done=? WHERE key=?", (self.clock(), e["key"]))
        self._asking, self._listen_until = None, 0.0
        self.publish()
        return {"ok": True, "title": e["title"] if e else None}

    def _understand(self, text: str, e: dict, base: date) -> tuple[list[dict], list[dict], str]:
        """Tasks and per-person notes from what J said: the model when there is one, else patterns."""
        tasks = find_tasks(text, base)
        people = self._mentions(text, e["others"])
        attendees = ", ".join(f"{p['name']} <{p['email']}>" for p in e["others"])
        prompt = (f"J just finished the meeting “{e['title']}” (ended {base.isoformat()}, a {base.strftime('%A')}) with "
                  f"{attendees}. Below is what he said to note. List the tasks he (or 'we') committed to as short "
                  "to-dos starting with a verb (max 12 words, name the person they involve), due = YYYY-MM-DD when a "
                  "day is said, else null. people = one short note per attendee he said something about (use their "
                  "email from the list).\n\nWHAT J SAID:\n" + text)
        try:
            got = self.svc.think(prompt, schema=SCHEMA)
        except Exception:
            got = None
        if not isinstance(got, dict) or not isinstance(got.get("tasks"), list):
            return tasks, people, "patterns"
        ok_emails = {p["email"] for p in e["others"]}
        mtasks = []
        for t in got["tasks"][:8]:
            if isinstance(t, dict) and str(t.get("title") or "").strip():
                due = t.get("due") if isinstance(t.get("due"), str) and re.fullmatch(r"\d{4}-\d\d-\d\d", t["due"]) else None
                mtasks.append({"title": str(t["title"]).strip()[:120], "due": due})
        mpeople = [{"email": str(p["email"]).lower(), "note": str(p.get("note") or "")[:200]} for p in got.get("people") or []
                   if isinstance(p, dict) and str(p.get("email", "")).lower() in ok_emails and p.get("note")]
        return mtasks, mpeople or people, "model"

    @staticmethod
    def _mentions(text: str, others: list[dict]) -> list[dict]:
        out = []
        sentences = [s.strip() for s in re.split(r"(?<=[.!?;])\s+|\n+", text) if s.strip()]
        for p in others:
            first = first_name(p["name"])
            if len(first) < 3:
                continue
            said = [s for s in sentences if re.search(rf"\b{re.escape(first)}\b", s, re.I)]
            if said:
                out.append({"email": p["email"], "note": " ".join(said)[:200]})
        return out

    def _people_notes(self, people: list[dict], e: dict) -> list[str]:
        ppl = self.svc.skills.get("people") if self.svc.skills is not None else None
        if ppl is None or not people:
            return []
        names = {p["email"]: p["name"] for p in e["others"]}
        out = []
        for p in people:
            self.svc.storage.execute("INSERT INTO people_notes(email, kind, text, created) VALUES (?, 'note', ?, ?)",
                                     (p["email"], f"{e['title']}: {p['note']}", self.clock()))
            out.append(names.get(p["email"], p["email"]))
        try:
            ppl.publish()
        except Exception:
            pass
        return out

    def _hint(self) -> str:
        try:
            if not any(s.get("kind") == "google" for s in self.svc.calendars.status()):
                return " (Who was in a meeting only comes from Google calendars: Setup → Google accounts.)"
        except Exception:
            pass
        return ""

    def tools(self):
        return [
            Tool("debrief_start", "Ask J what to note from the meeting that just ended (his next words are saved as "
                                  "the debrief).", obj(), self.start_debrief),
            Tool("debrief_save", "Save J's debrief of a meeting: a note, tasks from what he committed to (with due "
                                 "dates) and notes on the attendees he mentioned. key: empty for the last meeting.",
                 obj({"text": {"type": "string"}, "key": {"type": "string"}}, ["text"]), self.save, tier=1),
            Tool("debrief_skip", "Nothing to note from the last meeting.", obj(), self.skip),
        ]

    def route(self, t: str):
        if self.listening():
            if re.fullmatch(SKIP, t):
                return Intent("tool", "debrief_skip", {})
            if len(t.split()) >= 3 and not NOT_DEBRIEF.match(t):
                return Intent("tool", "debrief_save", {"text": t})
        m = re.fullmatch(r"(?:let'?s )?debrief(?: me)?(?: (?:on|from))?(?: (?:my|the))?(?: (?:last|latest|previous))? "
                         r"(?:meeting|call)(?: (.{6,}))?|debrief", t)
        if m:
            return Intent("tool", "debrief_save", {"text": m.group(1)}) if m and m.group(1) else Intent("tool", "debrief_start", {})
        m = re.fullmatch(r"(?:meeting )?debrief (.{6,})", t)
        if m:
            return Intent("tool", "debrief_save", {"text": m.group(1)})
        return None

    def say(self, tool, args, result):
        if tool == "debrief_start":
            return f"What should I note from {result['title']}?"
        if tool == "debrief_skip":
            return "Okay, nothing noted." if result.get("title") else "Okay."
        if tool != "debrief_save":
            return None
        tasks, ppl = result.get("tasks") or [], result.get("people") or []
        if on_air(self.svc):
            return f"Noted, with {len(tasks)} task{'s' if len(tasks) != 1 else ''}. It's on the HUD."
        bits = []
        if tasks:
            first = tasks[0]
            when = f", due {datetime.fromisoformat(first['due']).strftime('%A')}" if first.get("due") else ""
            bits.append(f"{len(tasks)} task{'s' if len(tasks) != 1 else ''} ({first['title']}{when}"
                        + (f", and {len(tasks) - 1} more)" if len(tasks) > 1 else ")"))
        if ppl:
            bits.append("a note on " + " and ".join(first_name(n) for n in ppl[:3]))
        return f"Noted for {result['title']}" + (": " + " and ".join(bits) if bits else "") + "."

    # ---- HUD -------------------------------------------------------------------------------------------------
    def recent(self, limit: int = 5) -> list[dict]:
        rows = self.svc.storage.query("SELECT * FROM debrief_log WHERE state='done' ORDER BY done DESC LIMIT ?", (limit,))
        return [{"key": r["key"], "title": r["title"], "end": r["end_at"], "text": r["text"], "done": r["done"],
                 "tasks": json.loads(r["tasks"] or "[]"), "people": json.loads(r["people"] or "[]")} for r in rows]

    def status(self) -> dict:
        live = on_air(self.svc)
        a = self._asking
        asking = {"key": a["key"], "title": a["title"], "end": a["end"], "with": [p["name"] for p in a["others"]],
                  "listening": self.listening()} if a else None
        last = None if a else next(iter(self.ended(hours=float(self.setting("lookback_hours", 12)))), None)
        out = {"ok": True, "asking": asking, "recent": [] if live else self.recent(),
               "last": {"key": last["key"], "title": last["title"], "end": last["end"],
                        "done": bool((self._row(last["key"]) or {}).get("state") in ("done", "skipped"))} if last else None}
        if self.error:
            out["warning"] = self.error
        if live:
            out["live"] = True
        return out

    def actions(self):
        return {"save": lambda b: self.save(str(b.get("text") or ""), str(b.get("key") or ""), source="hud"),
                "skip": lambda b: self.skip(str(b.get("key") or "")),
                "start": lambda b: self.start_debrief()}

    def tick(self) -> None:
        try:
            self.error = None
            self.check()
        except Exception as exc:
            self.error = f"Debrief check failed ({type(exc).__name__})."
        self.publish()
