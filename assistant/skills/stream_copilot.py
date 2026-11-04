"""Stream co-pilot: a second pair of eyes on the stream, on the HUD only (nothing viewers write is said aloud).

- Sessions: going live and offline (``svc.live()``) open and close one: viewers, follows, subs, raids, bits, chat
  pace, highlight moments and stream-health incidents, kept in SQLite.
- Chat: questions aimed at you that are still open (saying the asker's name or the topic out loud answers them),
  a mood gauge from emotes and words, first-time chatters and regulars to greet.
- After: a recap with VOD chapters ("00:12:30 Clutch round") and post drafts for X and Discord, and how it compares
  with your last 5 streams (viewers, follows, chat an hour, length, category), with what went with your better streams
  (game, start time, length) once there are 5 to compare.
- YouTube Live chat (skills/stream_youtube.py) comes through the same feed and counts like Twitch chat.
- Before: 30 minutes ahead of a "stream" calendar event, the plan from its title and the pre-stream check.
- Live in a game that isn't the Twitch category for a minute: a HUD card (never spoken) "Switch category to Hades II?";
  its Yes is your yes (the change runs as confirmed on the HUD). Going live with a stream block on the calendar: a
  card offers the block's title ("Use title 'Ranked grind + Q&A'?").
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter, deque
from datetime import datetime

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from ..integrations.desktop import normalize_app
from . import Skill
from ._stream import Cards, run_on_click, stream_event_now
from .nudges import fullscreen_state

log = logging.getLogger(__name__)

POS = {"pog", "pogchamp", "pogu", "poggers", "w", "lul", "lol", "lmao", "kekw", "kek", "omegalul", "hype", "gg", "clutch",
       "insane", "nice", "sheesh", "shesh", "goat", "love", "<3", "based", "clean", "huge", "wow", "epic", "banger", "letsgo"}
NEG = {"l", "boring", "lag", "laggy", "lagging", "buffering", "trash", "cringe", "yikes", "residentsleeper", "notlikethis",
       "biblethump", "mald", "worst", "bad", "rip", "frozen", "unwatchable", "z", "sleepy", "mid", "boo", "bo", "ratio"}
QWORD = re.compile(r"^(?:who|what|what's|whats|how|how's|hows|when|where|why|which|can|could|does|do|did|is|are|will|"
                   r"would|should|have|has)\b")
STOP = {"what", "when", "where", "which", "does", "have", "your", "that", "this", "with", "there", "they", "about", "would",
        "could", "should", "going", "doing", "been", "were", "will", "from", "just", "like", "into", "know", "think", "play"}
BOTS = {"nightbot", "streamelements", "moobot", "streamlabs", "fossabot", "soundalerts", "sery_bot", "wizebot", "botrixoficial"}
# Process name (no .exe) -> Twitch category. Add your own in config: skills.stream_copilot.games {exe: category}.
GAMES = {"valorant-win64-shipping": "VALORANT", "cs2": "Counter-Strike", "fortniteclient-win64-shipping": "Fortnite",
         "r5apex": "Apex Legends", "r5apex_dx12": "Apex Legends", "league of legends": "League of Legends",
         "eldenring": "ELDEN RING", "overwatch": "Overwatch 2", "rocketleague": "Rocket League", "gta5": "Grand Theft Auto V",
         "dota2": "Dota 2", "marvel-win64-shipping": "Marvel Rivals", "bg3": "Baldur's Gate 3", "deadlock": "Deadlock",
         "helldivers2": "HELLDIVERS 2", "escapefromtarkov": "Escape from Tarkov", "tslgame": "PUBG: BATTLEGROUNDS",
         "destiny2": "Destiny 2", "cyberpunk2077": "Cyberpunk 2077", "minecraft.windows": "Minecraft"}
NOT_GAMES = {"obs64", "obs", "chrome", "msedge", "firefox", "discord", "explorer", "code", "slack", "spotify", "steam",
             "steamwebhelper", "epicgameslauncher", "battle.net", "riotclientservices", "vlc", "idle", "python"}
INCIDENT = {"network": "dropped frames", "encoder": "encoder overload", "render": "render lag", "reconnecting": "reconnect",
            "obs_lost": "OBS connection lost", "offline": "unexpected offline", "mic_muted": "muted mic",
            "mic_silent": "silent mic", "mic_clipping": "mic clipping"}
RANK = {"raid": 5, "hype_train": 5, "clip": 4, "manual": 4, "moment": 4, "cheer": 3, "gift": 3, "chat_spike": 2}


# ---- chat reading -----------------------------------------------------------------------------------------------

def message_mood(text: str) -> float | None:
    """-1..1 for one chat message from emotes and words; None when it says nothing either way."""
    raw = re.findall(r"<3|[a-z0-9']+", (text or "").lower())
    toks = [(w, re.sub(r"(.)\1+", r"\1", w)) for w in raw]
    pos = sum(1 for w, c in toks if w in POS or c in POS or c.startswith(("pog", "kek", "hype")) or c.endswith("lul"))
    neg = sum(1 for w, c in toks if w in NEG or c in NEG)
    pos += len(re.findall(r"\blet'?s go+\b", " ".join(raw)))
    return None if not pos and not neg else (pos - neg) / (pos + neg)


def mood_label(score: float, rate: float) -> str:
    if not rate:
        return "quiet"
    return ("hyped" if score > 0.35 else "happy" if score > 0.1 else "salty" if score < -0.35
            else "restless" if score < -0.1 else "steady")


def is_question(text: str, me: str = "") -> bool:
    """Looks like a question for the streamer: ends with ? or starts like one, and isn't @ someone else."""
    t = (text or "").strip()
    if not t or t[0] in "!/":
        return False
    m = re.match(r"@(\w+)[,:]?\s*", t)
    if m:
        if m.group(1).lower() != (me or "").lower():
            return False
        t = t[m.end():]
    if len(re.findall(r"[a-zA-Z]{2,}", t)) < 3:
        return False
    return t.endswith("?") or bool(QWORD.match(t.lower()))


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def answered(q: dict, heard: str) -> bool:
    """You said the asker's name, or the question's key words, out loud."""
    name = _squash(q["user"])
    if len(name) >= 3 and name in _squash(heard):
        return True
    keys = {w for w in re.findall(r"[a-z0-9']{4,}", q["text"].lower()) if w not in STOP}
    said = set(re.findall(r"[a-z0-9']{4,}", heard.lower()))
    return bool(keys) and len(keys & said) >= min(2, len(keys))


def detect_game(app: str, title: str, extra: dict | None = None, fullscreen=fullscreen_state) -> str | None:
    a = normalize_app(app)
    table = {**GAMES, **{normalize_app(k): v for k, v in (extra or {}).items()}}
    if a in table:
        return table[a]
    if a and a not in NOT_GAMES and fullscreen() == "game":
        return (title or "").strip()[:60] or None
    return None


def same_game(a: str, b: str) -> bool:
    x, y = _squash(a), _squash(b)
    return bool(x and y) and (x == y or x in y or y in x)


def plan_title(event_title: str, category: str = "", games: dict | None = None) -> str:
    """The stream title a calendar block suggests: 'Stream: Elden Ring | Boss rush' -> 'Boss rush'; 'Stream: Ranked
    grind + Q&A' -> the whole thing; 'Stream: Elden Ring' (just a game) -> ''."""
    plan = parse_plan(event_title)
    if plan["title"]:
        return plan["title"][:140]
    one = plan["game"]
    if re.fullmatch(r"(?:twitch\s+)?(?:stream(?:ing)?|live(?:stream)?|going live)", one.strip(), re.I):
        return ""
    known = {*GAMES.values(), *(games or {}).values()}
    if not one or _squash(one) in {_squash(g) for g in (*known, category) if g}:  # just a game's name
        return ""
    return one[:140]


def parse_plan(title: str) -> dict:
    """'Stream: Elden Ring | Boss rush' -> game Elden Ring, title Boss rush."""
    t = re.sub(r"^\s*(?:twitch\s+)?(?:stream(?:ing)?|live(?:stream)?|going live)(?:\s*[:\-–—|]\s*|\s+)", "", title or "", flags=re.I)
    t = re.sub(r"\s*\b(?:stream|live)$", "", t.strip(), flags=re.I)
    parts = re.split(r"\s+[|\-–—]\s+|:\s+", t, maxsplit=1)
    return {"game": parts[0].strip(), "title": parts[1].strip() if len(parts) > 1 else ""}


# ---- recaps -----------------------------------------------------------------------------------------------------

def fmt_ts(sec: float) -> str:
    h, r = divmod(int(max(0, sec)), 3600)
    return f"{h:02d}:{r // 60:02d}:{r % 60:02d}"


def human(sec: float) -> str:
    m = int(sec // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m} minute{'s' if m != 1 else ''}"


def chapter_label(h: dict) -> str:
    kind, reason = h.get("kind"), (h.get("reason") or h.get("kind") or "Moment").strip()
    if kind == "chat_spike":
        r = (h.get("detail") or {}).get("reaction")
        return f"Chat goes off ({r})" if r else "Chat goes off"
    if kind == "clip" and reason.lower().startswith("clip that"):
        return "Clipped moment"
    return reason[:60]


def vod_chapters(highlights: list[dict], start: float, gap: float = 60, limit: int = 15) -> list[str]:
    """YouTube-style chapters: 00:00:00 first, then the best moments (at least ``gap`` s apart) in time order."""
    out = [(0.0, "Start")]
    for h in sorted(highlights, key=lambda h: RANK.get(h.get("kind"), 1) * 1000 + int((h.get("detail") or {})
                                                                                      .get("messages") or 0), reverse=True):
        o = h["uptime_s"] if h.get("uptime_s") is not None else h.get("ts", start) - start
        if len(out) < limit and all(abs(o - x) >= gap for x, _ in out):
            out.append((o, chapter_label(h)))
    return [f"{fmt_ts(o)} {label}" for o, label in sorted(out)]


def health_summary(incidents: list[dict]) -> dict:
    if not incidents:
        return {"clean": True, "count": 0, "text": "Clean: no dropped frames, reconnects or mic trouble."}
    c = Counter(INCIDENT.get(i["kind"], i["kind"]) for i in incidents)
    n = len(incidents)
    return {"clean": False, "count": n, "text": f"{n} incident{'s' if n != 1 else ''}: "
                                                 + ", ".join(f"{k} ×{v}" for k, v in c.most_common())}


def fallback_posts(r: dict) -> dict:
    facts = [f"{r['duration']} of {r['game'] or 'stream'}", f"peak {r['peak_viewers']} viewers" if r["peak_viewers"] else "",
             f"{r['follows']} new follows" if r["follows"] else "",
             f"a raid from {r['raids'][0]['user']}" if r["raids"] else ""]
    facts = [f for f in facts if f]
    best = r["chapters"][1] if len(r["chapters"]) > 1 else ""
    x = f"Thanks for hanging out today! {', '.join(facts)}." + (f" Best moment at {best}." if best else "") + \
        " VOD is up on the channel."
    lines = [f"**Stream recap: {r['title'] or r['game'] or 'today'}**", ", ".join(facts) + "."]
    if len(r["chapters"]) > 1:
        lines += ["Chapters:", *r["chapters"]]
    return {"x": x[:280], "discord": "\n".join(lines + ["Thanks for watching!"])}


def fallback_titles(game: str, idea: str) -> list[str]:
    if game and idea:
        return [f"{idea} | {game}", f"{game}: {idea} (come hang out)", f"LIVE: {idea}"]
    if game:
        return [f"{game} | chill vibes, good chat", f"LIVE: {game}", f"{game} grind | come hang out"]
    return [idea, f"LIVE: {idea}", f"{idea} | come hang out"]


# ---- growth: this stream next to the last ones (from the recaps in stream_copilot_sessions) ----------------------

MIN_STREAM_S = 600  # shorter than this (a test, a crash) isn't a stream to compare with
COMPARE = 5  # streams to compare with, and how many there must be before comparing at all
METRICS = [("avg_viewers", "average viewers"), ("peak_viewers", "peak viewers"), ("follows", "new follows"),
           ("chat_per_hour", "chat messages an hour"), ("duration_s", "length")]
STARTS = [("Afternoon starts (before 5 pm)", lambda h: 5 <= h < 17), ("Evening starts (5 to 9 pm)", lambda h: 17 <= h < 21),
          ("Late starts (after 9 pm)", lambda h: h >= 21 or h < 5)]


def stream_numbers(r: dict, tz) -> dict:
    """One finished stream's recap, as the numbers the comparison uses."""
    dur = float(r.get("duration_s") or 0)
    start = datetime.fromtimestamp(float(r.get("start") or 0), tz)
    return {"id": r.get("id"), "avg_viewers": r.get("avg_viewers"), "peak_viewers": r.get("peak_viewers"),
            "follows": r.get("follows"), "duration_s": dur or None,
            "chat_per_hour": round((r.get("messages") or 0) / (dur / 3600)) if dur >= 60 else None,
            "game": (r.get("game") or "").strip(), "hour": start.hour + start.minute / 60}


def _mean(vals) -> float | None:
    vals = [float(v) for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _fmt(v: float, key: str) -> str:
    return human(v) if key == "duration_s" else f"{round(v):,}"


def growth(now: dict, past: list[dict], window: int = COMPARE) -> dict:
    """This stream next to the last ``window`` (``past``: earlier streams, newest first): what was better or worse
    and by how much, and what went with more viewers. With fewer than ``window`` earlier streams: just the numbers."""
    last = past[:window]
    out = {"streams": len(past), "window": window, "compared": len(last) >= window, "lines": [], "patterns": [],
           "numbers": {k: now.get(k) for k, _ in METRICS}, "baseline": {}, "change": {}, "headline": "", "spoken": ""}
    if not out["compared"]:
        out["lines"] = [f"{label.capitalize()}: {_fmt(now[k], k)}." for k, label in METRICS if now.get(k) is not None]
        out["note"] = out["spoken"] = (f"Comparisons start once there are {window} earlier streams "
                                       f"({len(past)} so far).")
        return out
    moves = []
    for k, label in METRICS:
        base, v = _mean(x.get(k) for x in last), now.get(k)
        if v is None or not base:
            continue
        out["baseline"][k] = round(base, 1)
        pct = out["change"][k] = round((v - base) / base * 100)
        if k == "duration_s":
            diff = v - base
            how = "about as long as usual" if abs(diff) < 600 else \
                f"{human(abs(diff))} {'longer' if diff > 0 else 'shorter'} than usual"
            out["lines"].append(f"Length {human(v)}: {how} ({human(base)}).")
        elif abs(pct) < 5:
            out["lines"].append(f"{label.capitalize()} {_fmt(v, k)}: about the same as your last {window} ({_fmt(base, k)}).")
        else:
            out["lines"].append(f"{label.capitalize()} {_fmt(v, k)}: {'better' if pct > 0 else 'worse'}, "
                                f"{'up' if pct > 0 else 'down'} {abs(pct)}% on your last {window} ({_fmt(base, k)}).")
            moves.append((abs(pct), label, pct))
    if now["game"]:
        same = sum(1 for x in last if same_game(x.get("game") or "", now["game"]))
        out["lines"].append(f"Category {now['game']}: " + (f"{same} of your last {window} were too." if same
                                                           else f"none of your last {window} were."))
    out["patterns"] = patterns([now, *past])
    top = sorted(moves, reverse=True)[:2]
    out["headline"] = (" and ".join(f"{label} {'up' if pct > 0 else 'down'} {abs(pct)}%" for _, label, pct in top)
                       + f" on your last {window}.").capitalize() if top else f"About the same as your last {window}."
    out["spoken"] = out["headline"] + (f" {out['patterns'][0]}" if out["patterns"] else "")
    return out


def patterns(streams: list[dict], min_group: int = 2, min_diff: float = 0.2) -> list[str]:
    """What went with more (or fewer) viewers across these streams: the game, the start time, the length. Group
    averages, at most one per kind, only where both sides have ``min_group`` streams and differ by ``min_diff``."""
    rows = [s for s in streams if s.get("avg_viewers") is not None]
    if len(rows) < 2 * min_group:
        return []

    def split(name: str, inside) -> tuple[float, str] | None:
        a, b = [s for s in rows if inside(s)], [s for s in rows if not inside(s)]
        if len(a) < min_group or len(b) < min_group:
            return None
        ma, mb = _mean(s["avg_viewers"] for s in a), _mean(s["avg_viewers"] for s in b)
        if not mb or abs(ma - mb) < 2 or abs(ma - mb) / mb < min_diff:
            return None
        rel = (ma - mb) / mb
        return abs(rel), (f"{name} averaged {round(ma)} viewers, {abs(round(rel * 100))}% {'more' if rel > 0 else 'fewer'} "
                          f"than the rest ({round(mb)}), over {len(a)} and {len(b)} streams.")

    kinds = [[split(f"{g} streams", lambda s, g=g: same_game(s.get("game") or "", g))
              for g in {s["game"] for s in rows if s.get("game")}],
             [split(name, lambda s, f=f: f(s.get("hour") or 0)) for name, f in STARTS]]
    lengths = sorted(s["duration_s"] for s in rows if s.get("duration_s"))
    if lengths:
        mid = lengths[len(lengths) // 2]
        kinds.append([split(f"Streams of {human(mid)} or more", lambda s: (s.get("duration_s") or 0) >= mid)])
    best = [max(found) for found in ([x for x in k if x] for k in kinds) if found]
    return [text for _, text in sorted(best, reverse=True)]


def new_session(sid: int, start: float, tw: dict) -> dict:
    return {"id": sid, "start": start, "saved": start, "title": tw.get("title") or "", "game": tw.get("game") or "",
            "viewers": [], "follows": [], "subs": 0, "gifts": 0, "raids": [], "bits": 0, "hype_trains": 0, "messages": 0,
            "minutes": {}, "new_chatters": [], "incidents": [], "highlights": [], "mood": [0.0, 0], "yt_messages": 0}


# ---- the skill --------------------------------------------------------------------------------------------------

class StreamCopilot(Skill):
    name, title = "stream_copilot", "Stream co-pilot"
    poll_s = 10
    game_essential = True  # live stats, chat and the recap: what a stream needs
    delay_s = 10

    def __init__(self, svc, clock=time.time, fullscreen=fullscreen_state):
        super().__init__(svc)
        self.clock, self.fullscreen, self._lock = clock, fullscreen, threading.RLock()
        self.session: dict | None = None  # the stream going on now
        self.hint: dict | None = None  # playing a game that isn't the category
        self.plan: dict | None = None  # the next stream on the calendar
        self.questions, self.greet, self.titles = [], [], []
        self.chat_times, self.moods = deque(), deque()  # message times; (time, mood) for the gauge
        self.error: str | None = None
        self.run_command = None  # set at start: runtime.command
        self.run_tool = None  # set at start: the ToolBox (a HUD card's Yes runs a tool as confirmed on the HUD)
        self.cards = Cards(clock)  # one-tap questions on the HUD: switch category, use the calendar's title
        self._mismatch: dict | None = None  # {"game", "since"}: playing something that isn't the category
        self._declined: set = set()  # games J said "not now" to, this stream
        self._title_checked = 0.0  # when the calendar was last looked at for this stream's title
        self._last_chat, self._off_since, self._next_plan = self.clock(), 0.0, 0.0
        self._prepped, self._seen = set(), set()  # go-live nudges sent; chatters seen this session
        st = svc.storage
        st.execute("CREATE TABLE IF NOT EXISTS stream_copilot_sessions (id INTEGER PRIMARY KEY, start REAL, end REAL, data TEXT)")
        st.execute("CREATE TABLE IF NOT EXISTS stream_copilot_chatters (login TEXT PRIMARY KEY, name TEXT, first_seen REAL, "
                   "last_seen REAL, sessions INTEGER, messages INTEGER, last_session INTEGER)")
        row = st.query("SELECT data FROM stream_copilot_sessions WHERE end IS NOT NULL ORDER BY end DESC LIMIT 1")
        self.recap = (json.loads(row[0]["data"]).get("recap") if row else None)
        svc.bus.on(self._on_event)

    def start(self, runtime) -> None:
        self.run_command = lambda text: runtime.command(text, source="hud")
        tools = getattr(getattr(runtime, "assistant", None), "tools", None)
        self.run_tool = getattr(tools, "run", None)

    def _twitch(self) -> dict:
        return (self.svc.bus.latest.get("twitch") or {}).get("data") or {}

    def _live(self) -> bool:
        return bool(self.svc.live and self.svc.live())

    # ---- the session ------------------------------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        try:
            live, tw = self._live(), self._twitch()
            ended = None
            with self._lock:
                if live:
                    self._off_since = 0.0
                    if self.session is None:
                        self._open(now, tw)
                    self._sample(now, tw)
                elif self.session is not None:
                    self._off_since = self._off_since or now
                    if now - self._off_since >= float(self.setting("offline_grace_s", 90)):
                        ended, self.session = self.session, None
            if ended:
                self._close(ended, self._off_since)
            self._read_chat(now)
            if live:
                self._check_category()
                self._offer_title(now)
            if now >= self._next_plan:
                self._next_plan = now + 60
                self._go_live_prep(now)
            self.error = None
        except Exception as exc:
            log.exception("stream co-pilot tick failed")
            self.error = f"{type(exc).__name__}: {exc}"
        self.publish()

    def _open(self, now: float, tw: dict) -> None:
        row = self.svc.storage.query("SELECT id, data FROM stream_copilot_sessions WHERE end IS NULL ORDER BY start DESC LIMIT 1")
        old = json.loads(row[0]["data"]) if row else None
        if old and now - old.get("saved", 0) < 900:  # Vesper restarted mid-stream: carry on
            self.session = old
        else:
            start = now - float(tw.get("uptime_s") or 0) if tw.get("live") else now  # live before Vesper started
            cur = self.svc.storage.execute("INSERT INTO stream_copilot_sessions (start, data) VALUES (?, '{}')", (start,))
            self.session = new_session(cur.lastrowid, start, tw)
        self._seen, self.questions, self.greet, self.hint, self.plan = set(), [], [], None, None
        self._mismatch, self._declined, self._title_checked = None, set(), 0.0
        self.cards.drop()
        self._save()

    def _sample(self, now: float, tw: dict) -> None:
        s = self.session
        if tw.get("viewers") is not None:
            s["viewers"] = (s["viewers"] + [[round(now), int(tw["viewers"])]])[-720:]
        s["title"], s["game"] = tw.get("title") or s["title"], tw.get("game") or s["game"]
        if now - s["saved"] >= 60:
            self._save()

    def _save(self, s: dict | None = None, end: float | None = None) -> None:
        s = s or self.session
        s["saved"] = self.clock()
        self.svc.storage.execute("UPDATE stream_copilot_sessions SET data = ?, end = ? WHERE id = ?",
                                 (json.dumps(s), end, s["id"]))

    def _close(self, s: dict, end: float) -> None:
        self.recap = s["recap"] = self.build_recap(s, end)
        self._save(s, end)
        self.hint, self._mismatch = None, None
        self.cards.drop()
        self.nudge("Stream recap is ready on the HUD.", priority="normal", key=f"stream_recap:{s['id']}")

    def build_recap(self, s: dict, end: float) -> dict:
        hl = {round(h.get("ts", 0), 1): h for h in s["highlights"]}
        for h in (self.svc.highlights() if self.svc.highlights else []) or []:
            if s["start"] <= h.get("ts", 0) <= end:
                hl.setdefault(round(h["ts"], 1), h)
        views = [v for _, v in s["viewers"]]
        dur = max(0.0, end - s["start"])
        chapters = vod_chapters(list(hl.values()), s["start"])
        r = {"id": s["id"], "start": s["start"], "end": end, "duration_s": round(dur), "duration": human(dur),
             "title": s["title"], "game": s["game"], "peak_viewers": max(views, default=None),
             "avg_viewers": round(sum(views) / len(views)) if views else None, "follows": len(s["follows"]),
             "followers": s["follows"][:30], "subs": s["subs"], "gifts": s["gifts"], "raids": s["raids"], "bits": s["bits"],
             "messages": s["messages"], "avg_rate": round(s["messages"] / max(1.0, dur / 60), 1),
             "peak_rate": max(s["minutes"].values(), default=0), "new_chatters": len(s["new_chatters"]),
             "mood": round(s["mood"][0] / s["mood"][1], 2) if s["mood"][1] else 0.0, "chapters": chapters,
             "health": health_summary(s["incidents"]), "youtube_messages": s.get("yt_messages", 0)}
        r["growth"] = growth(stream_numbers(r, self.svc.tz), self.past_numbers(s["id"]))
        r["posts"] = self._posts(r)
        return r

    def past_numbers(self, sid: int | None = None, limit: int = 20) -> list[dict]:
        """The finished streams before this one, newest first (from their recaps), for the comparison."""
        rows = self.svc.storage.query("SELECT id, data FROM stream_copilot_sessions WHERE end IS NOT NULL AND id <> ? "
                                      "ORDER BY end DESC LIMIT ?", (sid if sid is not None else -1, limit * 2))
        out = []
        for row in rows:
            try:
                r = json.loads(row["data"]).get("recap")
            except (TypeError, ValueError):
                continue
            if r and (r.get("duration_s") or 0) >= MIN_STREAM_S:  # a 2-minute test stream isn't one to compare with
                out.append(stream_numbers(r, self.svc.tz))
        return out[:limit]

    def _posts(self, r: dict) -> dict:
        facts = {k: r[k] for k in ("duration", "title", "game", "peak_viewers", "follows", "subs", "bits", "chapters")}
        facts["raids"] = [x["user"] for x in r["raids"]]
        try:
            out = self.svc.think("Write two posts for after this Twitch stream, in the streamer's own casual voice. x: one "
                                 "post under 270 characters, at most two hashtags. discord: a few short lines with the VOD "
                                 "chapters. Facts: " + json.dumps(facts), schema={"type": "object", "properties": {
                                     "x": {"type": "string"}, "discord": {"type": "string"}}, "required": ["x", "discord"]})
        except Exception:
            out = None
        if isinstance(out, dict) and isinstance(out.get("x"), str) and isinstance(out.get("discord"), str):
            return {"x": out["x"][:280], "discord": out["discord"][:1800], "from_model": True}
        return fallback_posts(r)

    # ---- chat -------------------------------------------------------------------------------------------------
    def _read_chat(self, now: float) -> None:
        feed = self.svc.twitch_feed
        msgs = [m for m in (feed.recent_chat(300) if feed is not None else []) if m["ts"] > self._last_chat]
        me = (getattr(self.svc.twitch, "login_name", "") or "").lower()
        with self._lock:
            for m in msgs:
                self._last_chat = max(self._last_chat, m["ts"])
                login = (m.get("login") or m.get("user") or "").lower()  # YouTube's: yt:<channel id>
                if login in BOTS or not login:
                    continue
                self.chat_times.append(m["ts"])
                score = message_mood(m["text"])
                if score is not None:
                    self.moods.append((m["ts"], score))
                s = self.session
                if s is not None:
                    s["messages"] += 1
                    if m.get("platform") == "youtube":
                        s["yt_messages"] = s.get("yt_messages", 0) + 1
                    minute = str(int((m["ts"] - s["start"]) // 60))
                    s["minutes"][minute] = s["minutes"].get(minute, 0) + 1
                    if score is not None:
                        s["mood"] = [s["mood"][0] + score, s["mood"][1] + 1]
                if login == me:
                    continue
                if is_question(m["text"], me):
                    self.questions = ([{"id": f"{m['ts']:.6f}", "user": m["user"], "text": m["text"][:300], "ts": m["ts"],
                                        "done": False, "platform": m.get("platform") or "twitch"}] + self.questions)[:40]
                if s is not None and login not in self._seen:
                    self._seen.add(login)
                    self._chatter(login, m["user"], m["ts"], s)
            while self.chat_times and now - self.chat_times[0] > 300:
                self.chat_times.popleft()
            while self.moods and now - self.moods[0][0] > 120:
                self.moods.popleft()
            self.questions = [q for q in self.questions if now - q["ts"] < 1800]

    def _chatter(self, login: str, name: str, ts: float, s: dict) -> None:
        st = self.svc.storage
        row = st.query("SELECT sessions, last_session FROM stream_copilot_chatters WHERE login = ?", (login,))
        if not row:
            st.execute("INSERT INTO stream_copilot_chatters VALUES (?, ?, ?, ?, 1, 1, ?)", (login, name, ts, ts, s["id"]))
            kind, n = "new", 1
            s["new_chatters"].append(name)
        else:
            n = row[0]["sessions"] + (row[0]["last_session"] != s["id"])
            st.execute("UPDATE stream_copilot_chatters SET last_seen = ?, sessions = ?, last_session = ? WHERE login = ?",
                       (ts, n, s["id"], login))
            kind = "regular" if n >= int(self.setting("regular_after", 3)) else ""
        if kind:
            self.greet = ([{"user": name, "kind": kind, "sessions": n, "ts": ts}] + self.greet)[:30]

    def mood(self) -> dict:
        now = self.clock()
        vals = [s for t, s in self.moods if now - t <= 120]
        rate = sum(1 for t in self.chat_times if now - t <= 60)
        score = max(-1.0, min(1.0, sum(vals) / (len(vals) + 2))) if vals else 0.0
        return {"score": round(score, 2), "label": mood_label(score, rate), "rate": rate}

    def open_questions(self) -> list[dict]:
        now = self.clock()
        return [{**q, "age_s": round(now - q["ts"]), "late": now - q["ts"] > 120} for q in self.questions if not q["done"]][:10]

    # ---- events -----------------------------------------------------------------------------------------------
    def _on_event(self, ev: dict) -> None:
        typ, d = ev.get("type"), ev.get("data") or {}
        if typ == "activity_now":
            self._check_category(d)
            return
        if typ == "heard" and not d.get("ignored") and d.get("text"):
            with self._lock:
                for q in self.questions:
                    if not q["done"] and answered(q, d["text"]):
                        q["done"] = "answered"
            return
        with self._lock:
            s = self.session
            if s is None:
                return
            if typ == "twitch_event":
                kind, n, who = d.get("kind"), int(d.get("amount") or 0), d.get("user") or "someone"
                if kind == "follow":
                    s["follows"].append(who)
                elif kind == "raid":
                    s["raids"].append({"user": who, "viewers": n})
                for key, add in {"sub": [("subs", 1)], "resub": [("subs", 1)], "gift": [("subs", n), ("gifts", n)],
                                 "cheer": [("bits", n)], "hype_train": [("hype_trains", 1)]}.get(kind, []):
                    s[key] += add
            elif typ == "stream_alert" and d.get("level") in ("warning", "critical"):
                s["incidents"].append({"kind": d.get("kind"), "level": d.get("level"), "ts": d.get("ts") or self.clock()})
            elif typ == "highlight":
                s["highlights"].append(d)

    def _check_category(self, seg: dict | None = None) -> None:
        """Playing a game that isn't the Twitch category for ``category_after_s`` (60 s): a card asks to switch."""
        if not self._live():
            self._mismatch = None
            return
        if seg is None:
            seg = (self.svc.bus.latest.get("activity_now") or {}).get("data") or {}
        game = detect_game(seg.get("app", ""), seg.get("title", ""), self.setting("games") or {}, self.fullscreen)
        cat = self._twitch().get("game") or ""
        if not game or not cat:
            return
        if same_game(game, cat):
            self.hint, self._mismatch = None, None
            self.cards.drop("category")
            return
        now = self.clock()
        if self._mismatch is None or not same_game(self._mismatch["game"], game):
            self._mismatch = {"game": game, "since": now}
            self.cards.drop("category")
        self.hint = {"game": game, "category": cat, "ts": now, "since": self._mismatch["since"]}
        if now - self._mismatch["since"] >= float(self.setting("category_after_s", 60)) and _squash(game) not in self._declined:
            sid = (self.session or {}).get("id")
            card = self.cards.add("category", f"Switch category to {game}?", yes=f"Switch to {game}",
                                  data={"game": game, "from": cat}, ttl=1800, key=f"category:{_squash(game)}:{sid}")
            if card:  # on the HUD only, never said: it would go out on air
                self.notify(f"You're playing {game}, the category says {cat}. Switch it from the HUD card.",
                            level="urgent", key=card["key"])
        self.publish()

    def switch_category(self, game: str = "") -> dict:
        """J clicked Switch: that's the yes, so the change runs as confirmed on the HUD."""
        game = game or (self.hint or {}).get("game") or ""
        if not game:
            return {"ok": False, "error": "Which game?"}
        out = run_on_click(self.run_tool, self.svc.bus, "twitch_set_channel", {"category": game},
                           f"switch category to {game}")
        if out.get("ok"):
            self.hint, self._mismatch = None, None
            self.cards.drop("category")
            tw = self.svc.bus.latest.get("twitch")
            if tw and isinstance(tw.get("data"), dict):  # don't ask again before the next Twitch poll catches up
                tw["data"]["game"] = out.get("category") or game
        return {"ok": bool(out.get("ok")), "game": out.get("category") or game, "error": out.get("error"),
                "reply": f"Category is now {out.get('category') or game}." if out.get("ok") else out.get("error")}

    def _offer_title(self, now: float) -> None:
        """Just went live with a stream block on the calendar: offer its title (once per stream)."""
        s = self.session
        if s is None or now - s["start"] > 1800 or now - self._title_checked < 60:
            return
        self._title_checked = now
        ev = stream_event_now(self.svc, now)
        if not ev:
            return
        current = self._twitch().get("title") or s.get("title") or ""
        title = plan_title(ev.get("title") or "", self._twitch().get("game") or s.get("game") or "", self.setting("games"))
        if not title or title.strip().lower() == current.strip().lower():
            return
        self.cards.add("title", f"Use title “{title}”?", yes="Use it", data={"title": title, "event": ev.get("title")},
                       ttl=1800, key=f"title:{s['id']}:{title}")

    def answer_card(self, card_id: str, answer: str) -> dict:
        card = self.cards.take(card_id)
        if card is None:
            return {"ok": False, "error": "That question has expired."}
        d = card["data"]
        if answer != "yes":
            if card["kind"] == "category":
                self._declined.add(_squash(d.get("game", "")))
            return {"ok": True, "answer": "no"}
        if card["kind"] == "category":
            return self.switch_category(d["game"])
        if card["kind"] == "title":
            yt = self.svc.skills.get("stream_youtube") if self.svc.skills is not None else None
            tool = yt.title_tool() if yt is not None else "twitch_set_channel"  # Twitch and YouTube, if J chose both
            out = run_on_click(self.run_tool, self.svc.bus, tool, {"title": d["title"]},
                               f"set the stream title to {d['title']}")
            if out.get("ok") and self.session is not None:
                self.session["title"] = d["title"]
            return {"ok": bool(out.get("ok")), "error": out.get("error"),
                    "reply": "Title updated." if out.get("ok") else out.get("error")}
        return {"ok": False, "error": "Unknown card."}

    # ---- go-live prep -----------------------------------------------------------------------------------------
    def _go_live_prep(self, now: float) -> None:
        if self.plan and now > self.plan["start"] + 3600:
            self.plan = None
        if self._live():
            return
        lead = float(self.setting("prep_minutes", 30))
        dt = datetime.fromtimestamp(now, self.svc.tz)
        for e in self.svc.calendars.agenda(1):
            if e.get("all_day") or e.get("profile") != "stream":
                continue
            start = datetime.fromisoformat(e["start"])
            mins = (start - dt).total_seconds() / 60
            key = f"golive:{e.get('title')}:{e['start']}"
            if 0 < mins <= lead:
                self.plan = {**parse_plan(e.get("title") or ""), "event": e.get("title") or "Stream", "start": start.timestamp()}
                if key not in self._prepped:
                    self._prepped.add(key)
                    game = f" ({self.plan['game']})" if self.plan["game"] else ""
                    self.nudge(f"{self.plan['event']}{game} starts in {round(mins)} minutes. The go-live checklist is "
                               "on the Stream tab.", priority="normal", key=key, expires_in=mins * 60,
                               action={"label": "Pre-stream check", "command": "am I ready to stream"})
                return

    def suggest_title(self, game: str = "", idea: str = "") -> dict:
        plan, tw = self.plan or {}, self._twitch()
        game, idea = game or plan.get("game") or tw.get("game") or "", idea or plan.get("title") or ""
        if not game and not idea:
            return {"ok": False, "error": "Tell me the game or the idea, like “suggest a title for Elden Ring boss rush”."}
        try:
            out = self.svc.think(f"Suggest three catchy Twitch stream titles (under 100 characters each) for game "
                                 f"{game!r}, plan {idea!r}.", schema={"type": "object", "properties": {
                                     "titles": {"type": "array", "items": {"type": "string"}}}, "required": ["titles"]})
        except Exception:
            out = None
        titles = [str(x).strip()[:140] for x in (out or {}).get("titles", []) if str(x).strip()][:3] \
            if isinstance(out, dict) else []
        self.titles = titles or fallback_titles(game, idea)
        self.publish()
        return {"ok": True, "game": game, "titles": self.titles, "from_model": bool(titles)}

    # ---- what the HUD, the brain and the voice get ------------------------------------------------------------
    def connections(self) -> str | None:
        tw = self.svc.twitch
        tw_ok = bool(getattr(tw, "configured", False) and getattr(getattr(tw, "auth", None), "connected", False))
        obs_ok = bool(((self.svc.bus.latest.get("obs") or {}).get("data") or {}).get("connected"))
        if not tw_ok and not obs_ok:
            return "Twitch and OBS aren't connected, so I can't tell when you're live. Connect them on the Setup tab."
        if not tw_ok:
            return "Twitch isn't connected: live time and stream health work; viewers, chat and follows need Twitch (Setup tab)."
        if not obs_ok:
            return "OBS isn't connected: I go by Twitch to see when you're live; stream-health incidents need OBS."
        return None

    def stats_now(self) -> dict:
        now = self.clock()
        with self._lock:
            s = self.session
            if s is None:
                last = self.recap and {k: self.recap.get(k) for k in ("duration", "peak_viewers", "follows", "end")}
                return {"ok": True, "live": False, "last": last, "note": self.connections()}
            views = [v for _, v in s["viewers"]]
            return {"ok": True, "live": True, "uptime_s": round(now - s["start"]), "uptime": human(now - s["start"]),
                    "viewers": views[-1] if views else None, "peak_viewers": max(views, default=None),
                    "avg_viewers": round(sum(views) / len(views)) if views else None, "follows": len(s["follows"]),
                    "subs": s["subs"], "bits": s["bits"], "raids": len(s["raids"]), "messages": s["messages"],
                    "highlights": len(s["highlights"]), "incidents": len(s["incidents"]), "mood": self.mood(),
                    "questions": len(self.open_questions()), "game": s["game"], "note": self.connections()}

    def chat_pulse(self, focus: str = "mood") -> dict:
        with self._lock:
            return {"ok": True, "focus": focus, "mood": self.mood(), "questions": self.open_questions(),
                    "greet": self.greet[:10], "note": self.connections()}

    def stream_recap(self, session: str = "last", part: str = "all") -> dict:
        r = self.recap
        if str(session).isdigit():
            row = self.svc.storage.query("SELECT data FROM stream_copilot_sessions WHERE id = ?", (int(session),))
            r = json.loads(row[0]["data"]).get("recap") if row else None
        if not r:
            return {"ok": False, "error": "No finished stream yet. The recap shows up on the HUD when you go offline."}
        if not r.get("growth"):  # a recap from before 2.3: compare it now
            r = {**r, "growth": growth(stream_numbers(r, self.svc.tz), self.past_numbers(r.get("id")))}
        return {"ok": True, "part": part, **r}

    def status(self) -> dict:
        st = self.stats_now()
        with self._lock:
            return {"live": st["live"], "now": st if st["live"] else None, "note": st["note"], "error": self.error,
                    "start": self.session["start"] if self.session else None,
                    "viewers": [v for _, v in (self.session or {}).get("viewers", [])][-90:],
                    "mood": self.mood(), "questions": self.open_questions(), "greet": self.greet[:12], "hint": self.hint,
                    "plan": self.plan, "titles": self.titles, "recap": self.recap, "cards": self.cards.live()}

    def briefing(self):
        r = self.recap
        if r and self.clock() - r["end"] < 18 * 3600:
            return {"line": f"Last stream: {r['duration']}, peak {r['peak_viewers'] or '?'} viewers, "
                            f"{r['follows']} new follows. Recap and VOD chapters are on the Stream tab."}
        return None

    def actions(self):
        return {"dismiss": lambda b: self.dismiss(str(b.get("id") or "")),
                "greeted": lambda b: self.greeted(str(b.get("user") or "")),
                "switch_category": lambda b: self.switch_category(str(b.get("game") or "")),
                "card": lambda b: self.answer_card(str(b.get("id") or ""), str(b.get("answer") or "no")),
                "suggest_title": lambda b: self.suggest_title(str(b.get("game") or ""), str(b.get("idea") or "")),
                "prestream": lambda b: self.svc.prestream() if self.svc.prestream else
                {"ok": False, "error": "The pre-stream check needs Vesper's runtime (OBS)."}}

    def dismiss(self, qid: str) -> dict:
        hit = [q for q in self.questions if q["id"] == qid]
        for q in hit:
            q["done"] = "dismissed"
        return {"ok": True} if hit else {"ok": False, "error": "That question is gone."}

    def greeted(self, user: str) -> dict:
        self.greet = [g for g in self.greet if g["user"] != user]
        return {"ok": True}

    def tools(self):
        return [
            Tool("stream_stats_now", "The live stream right now: uptime, viewers (now, peak, average), new follows, subs, "
                                     "bits, raids, chat pace and mood, open questions, highlights, health incidents.",
                 obj(), self.stats_now),
            Tool("chat_pulse", "Twitch chat at a glance: mood (-1..1) and messages a minute, questions viewers asked "
                               "that are still unanswered, first-time chatters and regulars to greet. focus: mood or "
                               "questions.", obj({"focus": {"type": "string"}}), self.chat_pulse, untrusted=True),
            Tool("stream_recap", "The recap of a finished stream (session: 'last' or an id): duration, viewers, follows, "
                                 "subs, raids, VOD chapters, stream health, drafts for X and Discord, and growth: how it "
                                 "compares with the last 5 streams and what went with better ones. part: all, chapters "
                                 "or growth.", obj({"session": {"type": "string"}, "part": {"type": "string"}}),
                 self.stream_recap),
            Tool("suggest_title", "Suggest stream titles for a game and plan (defaults to the next stream on the "
                                  "calendar).", obj({"game": {"type": "string"}, "idea": {"type": "string"}}),
                 self.suggest_title),
        ]

    def route(self, t: str):
        if re.fullmatch(r"how(?:'s| is) (?:the |my )?stream (?:going|doing)(?: so far)?|how am i doing (?:on stream|live)"
                        r"|(?:current |live )?stream stats(?: so far)?", t):
            return Intent("tool", "stream_stats_now")
        if re.fullmatch(r"(?:are there )?any (?:unanswered )?questions (?:in|from) (?:the )?chat|(?:show |read )?(?:the )?"
                        r"(?:chat questions|questions in chat)|did (?:anyone|chat) ask (?:anything|something)", t):
            return Intent("tool", "chat_pulse", {"focus": "questions"})
        if re.fullmatch(r"how(?:'s| is) (?:the )?chat(?: doing| feeling| looking| vibing)?|(?:what's )?(?:the )?chat(?:'s)? "
                        r"(?:mood|vibe)", t):
            return Intent("tool", "chat_pulse", {"focus": "mood"})
        if re.fullmatch(r"(?:give me |show me |make |what are )?(?:the |my )?vod chapters(?: for (?:the |my )?(?:last )?"
                        r"(?:stream|vod))?", t):
            return Intent("tool", "stream_recap", {"session": "last", "part": "chapters"})
        if re.fullmatch(r"how did (?:the|my|that) (?:last )?stream compare|compare (?:the |my |that )?(?:last )?stream"
                        r"(?: to (?:the |my )?(?:last|other|previous) (?:ones|streams))?|(?:my )?stream growth|how(?:'s| is) "
                        r"(?:my |the )?(?:stream|channel) growing|am i growing(?: on twitch)?", t):
            return Intent("tool", "stream_recap", {"session": "last", "part": "growth"})
        if re.fullmatch(r"(?:give me |show me |show |read )?(?:the |a |my )?(?:last )?(?:post-?stream|stream) recap"
                        r"|recap (?:the |my )?(?:last )?stream|how did (?:the|my|that) (?:last )?stream go", t):
            return Intent("tool", "stream_recap", {"session": "last"})
        m = re.fullmatch(r"(?:suggest|give me|think of|come up with) (?:a |some )?(?:stream |twitch )?titles?(?: for (.+))?", t)
        if m:
            return Intent("tool", "suggest_title", {"idea": m.group(1)} if m.group(1) else {})
        return None

    def say(self, tool, args, result):
        if result.get("ok") is False:
            return result.get("error")
        if tool == "stream_stats_now":
            if not result["live"]:
                last = result.get("last")
                return "You're not live." + (f" Last stream: {last['duration']}, peak {last['peak_viewers'] or 'unknown'}."
                                             if last else "")
            v = f"{result['viewers']} watching, peak {result['peak_viewers']}" if result["viewers"] is not None else \
                "no viewer count yet"
            return (f"Live {result['uptime']}, {v}. {result['follows']} new follows; chat's {result['mood']['label']}.")
        if tool == "chat_pulse":
            m, qs = result["mood"], result["questions"]
            if result["focus"] == "questions":  # viewers' words stay on the HUD
                if not qs:
                    return "No open questions in chat."
                late = sum(1 for q in qs if q["late"])
                # On air, no names at all: a viewer picks their own (a YouTube channel name is any text they type).
                # Off air, a Twitch login only (letters, digits and _).
                if self._live():
                    who = ""
                elif qs[0].get("platform") == "youtube":
                    who = " Latest from a YouTube viewer."
                else:
                    who = f" Latest from {qs[0]['user']}."
                return (f"{len(qs)} open question{'s' if len(qs) != 1 else ''}" + (f", {late} waiting over two minutes"
                        if late else "") + f".{who} They're on the HUD.")
            return f"Chat's {m['label']}, {m['rate']} messages a minute."
        if tool == "stream_recap":
            g = result.get("growth") or {}
            if args.get("part") == "growth" or result.get("part") == "growth":
                return g.get("spoken") or "No comparison yet."
            if args.get("part") == "chapters" or result.get("part") == "chapters":
                ch = result["chapters"]
                return f"{len(ch)} chapter{'s' if len(ch) != 1 else ''} on the HUD" + (f", first at {ch[1]}." if len(ch) > 1 else ".")
            raid = f", raid from {result['raids'][0]['user']}" if result["raids"] else ""
            vs = f" {g['headline']}" if g.get("headline") else ""
            return (f"Last stream: {result['duration']}, peak {result['peak_viewers'] or 'unknown'}, "
                    f"{result['follows']} new follows{raid}.{vs} Chapters and post drafts are on the HUD.")
        if tool == "suggest_title":
            return f"How about: {result['titles'][0]}. More on the HUD."
        return None
