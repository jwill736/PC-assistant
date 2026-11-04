"""YouTube Live next to Twitch (multistream): J's live broadcast, its chat, and its title and description.

Sign-in is the Google one Gmail and Calendar already use (integrations/google.py): Connect YouTube Live on the Stream
tab asks Google for one more box, "Manage your YouTube account", on top of what that account allowed (incremental
consent: nothing else is asked again). ``skills.stream_youtube.account`` picks the account if more than one allowed it.
Not connected: nothing here runs, and titles stay Twitch-only exactly as before.

While live (``svc.live()``, from OBS or Twitch):
- finds the broadcast that's live on YouTube (liveBroadcasts.list, status active); with none (a Twitch-only stream) it
  looks again less and less often, up to every 15 minutes;
- reads its chat into the feed Twitch chat goes to (``svc.twitch_feed``, ``platform: "youtube"``), so the co-pilot's
  questions, mood and greetings, clip-worthy moments' chat signal and the recap count it. Viewers wrote it: it is
  untrusted text wherever it goes, like Twitch chat (never spoken; tools that return it are marked untrusted).

Quota: Google gives a Cloud project 10,000 units a day (``daily_quota``), reset at midnight Pacific. A chat read costs 5
units, a broadcast lookup 5, a title change 51 (read + update). ``reserve`` (1,500) is kept for everything else and the
rest is spread over ``stream_hours`` (5) of streaming: 8,500 / 5 = 1,700 reads in 5 hours, one every ~11 s, and never
faster than YouTube's own pollingIntervalMillis. What's spent today is kept (table stream_youtube_quota), so a restart
doesn't start the count over; at the budget chat reading stops, with a note on the HUD.

Titles: "set the title to X" with YouTube connected asks once "Twitch and YouTube, or just Twitch?" and remembers the
answer (``title_on``: both | twitch); the change itself then waits for the usual yes (T2), like the Twitch one. "set the
YouTube title / description to …" is YouTube only. "titles on Twitch only" / "titles on both" changes the choice.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import webbrowser
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from ..brain.router import Intent, clean
from ..brain.tools import Tool, obj
from ..integrations.google import YOUTUBE_SCOPE, GoogleError
from . import Skill, check
from ._kit import save_setting
from ._stream import LiveState, run_on_click

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
PACIFIC = ZoneInfo("America/Los_Angeles")  # YouTube's quota day
COST = {"find": 5, "chat": 5, "video": 1, "update": 50, "me": 1}
TEXT_TYPES = {"textMessageEvent", "superChatEvent"}
ASK_S = 120  # "just Twitch" / "both" answers the question for this long
WHERE = {"both": "Twitch and YouTube", "twitch": "Twitch", "youtube": "YouTube"}
NOT_CONNECTED = "YouTube Live isn't connected: Stream tab → YouTube Live → Connect."
PAUSED = "Paused YouTube chat: today's quota budget is spent (it resets at midnight Pacific)."
CHAT_GONE = {"liveChatEnded": "The YouTube chat ended.", "liveChatNotFound": "That YouTube chat is gone.",
             "liveChatDisabled": "Chat is turned off on this YouTube stream.",
             "forbidden": "YouTube won't let this account read that chat."}


class YTLiveError(Exception):
    def __init__(self, text: str, reason: str = ""):
        super().__init__(text)
        self.reason = reason


def _reason(r: httpx.Response) -> tuple[str, str]:
    try:
        err = r.json().get("error") or {}
    except ValueError:
        return "", f"YouTube answered HTTP {r.status_code}."
    if not isinstance(err, dict):
        return "", f"YouTube said: {err or r.status_code}"
    reason = ((err.get("errors") or [{}])[0].get("reason") or "")
    text = {"quotaExceeded": "YouTube's daily quota is used up; it resets at midnight Pacific.",
            "rateLimitExceeded": "YouTube asked Vesper to slow down.",
            "insufficientPermissions": NOT_CONNECTED}.get(reason) or CHAT_GONE.get(reason) \
        or f"YouTube said: {err.get('message') or r.status_code}"
    return reason, text


def youtube_text(s: str, n: int) -> str:
    """YouTube refuses < and > in titles and descriptions."""
    return re.sub(r"[<>]", "", s or "").strip()[:n]


class StreamYouTube(Skill):
    name, title = "stream_youtube", "YouTube Live"
    poll_s = 5
    game_essential = True  # live chat while you stream; not live, a tick does nothing
    delay_s = 20

    def __init__(self, svc, clock=time.time, opener=webbrowser.open):
        super().__init__(svc)
        self.clock, self.opener = clock, opener
        self.state = LiveState(svc, clock, grace_s=90)
        self.broadcast: dict | None = None  # {"id", "title", "chat_id"}
        self.assistant = None  # set at start: who asked (the words as said, for a title's capitals)
        self.run_tool = None  # set at start: a HUD click on Both / Just Twitch runs the change as confirmed
        self.asking: dict | None = None  # {"title", "at"}: "Twitch and YouTube, or just Twitch?"
        self.error: str | None = None
        self.chat_note: str | None = None  # why chat isn't being read right now
        self._lock = threading.RLock()
        self._reset()
        svc.storage.execute("CREATE TABLE IF NOT EXISTS stream_youtube_quota (day TEXT PRIMARY KEY, units INTEGER)")

    def _reset(self) -> None:
        self.broadcast, self.page, self.read, self.interval = None, None, 0, 0.0
        self.next_find = self.next_chat = 0.0
        self.find_wait = 60.0

    def start(self, runtime) -> None:
        self.assistant = getattr(runtime, "assistant", None)
        self.run_tool = getattr(getattr(self.assistant, "tools", None), "run", None)

    # ---- connection & quota -------------------------------------------------------------------------------------
    def account(self) -> str | None:
        g = self.svc.google
        able = g.able(YOUTUBE_SCOPE) if g is not None else []
        want = str(self.setting("account") or "")
        return want if want in able else (able[0] if able else None)

    def connected(self) -> bool:
        return self.account() is not None

    def connect(self) -> dict:
        g = self.svc.google
        if g is None or not g.configured:
            return {"ok": False, "error": "Add the Google sign-in key first: Setup → Google accounts."}
        port = int(((self.svc.cfg.get("server") or {}).get("port")) or 8765)
        try:
            url = g.begin(f"http://127.0.0.1:{port}/api/google/callback", str(self.setting("account") or ""),
                          scopes=[YOUTUBE_SCOPE])
        except GoogleError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            self.opener(url)
        except Exception:
            pass
        return {"ok": True, "url": url}

    def _day(self) -> str:
        return datetime.fromtimestamp(self.clock(), PACIFIC).date().isoformat()

    def daily(self) -> int:
        return int(self.setting("daily_quota", 10000))

    def spent(self) -> int:
        row = self.svc.storage.query("SELECT units FROM stream_youtube_quota WHERE day = ?", (self._day(),))
        return int(row[0]["units"]) if row else 0

    def _spend(self, units: int, *, all_of_it: bool = False) -> None:
        day, st = self._day(), self.svc.storage
        st.execute("DELETE FROM stream_youtube_quota WHERE day <> ?", (day,))
        st.execute("INSERT INTO stream_youtube_quota (day, units) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET units = "
                   + ("MAX(units, excluded.units)" if all_of_it else "units + excluded.units"), (day, units))

    def _midnight(self) -> float:
        """When today's quota day began (midnight Pacific)."""
        day = datetime.fromtimestamp(self.clock(), PACIFIC)
        return day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def chat_interval(self, api_s: float) -> float | None:
        """Seconds to the next chat read: YouTube's own pace, or slower so the rest of today's budget lasts the stream
        (``stream_hours`` minus what's been streamed today, at least an hour). None: the budget is spent.

        Paced by the quota day: a late stream that runs past midnight Pacific starts on a new budget, and that day
        still has tonight's stream ahead of it. So the hours past midnight are paced as if a whole ``stream_hours``
        stream follows them, rather than spent at full speed as the last hour of this one."""
        left = (self.daily() - int(self.setting("reserve", 1500)) - self.spent()) // COST["chat"]
        if left <= 0:
            return None
        hours = float(self.setting("stream_hours", 5)) * 3600
        now, midnight = self.clock(), self._midnight()
        start = self.state.since if self.state.live else now
        if start < midnight:  # began before today's quota day: the rest of it, then tonight's stream
            rest = hours + max(3600.0, hours - (now - midnight))
        else:
            rest = max(3600.0, hours - (now - start))
        return round(max(api_s, float(self.setting("min_chat_s", 3)), rest / left), 1)

    def _call(self, method: str, path: str, units: int, *, params: dict | None = None, json: dict | None = None) -> dict:
        email = self.account()
        if email is None:
            raise YTLiveError(NOT_CONNECTED)
        if self.spent() + units > self.daily():
            raise YTLiveError("YouTube's daily quota is used up; it resets at midnight Pacific.", "quotaExceeded")
        try:
            r = self.svc.google.request(method, email, f"{API}/{path}", params=params, json=json)
        except GoogleError as exc:
            raise YTLiveError(str(exc)) from None
        except httpx.HTTPError as exc:
            raise YTLiveError(f"YouTube didn't answer ({type(exc).__name__}).") from None
        self._spend(units)  # a refused call costs quota too
        if r.status_code in (200, 201):
            return r.json()
        reason, text = _reason(r)
        if reason == "quotaExceeded":
            self._spend(self.daily(), all_of_it=True)
        raise YTLiveError(text, reason)

    # ---- the live broadcast and its chat -------------------------------------------------------------------------
    def _find(self, status: str = "active") -> dict | None:
        data = self._call("GET", "liveBroadcasts", COST["find"], params={
            "part": "snippet,status", "broadcastStatus": status, "broadcastType": "all", "maxResults": 5})
        items = sorted(data.get("items") or [], key=lambda b: (b.get("snippet") or {}).get("scheduledStartTime") or "")
        if not items:
            return None
        sn = items[0].get("snippet") or {}
        return {"id": items[0]["id"], "title": sn.get("title") or "", "chat_id": sn.get("liveChatId") or ""}

    def _look(self, now: float) -> None:
        b = self._find()
        if b is None:
            self.find_wait = min(900.0, self.find_wait * 2)
            self.next_find = now + self.find_wait
            self.chat_note = "You're not live on YouTube (Vesper looks again in a few minutes)."
            return
        self.broadcast, self.page, self.next_chat = b, None, now
        self.chat_note = None if b["chat_id"] else "Chat is turned off on this YouTube stream."
        self.notify("Live on YouTube too: its chat joins the co-pilot.", key=f"stream_youtube:found:{b['id']}")

    def _poll_chat(self, now: float) -> None:
        b = self.broadcast
        if self.chat_interval(0) is None:  # today's budget is spent: look again in 10 minutes (a new quota day)
            self.chat_note, self.next_chat = PAUSED, now + 600
            return
        params = {"liveChatId": b["chat_id"], "part": "snippet,authorDetails", "maxResults": 200}
        if self.page:
            params["pageToken"] = self.page
        try:
            data = self._call("GET", "liveChat/messages", COST["chat"], params=params)
        except YTLiveError as exc:
            if exc.reason in CHAT_GONE:
                self.broadcast, self.chat_note, self.next_find = None, str(exc), now + 120
                return
            if exc.reason == "rateLimitExceeded":
                self.next_chat = now + max(30.0, self.interval * 2)
                return
            raise
        first, self.page = self.page is None, data.get("nextPageToken") or self.page
        feed, n, got = self.svc.twitch_feed, 0, self.clock()  # got: when the read came back (Twitch chat kept coming)
        for m in data.get("items") or []:
            sn, who = m.get("snippet") or {}, m.get("authorDetails") or {}
            text = (sn.get("displayMessage") or "").strip()
            if sn.get("type") not in TEXT_TYPES or who.get("isChatOwner") or not text:
                continue  # J's own messages, deleted ones, polls, bans and the like
            if first and _ts(sn.get("publishedAt")) < now - 60:
                continue  # the backlog from before Vesper looked: not this stretch of the stream
            if feed is not None:  # arrival time (after the read): the co-pilot and moments read the feed in time order
                feed.add({"kind": "chat", "platform": "youtube", "user": (who.get("displayName") or "someone")[:40],
                          "login": f"yt:{who.get('channelId') or ''}", "text": text[:500], "ts": got + n * 1e-6})
            n += 1
        self.read += n
        if data.get("offlineAt"):
            self.broadcast, self.chat_note, self.next_find = None, "The YouTube stream ended.", now + 300
            return
        iv = self.chat_interval(float(data.get("pollingIntervalMillis") or 5000) / 1000)
        if iv is None:
            self.chat_note, self.next_chat = PAUSED, now + 600
            return
        self.interval, self.next_chat, self.chat_note = iv, now + iv, None

    def tick(self) -> None:
        now = self.clock()
        edge = self.state.update()
        before = (bool(self.broadcast), self.read // 25, self.error, self.chat_note)
        with self._lock:
            if edge:
                self._reset()
                self.chat_note = None
            try:
                if self.state.live and self.connected():
                    if self.broadcast is None and now >= self.next_find:
                        self._look(now)
                    if self.broadcast and self.broadcast["chat_id"] and now >= self.next_chat:
                        self._poll_chat(now)
                self.error = None
            except YTLiveError as exc:
                self.error = str(exc)
                self.next_find, self.next_chat = max(self.next_find, now + 120), max(self.next_chat, now + 60)
            except Exception as exc:  # never break the stream over YouTube
                log.exception("YouTube Live tick failed")
                self.error = f"{type(exc).__name__}: {exc}"
                self.next_find, self.next_chat = now + 120, now + 60
        if edge or before != (bool(self.broadcast), self.read // 25, self.error, self.chat_note) \
                or now - (self.last_status or {}).get("ts", 0) >= 60:
            self.publish()

    # ---- titles ---------------------------------------------------------------------------------------------------
    def targets(self) -> str | None:
        """Where "set the title" goes: both | twitch, or None (not asked yet)."""
        if not self.connected():
            return "twitch"
        on = str(self.setting("title_on") or "")
        return on if on in ("both", "twitch") else None

    def title_tool(self) -> str:
        """The tool for a title J said yes to elsewhere (the co-pilot's calendar card)."""
        return "stream_title" if self.targets() == "both" else "twitch_set_channel"

    def _cased(self, value: str) -> str:
        """The fast path hands over lower case: the words as J said them ("Ranked grind + Q&A")."""
        said = str((getattr(self.assistant, "_ctx", None) or {}).get("utterance") or "")
        if not value or value != value.lower() or not said:
            return value
        m = re.search(r"\b(?:title|description)\s+(?:to\s+)?(.+)$", said, re.I)
        if m and clean(m.group(1)) == clean(value):
            return m.group(1).strip(" .“”\"'")
        i = said.lower().find(value)
        return said[i:i + len(value)] if i >= 0 else value

    def ask_where(self, title: str = "") -> dict:
        title = self._cased((title or "").strip())
        if not title:
            return {"ok": False, "error": "What should the title be?"}
        self.asking = {"title": title, "at": self.clock()}
        self.publish()
        return {"ok": True, "title": title, "asking": True}

    def _asked(self) -> dict | None:
        if self.asking and self.clock() - self.asking["at"] < ASK_S:
            return self.asking
        return None

    def answer_where(self, where: str, from_hud: bool = False) -> dict:
        """Where titles go from now on. From the HUD's Both / Just Twitch buttons under the question, the click is
        also the yes for the title it shows (by voice, the answer leads to the usual yes question: see route)."""
        if where not in ("both", "twitch"):
            return {"ok": False, "error": "Both, or just Twitch?"}
        a = self._asked()
        save_setting(self, "title_on", where)
        self.asking = None
        self.publish()
        if a is None or not from_hud:
            return {"ok": True, "where": where}
        args = {"title": a["title"], "where": where}
        out = run_on_click(self.run_tool, self.svc.bus, "stream_title", args, f"set the title to {a['title']}")
        return {**out, "reply": self.say("stream_title", args, out) if out.get("ok") else out.get("error")}

    def _describe(self, a: dict) -> str:
        """The yes question; works out and pins where the change goes, so a yes runs exactly that. ``remember``: this
        is J's answer to "Twitch and YouTube, or just Twitch?", kept now (asked once, even if he then says no)."""
        for k in ("title", "description"):
            if a.get(k):
                a[k] = self._cased(str(a[k]))
        where = a.get("where") or self.targets() or "both"
        if where == "both" and not self.connected():
            where = "twitch"
        if not a.get("title"):
            where = "youtube"
        a["where"] = where
        if a.pop("remember", False) and where in ("both", "twitch"):
            save_setting(self, "title_on", where)
            self.asking = None
        parts = [f"set the stream title on {WHERE[where]} to “{a['title']}”"] if a.get("title") else []
        if a.get("description") and where != "twitch":
            parts.append(f"set the YouTube description to “{str(a['description'])[:80]}”")
        return " and ".join(parts) or "change the stream title"

    def set_title(self, title: str | None = None, description: str | None = None, where: str | None = None,
                  remember: bool = False) -> dict:
        where = where or self.targets() or "both"
        if remember and where in ("both", "twitch"):  # (the yes question already kept it; a direct call keeps it here)
            save_setting(self, "title_on", where)
        out: dict = {"where": where, "title": title, "description": bool(description)}
        if title and where in ("both", "twitch"):
            out["twitch"] = self.svc.twitch.set_channel(title=title)
        if where in ("both", "youtube") and (title or description):
            out["youtube"] = self.set_youtube(title, description)
        done = [k for k in ("twitch", "youtube") if (out.get(k) or {}).get("ok")]
        out["ok"], out["done"] = bool(done), done
        if not done:
            out["error"] = " ".join((out.get(k) or {}).get("error") or "" for k in ("twitch", "youtube")).strip() \
                or "Tell me the new title."
        return out

    def set_youtube(self, title: str | None = None, description: str | None = None) -> dict:
        if not self.connected():
            return {"ok": False, "error": NOT_CONNECTED}
        try:
            with self._lock:
                b = self.broadcast or self._find("active") or self._find("upcoming")
            if b is None:
                return {"ok": False, "error": "There's no YouTube stream to rename yet: YouTube makes one when you go "
                                              "live (or schedule one in YouTube Studio)."}
            if self.spent() + COST["video"] + COST["update"] > self.daily():
                return {"ok": False, "error": "YouTube's daily quota is used up; it resets at midnight Pacific."}
            items = self._call("GET", "videos", COST["video"], params={"part": "snippet", "id": b["id"]}).get("items") or []
            if not items:
                return {"ok": False, "error": "YouTube didn't find that stream any more."}
            sn = items[0].get("snippet") or {}
            # videos.update replaces the whole snippet: send back what's there (tags, category, language) unchanged
            keep = {k: sn[k] for k in ("title", "description", "categoryId", "tags", "defaultLanguage",
                                       "defaultAudioLanguage") if k in sn}
            if title:
                keep["title"] = youtube_text(title, 100)
            if description is not None:
                keep["description"] = youtube_text(description, 5000)
            self._call("PUT", "videos", COST["update"], params={"part": "snippet"}, json={"id": b["id"], "snippet": keep})
        except YTLiveError as exc:
            return {"ok": False, "error": str(exc)}
        if title and self.broadcast:
            self.broadcast["title"] = keep["title"]
        return {"ok": True, "title": keep.get("title"), "description": description is not None}

    # ---- the skill surface ----------------------------------------------------------------------------------------
    def status(self) -> dict:
        b, a = self.broadcast, self._asked()
        return {"connected": self.connected(), "account": self.account(),
                "google": bool(self.svc.google is not None and self.svc.google.configured),
                "live": self.state.live, "broadcast": {"id": b["id"], "title": b["title"]} if b else None,
                "read": self.read, "interval_s": self.interval, "note": self.chat_note, "error": self.error,
                "quota": {"spent": self.spent(), "daily": self.daily(),
                          "budget": self.daily() - int(self.setting("reserve", 1500))},
                "title_on": self.targets() if self.connected() else None, "asking": a and {"title": a["title"]},
                "ts": self.clock()}

    def live_status(self) -> dict:
        s = self.status()
        if not s["connected"]:
            return {"ok": False, "error": NOT_CONNECTED}
        return {"ok": True, **{k: s[k] for k in ("live", "broadcast", "read", "interval_s", "note", "quota", "title_on")}}

    def actions(self):
        return {"connect": lambda b: self.connect(),
                "where": lambda b: self.answer_where(str(b.get("where") or ""), from_hud=bool(b.get("run", True))),
                "look": lambda b: self._look_now()}

    def _look_now(self) -> dict:
        with self._lock:
            self.next_find = 0.0
        self.tick()
        return {"ok": True, **self.status()}

    def selftest(self) -> list[dict]:
        email = self.account()
        if email is None:
            return [check("YouTube Live", "skip", "not connected", "Optional: Stream tab → YouTube Live → Connect")]
        try:
            self._call("GET", "channels", COST["me"], params={"part": "id", "mine": "true"})
        except YTLiveError as exc:
            return [check("YouTube Live", "fail" if exc.reason != "quotaExceeded" else "warn", str(exc),
                          "Stream tab → YouTube Live → Connect")]
        return [check("YouTube Live", "pass", "signed in; chat and titles work")]

    def tools(self):
        return [
            Tool("stream_title", "Change the live stream's title on Twitch and YouTube, and/or the YouTube description. "
                                 "where: both, twitch or youtube (default: what the user chose before).",
                 obj({"title": {"type": "string"}, "description": {"type": "string"},
                      "where": {"type": "string", "enum": ["both", "twitch", "youtube"]}}),
                 self.set_title, tier=2, confirm_text=self._describe),
            Tool("stream_title_where", "Ask the user whether a new stream title goes to Twitch and YouTube, or just "
                                       "Twitch (once; the answer is remembered).",
                 obj({"title": {"type": "string"}}, ["title"]), self.ask_where),
            Tool("stream_title_on", "Where stream titles go from now on: both (Twitch and YouTube) or twitch.",
                 obj({"where": {"type": "string", "enum": ["both", "twitch"]}}, ["where"]),
                 lambda where: self.answer_where(where), tier=1),
            Tool("youtube_live", "YouTube Live now: connected, the live broadcast, chat messages read, the quota used "
                                 "today and where titles go.", obj(), self.live_status),
        ]

    def route(self, t: str):
        m = re.fullmatch(r"(?:set|change|update|make) (?:the |my )?youtube (?:stream |live )?(title|description) "
                         r"(?:to )?(.+)", t)
        if m:  # not connected: say so, rather than ask a yes for something that can't happen
            return Intent("tool", "stream_title", {m.group(1): m.group(2), "where": "youtube"}) if self.connected() \
                else Intent("tool", "youtube_live")
        if not self.connected():
            return None  # Twitch-only, exactly as before
        a = self._asked()
        if a:  # the answer to "Twitch and YouTube, or just Twitch?": then the change's own yes question
            if re.fullmatch(r"(?:on )?(?:both|both of them|twitch and youtube|youtube and twitch|both please|everywhere)", t):
                return Intent("tool", "stream_title", {"title": a["title"], "where": "both", "remember": True})
            if re.fullmatch(r"(?:just|only) (?:on )?twitch|twitch only|on twitch only|(?:on )?twitch", t):
                return Intent("tool", "stream_title", {"title": a["title"], "where": "twitch", "remember": True})
        m = re.fullmatch(r"(?:set|change|update|make) (?:the |my )?(?:stream |live )?description (?:to )?(.+)", t)
        if m:
            return Intent("tool", "stream_title", {"description": m.group(1), "where": "youtube"})
        m = re.fullmatch(r"(?:(?:put|send|set|change) )?(?:the |my )?(?:stream )?titles? (?:on|to) (both|twitch and youtube|twitch only"
                         r"|just twitch|only twitch)(?: from now on)?", t)
        if m:
            return Intent("tool", "stream_title_on", {"where": "both" if "youtube" in m.group(1) or m.group(1) == "both"
                                                      else "twitch"})
        m = re.fullmatch(r"(?:set|change|update|make) (?:the |my )?(?:stream |live )?title (?:to )?(.+)", t)
        if m:
            where = self.targets()
            if where is None:
                return Intent("tool", "stream_title_where", {"title": m.group(1)})
            if where == "both":
                return Intent("tool", "stream_title", {"title": m.group(1)})
        if re.fullmatch(r"(?:how(?:'s| is) )?(?:my )?youtube (?:live|chat|stream)(?: doing| going)?|is youtube (?:live )?connected", t):
            return Intent("tool", "youtube_live")
        return None

    def say(self, tool, args, result):
        if result.get("ok") is False:
            return result.get("error")
        if tool == "stream_title_where":
            return "Twitch and YouTube, or just Twitch?"
        if tool == "stream_title_on":
            return f"Stream titles go to {WHERE.get(result.get('where'), result.get('where'))} from now on."
        if tool == "stream_title":
            done, what = result.get("done") or [], "Title" if result.get("title") else "Description"
            if result.get("title") and result.get("description"):
                what = "Title and description"
            missed = [k for k in ("twitch", "youtube") if k in result and k not in done]
            line = f"{what} updated on {' and '.join(WHERE[k] for k in done)}."
            for k in missed:
                line += f" {WHERE[k]} didn't take it: {(result.get(k) or {}).get('error') or 'no reason given'}"
            return line
        if tool == "youtube_live":
            if not result.get("live") or not result.get("broadcast"):
                return "YouTube Live is connected; you're not live there right now."
            return f"Live on YouTube, {result['read']} chat messages so far. Details are on the HUD."
        return None


def _ts(iso: str | None) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0
