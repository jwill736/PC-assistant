"""Twitch EventSub over WebSocket: follows, subs, raids, cheers, redemptions and chat, live.

No public server or open port is needed: the app dials out to Twitch, gets a
session id in the welcome message, and has 10 seconds to subscribe to events
with the logged-in user's token. Twitch sends a keepalive when nothing else
happens; silence past the keepalive window means the connection is dead, so
the loop reconnects and subscribes again. A ``session_reconnect`` message
(Twitch moving you to another edge) is followed without losing events: the
new connection is opened before the old one is closed, and subscriptions carry
over.

Viewer-written text (chat, cheer and sub messages, redemption input) is kept
for the HUD and for Claude to *read* when asked, marked untrusted. It is never
spoken aloud and never treated as a command.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import threading
import time
from collections import deque
from typing import Callable

log = logging.getLogger(__name__)

WS_URL = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30"

# type -> (version, condition builder(broadcaster_id, user_id))
SUBSCRIPTIONS: dict[str, tuple[str, Callable[[str, str], dict]]] = {
    "stream.online": ("1", lambda b, u: {"broadcaster_user_id": b}),
    "stream.offline": ("1", lambda b, u: {"broadcaster_user_id": b}),
    "channel.follow": ("2", lambda b, u: {"broadcaster_user_id": b, "moderator_user_id": u}),
    "channel.subscribe": ("1", lambda b, u: {"broadcaster_user_id": b}),
    "channel.subscription.gift": ("1", lambda b, u: {"broadcaster_user_id": b}),
    "channel.subscription.message": ("1", lambda b, u: {"broadcaster_user_id": b}),
    "channel.cheer": ("1", lambda b, u: {"broadcaster_user_id": b}),
    "channel.raid": ("1", lambda b, u: {"to_broadcaster_user_id": b}),
    "channel.channel_points_custom_reward_redemption.add": ("1", lambda b, u: {"broadcaster_user_id": b}),
    "channel.hype_train.begin": ("2", lambda b, u: {"broadcaster_user_id": b}),
    "channel.chat.message": ("1", lambda b, u: {"broadcaster_user_id": b, "user_id": u}),
}

TIERS = {"1000": "tier 1", "2000": "tier 2", "3000": "tier 3", "prime": "Prime"}


def normalize(sub_type: str, e: dict) -> dict | None:
    """One EventSub notification -> {kind, user, amount, text, detail}. ``text`` is viewer-written."""
    name = e.get("user_name") or e.get("user_login")
    if sub_type == "channel.follow":
        return {"kind": "follow", "user": name}
    if sub_type == "channel.subscribe":
        if e.get("is_gift"):
            return None  # the gift event already announced it
        return {"kind": "sub", "user": name, "detail": {"tier": TIERS.get(e.get("tier"), e.get("tier"))}}
    if sub_type == "channel.subscription.message":
        return {"kind": "resub", "user": name, "amount": e.get("cumulative_months"),
                "text": (e.get("message") or {}).get("text") or "",
                "detail": {"tier": TIERS.get(e.get("tier"), e.get("tier")), "streak": e.get("streak_months")}}
    if sub_type == "channel.subscription.gift":
        return {"kind": "gift", "user": None if e.get("is_anonymous") else name, "amount": e.get("total"),
                "detail": {"tier": TIERS.get(e.get("tier"), e.get("tier"))}}
    if sub_type == "channel.cheer":
        return {"kind": "cheer", "user": None if e.get("is_anonymous") else name, "amount": e.get("bits"),
                "text": e.get("message") or ""}
    if sub_type == "channel.raid":
        return {"kind": "raid", "user": e.get("from_broadcaster_user_name") or e.get("from_broadcaster_user_login"),
                "amount": e.get("viewers"), "detail": {"login": e.get("from_broadcaster_user_login")}}
    if sub_type == "channel.channel_points_custom_reward_redemption.add":
        return {"kind": "redemption", "user": name, "amount": (e.get("reward") or {}).get("cost"),
                "text": e.get("user_input") or "", "detail": {"reward": (e.get("reward") or {}).get("title")}}
    if sub_type == "channel.hype_train.begin":
        return {"kind": "hype_train", "user": None, "amount": e.get("level"), "detail": {"total": e.get("total")}}
    if sub_type == "channel.chat.message":
        return {"kind": "chat", "user": e.get("chatter_user_name") or e.get("chatter_user_login"),
                "text": (e.get("message") or {}).get("text") or "",
                "detail": {"login": e.get("chatter_user_login")}}
    if sub_type in ("stream.online", "stream.offline"):
        return {"kind": "online" if sub_type == "stream.online" else "offline", "user": None}
    return None


def callout(ev: dict, cfg: dict) -> str | None:
    """What to say out loud for an event, or None. Only names and numbers, never viewer text."""
    kind, who = ev["kind"], ev.get("user") or "Someone"
    n = ev.get("amount")
    if kind == "raid" and cfg.get("raid", True):
        return f"Raid from {who} with {n} viewer{'s' if n != 1 else ''}. Want me to shout them out?"
    if kind == "gift" and cfg.get("gift", True):
        return f"{who} just gifted {n} sub{'s' if n != 1 else ''}."
    if kind == "sub" and cfg.get("sub", True):
        return f"{who} just subscribed."
    if kind == "resub" and cfg.get("sub", True):
        return f"{who} resubscribed, {n} months."
    if kind == "cheer" and n and n >= int(cfg.get("cheer_min", 100)):
        return f"{who} cheered {n} bits."
    if kind == "follow" and cfg.get("follow", False):
        return f"{who} followed."
    if kind == "redemption" and cfg.get("redemption", False):
        return f"{who} redeemed {ev.get('detail', {}).get('reward')}."
    if kind == "hype_train" and cfg.get("hype_train", True):
        return "A hype train just started."
    return None


class TwitchFeed:
    """Recent events and chat for the HUD, Claude and the highlight detector."""

    def __init__(self, keep_events: int = 100, keep_chat: int = 300, clock=time.time):
        self.events: deque = deque(maxlen=keep_events)
        self.chat: deque = deque(maxlen=keep_chat)
        self.names: dict[str, str] = {}  # squashed spoken form -> login, for "shout out <name>"
        self.clock = clock
        self._lock = threading.Lock()

    def add(self, ev: dict) -> dict:
        ev = {**ev, "ts": ev.get("ts") or self.clock()}
        with self._lock:
            if ev["kind"] == "chat":
                self.chat.append(ev)
            else:
                self.events.appendleft(ev)
            login = (ev.get("detail") or {}).get("login") or (ev.get("user") or "").lower()
            if login:
                self.names[_squash(ev.get("user") or login)] = login
                self.names[_squash(login)] = login
        return ev

    def recent_events(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return [{k: v for k, v in e.items() if k != "text"} for e in list(self.events)[:limit]]

    def recent_chat(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [{"user": m["user"], "text": m["text"][:300], "ts": m["ts"]} for m in list(self.chat)[-limit:]]

    def resolve(self, spoken: str) -> str:
        """A name as heard ("john doe", "tenz") -> the login of someone seen recently, else the squashed name."""
        key = _squash(spoken)
        with self._lock:
            if key in self.names:
                return self.names[key]
            close = difflib.get_close_matches(key, list(self.names), n=1, cutoff=0.75)
            return self.names[close[0]] if close else key

    def snapshot(self) -> dict:
        with self._lock:
            return {"events": [{k: v for k, v in e.items() if k != "text"} for e in list(self.events)[:25]],
                    "chat_count": len(self.chat)}


def _close(ws) -> None:
    try:
        ws.close()
    except Exception:
        pass


def _squash(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", (name or "").lower().replace(" ", ""))


class EventSub:
    """The WebSocket loop. ``twitch`` supplies ``subscribe_eventsub(type, version, condition, session_id)``
    and ``ids()`` -> (broadcaster_id, user_id)."""

    def __init__(self, twitch, on_event: Callable[[str, dict], None], connect=None, url: str = WS_URL,
                 types: list[str] | None = None, on_state: Callable[[dict], None] | None = None):
        self.twitch, self.on_event, self.url = twitch, on_event, url
        self.types = types or list(SUBSCRIPTIONS)
        self.on_state = on_state or (lambda s: None)
        self._connect = connect
        self.state, self.error = "stopped", ""
        self.subscribed: list[str] = []
        self.failed: dict[str, str] = {}
        self.heartbeat: Callable[[], None] = lambda: None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: deque = deque(maxlen=200)  # message ids: Twitch may resend one

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="twitch-eventsub", daemon=True)
        self._thread.start()
        return self._thread

    restart = start

    def stop(self) -> None:
        self._stop.set()
        self._set("stopped")

    def status(self) -> dict:
        return {"state": self.state, "error": self.error, "subscribed": self.subscribed, "failed": self.failed}

    def _set(self, state: str, error: str = "") -> None:
        self.state, self.error = state, error
        self.on_state(self.status())

    # ---- the loop --------------------------------------------------------
    def _loop(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            self._set("connecting")
            try:
                self._session()
                backoff = 2.0
                continue
            except Exception as exc:  # network drop, Twitch closing with an error code, bad token
                log.info("EventSub connection ended: %s", exc)
                self._set("reconnecting", str(exc)[:200])
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60.0)

    def _open(self, url: str):
        if self._connect is None:
            from websockets.sync.client import connect

            self._connect = lambda u: connect(u, open_timeout=15, close_timeout=2, max_size=2 ** 20)
        return self._connect(url)

    def _welcome(self, ws) -> dict:
        msg = self._recv(ws, 15)
        if (msg.get("metadata") or {}).get("message_type") != "session_welcome":
            raise ConnectionError(f"expected a welcome, got {msg.get('metadata')}")
        return msg["payload"]["session"]

    def _session(self) -> None:
        """One session, which may move between connections when Twitch asks it to."""
        ws = self._open(self.url)
        try:
            session = self._welcome(ws)
            self._subscribe(session["id"])
            self._set("connected")
            while not self._stop.is_set():
                msg = self._recv(ws, float(session.get("keepalive_timeout_seconds") or 30) + 5)
                self.heartbeat()
                meta = msg.get("metadata") or {}
                kind = meta.get("message_type")
                if meta.get("message_id") in self._seen:
                    continue
                self._seen.append(meta.get("message_id"))
                if kind == "notification":
                    sub_type = meta.get("subscription_type") or msg["payload"]["subscription"]["type"]
                    try:
                        self.on_event(sub_type, msg["payload"].get("event") or {})
                    except Exception:
                        log.exception("EventSub handler failed for %s", sub_type)
                elif kind == "session_reconnect":
                    # Open the new connection and take its welcome before closing the old one;
                    # the subscriptions move with the session, so nothing is resubscribed.
                    new_ws = self._open(msg["payload"]["session"]["reconnect_url"])
                    try:
                        session = self._welcome(new_ws)
                    except Exception:
                        _close(new_ws)
                        raise
                    ws, old = new_ws, ws
                    _close(old)
                    log.info("EventSub moved to a new connection")
                elif kind == "revocation":
                    sub = msg["payload"]["subscription"]
                    self.failed[sub["type"]] = f"revoked: {sub.get('status')}"
                    if sub["type"] in self.subscribed:
                        self.subscribed.remove(sub["type"])
        finally:
            _close(ws)

    def _recv(self, ws, timeout: float) -> dict:
        try:
            raw = ws.recv(timeout=timeout)
        except TimeoutError:
            raise ConnectionError(f"no message from Twitch for {timeout:.0f}s") from None
        return json.loads(raw)

    def _subscribe(self, session_id: str) -> None:
        broadcaster, user = self.twitch.ids()
        self.subscribed, self.failed = [], {}
        for sub_type in self.types:
            version, cond = SUBSCRIPTIONS[sub_type]
            out = self.twitch.subscribe_eventsub(sub_type, version, cond(broadcaster, user), session_id)
            if out.get("ok"):
                self.subscribed.append(sub_type)
            else:
                self.failed[sub_type] = out.get("error", "failed")
        if not self.subscribed:
            raise ConnectionError("no event subscriptions were accepted: " + "; ".join(
                f"{k}: {v}" for k, v in list(self.failed.items())[:3]))
