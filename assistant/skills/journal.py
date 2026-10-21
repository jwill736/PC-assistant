"""Journal: say it, it's kept; mood and topics noted; private by default.

"journal: shipped the deck, felt great", "dear diary …", "end of day journal" (Vesper asks "How did today go?" and
keeps your next answer), "what did I journal about last week", "search my journal for the stream", "how have I been
feeling". Each entry gets a mood from -2 to 2 and a few tags (the model when there is one, a word list otherwise).
Entries live in SQLite (journal_entries), apart from notes and tasks (the conversation log still records what was
said, like any command). While you're live, nothing from an entry is spoken or shown on the HUD: only counts. At 9 PM,
if there's no entry yet, one quiet nudge.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill

SCHEMA = """CREATE TABLE IF NOT EXISTS journal_entries (id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL,
    text TEXT NOT NULL, mood INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL DEFAULT 'voice')"""
MOOD_SCHEMA = {"type": "object", "properties": {
    "mood": {"type": "integer", "minimum": -2, "maximum": 2, "description": "-2 awful, 0 neutral, 2 great"},
    "tags": {"type": "array", "items": {"type": "string"}, "description": "1-4 short lower-case topics"}},
    "required": ["mood", "tags"]}

POS = {"great": 2, "amazing": 2, "awesome": 2, "fantastic": 2, "excellent": 2, "happy": 2, "proud": 2, "excited": 2,
       "hyped": 2, "love": 1, "loved": 1, "good": 1, "nice": 1, "productive": 1, "fun": 1, "calm": 1, "relaxed": 1,
       "grateful": 1, "thankful": 1, "win": 1, "won": 1, "shipped": 1, "accomplished": 1, "energized": 1, "rested": 1,
       "better": 1, "smooth": 1, "chill": 1, "solid": 1, "peaceful": 1}
NEG = {"exhausted": -2, "drained": -2, "sad": -2, "angry": -2, "awful": -2, "terrible": -2, "horrible": -2,
       "overwhelmed": -2, "upset": -2, "depressed": -2, "miserable": -2, "tired": -1, "stressed": -1, "stressful": -1,
       "anxious": -1, "frustrated": -1, "frustrating": -1, "bad": -1, "annoyed": -1, "sick": -1, "lonely": -1,
       "worried": -1, "burned": -1, "burnt": -1, "rough": -1, "meh": -1, "bored": -1, "behind": -1, "stuck": -1}
NOT = {"not", "no", "never", "isn't", "wasn't", "didn't", "don't", "hardly"}
MORE = {"really", "very", "so", "super", "extremely", "incredibly"}
TOPICS = {"work": {"work", "meeting", "meetings", "boss", "deck", "client", "deadline", "office", "job", "email", "project"},
          "stream": {"stream", "streamed", "streaming", "twitch", "viewers", "chat", "raid", "obs", "vod", "subs"},
          "health": {"gym", "workout", "run", "ran", "walk", "sick", "doctor", "ate", "diet", "water", "headache"},
          "sleep": {"sleep", "slept", "tired", "nap", "insomnia", "rested"},
          "family": {"family", "mom", "dad", "kids", "son", "daughter", "wife", "husband", "partner", "brother", "sister"},
          "friends": {"friend", "friends", "hung", "party", "dinner"},
          "money": {"money", "bills", "paid", "budget", "rent", "sponsor", "income"},
          "creative": {"edit", "editing", "video", "music", "art", "design", "wrote", "writing"}}
QUESTION = "How did today go?"
WHEN = r"(today|yesterday|this week|last week|this month|last month|lately|recently)"


def mood_words(text: str) -> tuple[int, list[str]]:
    """A mood from -2 to 2 and topic tags, from words alone."""
    ws = re.findall(r"[a-z']+", text.lower())
    score = 0.0
    for i, w in enumerate(ws):
        v = POS.get(w) or NEG.get(w)
        if not v:
            continue
        prev = ws[max(0, i - 2): i]
        v *= -0.5 if any(p in NOT for p in prev) else (1.5 if any(p in MORE for p in prev) else 1)
        score += v
    mood = 0 if abs(score) < 0.75 else (2 if score >= 2 else 1 if score > 0 else -2 if score <= -2 else -1)
    tags = [k for k, vocab in TOPICS.items() if vocab & set(ws)]
    return mood, tags[:4]


def window(when: str, today: date) -> tuple[date, date, str]:
    w = (when or "").lower().strip()
    mon = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    if w == "today":
        return today, today, "today"
    if w == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1), "yesterday"
    if w == "this week":
        return mon, today, "this week"
    if w == "last week":
        return mon - timedelta(days=7), mon - timedelta(days=1), "last week"
    if w == "this month":
        return first, today, "this month"
    if w == "last month":
        prev = first - timedelta(days=1)
        return prev.replace(day=1), prev, "last month"
    return today - timedelta(days=6), today, "the last 7 days"


class Journal(Skill):
    name, title = "journal", "Journal"
    poll_s = 60
    game_poll_s = 600
    delay_s = 50

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        svc.storage.execute(SCHEMA)
        self._awaiting = 0.0  # after "end of day journal": the next thing said is the entry
        self._nudged = ""
        self._was_live = False

    def _live(self) -> bool:
        return bool(self.svc.live and self.svc.live())

    def today(self) -> date:
        return datetime.fromtimestamp(self.clock(), self.svc.tz).date()

    # ---- writing --------------------------------------------------------------------------------------
    def add(self, text: str, source: str = "voice") -> dict:
        text = re.sub(r"\s+", " ", (text or "")).strip()
        if len(text) < 3:
            return {"ok": False, "error": "Say “journal:” and then the entry."}
        text = text[0].upper() + text[1:]
        mood, tags = mood_words(text)
        how = "words"
        got = self.svc.think("Rate the writer's mood in this private journal entry from -2 (awful) to 2 (great) and give "
                             "1-4 short topic tags.\n\nENTRY:\n" + text, schema=MOOD_SCHEMA)
        if isinstance(got, dict) and isinstance(got.get("mood"), (int, float)):
            mood = max(-2, min(2, int(round(got["mood"]))))
            tags = [str(t).lower().strip()[:24] for t in (got.get("tags") or []) if str(t).strip()][:4] or tags
            how = "model"
        cur = self.svc.storage.execute(
            "INSERT INTO journal_entries(ts,day,text,mood,tags,source) VALUES (?,?,?,?,?,?)",
            (self.clock(), self.today().isoformat(), text, mood, json.dumps(tags), source))
        self._awaiting = 0.0
        self.publish()
        return {"ok": True, "id": cur.lastrowid, "mood": mood, "tags": tags, "mood_by": how,
                "today": len(self._rows(self.today(), self.today()))}

    def prompt(self) -> dict:
        self._awaiting = self.clock() + 120
        return {"ok": True, "question": QUESTION}

    # ---- reading --------------------------------------------------------------------------------------
    def _rows(self, a: date, b: date) -> list[dict]:
        rows = self.svc.storage.query("SELECT * FROM journal_entries WHERE day >= ? AND day <= ? ORDER BY ts DESC, id DESC",
                                      (a.isoformat(), b.isoformat()))
        for r in rows:
            r["tags"] = json.loads(r["tags"] or "[]")
        return rows

    def search(self, query: str = "", when: str = "") -> dict:
        a, b, label = window(when, self.today())
        if query and not when:
            a = b - timedelta(days=365)
            label = "this past year"
        rows = self._rows(a, b)
        terms = {w for w in re.findall(r"[a-z']+", (query or "").lower()) if len(w) > 2 and w not in {"the", "and", "my"}}
        if terms:
            rows = [r for r in rows if terms & set(re.findall(r"[a-z']+", r["text"].lower()) + r["tags"])]
        return {"ok": True, "when": label, "query": query, "count": len(rows), "entries": rows[:20]}

    def mood(self, when: str = "") -> dict:
        a, b, label = window(when, self.today())
        rows = self._rows(a, b)
        by_day: dict[str, list[int]] = defaultdict(list)
        for r in rows:
            by_day[r["day"]].append(r["mood"])
        days = [{"day": d, "mood": round(sum(v) / len(v), 2), "n": len(v)} for d, v in sorted(by_day.items())]
        tags = Counter(t for r in rows for t in r["tags"]).most_common(3)
        avg = round(sum(r["mood"] for r in rows) / len(rows), 2) if rows else None
        best = max(days, key=lambda d: d["mood"]) if days else None
        worst = min(days, key=lambda d: d["mood"]) if days else None
        return {"ok": True, "when": label, "count": len(rows), "average": avg, "days": days,
                "tags": [t for t, _ in tags], "best": best, "worst": worst}

    def week(self) -> list[dict]:
        today = self.today()
        m = {d["day"]: d for d in self.mood("lately")["days"]}
        return [{"day": (today - timedelta(days=i)).isoformat(), **({k: m[(today - timedelta(days=i)).isoformat()][k]
                 for k in ("mood", "n")} if (today - timedelta(days=i)).isoformat() in m else {"mood": None, "n": 0})}
                for i in range(6, -1, -1)]

    # ---- skill surface --------------------------------------------------------------------------------
    def tick(self) -> None:
        try:
            now = datetime.fromtimestamp(self.clock(), self.svc.tz)
            day = now.date().isoformat()
            if (self.setting("evening_prompt", True) and now.strftime("%H:%M") >= str(self.setting("prompt_at", "21:00"))
                    and self._nudged != day and not self._rows(now.date(), now.date())):
                self._nudged = day
                self.nudge("How did today go? Say “journal” and tell me, or type it on the HUD.", key=f"journal:{day}",
                           expires_in=2 * 3600, action={"label": "Journal", "command": "end of day journal"})
        except Exception as exc:
            self.notify(f"Journal: {type(exc).__name__}", level="warning")
        live = self._live()
        if live != self._was_live or not self.last_status:
            self._was_live = live
            self.publish()

    def status(self) -> dict:
        live = self._live()
        recent = self._rows(self.today() - timedelta(days=30), self.today())[:12]
        mw = self.mood("this week")
        return {"live": live, "week": self.week(), "count_week": mw["count"], "average_week": mw["average"],
                "tags_week": [] if live else mw["tags"], "awaiting": self._awaiting > self.clock(),
                "entries": [{"id": r["id"], "ts": r["ts"], "day": r["day"], "mood": r["mood"],
                             **({} if live else {"text": r["text"], "tags": r["tags"]})} for r in recent]}

    def actions(self):
        return {"add": lambda b: self.add(str(b.get("text") or ""), source="hud"),
                "delete": lambda b: self._delete(int(b.get("id") or 0))}

    def _delete(self, id: int) -> dict:
        self.svc.storage.execute("DELETE FROM journal_entries WHERE id=?", (id,))
        return {"ok": True}

    def briefing(self):
        n = self.mood("this week")["count"]
        return {"entries_this_week": n} if n else None  # counts only: the briefing may be read out

    def tools(self):
        return [
            Tool("journal_add", "Save a private journal entry in the user's words.", obj({"text": {"type": "string"}}, ["text"]),
                 self.add, tier=1),
            Tool("journal_prompt", "Start an end-of-day journal entry: ask how the day went; the next reply is saved.",
                 obj(), self.prompt),
            Tool("journal_search", "Find the user's journal entries by topic and/or time (when: today, yesterday, this "
                                   "week, last week, this month, last month, lately). Private: never read out on stream.",
                 obj({"query": {"type": "string"}, "when": {"type": "string"}}), self.search),
            Tool("journal_mood", "How the user has been feeling, from journal moods over a time (when: see journal_search).",
                 obj({"when": {"type": "string"}}), self.mood),
        ]

    def route(self, t: str):
        if self._awaiting and self.clock() < self._awaiting and len(t.split()) >= 3:
            return Intent("tool", "journal_add", {"text": t})
        if re.fullmatch(r"(?:(?:end of (?:the )?day|evening|nightly|daily) journal|let'?s journal|journal time|"
                        r"start (?:a |my )?journal(?: entry)?|journal|dear diary|time to journal)", t):
            return Intent("tool", "journal_prompt")
        m = re.fullmatch(r"how (?:have|has) (?:i|my mood) been(?: feeling)?(?: " + WHEN + r")?|"
                         r"how'?s my mood(?: been)?(?: " + WHEN + r")?|(?:my )?mood (?:trend|lately|this week|last week)|"
                         r"how (?:was|did) i (?:feel|feeling)(?: " + WHEN + r")?", t)
        if m:
            when = next((g for g in m.groups() if g), "") or next((w for w in ("this week", "last week") if w in t), "")
            return Intent("tool", "journal_mood", {"when": when})
        m = re.fullmatch(r"what did i (?:journal|write in my (?:journal|diary))(?: about (.+?))?(?: " + WHEN + r")?|"
                         r"(?:show|read|open)(?: me)? my (?:journal|diary)(?: entries)?(?: (?:from|for))?(?: " + WHEN + r")?|"
                         r"(?:search|find) (?:in )?my (?:journal|diary)(?: for| about)? (.+?)(?: " + WHEN + r")?|"
                         r"journal entries(?: (?:from|for|about))?(?: " + WHEN + r")?", t)
        if m:
            g = m.groups()
            query, when = g[0] or g[3] or "", g[1] or g[2] or g[4] or g[5] or ""
            tail = re.search(r"(?:^| )(?:from |in |during )?" + WHEN + "$", query)
            if tail:
                query, when = query[: tail.start()].strip(), when or tail.group(1)
            return Intent("tool", "journal_search", {"query": query, "when": when})
        m = re.fullmatch(r"(?:journal(?: entry)?|dear diary|diary|add to (?:my )?journal|(?:write|put) (?:this )?in (?:my )?"
                         r"(?:journal|diary)|note for my journal) (.{3,})", t)
        if m:
            return Intent("tool", "journal_add", {"text": m.group(1)})
        return None

    def say(self, tool, args, result):
        live = self._live()
        if tool == "journal_prompt":
            return result["question"]
        if tool == "journal_add":
            if live or result["mood"] == 0:
                return "Saved."
            return "Saved. " + ("Sounds like a good one." if result["mood"] > 0 else "Sounds like a rough one. Go easy on yourself.")
        n = result["count"]
        counted = f"{n} entr{'y' if n == 1 else 'ies'} from {result['when']}"
        if tool == "journal_search":
            if not n:
                return f"No journal entries from {result['when']}" + (f" about {result['query']}." if result["query"] else ".")
            if live:
                return f"{counted}. They're private, so they're on the HUD after the stream."
            first = result["entries"][:2]
            lines = [f"{datetime.fromisoformat(e['day']).strftime('%A')}: {' '.join(e['text'].split()[:24])}" for e in first]
            return f"{counted}. " + ". ".join(lines) + "." + (f" And {n - 2} more on the HUD." if n > 2 else "")
        if tool == "journal_mood":
            if not n:
                return f"No journal entries from {result['when']}. Say “journal” and tell me how it's going."
            if live:
                return f"{counted}. The mood details are private; they're on the HUD after the stream."
            avg = result["average"]
            word = "great" if avg >= 1.2 else "good" if avg >= 0.4 else "mixed" if avg > -0.4 else "rough" if avg > -1.2 else "hard"
            out = f"Mostly {word} {result['when']}, over {counted.split(' from ')[0]}"
            if result["best"] and len(result["days"]) > 1:
                out += f"; best on {datetime.fromisoformat(result['best']['day']).strftime('%A')}"
            if result["tags"]:
                out += f". {result['tags'][0].capitalize()} came up most"
            return out + "."
        return None
