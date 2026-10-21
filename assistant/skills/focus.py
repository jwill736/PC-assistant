"""Focus sessions: a timer, held interruptions, and one gentle word if you drift.

"start a 50 minute focus session on the Q3 deck", "focus mode", "focus for 25 minutes", "stop focus", "how much focus
time today". While a session runs, the nudges skill only lets urgent things through; if you sit on YouTube, Reddit,
X, TikTok, Instagram (or Discord, during work) for a minute, Vesper says it once, lightly ("That's YouTube. Back to the
Q3 deck?") and never again that session. Nothing is blocked. The end brings a summary (minutes, distractions, % on
task), a break suggestion, and today's total and streak. Sessions are kept in SQLite (focus_sessions).
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, time as dtime, timedelta

from ..brain.router import Intent, number
from ..brain.speech import duration
from ..brain.tools import Tool, obj
from ..integrations.activity import focus_sessions
from . import Skill

DISTRACTIONS = {"youtube": "YouTube", "reddit": "Reddit", "twitter": "Twitter", "x.com": "X", "tiktok": "TikTok",
                "instagram": "Instagram", "facebook": "Facebook", "discord": "Discord", "netflix": "Netflix"}
WORK_ONLY = ("discord",)  # a streamer lives in Discord; it only counts against a work session

SCHEMA = """CREATE TABLE IF NOT EXISTS focus_sessions (id INTEGER PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL,
    planned_min INTEGER NOT NULL, label TEXT NOT NULL DEFAULT '', profile TEXT NOT NULL DEFAULT 'work',
    distractions INTEGER NOT NULL DEFAULT 0, off_task_s REAL NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0)"""


def span_minutes(span: str) -> float | None:
    """'50 minutes', 'fifty minutes', 'an hour', '1.5 hours' -> minutes."""
    m = re.fullmatch(r"(.+?)[ -](minutes?|hours?)", span.strip())
    if not m:
        return None
    w = m.group(1)
    n = 1 if w in ("a", "an", "one") else 0.5 if w in ("half an", "half a") else \
        float(w) if re.fullmatch(r"\d+(?:\.\d+)?", w) else number(w)
    return n * (60 if m.group(2).startswith("hour") else 1) if n else None


def break_minutes(focused_min: float) -> int:
    return 5 if focused_min <= 30 else 10 if focused_min <= 60 else 15


class Focus(Skill):
    name, title = "focus", "Focus"
    poll_s = 10
    essential = True  # a focus session ends on time
    delay_s = 5

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        self.session: dict | None = None
        self.last: dict | None = None
        svc.storage.execute(SCHEMA)
        svc.bus.on(self._on_event)

    def start(self, runtime) -> None:
        self._gate()

    def _gate(self) -> None:
        """Hold non-urgent nudges while a session runs (skills/nudges.py asks ``focus()``)."""
        nud = self.svc.skills.get("nudges") if self.svc.skills is not None else None
        if nud is not None:
            nud.focus = self.active

    def active(self) -> bool:
        return self.session is not None

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.svc.tz)

    # ---- distractions ---------------------------------------------------------------------------------------
    def distraction(self, app: str, title: str, profile: str = "work") -> str | None:
        terms = self.setting("distractions") or list(DISTRACTIONS)
        hay = f"{app or ''} {title or ''}".lower()
        if hay.rstrip().endswith(" / x"):  # X's tab titles: "Home / X"
            return "X"
        for term in terms:
            term = str(term).lower()
            if term in hay and (profile == "work" or term not in WORK_ONLY):
                return DISTRACTIONS.get(term, term.title())
        return None

    def _on_event(self, ev: dict) -> None:
        if ev.get("type") == "activity_now" and self.session and isinstance(ev.get("data"), dict):
            seg = ev["data"]
            self.observe(seg.get("app", ""), seg.get("title", ""))

    def observe(self, app: str, title: str) -> None:
        s, now = self.session, self.clock()
        if s is None:
            return
        cur = s["current"]
        if cur.get("what"):
            s["off_task_s"] += now - cur["since"]
        what = self.distraction(app, title, s["profile"])
        if what and not cur.get("what"):
            s["distractions"].append({"what": what, "ts": now})
        s["current"] = {"what": what, "since": now}
        self.publish()

    def _off_task(self, now: float) -> float:
        s = self.session
        cur = s["current"]
        return s["off_task_s"] + (now - cur["since"] if cur.get("what") else 0)

    # ---- sessions -------------------------------------------------------------------------------------------
    def start_session(self, minutes: float = 0, label: str = "", profile: str = "") -> dict:
        if self.session:
            left = max(0, self.session["end"] - self.clock())
            return {"ok": False, "error": f"You're already focusing: {duration(left)} left. Say “stop focus” to end it."}
        minutes = float(minutes or self.setting("default_minutes", 50))
        if not 1 <= minutes <= 240:
            return {"ok": False, "error": "A focus session can be 1 to 240 minutes."}
        now = self.clock()
        label = (label or "").strip().rstrip(".")
        self.session = {"start": now, "end": now + minutes * 60, "minutes": minutes, "label": label,
                        "profile": profile or ("stream" if "stream" in label.lower() else "work"),
                        "distractions": [], "off_task_s": 0.0, "current": {"what": None, "since": now}, "nudged": False}
        self._gate()
        self.publish()
        return {"ok": True, "session": self._view(now)}

    def stop_session(self) -> dict:
        if not self.session:
            return {"ok": False, "error": "No focus session is running."}
        return self.finish(completed=False)

    def finish(self, completed: bool) -> dict:
        s, now = self.session, self.clock()
        end = min(now, s["end"])
        mins = (end - s["start"]) / 60
        off = min(self._off_task(end), end - s["start"])
        pct = round(100 * (1 - off / (end - s["start"]))) if end > s["start"] else 100
        self.session = None
        if mins >= 1:
            self.svc.storage.execute(
                "INSERT INTO focus_sessions(start,end,planned_min,label,profile,distractions,off_task_s,completed) "
                "VALUES (?,?,?,?,?,?,?,?)", (s["start"], end, int(s["minutes"]), s["label"], s["profile"],
                                             len(s["distractions"]), off, int(completed)))
        kinds = sorted({d["what"] for d in s["distractions"]})
        self.last = {"ok": True, "minutes": round(mins), "label": s["label"], "distractions": len(s["distractions"]),
                     "what": kinds, "on_task_pct": pct, "completed": completed, "break_min": break_minutes(mins),
                     "today": self.today(), "streak": self.streak(), "at": now}
        if completed:
            self.nudge(summary_line(self.last), priority="urgent", key=f"focus:done:{int(s['start'])}", expires_in=900)
        self.publish()
        return self.last

    def _view(self, now: float) -> dict:
        s = self.session
        return {"start": s["start"], "end": s["end"], "minutes": s["minutes"], "label": s["label"],
                "left_s": max(0, round(s["end"] - now)), "distractions": len(s["distractions"]),
                "drifting": s["current"].get("what"), "profile": s["profile"]}

    # ---- numbers --------------------------------------------------------------------------------------------
    def _day_rows(self, d: date) -> list[dict]:
        a = datetime.combine(d, dtime.min, tzinfo=self.svc.tz).timestamp()
        return self.svc.storage.query("SELECT * FROM focus_sessions WHERE start >= ? AND start < ?", (a, a + 86400))

    def today(self) -> dict:
        rows = self._day_rows(self.now().date())
        mins = sum(r["end"] - r["start"] for r in rows) / 60
        if self.session:
            mins += (self.clock() - self.session["start"]) / 60
        return {"minutes": round(mins), "sessions": len(rows) + (1 if self.session else 0),
                "distractions": sum(r["distractions"] for r in rows)}

    def streak(self) -> int:
        """Days in a row with at least one finished session (today counts once it has one)."""
        d, n = self.now().date(), 0
        if not any(r["completed"] for r in self._day_rows(d)):
            d -= timedelta(days=1)
        while n < 366 and any(r["completed"] for r in self._day_rows(d)):
            n, d = n + 1, d - timedelta(days=1)
        return n

    def deep_work_today(self) -> int:
        """Minutes of unbroken work the activity tracker saw today (0 when tracking is off)."""
        try:
            day0 = datetime.combine(self.now().date(), dtime.min, tzinfo=self.svc.tz).timestamp()
            return sum(s["minutes"] for s in focus_sessions(self.svc.activity.segments(day0, self.clock())))
        except Exception:
            return 0

    def stats(self) -> dict:
        out = {"ok": True, "today": self.today(), "streak": self.streak(), "deep_work_min": self.deep_work_today()}
        if self.session:
            out["active"] = self._view(self.clock())
        return out

    # ---- skill surface --------------------------------------------------------------------------------------
    def tick(self) -> None:
        s, now = self.session, self.clock()
        if s is None:
            return
        try:
            if now >= s["end"]:
                self.finish(completed=True)
                return
            cur = s["current"]
            grace = float(self.setting("drift_grace_s", 60))
            if cur.get("what") and not s["nudged"] and now - cur["since"] >= grace:
                s["nudged"] = True
                back = f"back to {s['label']}?" if s["label"] else "back to it?"
                self.notify(f"That's {cur['what']}. {back[0].upper()}{back[1:]}", level="info", speak=True,
                            key=f"focus:drift:{int(s['start'])}")
        except Exception as exc:
            self.notify(f"Focus: {type(exc).__name__}", level="warning")
        self.publish()

    def status(self) -> dict:
        now = self.clock()
        return {"active": self._view(now) if self.session else None, "today": self.today(), "streak": self.streak(),
                "last": self.last, "now": now, "default_minutes": int(self.setting("default_minutes", 50))}

    def actions(self):
        return {"start": lambda b: self.start_session(float(b.get("minutes") or 0), str(b.get("label") or "")),
                "stop": lambda b: self.stop_session()}

    def briefing(self):
        y = self.now().date() - timedelta(days=1)
        mins = round(sum(r["end"] - r["start"] for r in self._day_rows(y)) / 60)
        streak = self.streak()
        return {"yesterday_min": mins, "streak": streak} if mins or streak else None

    def tools(self):
        return [
            Tool("focus_start", "Start a focus session: minutes (default 50) and what it's for. Holds non-urgent updates "
                                "and gently flags distractions.",
                 obj({"minutes": {"type": "number"}, "label": {"type": "string"}}), self.start_session, tier=1),
            Tool("focus_stop", "End the current focus session and summarise it.", obj(), self.stop_session, tier=1),
            Tool("focus_stats", "Focus time today, sessions, streak, and time left in the current session.", obj(),
                 self.stats),
        ]

    def route(self, t: str):
        if re.fullmatch(r"(?:let'?s|time to) (?:focus|do (?:some )?deep work|get focused)", t):
            return Intent("tool", "focus_start", {"minutes": 0})
        # "a focus timer" is a focus session (automations leaves any timer with "focus" in it to us)
        m = re.fullmatch(r"(?:(?:let'?s |time to )?(start|begin|enter|do|go into|turn on|start a|begin a|do a|set)(?: an?| my)? )?"
                         r"(?:(.+?)[ -]minutes? )?(?:focus|focusing|deep work|focused work)( session| mode| block| sprint| time"
                         r"| timer)?"
                         r"(?: for (.+? (?:minutes?|hours?)))?(?: on (.+?))?(?: for (.+? (?:minutes?|hours?)))?", t)
        if m and (m.group(1) or m.group(3) or m.group(2) or m.group(4) or m.group(6)):
            span = m.group(4) or m.group(6) or (m.group(2) + " minutes" if m.group(2) else "")
            mins = span_minutes(span) if span else 0
            if mins is None:
                return None
            return Intent("tool", "focus_start", {"minutes": round(mins),
                                                  **({"label": m.group(5)} if m.group(5) else {})})
        if re.fullmatch(r"(?:stop|end|cancel|finish|quit|exit|turn off|i'?m done with)(?: the| my)? (?:focus|focusing|deep work)"
                        r"(?: session| mode| block| sprint| timer)?|stop focusing", t):
            return Intent("tool", "focus_stop")
        if re.fullmatch(r"how much (?:focus|deep work|focused)(?: time| work)?(?: did i (?:get|do|have)| have i (?:had|done|got))?"
                        r"(?: today)?|(?:my )?focus (?:stats|streak|time today)|how(?:'s| is) my focus(?: going)?(?: today)?|"
                        r"how (?:long|much time) (?:is )?left (?:in|on) (?:my |the )?focus(?: session)?", t):
            return Intent("tool", "focus_stats")
        return None

    def say(self, tool, args, result):
        live = bool(self.svc.live and self.svc.live())
        if tool == "focus_start":
            s = result["session"]
            what = f" on {s['label']}" if s["label"] and not live else ""
            return f"Focus on: {duration(s['minutes'] * 60)}{what}. I'll hold everything but urgent stuff."
        if tool == "focus_stop":
            return summary_line(result, live)
        if tool == "focus_stats":
            td = result["today"]
            out = (f"{duration(td['minutes'] * 60)} of focus today over {td['sessions']} session{'s' if td['sessions'] != 1 else ''}"
                   if td["minutes"] else "No focus sessions yet today")
            if result["streak"] > 1:
                out += f", a {result['streak']}-day streak"
            out += "."
            if result.get("active"):
                out += f" {duration(result['active']['left_s'])} left on this one."
            elif result.get("deep_work_min"):
                out += f" I also saw {duration(result['deep_work_min'] * 60)} of unbroken work."
            return out
        return None


def summary_line(r: dict, live: bool = False) -> str:
    what = f" on {r['label']}" if r.get("label") and not live else ""
    d = r["distractions"]
    out = (f"Focus {'done' if r['completed'] else 'ended'}: {duration(r['minutes'] * 60)}{what}, "
           + (f"{d} distraction{'s' if d != 1 else ''}, {r['on_task_pct']}% on task." if d else "no distractions."))
    return out + f" Take {r['break_min']}: stand up, water, look away from the screen."
