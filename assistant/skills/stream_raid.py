"""Raid planner: who to send your viewers to at the end of the stream.

Ten minutes before the stream block on your calendar ends (or when you ask "who should I raid"), Vesper lists who's
live in your category right now (Twitch Helix ``GET /streams?game_id=…``) and ranks them:

- about your size (within ±50% of your viewers) first: a raid that matters to them, and they can raid back;
- your language;
- friends (★ on the HUD, or ``skills.stream_raid.friends``) and channels you've raided before, or who raided you;
- nobody you raided in the last 7 days (they drop to the bottom, marked).

The HUD card has a Raid button per channel: a raid is tier 2, so the click (a second "Confirm" tap) is the yes. By
voice, "raid tenz" asks for a yes first. Every raid, sent or received, goes into the history the ranking uses.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill
from ._stream import Cards, run_on_click, stream_event_now

log = logging.getLogger(__name__)
WEEK = 7 * 86400


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", (s or "").lower())


def rank(streams: list[dict], *, me: str = "", my_viewers: int | None = None, language: str = "", friends: set | None = None,
         history: dict | None = None, now: float | None = None, limit: int = 8) -> list[dict]:
    """Score each live channel and say why. ``history``: login -> {"out": last raid you sent, "in": last raid you got}."""
    now = time.time() if now is None else now
    friends, history = {f.lower() for f in (friends or set())}, history or {}
    out = []
    for s in streams:
        login = (s.get("user_login") or "").lower()
        if not login or login == (me or "").lower():
            continue
        v = int(s.get("viewer_count") or 0)
        score, why = 0.0, []
        if my_viewers:
            ratio = v / my_viewers
            if 0.5 <= ratio <= 1.5:
                score += 3
                why.append("about your size")
            else:
                score += max(-3.0, 2 - 2 * abs(math.log2(max(ratio, 0.01))))  # 2x or half: 0; 4x or a quarter: -2
                why.append("much bigger" if ratio > 1.5 else "smaller")
        if v == 0:
            score -= 1
        lang = (s.get("language") or "").lower()
        if language and lang:
            if lang == language.lower():
                score += 2
            else:
                score -= 2
                why.append(f"streams in {lang}")
        h = history.get(login) or {}
        if login in friends:
            score += 3
            why.append("friend")
        if h.get("in"):
            score += 2
            why.append("raided you before")
        recent = h.get("out") and now - h["out"] < WEEK
        if recent:
            score -= 10
            days = int((now - h["out"]) // 86400)
            why.append("raided today" if days == 0 else f"raided {days} day{'s' if days != 1 else ''} ago")
        elif h.get("out"):
            score += 1
            why.append("you raided them before")
        out.append({"login": login, "name": s.get("user_name") or login, "viewers": v, "title": (s.get("title") or "")[:140],
                    "game": s.get("game_name") or "", "language": lang, "mature": bool(s.get("is_mature")),
                    "score": round(score, 2), "why": why, "recent": bool(recent), "friend": login in friends,
                    "_near": abs(math.log2(max(v, 1) / my_viewers)) if my_viewers else 0.0})
    out.sort(key=lambda c: (-c["score"], c.pop("_near"), -c["viewers"]))  # a tie goes to the closest in size
    return out[:limit]


class RaidPlanner(Skill):
    name, title = "stream_raid", "Raid planner"
    poll_s = 30
    delay_s = 20

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock, self._lock = clock, threading.RLock()
        self.list: list[dict] = []
        self.updated = 0.0
        self.game = ""
        self.error: str | None = None
        self.cards = Cards(clock)
        self.run_tool = None
        st = svc.storage
        st.execute("CREATE TABLE IF NOT EXISTS stream_raid_history (id INTEGER PRIMARY KEY, login TEXT, name TEXT, ts REAL, "
                   "viewers INTEGER, direction TEXT)")
        st.execute("CREATE TABLE IF NOT EXISTS stream_raid_friends (login TEXT PRIMARY KEY, added REAL)")
        svc.bus.on(self._on_event)

    def start(self, runtime) -> None:
        tools = getattr(getattr(runtime, "assistant", None), "tools", None)
        self.run_tool = getattr(tools, "run", None)

    def _live(self) -> bool:
        return bool(self.svc.live and self.svc.live())

    def _twitch(self) -> dict:
        return (self.svc.bus.latest.get("twitch") or {}).get("data") or {}

    # ---- history ----------------------------------------------------------------------------------------------
    def _on_event(self, ev: dict) -> None:
        d = ev.get("data") or {}
        if ev.get("type") == "twitch_event" and d.get("kind") == "raid" and d.get("user"):
            login = (d.get("detail") or {}).get("login") or _squash(d["user"])
            self.record(login, d["user"], int(d.get("amount") or 0), "in")

    def record(self, login: str, name: str, viewers: int, direction: str) -> None:
        self.svc.storage.execute("INSERT INTO stream_raid_history (login, name, ts, viewers, direction) VALUES (?, ?, ?, ?, ?)",
                                 (login.lower(), name, self.clock(), viewers, direction))

    def history(self) -> dict:
        out: dict = {}
        for r in self.svc.storage.query("SELECT login, direction, MAX(ts) AS ts FROM stream_raid_history GROUP BY login, direction"):
            out.setdefault(r["login"], {})[r["direction"]] = r["ts"]
        return out

    def friends(self) -> set:
        conf = {str(f).lower().lstrip("@") for f in (self.setting("friends") or [])}
        return conf | {r["login"] for r in self.svc.storage.query("SELECT login FROM stream_raid_friends")}

    # ---- the list ---------------------------------------------------------------------------------------------
    def suggest(self, refresh: bool = True) -> dict:
        tw = self.svc.twitch
        if not getattr(tw, "configured", False) or not getattr(tw, "enabled", False):
            return {"ok": False, "error": "Connect Twitch first (Setup tab, or say “connect Twitch”) so I can see who's live."}
        if not refresh and self.list and self.clock() - self.updated < 120:
            return self._view()
        me = self._twitch()
        game = me.get("game") or ""
        try:
            if not game:
                game = (tw.channel_info() or {}).get("game") or ""
            if not game:
                return {"ok": False, "error": "Your Twitch category isn't set, so I don't know whose streams to look at."}
            cat = tw.find_category(game)
            if not cat:
                return {"ok": False, "error": f"Twitch has no category called {game}."}
            streams = tw.live_streams(cat["id"], first=100)
        except Exception as exc:
            self.error = f"Couldn't ask Twitch who's live: {exc}"
            return {"ok": False, "error": self.error}
        with self._lock:
            self.game, self.error = cat.get("name") or game, None
            self.list = rank(streams, me=getattr(tw, "login_name", ""), my_viewers=me.get("viewers") if me.get("live") else None,
                             language=str(self.setting("language", "en") or ""), friends=self.friends(),
                             history=self.history(), now=self.clock(), limit=int(self.setting("limit", 8)))
            self.updated = self.clock()
        self.publish()
        return self._view()

    def _view(self) -> dict:
        return {"ok": True, "game": self.game, "channels": self.list, "updated": self.updated}

    def tick(self) -> None:
        """Ten minutes before the stream block on the calendar ends: the list, and a card on the HUD."""
        try:
            if self._live():
                now = self.clock()
                ev = stream_event_now(self.svc, now)
                lead = float(self.setting("lead_minutes", 10)) * 60
                if ev and 0 < ev["end_ts"] - now <= lead:
                    key = f"raid:{ev.get('title')}:{ev['end_ts']}"
                    if key not in self.cards.asked:
                        out = self.suggest()
                        mins = max(1, round((ev["end_ts"] - now) / 60))
                        top = [c for c in out.get("channels", []) if not c["recent"]][:5] if out.get("ok") else []
                        if top:
                            self.cards.add("raid", f"Stream ends in {mins} min. Raid someone?", yes="", no="Not tonight",
                                           data={"channels": top, "game": self.game}, ttl=lead + 1800, key=key)
                            self.notify(f"Stream ends in {mins} minutes: raid picks are on the HUD "
                                        f"(top: {top[0]['name']}, {top[0]['viewers']} watching).", level="normal", key=key)
                        else:
                            self.cards.asked.add(key)
            self.error = None if self.list or not self.error else self.error
        except Exception as exc:
            log.exception("raid planner tick failed")
            self.error = f"{type(exc).__name__}: {exc}"
        self.publish()

    # ---- raiding ------------------------------------------------------------------------------------------------
    def _resolve(self, user: str) -> str:
        key = _squash(user)
        hit = next((c["login"] for c in self.list if key in (_squash(c["login"]), _squash(c["name"]))), None)
        if hit:
            return hit
        feed = self.svc.twitch_feed
        return feed.resolve(user) if feed is not None else key

    def raid(self, user: str) -> dict:
        """The tool's handler: by the time this runs, J said yes (voice) or clicked Confirm (HUD)."""
        login = self._resolve(user)
        if not login:
            return {"ok": False, "error": "Who should I raid?"}
        out = self.svc.twitch.start_raid(login)
        if out.get("ok"):
            c = next((c for c in self.list if c["login"] == login), {})
            self.record(login, c.get("name") or login, int(c.get("viewers") or 0), "out")
            self.cards.drop("raid")
            out = {**out, "user": c.get("name") or login, "viewers": c.get("viewers")}
            self.publish()
        return out

    def raid_from_hud(self, login: str) -> dict:
        if not login:
            return {"ok": False, "error": "Which channel?"}
        return run_on_click(self.run_tool, self.svc.bus, "twitch_raid", {"user": login}, f"raid {login}")

    def set_friend(self, login: str, on: bool) -> dict:
        login = _squash(login)
        if not login:
            return {"ok": False, "error": "Which channel?"}
        if on:
            self.svc.storage.execute("INSERT OR REPLACE INTO stream_raid_friends (login, added) VALUES (?, ?)", (login, self.clock()))
        else:
            self.svc.storage.execute("DELETE FROM stream_raid_friends WHERE login = ?", (login,))
        fr = self.friends()
        for c in self.list:
            c["friend"] = c["login"] in fr
        return {"ok": True, "login": login, "friend": on}

    def answer_card(self, card_id: str, answer: str) -> dict:
        card = self.cards.take(card_id)
        if card is None:
            return {"ok": False, "error": "That card has expired."}
        return {"ok": True, "answer": answer}

    # ---- what the HUD, the brain and the voice get ------------------------------------------------------------
    def status(self) -> dict:
        recent = self.svc.storage.query("SELECT login, name, ts, viewers, direction FROM stream_raid_history ORDER BY ts DESC LIMIT 6")
        return {"live": self._live(), "game": self.game, "channels": self.list, "updated": self.updated or None,
                "error": self.error, "cards": self.cards.live(), "history": [dict(r) for r in recent],
                "friends": sorted(self.friends())}

    def actions(self):
        return {"refresh": lambda b: self.suggest(),
                "raid": lambda b: self.raid_from_hud(str(b.get("login") or "")),
                "friend": lambda b: self.set_friend(str(b.get("login") or ""), bool(b.get("on", True))),
                "card": lambda b: self.answer_card(str(b.get("id") or ""), str(b.get("answer") or "no"))}

    def tools(self):
        return [
            Tool("raid_suggestions", "Who to raid at the end of the stream: channels live in J's category now, ranked by "
                                     "similar viewer count, language, friends and past raids (anyone raided in the last "
                                     "7 days goes last).", obj(), lambda: self.suggest()),
            Tool("twitch_raid", "Raid another Twitch channel (sends J's viewers there; Twitch counts down 90 s). user: "
                                "their channel name as said.", obj({"user": {"type": "string"}}, ["user"]), self.raid,
                 tier=2, confirm_text=lambda a: f"raid {self._resolve(a.get('user', ''))}"),
        ]

    def route(self, t: str):
        if re.fullmatch(r"who should i raid(?: tonight| today| now)?|(?:find|suggest|give me|show me) (?:someone|somebody|a channel|"
                        r"channels|streamers|people) to raid|(?:show |give me )?(?:the )?raid (?:ideas|suggestions|targets|picks|list)",
                        t):
            return Intent("tool", "raid_suggestions")
        m = re.fullmatch(r"(?:start a )?raid (?:on |to )?@?([a-z0-9_][a-z0-9_ ]{1,30})", t)
        if m and m.group(1).strip() not in ("someone", "somebody", "now", "them", "it"):
            return Intent("tool", "twitch_raid", {"user": m.group(1).strip()})
        return None

    def say(self, tool, args, result):
        if result.get("ok") is False:
            return result.get("error")
        if tool == "raid_suggestions":
            chans = [c for c in result.get("channels", []) if not c["recent"]]
            if not chans:
                return f"Nobody else is live in {result.get('game') or 'your category'} right now."
            top = chans[0]
            more = len(chans) - 1
            return (f"Top pick: {top['name']}, {top['viewers']} watching" + (f" ({top['why'][0]})" if top["why"] else "")
                    + (f". {more} more on the HUD." if more else "."))
        if tool == "twitch_raid":
            return f"Raiding {result.get('user')}. Twitch counts down 90 seconds."
        return None
