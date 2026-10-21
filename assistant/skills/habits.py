"""Habits: small daily and weekly targets, streaks, and one quiet evening reminder.

"track a habit: drink water 8 times a day", "workout 3 times a week", "read daily", then "I drank water", "drank 2
glasses of water", "did my workout", "I read", "how are my habits", "stop tracking reading". A day (or week) counts once
the target is met; the streak is how many in a row. At 8:30 PM, unfinished daily habits go to the HUD feed (spoken
only if ``skills.habits.nudge_priority`` is "normal"). Kept in SQLite (habits, habit_log).
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from ..brain.router import Intent, number
from ..brain.tools import Tool, obj
from . import Skill

SCHEMA = ["""CREATE TABLE IF NOT EXISTS habits (id INTEGER PRIMARY KEY, name TEXT NOT NULL, target INTEGER NOT NULL
    DEFAULT 1, period TEXT NOT NULL DEFAULT 'day', created REAL NOT NULL, archived INTEGER NOT NULL DEFAULT 0)""",
          """CREATE TABLE IF NOT EXISTS habit_log (id INTEGER PRIMARY KEY, habit_id INTEGER NOT NULL, ts REAL NOT NULL,
    day TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 1)"""]

IRREGULAR = {"drank": "drink", "drunk": "drink", "did": "do", "done": "do", "ran": "run", "went": "go", "gone": "go",
             "ate": "eat", "eaten": "eat", "wrote": "write", "written": "write", "took": "take", "slept": "sleep",
             "swam": "swim", "rode": "ride", "made": "make", "had": "have", "got": "get", "read": "read"}
STOP = {"i", "i've", "ive", "just", "already", "my", "a", "an", "the", "some", "today", "do", "have", "go", "take",
        "make", "log", "logg", "finish", "complet", "again", "another", "one", "more", "glass", "cup", "bottle", "time",
        "of", "to", "for", "it", "get", "and", "so", "all", "mark", "check", "tick", "off", "as", "a", "on", "in", "with",
        "daily", "every", "day", "week", "habit", "x", "now", "too", "also", "page", "minut"}
COUNTS = {"once": 1, "twice": 2, "thrice": 3}
PREFIX = re.compile(r"^(?:(?:can you |please )?(?:track|add|start tracking|new|create)(?: a| my)?(?: new)? habit(?: to)?|"
                    r"(?:start )?track(?:ing)?|habit|i want to)\s*(?:of\s+)?")


def stem(w: str) -> str:
    w = IRREGULAR.get(w, w)
    for suf, keep in (("ies", "y"), ("ied", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3 and not w.endswith("ss"):
            w = w[: -len(suf)] + keep
            if suf in ("ing", "ed") and len(w) > 3 and w[-1] == w[-2] and w[-1] not in "ls":
                w = w[:-1]  # jogged -> jog, running -> run
            break
    return w.rstrip("e") if len(w) > 4 else w


def content(text: str) -> set[str]:
    return {w for w in (stem(x) for x in re.findall(r"[a-z']+", text.lower())) if w not in STOP and len(w) > 1}


def keys(text: str) -> set[str]:
    """Content stems of a phrase, plus joined neighbours ('worked out' -> 'workout')."""
    ws = [stem(w) for w in re.findall(r"[a-z']+", text.lower())]
    return content(text) | {a + b for a, b in zip(ws, ws[1:]) if a not in STOP or b not in STOP}


def parse_habit(text: str) -> tuple[str, int, str] | None:
    """'drink water 8 times a day' -> ('drink water', 8, 'day'); 'read daily' -> ('read', 1, 'day')."""
    t = PREFIX.sub("", text.lower().strip()).strip(" :.-")
    t = re.sub(r"^to ", "", t)
    m = re.search(r"\s+(?:(\d+)\s*x|(\S+?)\s+times?|(once|twice|thrice))\s+(?:a|per|each|every)\s+(day|week)$", t)
    if m:
        raw = m.group(1) or m.group(2) or m.group(3)
        n = COUNTS.get(raw) or (int(raw) if raw.isdigit() else number(raw))
        name, target, period = t[: m.start()], n, m.group(4)
    elif re.search(r"\s+(?:daily|every ?day|each day|a day|every (?:morning|night|evening))$", t):
        name, target, period = re.sub(r"\s+(?:daily|every ?day|each day|a day|every \w+)$", "", t), 1, "day"
    elif re.search(r"\s+(?:weekly|every week|once a week|a week)$", t):
        name, target, period = re.sub(r"\s+(?:weekly|every week|once a week|a week)$", "", t), 1, "week"
    else:
        name, target, period = t, 1, "day"
    name = name.strip(" ,.")
    if not name or not target or not 1 <= target <= 50 or not content(name):
        return None
    return name, int(target), period


def week_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


class Habits(Skill):
    name, title = "habits", "Habits"
    poll_s = 300
    game_poll_s = 1200
    delay_s = 40

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        for sql in SCHEMA:
            svc.storage.execute(sql)
        self._nudged = ""

    def today(self) -> date:
        return datetime.fromtimestamp(self.clock(), self.svc.tz).date()

    def _habits(self) -> list[dict]:
        return self.svc.storage.query("SELECT * FROM habits WHERE archived=0 ORDER BY period, created")

    # ---- changes ----------------------------------------------------------------------------------------
    def add(self, name: str = "", target: int = 1, period: str = "day", text: str = "") -> dict:
        if text:
            parsed = parse_habit(text)
            if not parsed:
                return {"ok": False, "error": "Try “track a habit: drink water 8 times a day” or “read daily”."}
            name, target, period = parsed
        name, period = (name or "").strip().lower(), "week" if str(period).startswith("week") else "day"
        if not name or not 1 <= int(target or 0) <= 50:
            return {"ok": False, "error": "What habit, and how often?"}
        old = self.match(name)
        if old and old["name"] == name:
            self.svc.storage.execute("UPDATE habits SET target=?, period=? WHERE id=?", (int(target), period, old["id"]))
        else:
            self.svc.storage.execute("INSERT INTO habits(name,target,period,created) VALUES (?,?,?,?)",
                                     (name, int(target), period, self.clock()))
        self.publish()
        return {"ok": True, "habit": self.view(self.match(name))}

    def match(self, text: str) -> dict | None:
        """The habit a phrase is about ('i drank 2 glasses of water' -> drink water), or None."""
        said, best, score = keys(text), None, 0.0
        for h in self._habits():
            hit = len(said & keys(h["name"])) / max(1, len(content(h["name"])))
            if hit > score:
                best, score = h, hit
        return best if score > 0 else None

    def log(self, name: str = "", count: int = 1, id: int | None = None) -> dict:
        h = next((x for x in self._habits() if x["id"] == id), None) if id else self.match(name)
        if h is None:
            return {"ok": False, "error": f"I'm not tracking {name or 'that'}. Say “track a habit: …” first."}
        n = max(1, min(int(count or 1), 50)) if h["target"] > 1 else 1
        self.svc.storage.execute("INSERT INTO habit_log(habit_id,ts,day,n) VALUES (?,?,?,?)",
                                 (h["id"], self.clock(), self.today().isoformat(), n))
        self.publish()
        return {"ok": True, "habit": self.view(h), "added": n}

    def undo(self, id: int) -> dict:
        rows = self.svc.storage.query("SELECT id FROM habit_log WHERE habit_id=? AND day=? ORDER BY ts DESC LIMIT 1",
                                      (int(id), self.today().isoformat()))
        if not rows:
            return {"ok": False, "error": "Nothing logged today to undo."}
        self.svc.storage.execute("DELETE FROM habit_log WHERE id=?", (rows[0]["id"],))
        return {"ok": True}

    def remove(self, name: str = "", id: int | None = None) -> dict:
        h = next((x for x in self._habits() if x["id"] == id), None) if id else self.match(name)
        if h is None:
            return {"ok": False, "error": f"I'm not tracking {name or 'that'}."}
        self.svc.storage.execute("UPDATE habits SET archived=1 WHERE id=?", (h["id"],))
        self.publish()
        return {"ok": True, "removed": h["name"]}

    # ---- numbers ----------------------------------------------------------------------------------------
    def _counts(self, since: date) -> dict[int, dict[str, int]]:
        out: dict[int, dict[str, int]] = defaultdict(dict)
        for r in self.svc.storage.query("SELECT habit_id, day, SUM(n) AS n FROM habit_log WHERE day >= ? "
                                        "GROUP BY habit_id, day", (since.isoformat(),)):
            out[r["habit_id"]][r["day"]] = r["n"]
        return out

    def view(self, h: dict, counts: dict[str, int] | None = None) -> dict:
        today = self.today()
        if counts is None:
            counts = self._counts(today - timedelta(days=400)).get(h["id"], {})
        days = [today - timedelta(days=i) for i in range(6, -1, -1)]
        t = h["target"]
        if h["period"] == "day":
            done = lambda d: counts.get(d.isoformat(), 0) >= t  # noqa: E731
            now_n = counts.get(today.isoformat(), 0)
            step, cur = timedelta(days=1), today
        else:
            def done(d):
                w = week_of(d)
                return sum(counts.get((w + timedelta(days=i)).isoformat(), 0) for i in range(7)) >= t
            cur, step = week_of(today), timedelta(days=7)
            now_n = sum(counts.get((cur + timedelta(days=i)).isoformat(), 0) for i in range(7))
        streak, d = 0, cur if done(cur) else cur - step
        while streak < 400 and done(d):
            streak, d = streak + 1, d - step
        strip = [{"day": d.isoformat(), "n": counts.get(d.isoformat(), 0),
                  "frac": min(1.0, counts.get(d.isoformat(), 0) / (t if h["period"] == "day" else 1))} for d in days]
        return {"id": h["id"], "name": h["name"], "target": t, "period": h["period"], "count": now_n,
                "done": now_n >= t, "streak": streak, "unit": "day" if h["period"] == "day" else "week", "days": strip}

    def views(self) -> list[dict]:
        counts = self._counts(self.today() - timedelta(days=400))
        return [self.view(h, counts.get(h["id"], {})) for h in self._habits()]

    def report(self) -> dict:
        vs = self.views()
        return {"ok": True, "habits": vs, "open_today": [v for v in vs if v["period"] == "day" and not v["done"]]}

    # ---- skill surface ----------------------------------------------------------------------------------
    def tick(self) -> None:
        try:
            now = datetime.fromtimestamp(self.clock(), self.svc.tz)
            day = now.date().isoformat()
            if now.strftime("%H:%M") >= str(self.setting("evening", "20:30")) and self._nudged != day:
                self._nudged = day
                left = self.report()["open_today"]
                if left:
                    self.nudge("Still open today: " + ", ".join(_progress(v) for v in left[:4]) + ".",
                               priority=str(self.setting("nudge_priority", "low")), key=f"habits:{day}",
                               expires_in=3 * 3600)
        except Exception as exc:
            self.notify(f"Habits: {type(exc).__name__}", level="warning")
        self.publish()

    def status(self) -> dict:
        today = self.today()
        return {"habits": self.views(), "today": today.isoformat(),
                "days": [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]}

    def actions(self):
        return {"log": lambda b: self.log(id=int(b.get("id") or 0), count=int(b.get("count") or 1)),
                "undo": lambda b: self.undo(int(b.get("id") or 0)),
                "add": lambda b: self.add(text=str(b.get("text") or "")),
                "remove": lambda b: self.remove(id=int(b.get("id") or 0))}

    def briefing(self):
        vs = self.views()
        if not vs:
            return None
        best = max(vs, key=lambda v: v["streak"])
        return {"daily": sum(1 for v in vs if v["period"] == "day"), "weekly": sum(1 for v in vs if v["period"] == "week"),
                "best_streak": {"name": best["name"], "streak": best["streak"], "unit": best["unit"]} if best["streak"] else None}

    def tools(self):
        return [
            Tool("habit_add", "Start tracking a habit: name, target (times), period (day or week).",
                 obj({"name": {"type": "string"}, "target": {"type": "integer"}, "period": {"type": "string",
                                                                                            "enum": ["day", "week"]}},
                     ["name"]), self.add, tier=1),
            Tool("habit_log", "Log that the user did a habit (count for things like glasses of water).",
                 obj({"name": {"type": "string"}, "count": {"type": "integer"}}, ["name"]), self.log, tier=1),
            Tool("habit_status", "How the user's habits are going: today's progress, this week, streaks.", obj(), self.report),
            Tool("habit_remove", "Stop tracking a habit.", obj({"name": {"type": "string"}}, ["name"]), self.remove, tier=1),
        ]

    def route(self, t: str):
        if re.match(r"(?:(?:can you |please )?(?:track|add|start tracking|new|create)(?: a| my)?(?: new)? habit\b)", t):
            parsed = parse_habit(t)
            if parsed:
                return Intent("tool", "habit_add", dict(zip(("name", "target", "period"), parsed)))
        if re.fullmatch(r"how (?:are|r) my habits(?: going| doing)?(?: today| this week)?|(?:my |show my )?habits(?: today)?|"
                        r"habit (?:check|status|progress)|how am i doing on (?:my )?habits", t):
            return Intent("tool", "habit_status")
        m = re.fullmatch(r"(?:stop tracking|delete (?:the )?habit|remove (?:the )?habit|drop (?:the )?habit) (.+)", t)
        if m:
            return Intent("tool", "habit_remove", {"name": m.group(1)})
        m = re.fullmatch(r"(?:i |i've |i have |i just |just |i already )(?:just |already )?(\w+(?: out| up)?) ?(.*)|"
                         r"(?:did|done|finished|completed|log|logged|mark|check off|tick off) (?:my |the )?(.+?)(?: (?:as )?done)?", t)
        verb = (m.group(1) or "").split(" ")[0] if m else ""
        past = m and (m.group(3) or verb in IRREGULAR or (verb.endswith("ed") and verb not in {"need", "feed", "speed"}))
        if past and len(t.split()) <= 9 and self._habits():
            h = self.match(t)
            if h:
                n = re.search(r"\b(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten|twice)\b", t)
                count = (2 if n.group(1) == "twice" else int(n.group(1)) if n.group(1).isdigit() else number(n.group(1))) if n else 1
                return Intent("tool", "habit_log", {"name": h["name"], "count": count or 1})
        return None

    def say(self, tool, args, result):
        if tool == "habit_add":
            v = result["habit"]
            how = "daily" if v["target"] == 1 and v["period"] == "day" else \
                f"{_times(v['target'])} a {v['period']}"
            return f"Tracking {v['name']}, {how}."
        if tool == "habit_log":
            v = result["habit"]
            out = _progress(v).capitalize()
            if v["done"] and v["streak"] > 1:
                out += f". {v['streak']}-{v['unit']} streak"
            return out + "."
        if tool == "habit_remove":
            return f"Stopped tracking {result['removed']}."
        if tool == "habit_status":
            vs = result["habits"]
            if not vs:
                return "No habits yet. Say “track a habit: read daily”."
            daily = [v for v in vs if v["period"] == "day"]
            parts = [f"{sum(v['done'] for v in daily)} of {len(daily)} daily habits done"] if daily else []
            if result["open_today"]:
                parts.append("still open: " + ", ".join(_progress(v) for v in result["open_today"][:3]))
            parts += [_progress(v) for v in vs if v["period"] == "week"][:2]
            best = max(vs, key=lambda v: v["streak"])
            if best["streak"] > 1:
                parts.append(f"best streak {best['name']}, {best['streak']} {best['unit']}s")
            return "; ".join(parts).capitalize() + "."
        return None


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def _progress(v: dict) -> str:
    if v["target"] == 1 and v["period"] == "day":
        return f"{v['name']} {'done' if v['done'] else 'not yet'}"
    return f"{v['name']} {v['count']} of {v['target']}" + (" this week" if v["period"] == "week" else "")
