"""Nudges: Vesper speaking first, at the right moment, and learning when to keep quiet.

Every skill hands its proactive messages to ``svc.nudge(...)``. They wait in a queue until a natural break (you
switch apps, come back from being away, a game or the stream ends) and the gate is open:

- live on stream: nothing is spoken (it would go out on air); the HUD feed still gets it;
- a fullscreen game or presentation: held until you're out of it (urgent ones show on the HUD);
- a focus session or quiet hours: only urgent ones;
- away from the PC: held until you're back, then "while you were away".

A budget caps spoken nudges per hour, each kind has a cooldown, and a kind you keep dismissing goes quiet (HUD only).
Built in: a meeting starting soon, a break after a long stretch, tasks due today, and the welcome-back summary.

Every nudge carries a ``why``: the rule that raised it and the evidence it saw ("Calendar: Client sync at 2:00 PM",
"Gmail: Dana Ortiz, “Redlines”, 3 days ago"). The HUD shows it on tap; "why did you say that" explains the last one.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill

PRIORITY = {"urgent": 0, "normal": 1, "low": 2}
RULE_NAMES = {"calendar": "Meeting starting soon", "break": "Long stretch without a break", "tasks": "Tasks due today",
              "welcome": "Welcome back after time away", "ambient": "Look-around (AI)", "inbox_zero": "Email follow-up"}


@dataclass
class Nudge:
    text: str
    source: str = "vesper"
    priority: str = "normal"  # urgent: now if at all possible; normal: at the next break; low: feed only
    key: str = ""
    speak: bool = True
    created: float = field(default_factory=time.time)
    expires: float = 0.0  # 0: an hour after created
    delivered: float = 0.0
    spoken: bool = False
    action: dict | None = None  # {"label": "Open", "url": ...} or {"label": ..., "command": "..."}
    held: str = ""  # why it's still waiting
    why: dict | None = None  # {"rule": "Meeting starting soon", "evidence": ["Calendar: Client sync at 2:00 PM"]}

    def expired(self, now: float) -> bool:
        return now > (self.expires or self.created + 3600)


def make_why(why, source: str = "") -> dict:
    """A nudge's reason in one shape, {"rule", "evidence": [...], "note"?}: a str is the rule, a list the evidence."""
    default = RULE_NAMES.get(source, f"{(source or 'Vesper').replace('_', ' ').capitalize()} update")
    if isinstance(why, dict):
        ev = why.get("evidence") or []
        out = {"rule": str(why.get("rule") or default)[:120],
               "evidence": [str(e)[:200] for e in (ev if isinstance(ev, (list, tuple)) else [ev]) if e][:6]}
        if why.get("note"):
            out["note"] = str(why["note"])[:200]
        return out
    if isinstance(why, (list, tuple)):
        return {"rule": default, "evidence": [str(e)[:200] for e in why if e][:6]}
    if isinstance(why, str) and why.strip():
        return {"rule": why.strip()[:120], "evidence": []}
    return {"rule": default, "evidence": []}


def ago_words(seconds: float) -> str:
    """'just now', '40 minutes ago', '5 hours ago', '3 days ago' (for evidence lines)."""
    s = max(0.0, float(seconds))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if s >= max(size, 120):
            n = int(s // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def fullscreen_state() -> str:
    """'game', 'presentation', 'busy' or '' (Windows' own "don't disturb" signal: SHQueryUserNotificationState)."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes

        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return ""
        return {2: "busy", 3: "game", 4: "presentation", 6: "busy"}.get(state.value, "")
    except Exception:
        return ""


class Nudges(Skill):
    name, title = "nudges", "Nudges"
    poll_s = 15
    delay_s = 30

    def __init__(self, svc, clock=time.time, idle=None, fullscreen=fullscreen_state):
        super().__init__(svc)
        self.clock, self.fullscreen = clock, fullscreen
        if idle is None:
            from ..integrations import desktop

            idle = desktop.idle_seconds
        self.idle = idle
        self.queue: list[Nudge] = []
        self.feed: list[Nudge] = []
        self._lock = threading.RLock()
        self._spoken_at: list[float] = []
        self._cooldown: dict[str, float] = {}
        self._muted_until = 0.0
        self._away_since = 0.0
        self._was_away = False
        self._last_break = 0.0
        self.last_spoken: Nudge | None = None
        self.focus = lambda: False  # set by the focus skill
        self.stats = self._load_stats()
        svc.storage.execute("CREATE TABLE IF NOT EXISTS nudges_feedback (key TEXT, source TEXT, text TEXT, "
                            "useful INTEGER, ts REAL)")
        svc.nudge = self.add
        svc.bus.on(self._on_event)

    # ---- the queue ---------------------------------------------------------------------------------------
    def add(self, text: str, *, source: str = "vesper", priority: str = "normal", key: str = "", speak: bool = True,
            expires_in: float = 3600, action: dict | None = None, cooldown: float = 0, why=None) -> Nudge | None:
        now = self.clock()
        key = key or f"{source}:{re.sub(r'[^a-z0-9]+', '-', text.lower())[:40]}"
        with self._lock:
            if self._cooldown.get(key, 0) > now or any(n.key == key for n in self.queue + self.feed[:30]):
                return None
            if cooldown:
                self._cooldown[key] = now + cooldown
            n = Nudge(text, source, priority if priority in PRIORITY else "normal", key, speak, now,
                      now + expires_in, action=action, why=make_why(why, source))
            if n.priority == "low" or self._quiet_source(source):
                n.speak = False
            self.queue.append(n)
            self.queue.sort(key=lambda x: (PRIORITY[x.priority], x.created))
        if n.priority == "urgent" or n.priority == "low":
            self.deliver()
        else:
            self.publish()
        return n

    def gate(self) -> dict:
        """Whether a nudge may be shown and spoken right now, and why not."""
        now = self.clock()
        live = bool(self.svc.live and self.svc.live())
        fs = self.fullscreen()
        idle = self.idle() if self.idle else 0
        quiet = in_quiet_hours(datetime.now(self.svc.tz), self.setting("quiet_hours", "23:00-07:30"))
        if now < self._muted_until:
            why = f"muted until {datetime.fromtimestamp(self._muted_until, self.svc.tz).strftime('%I:%M %p').lstrip('0')}"
        elif live:
            why = "live on stream"
        elif fs:
            why = {"game": "in a game", "presentation": "presenting"}.get(fs, "fullscreen")
        elif idle > 300:
            why = "away from the PC"
        elif self.focus():
            why = "focus session"
        elif quiet:
            why = "quiet hours"
        else:
            why = ""
        spoken_hour = [t for t in self._spoken_at if now - t < 3600]
        budget = int(self.setting("spoken_per_hour", 6))
        return {"open": not why, "why": why, "live": live, "away": idle > 300, "idle_s": int(idle),
                "speak_ok": not why and len(spoken_hour) < budget and now - (spoken_hour[-1] if spoken_hour else 0) > 90,
                "spoken_last_hour": len(spoken_hour), "budget": budget}

    def deliver(self, at_break: bool = False) -> list[Nudge]:
        """Send what may go out now. ``at_break``: you just switched apps or came back."""
        now = self.clock()
        g = self.gate()
        out = []
        with self._lock:
            keep = []
            for n in self.queue:
                if n.expired(now):
                    continue
                go = (n.priority == "low" or (n.priority == "urgent" and g["why"] not in ("away from the PC",)) or
                      (g["open"] and (at_break or now - n.created > 600 or now - self._last_break < 5)))
                if not go:
                    n.held = g["why"] or "waiting for a pause"
                    keep.append(n)
                    continue
                n.delivered, n.held = now, ""
                n.spoken = bool(n.speak and g["speak_ok"] and n.priority != "low")
                if n.spoken:
                    self._spoken_at.append(now)
                    self.last_spoken = n
                    g = self.gate()  # budget and spacing
                out.append(n)
            self.queue = keep
            self.feed = (list(reversed(out)) + self.feed)[:60]
        for n in out:
            self.svc.bus.publish("notice", {"source": n.source, "text": n.text, "level": n.priority, "ts": n.delivered,
                                            "key": n.key, "spoken": n.spoken, "action": n.action, "why": n.why})
            if n.spoken:
                self.svc.speak(n.text)
        if out or self.queue:
            self.publish()
        return out

    # ---- moments ------------------------------------------------------------------------------------------
    def _on_event(self, ev: dict) -> None:
        if ev.get("type") == "activity_now":  # switched apps: a natural break
            self._last_break = self.clock()
            if self.queue:
                threading.Thread(target=self.deliver, kwargs={"at_break": True}, daemon=True).start()

    def tick(self) -> None:
        now = self.clock()
        idle = self.idle() if self.idle else 0
        if idle > 300 and not self._away_since:
            self._away_since = now - idle
        elif idle < 60 and self._away_since:
            away = now - self._away_since
            self._away_since = 0
            if away > 20 * 60:
                self._welcome_back(away)
            self._last_break = now
        self._rules(now)
        self.deliver()
        self.publish()

    def _welcome_back(self, away_s: float) -> None:
        missed = [n for n in self.feed if n.delivered >= self.clock() - away_s and not n.spoken]
        parts = [f"{len(missed)} update{'s' if len(missed) != 1 else ''} on the HUD"] if missed else []
        mins = int(away_s // 60)
        evidence = [f"Idle: no keyboard or mouse for {mins} minutes"]
        gm = self.svc.gmail
        if gm is not None and gm.accounts():
            waiting = sum(a.get("waiting", 0) for a in gm.needs_you().get("accounts", []))
            if waiting:
                parts.append(f"{waiting} email{'s' if waiting != 1 else ''} waiting on you")
                evidence.append(f"Gmail: {waiting} unread from people in the last 3 days")
        nxt = self._next_event()
        if nxt:
            parts.append(f"next up {nxt['title']} at {_clock(nxt['start'])}")
            evidence.append(f"Calendar: {nxt['title']} at {_clock(nxt['start'])}")
        text = f"Welcome back. You were away {mins // 60} hours {mins % 60} minutes" if mins >= 60 else \
            f"Welcome back. You were away {mins} minutes"
        self.add(text + (": " + "; ".join(parts) if parts else "") + ".", source="welcome", priority="normal",
                 key=f"welcome:{int(self.clock() // 600)}", why={"rule": RULE_NAMES["welcome"], "evidence": evidence})

    def _next_event(self) -> dict | None:
        try:
            now = datetime.now(self.svc.tz)
            for e in self.svc.calendars.agenda(1):
                if not e["all_day"] and datetime.fromisoformat(e["start"]) > now:
                    return e
        except Exception:
            return None
        return None

    def _rules(self, now: float) -> None:
        tz = self.svc.tz
        dt = datetime.now(tz)
        # a meeting starting soon
        try:
            lead = float(self.setting("meeting_lead_minutes", 10))
            for e in self.svc.calendars.agenda(1):
                if e["all_day"]:
                    continue
                mins = (datetime.fromisoformat(e["start"]) - dt).total_seconds() / 60
                if 0 < mins <= lead:
                    where = f" ({e['location']})" if e.get("location") and len(e["location"]) < 60 else ""
                    ev = [f"Calendar: {e['title']} at {_clock(e['start'])}{where}",
                          f"Rule: warn {int(lead)} minutes before a meeting"]
                    self.add(f"{e['title']} starts in {int(round(mins))} minutes{where}.", source="calendar",
                             priority="urgent", key=f"meeting:{e['title']}:{e['start']}", expires_in=mins * 60 + 300,
                             why={"rule": RULE_NAMES["calendar"], "evidence": ev})
        except Exception:
            pass
        # a long stretch without a break
        try:
            segs = self.svc.activity.segments(now - 3 * 3600, now)
            active = streak(segs, now)
            limit = float(self.setting("break_after_minutes", 90)) * 60
            if active >= limit:
                since = datetime.fromtimestamp(now - active, tz).strftime("%I:%M %p").lstrip("0")
                ev = [f"Activity: at the PC since {since} with no pause over 5 minutes",
                      f"Setting: a break after {int(limit // 60)} minutes"]
                self.add(f"You've been at it {int(active // 60)} minutes straight. Stand up, water, two minutes.",
                         source="break", key=f"break:{int(now // limit)}", cooldown=limit,
                         why={"rule": RULE_NAMES["break"], "evidence": ev})
        except Exception:
            pass
        # tasks due today, once, early afternoon
        if dt.hour == 14:
            due = [t for t in self.svc.storage.list_tasks() if t.get("due") == dt.date().isoformat()]
            if due:
                names = ", ".join(t["title"] for t in due[:3])
                self.add(f"Due today: {names}.", source="tasks", key=f"due:{dt.date()}",
                         why={"rule": "Tasks due today (the 2 PM check)",
                              "evidence": [f"Tasks: “{t['title']}”, due today" for t in due[:4]]})

    # ---- feedback -------------------------------------------------------------------------------------------
    def _load_stats(self) -> dict:
        try:
            return json.loads((self.svc.cfg.data_dir / "nudges.json").read_text())
        except (OSError, ValueError, AttributeError, KeyError):
            return {}

    def _save_stats(self) -> None:
        try:
            (self.svc.cfg.data_dir / "nudges.json").write_text(json.dumps(self.stats, indent=1))
        except (OSError, AttributeError, KeyError):
            pass

    def _quiet_source(self, source: str) -> bool:
        s = self.stats.get(source) or {}
        return s.get("dismissed", 0) >= 3 and s.get("dismissed", 0) > 2 * s.get("useful", 0)

    def feedback(self, key: str, useful: bool) -> dict:
        n = next((x for x in self.feed if x.key == key), None)
        if n is None:
            return {"ok": False, "error": "That nudge is gone."}
        s = self.stats.setdefault(n.source, {"useful": 0, "dismissed": 0})
        s["useful" if useful else "dismissed"] += 1
        self._save_stats()
        try:
            self.svc.storage.execute("INSERT INTO nudges_feedback(key, source, text, useful, ts) VALUES (?,?,?,?,?)",
                                     (n.key, n.source, n.text, int(useful), self.clock()))
        except Exception:
            pass
        if not useful:
            self.feed = [x for x in self.feed if x.key != key]
        return {"ok": True, "source": n.source, "quiet": self._quiet_source(n.source)}

    def recent_feedback(self, source: str | None = None, limit: int = 12) -> list[dict]:
        """What J thought of recent updates (newest first): {source, text, useful, ts}."""
        q = "SELECT source, text, useful, ts FROM nudges_feedback" + (" WHERE source = ?" if source else "") + \
            " ORDER BY ts DESC, rowid DESC LIMIT ?"
        try:
            rows = self.svc.storage.query(q, ((source, limit) if source else (limit,)))
        except Exception:
            return []
        return [{"source": r["source"], "text": r["text"], "useful": bool(r["useful"]), "ts": r["ts"]} for r in rows]

    def mute(self, minutes: float) -> dict:
        self._muted_until = self.clock() + max(0.0, minutes) * 60
        self.publish()
        return {"ok": True, "until": self._muted_until, "minutes": minutes}

    # ---- "why did you say that" ---------------------------------------------------------------------------
    def explain(self) -> dict:
        """The last thing said out loud (or, failing that, shown), and why."""
        n = self.last_spoken
        spoken = n is not None
        if n is None:
            n = next((x for x in self.feed if x.delivered), None)
        if n is None:
            return {"ok": False, "error": "I haven't brought anything up lately."}
        out = {"ok": True, "text": n.text, "source": n.source, "spoken": spoken, "why": n.why or make_why(None, n.source),
               "ago_s": max(0, int(self.clock() - (n.delivered or n.created)))}
        if self.svc.live and self.svc.live():
            out["live_note"] = "Live on stream: give the rule, not the evidence (it may be private)."
        return out

    # ---- skill surface -------------------------------------------------------------------------------------
    def status(self) -> dict:
        out = {"gate": self.gate(), "queue": [asdict(n) for n in self.queue[:20]],
               "feed": [asdict(n) for n in self.feed[:30]], "muted_until": self._muted_until,
               "quiet_sources": [k for k in self.stats if self._quiet_source(k)]}
        amb = self.svc.skills.get("ambient") if getattr(self.svc, "skills", None) is not None else None
        if amb is not None and getattr(amb, "last_status", None):
            out["ambient"] = amb.last_status
        return out

    def actions(self):
        return {"useful": lambda b: self.feedback(str(b.get("key")), True),
                "dismiss": lambda b: self.feedback(str(b.get("key")), False),
                "mute": lambda b: self.mute(float(b.get("minutes", 60))),
                "unmute": lambda b: self.mute(0),
                "clear": lambda b: self._clear()}

    def _clear(self) -> dict:
        self.feed = []
        return {"ok": True}

    def tools(self):
        return [
            Tool("what_did_i_miss", "Recent proactive updates (meetings, emails, reminders, breaks) the user may have "
                                    "missed, newest first.", obj(), self._missed),
            Tool("quiet_mode", "Hold Vesper's proactive updates (no speaking up) for some minutes; 0 turns it back on.",
                 obj({"minutes": {"type": "number"}}, ["minutes"]), self.mute, tier=1),
            Tool("explain_nudge", "Why Vesper brought something up on its own: the rule and the evidence behind the last "
                                  "update it said. Use for 'why did you say that', 'why are you telling me this'.",
                 obj(), self.explain),
        ]

    def _missed(self) -> dict:
        return {"ok": True, "updates": [{"text": n.text, "source": n.source, "when": n.delivered}
                                        for n in self.feed[:10]], "waiting": len(self.queue)}

    def route(self, t: str):
        if re.search(r"^what did i miss$|^(?:any )?(?:updates|notifications)$|^catch me up$", t):
            return Intent("tool", "what_did_i_miss")
        if re.fullmatch(r"why did you (?:say|tell me|mention|bring up|bring) (?:that|this)(?: up)?|why (?:are|were) you "
                        r"telling me (?:that|this)|what made you say that|why (?:that|this) (?:reminder|nudge|update)|"
                        r"why did you (?:remind|nudge) me(?: about that)?", t):
            return Intent("tool", "explain_nudge")
        m = re.search(r"^(?:quiet mode|do not disturb|don't disturb me|stop (?:interrupting|nudging) me|hold (?:my )?"
                      r"(?:notifications|updates))(?: for (\w+(?: \w+)?) (minutes?|hours?))?$", t)
        if m:
            from ..brain.router import number

            n = number(m.group(1)) if m.group(1) else None
            mins = (n or 1) * (60 if (m.group(2) or "hour").startswith("hour") else 1) if m.group(1) else 60
            return Intent("tool", "quiet_mode", {"minutes": mins})
        if re.search(r"^(?:you can )?(?:talk|interrupt) (?:to me )?again$|^(?:end|stop|turn off) quiet mode$", t):
            return Intent("tool", "quiet_mode", {"minutes": 0})
        return None

    def say(self, tool, args, result):
        if tool == "quiet_mode":
            m = args.get("minutes", 0)
            return "Okay, I'll speak up again." if not m else \
                f"Quiet for {int(m)} minutes. Urgent things still show on the HUD."
        if tool == "what_did_i_miss":
            ups = result.get("updates") or []
            if not ups:
                return "Nothing you missed."
            return " ".join(u["text"] for u in ups[:3]) + (f" Plus {len(ups) - 3} more on the HUD." if len(ups) > 3 else "")
        if tool == "explain_nudge":
            if not result.get("ok"):
                return result.get("error") or "I haven't brought anything up lately."
            why = result.get("why") or {}
            rule = (why.get("rule") or "a rule").rstrip(".")
            lead = "I said it" if result.get("spoken") else "The last update"
            if result.get("live_note"):
                return f"{lead} because of: {rule.lower()}. The details are on the HUD, under Updates."
            ev = [e.rstrip(".") for e in why.get("evidence") or []][:3]
            note = why.get("note")
            line = f"{lead} because of: {rule.lower()}."
            if ev:
                line += " I saw: " + "; ".join(ev) + "."
            if note:
                line += f" {note.rstrip('.')}."
            return line
        return None


def streak(segments: list[dict], now: float, gap_s: float = 300) -> float:
    """Seconds of activity up to now without a break longer than ``gap_s`` (idle counts as a break)."""
    segs = sorted((s for s in segments if s.get("category") != "idle"), key=lambda s: s["end"], reverse=True)
    if not segs or now - segs[0]["end"] > gap_s:
        return 0.0
    start = segs[0]["start"]
    for s in segs[1:]:
        if start - s["end"] > gap_s:
            break
        start = min(start, s["start"])
    return now - start


def in_quiet_hours(dt: datetime, spec: str) -> bool:
    try:
        a, b = (datetime.strptime(x.strip(), "%H:%M").time() for x in spec.split("-"))
    except (ValueError, AttributeError):
        return False
    t = dt.time()
    return (a <= t or t < b) if a > b else (a <= t < b)


def _clock(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%I:%M %p").lstrip("0").replace(":00", "")


__all__ = ["Nudges", "Nudge", "streak", "in_quiet_hours", "make_why", "ago_words", "timedelta"]
