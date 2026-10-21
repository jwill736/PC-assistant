"""Discord: posts to your server through a channel webhook (no bot): "going live" when the stream starts, "announce
<text>" by voice (it waits for your yes: it's public), and "post my clip" with your latest Twitch clip.

The webhook link (Discord: Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL) goes in .env as
DISCORD_WEBHOOK_URL, pasted on the HUD. Anyone holding that link can post in the channel, so it stays in .env.

Never spams: one go-live post per stream (remembered across restarts), the same text at most once in 10 minutes, at
least 30 seconds between posts, and no @mentions unless you set skills.discord.mention (go-live posts only).
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill, check, lazy_client
from ._kit import LiveEdge, save_secret, save_setting

URL_ENV = "DISCORD_WEBHOOK_URL"
HOOK = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[\w-]+$")
TEMPLATE = "**{name} is live!** {title}{game}\n{link}"
MIN_GAP_S = 30
DEDUPE_S = 600
ONE_STREAM_S = 6 * 3600


class Discord(Skill):
    name, title = "discord", "Discord"
    poll_s = 20
    delay_s = 20

    def __init__(self, svc, http: httpx.Client | None = None, clock=time.time):
        super().__init__(svc)
        self.http = http or lazy_client(timeout=10)
        self.clock = clock
        self._lock = threading.RLock()
        self._live = LiveEdge(svc)
        self.state: dict = self._load()
        self.hook: dict | None = None  # the webhook's name and channel, from Discord
        self.error: str | None = None

    # ---- setup ---------------------------------------------------------------------------------------
    @property
    def url(self) -> str:
        return self.svc.cfg.secret(URL_ENV).strip()

    def _path(self):
        return self.svc.cfg.data_dir / "discord.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"posts": []}
        except (OSError, ValueError, AttributeError, KeyError):
            return {"posts": []}

    def _save(self) -> None:
        try:
            self._path().write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        except OSError:
            pass

    def set_webhook(self, url: str) -> dict:
        url = (url or "").strip().split("?")[0]
        if not HOOK.match(url):
            return {"ok": False, "error": "That isn't a Discord webhook link: Server Settings → Integrations → "
                                          "Webhooks → New Webhook → Copy Webhook URL."}
        info = self._lookup(url)
        if info.get("ok") is False:
            return info
        save_secret(self.svc, URL_ENV, url)
        return {"ok": True, "name": self.hook.get("name") if self.hook else None}

    def _lookup(self, url: str | None = None) -> dict:
        """The webhook's name (a GET: posts nothing)."""
        try:
            r = self.http.get(url or self.url)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"Couldn't reach Discord ({type(exc).__name__})."}
        if r.status_code in (401, 404):
            return {"ok": False, "error": "Discord doesn't know that webhook: it was deleted. Make a new one."}
        if r.status_code != 200:
            return {"ok": False, "error": f"Discord said {r.status_code}."}
        d = r.json()
        self.hook = {"name": d.get("name") or "Webhook", "channel_id": d.get("channel_id"), "guild_id": d.get("guild_id")}
        self.error = None
        return {"ok": True, **self.hook}

    # ---- posting -------------------------------------------------------------------------------------
    def post(self, text: str, kind: str = "manual", mention: str = "") -> dict:
        if not self.url:
            return {"ok": False, "error": "Discord isn't set up: paste a webhook link on the HUD (Stream → Discord)."}
        text = (text or "").strip()[:2000]
        if not text:
            return {"ok": False, "error": "Nothing to post."}
        now = self.clock()
        with self._lock:
            posts = self.state.setdefault("posts", [])
            if posts and now - posts[0]["at"] < MIN_GAP_S:
                return {"ok": False, "error": f"I posted {int(now - posts[0]['at'])} seconds ago: give it half a minute."}
            if any(p["text"] == text and now - p["at"] < DEDUPE_S for p in posts):
                return {"ok": False, "error": "That exact message already went out a moment ago.", "duplicate": True}
            content = f"{mention} {text}" if mention else text
            body = {"content": content[:2000], "allowed_mentions": _mentions(mention)}
            try:
                r = self.http.post(self.url, params={"wait": "true"}, json=body)
            except httpx.HTTPError as exc:
                self.error = f"Couldn't reach Discord ({type(exc).__name__})."
                return {"ok": False, "error": self.error}
            if r.status_code == 429:
                wait = _json(r).get("retry_after", 5)
                return {"ok": False, "error": f"Discord says slow down: try again in {round(float(wait))} seconds."}
            if r.status_code not in (200, 204):
                self.error = f"Discord refused the post: {_json(r).get('message') or r.status_code}"
                return {"ok": False, "error": self.error}
            self.error = None
            posts.insert(0, {"text": text, "at": now, "kind": kind, "id": _json(r).get("id")})
            del posts[20:]
            self._save()
        return {"ok": True, "kind": kind, "text": text, "channel": (self.hook or {}).get("name")}

    def announce(self, text: str) -> dict:
        return self.post(text, "manual")

    def stream_info(self, fetch: bool = False) -> dict:
        tw = (self.svc.bus.latest.get("twitch") or {}).get("data") or {}
        title, game, login = tw.get("title") or "", tw.get("game") or "", tw.get("channel") or ""
        twitch = getattr(self.svc, "twitch", None)
        try:
            if fetch and not title and twitch is not None:
                info = twitch.channel_info() or {}
                title, game = info.get("title") or "", game or info.get("game") or ""
            login = login or getattr(twitch, "login_name", "") or ""
        except Exception:
            pass
        return {"title": title, "game": game, "login": login, "uptime_s": tw.get("uptime_s") if tw.get("live") else None}

    def golive_message(self, fetch: bool = False) -> str:
        i = self.stream_info(fetch)
        name = self.setting("name") or i["login"] or self.svc.cfg["assistant"].get("user_name") or "We"
        values = {"name": name, "title": i["title"] or "come hang out", "game": f" · {i['game']}" if i["game"] else "",
                  "link": f"https://twitch.tv/{i['login']}" if i["login"] else ""}
        try:
            return str(self.setting("template") or TEMPLATE).format_map(_Blank(values)).strip()
        except (ValueError, IndexError):
            return TEMPLATE.format_map(values).strip()

    def announce_live(self, manual: bool = False) -> dict:
        now = self.clock()
        if not manual and now - float(self.state.get("last_golive") or 0) < ONE_STREAM_S:
            return {"ok": False, "error": "Already announced this stream."}
        out = self.post(self.golive_message(fetch=True), "golive", mention=str(self.setting("mention") or ""))
        if out["ok"]:
            self.state["last_golive"] = now
            self._save()
        self.notify("Posted your go-live to Discord." if out["ok"] else f"Discord go-live post failed: {out['error']}",
                    level="info" if out["ok"] else "warning", key=f"discord:golive:{int(now // 60)}")
        return out

    def latest_clip(self) -> dict | None:
        """The newest Twitch clip: from today's highlights, else Twitch's clip list for the past week."""
        try:
            for x in (self.svc.highlights() if self.svc.highlights else []):
                url = (x.get("detail") or {}).get("url") or ""
                if x.get("kind") == "clip" and "twitch.tv" in url:
                    return {"url": url, "title": x.get("reason", "").removeprefix("Twitch clip").strip(": ") or ""}
        except Exception:
            pass
        tw = getattr(self.svc, "twitch", None)
        if not (getattr(tw, "enabled", False) and hasattr(tw, "_get") and hasattr(tw, "broadcaster_id")):
            return None
        try:
            since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
            clips = tw._get("clips", {"broadcaster_id": tw.broadcaster_id(), "first": 20, "started_at": since})
        except Exception:
            return None
        clips = sorted((c for c in clips if c.get("url")), key=lambda c: c.get("created_at", ""), reverse=True)
        return {"url": clips[0]["url"], "title": clips[0].get("title", "")} if clips else None

    def post_clip(self) -> dict:
        clip = self.latest_clip()
        if clip is None:
            return {"ok": False, "error": "I don't have a Twitch clip yet: say “make a clip” first (Twitch has to be connected)."}
        out = self.post(f"{clip['title']}\n{clip['url']}".strip(), "clip")
        return {**out, "clip": clip}

    # ---- skill surface -------------------------------------------------------------------------------
    def tick(self) -> None:
        edge = self._live()
        if edge == "start" and self.url and self.setting("auto_announce", True):
            up = self.stream_info()["uptime_s"]
            if up is None or up < 900:  # Vesper started mid-stream: too late for "going live"
                self.announce_live()
        if self.url and self.hook is None:
            out = self._lookup()
            if not out.get("ok"):
                self.error = out["error"]
        self.publish()

    def selftest(self) -> list[dict]:
        if not self.url:
            return [check("Discord", "skip", "no webhook", "Optional: Stream → Discord")]
        r = self._lookup()  # a GET: posts nothing
        if r.get("ok") is False:
            return [check("Discord", "fail", r["error"], "Stream → Discord: paste a new webhook link")]
        return [check("Discord", "pass", f"webhook “{r.get('name')}” works")]

    def status(self) -> dict:
        step = None if self.url else "In Discord: Server Settings → Integrations → Webhooks → New Webhook → pick the " \
                                     "channel → Copy Webhook URL. Paste it here."
        return {"configured": bool(self.url), "next_step": step, "hook": self.hook, "error": self.error,
                "auto_announce": bool(self.setting("auto_announce", True)), "preview": self.golive_message(),
                "mention": self.setting("mention") or "", "live": self._live.was,
                "last_golive": self.state.get("last_golive"), "posts": self.state.get("posts", [])[:8]}

    def actions(self):
        return {"webhook": lambda b: self.set_webhook(str(b.get("url") or "")),
                "auto": lambda b: self.remember("auto_announce", bool(b.get("on"))),
                "template": lambda b: self.remember("template", str(b.get("template") or "").strip() or None),
                "mention": lambda b: self.remember("mention", str(b.get("mention") or "").strip() or None),
                "test": lambda b: self.post(f"Test from Vesper: this channel will get go-live posts. ({datetime.now(self.svc.tz):%H:%M})", "test"),
                "post": lambda b: self.post(str(b.get("text") or ""), "manual"),
                "golive": lambda b: self.announce_live(manual=True)}

    def remember(self, key: str, value) -> dict:
        if key == "mention" and value and not re.fullmatch(r"@everyone|@here|<@&\d+>", value):
            return {"ok": False, "error": "Mention @everyone, @here, or a role as <@&role id> (Discord: right-click the role → Copy Role ID)."}
        save_setting(self, key, value)
        return {"ok": True}

    def tools(self):
        return [
            Tool("discord_announce", "Post a message to the user's Discord server (public: needs their yes).",
                 obj({"text": {"type": "string"}}, ["text"]), self.announce, tier=2,
                 confirm_text=lambda a: f"post to Discord: “{a.get('text', '')}”"),
            Tool("discord_post_clip", "Post the user's latest Twitch clip to their Discord server (public).", obj(),
                 self.post_clip, tier=2, confirm_text=lambda a: "post your latest Twitch clip to Discord"),
        ]

    def route(self, t: str):
        if re.fullmatch(r"(?:post|share|send) (?:my |the )?(?:latest |last |new )?clip(?: (?:to|on|in) discord)?", t):
            return Intent("tool", "discord_post_clip")
        m = re.fullmatch(r"(?:announce|post|send|say) (?:on|in|to) discord (.+)|(?:announce|post|send|say) (.+?) (?:on|in|to) discord"
                         r"|announce (.+)", t)
        if m and (self.url or not m.group(3)):
            text = next(g for g in m.groups() if g)
            return Intent("tool", "discord_announce", {"text": text[:1].upper() + text[1:]})
        return None

    def say(self, tool, args, result):
        if tool == "discord_announce":
            return "Posted to Discord."
        if tool == "discord_post_clip":
            return "Posted your clip to Discord."
        return None


class _Blank(dict):
    def __missing__(self, key):  # an unknown {placeholder} in a custom template stays empty
        return ""


def _mentions(mention: str) -> dict:
    """Pings only what the go-live mention names; a voice announcement never pings anyone."""
    if mention in ("@everyone", "@here"):
        return {"parse": ["everyone"]}
    role = re.fullmatch(r"<@&(\d+)>", mention or "")
    return {"parse": [], "roles": [role.group(1)]} if role else {"parse": []}


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
