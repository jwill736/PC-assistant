"""YouTube: your channel's subscribers, views and latest videos, and how they're growing.

Public numbers, so no sign-in: an API key (Google Cloud → YouTube Data API v3 → Credentials → API key) in .env as
YOUTUBE_API_KEY, and your channel (@handle, a channel link, or its UC… ID) in skills.youtube.channel, both pasted on
the HUD. A refresh costs 3 of the 10,000 free quota units a day and is cached 30 minutes. One snapshot a day is kept
(table youtube_daily) for "subs +N this week" and the sparkline. YouTube rounds big subscriber counts (12,345 → 12,300).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

import httpx

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill
from ._kit import save_secret, save_setting

API = "https://www.googleapis.com/youtube/v3"
KEY_ENV = "YOUTUBE_API_KEY"
CACHE_S = 1800
FRESH_S = 60  # a "refresh" click sooner than this uses the cache (quota)


class YTError(Exception):
    pass


def parse_channel(text: str) -> tuple[str, str] | None:
    """'@name', 'name', a youtube.com/@name or /channel/UC… link, or a UC… ID -> ("id"|"handle", value)."""
    t = (text or "").strip()
    m = re.search(r"\b(UC[\w-]{22})\b", t)
    if m:
        return "id", m.group(1)
    m = re.search(r"youtube\.com/(@[\w.-]+)", t) or re.fullmatch(r"(@?[\w.-]{3,30})", t)
    if m:
        return "handle", m.group(1) if m.group(1).startswith("@") else "@" + m.group(1)
    return None


def _check(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        raise YTError(f"YouTube answered HTTP {r.status_code}.") from None
    if r.status_code == 200:
        return data
    err = data.get("error") or {}
    reason = ((err.get("errors") or [{}])[0].get("reason") or "") if isinstance(err, dict) else ""
    raise YTError({
        "quotaExceeded": "YouTube's daily quota is used up; it resets at midnight Pacific.",
        "keyInvalid": "YouTube refused the API key: copy it again from Google Cloud → Credentials.",
        "accessNotConfigured": "Turn on “YouTube Data API v3” for that key's Google Cloud project (APIs & Services → Library).",
        "ipRefererBlocked": "That API key is restricted to other websites or IPs: remove the restriction or make a new key.",
    }.get(reason) or f"YouTube said: {err.get('message') if isinstance(err, dict) else err or r.status_code}")


class YouTube(Skill):
    name, title = "youtube", "YouTube"
    poll_s = CACHE_S
    game_poll_s = 4 * 3600
    delay_s = 40

    def __init__(self, svc, http: httpx.Client | None = None, clock=time.time):
        super().__init__(svc)
        self.http = http or httpx.Client(timeout=15)
        self.clock = clock
        self._data: dict | None = None
        self._at = 0.0
        self._table = False
        self.error: str | None = None

    # ---- setup ---------------------------------------------------------------------------------------
    @property
    def key(self) -> str:
        return self.svc.cfg.secret(KEY_ENV).strip()

    def setup(self, key: str = "", channel: str = "") -> dict:
        key, channel = (key or "").strip(), (channel or "").strip()
        if key:
            if not re.fullmatch(r"AIza[\w-]{35}", key):
                return {"ok": False, "error": "That isn't a Google API key: it starts with AIza and is 39 characters."}
            save_secret(self.svc, KEY_ENV, key)
        if channel:
            if parse_channel(channel) is None:
                return {"ok": False, "error": "Paste your channel's @handle or its youtube.com link."}
            save_setting(self, "channel", channel)
        self._at = 0
        return self.stats(fresh=True)

    def next_step(self) -> str | None:
        if not self.key:
            return "Make an API key: Google Cloud → enable YouTube Data API v3 → Credentials → API key. Paste it here."
        if not self.setting("channel"):
            return "Paste your channel's @handle (or its youtube.com link)."
        return None

    # ---- numbers -------------------------------------------------------------------------------------
    def stats(self, fresh: bool = False) -> dict:
        age = self.clock() - self._at
        if self._data and (age < CACHE_S and not fresh or age < FRESH_S):
            return self._data
        step = self.next_step()
        if step:
            self.error = step
            return {"ok": False, "error": step}
        kind, value = parse_channel(str(self.setting("channel"))) or ("handle", "@")
        try:
            c = _check(self.http.get(f"{API}/channels", params={
                "part": "snippet,statistics,contentDetails", "key": self.key, "id" if kind == "id" else "forHandle": value}))
            if not c.get("items"):
                raise YTError(f"YouTube has no channel {value}: check the handle on your channel page.")
            ch = c["items"][0]
            uploads = ((ch.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
            ids = []
            if uploads:
                pl = _check(self.http.get(f"{API}/playlistItems", params={
                    "part": "contentDetails", "playlistId": uploads, "maxResults": 5, "key": self.key}))
                ids = [i["contentDetails"]["videoId"] for i in pl.get("items") or [] if i.get("contentDetails")]
            vids = _check(self.http.get(f"{API}/videos", params={
                "part": "snippet,statistics", "id": ",".join(ids), "key": self.key})).get("items", []) if ids else []
        except YTError as exc:
            self.error = str(exc)
            return {"ok": False, "error": self.error}
        except httpx.HTTPError as exc:
            self.error = f"YouTube didn't answer ({type(exc).__name__})."
            return {"ok": False, "error": self.error}
        self._data, self._at, self.error = self._shape(ch, vids), self.clock(), None
        return self._data

    def _shape(self, ch: dict, vids: list[dict]) -> dict:
        sn, st = ch.get("snippet") or {}, ch.get("statistics") or {}
        n = lambda d, k: int(d[k]) if str(d.get(k, "")).isdigit() else None  # noqa: E731
        data = {"ok": True, "id": ch.get("id"), "channel": sn.get("title") or "", "handle": sn.get("customUrl") or "",
                "thumb": ((sn.get("thumbnails") or {}).get("default") or {}).get("url"),
                "subs": None if st.get("hiddenSubscriberCount") else n(st, "subscriberCount"),
                "views": n(st, "viewCount"), "videos": n(st, "videoCount"), "at": self.clock(),
                "latest": [{"id": v["id"], "title": (v.get("snippet") or {}).get("title", ""),
                            "published": (v.get("snippet") or {}).get("publishedAt", ""),
                            "thumb": (((v.get("snippet") or {}).get("thumbnails") or {}).get("medium") or {}).get("url"),
                            "views": n(v.get("statistics") or {}, "viewCount"), "likes": n(v.get("statistics") or {}, "likeCount"),
                            "comments": n(v.get("statistics") or {}, "commentCount"),
                            "url": f"https://youtu.be/{v['id']}"} for v in vids if v.get("id")]}
        self._snapshot(data)
        data.update(history=self.history(data["id"]), week=self.delta(data, 7), day=self.delta(data, 1))
        return data

    # ---- daily snapshots -----------------------------------------------------------------------------
    def _today(self, days_ago: int = 0) -> str:
        return (datetime.fromtimestamp(self.clock(), self.svc.tz) - timedelta(days=days_ago)).date().isoformat()

    def _db(self):
        if not self._table:
            self.svc.storage.execute("CREATE TABLE IF NOT EXISTS youtube_daily (day TEXT, channel TEXT, subs INTEGER, "
                                     "views INTEGER, videos INTEGER, PRIMARY KEY (day, channel))")
            self._table = True
        return self.svc.storage

    def _snapshot(self, d: dict) -> None:
        self._db().execute("INSERT OR REPLACE INTO youtube_daily (day, channel, subs, views, videos) VALUES (?,?,?,?,?)",
                           (self._today(), d["id"], d["subs"], d["views"], d["videos"]))

    def history(self, channel: str, days: int = 90) -> list[dict]:
        return self._db().query("SELECT day, subs, views FROM youtube_daily WHERE channel = ? AND day >= ? ORDER BY day",
                                (channel, self._today(days)))

    def delta(self, d: dict, days: int) -> dict | None:
        """Growth since ``days`` ago (or since the first snapshot, if that's more recent)."""
        rows = self._db().query("SELECT day, subs, views FROM youtube_daily WHERE channel = ? AND day <= ? "
                                "ORDER BY day DESC LIMIT 1", (d["id"], self._today(days)))
        rows = rows or self._db().query("SELECT day, subs, views FROM youtube_daily WHERE channel = ? AND day < ? "
                                        "ORDER BY day LIMIT 1", (d["id"], self._today()))
        if not rows:
            return None
        old = rows[0]
        diff = lambda k: d[k] - old[k] if d.get(k) is not None and old.get(k) is not None else None  # noqa: E731
        return {"subs": diff("subs"), "views": diff("views"), "since": old["day"]}

    # ---- skill surface -------------------------------------------------------------------------------
    def tick(self) -> None:
        self.stats()
        self.publish()

    def status(self) -> dict:
        base = {"next_step": self.next_step(), "channel_setting": self.setting("channel") or "", "has_key": bool(self.key)}
        if self._data:
            return {**self._data, **base, "error": self.error, "stale": bool(self.error)}
        return {"ok": False, **base, "error": self.error}

    def briefing(self):
        d = self._data if self._data and self.clock() - self._at < 6 * 3600 else self.stats()
        if not d.get("ok") or d.get("subs") is None:
            return None
        return {"subs": d["subs"], "week": (d.get("week") or {}).get("subs")}

    def actions(self):
        return {"setup": lambda b: self.setup(str(b.get("key") or ""), str(b.get("channel") or "")),
                "refresh": lambda b: self.stats(fresh=True)}

    def tools(self):
        return [
            Tool("youtube_stats", "The user's YouTube channel: subscribers, total views, growth this week and today.",
                 obj(), self.stats),
            Tool("youtube_latest", "The user's latest YouTube videos with views, likes and comments.",
                 obj({"count": {"type": "integer"}}), self.latest),
        ]

    def latest(self, count: int = 3) -> dict:
        d = self.stats()
        if not d.get("ok"):
            return d
        return {"ok": True, "channel": d["channel"], "videos": d["latest"][:max(1, min(5, int(count or 3)))]}

    def route(self, t: str):
        if re.fullmatch(r"how(?:'s| is) my youtube(?: channel)?(?: doing| going)?|(?:my )?youtube (?:stats|numbers|channel stats)"
                        r"|how many (?:youtube )?(?:subscribers|subs) do i have on youtube|how many youtube (?:subscribers|subs)"
                        r"(?: do i have)?|how(?:'s| is) youtube (?:doing|going)", t):
            return Intent("tool", "youtube_stats")
        if re.fullmatch(r"how(?:'s| is| are) my (?:latest|last|newest|recent) (?:youtube )?videos?(?: doing)?(?: on youtube)?"
                        r"|(?:my )?(?:latest|recent|newest) youtube videos?", t):
            return Intent("tool", "youtube_latest", {"count": 3})
        return None

    def say(self, tool, args, result):
        if tool == "youtube_stats":
            if result.get("subs") is None:
                line = "Your subscriber count is hidden on YouTube."
            else:
                wk = (result.get("week") or {}).get("subs")
                trend = f", {'up' if wk > 0 else 'down'} {abs(wk):,} this week" if wk else (", flat this week" if wk == 0 else "")
                line = f"{result['subs']:,} subscribers on YouTube{trend}."
            return line + (f" {result['views']:,} views in all." if result.get("views") is not None else "")
        if tool == "youtube_latest":
            vids = result.get("videos") or []
            if not vids:
                return "No videos on the channel yet."
            v = vids[0]
            extra = f" and {v['likes']:,} likes" if v.get("likes") is not None else ""
            return f"Your latest, “{v['title']}”, has {v.get('views') or 0:,} views{extra}."
        return None
