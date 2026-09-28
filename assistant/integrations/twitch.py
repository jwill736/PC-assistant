"""Twitch: live status, your login, and the actions a streamer does by hand mid-stream.

Login uses Twitch's Device Code flow, made for apps like this one: Vesper
shows a short code, you type it at twitch.tv/activate (or click the link), and
that's it. Register the app as a *Public* client and no secret is needed at all;
only the Client ID goes in ``.env``. Tokens are saved to ``data/twitch_token.json``,
refreshed before they expire (a public client's refresh token works once, so
each new one is saved straight away) and validated hourly, as Twitch requires.

Without a login, a Client ID *and* secret still give read-only status (live,
viewers, title) through an app token, as before.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger(__name__)

ID = "https://id.twitch.tv/oauth2"
HELIX = "https://api.twitch.tv/helix"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SCOPES = (
    "clips:edit channel:manage:broadcast channel:edit:commercial moderator:manage:shoutouts channel:manage:polls "
    "user:write:chat user:read:chat moderator:read:followers channel:read:subscriptions bits:read "
    "channel:read:redemptions channel:read:hype_train"
)
# What each scope unlocks, for "reconnect Twitch to allow X" messages.
NEEDS = {"clips:edit": "clips", "channel:manage:broadcast": "markers and title changes",
         "channel:edit:commercial": "ads", "moderator:manage:shoutouts": "shoutouts", "channel:manage:polls": "polls",
         "user:write:chat": "sending chat messages"}


class TwitchAuth:
    """The logged-in user's token: device-code login, save, refresh, validate."""

    def __init__(self, client_id: str, client_secret: str = "", path: Path | None = None,
                 http: httpx.Client | None = None, scopes: str = SCOPES, clock=time.time):
        self.client_id, self.client_secret, self.path = client_id, client_secret, path
        self.http = http or httpx.Client(timeout=15)
        self.scopes, self.clock = scopes, clock
        self.token: dict = self._load()
        self.login: dict | None = None      # the device-code flow in progress
        self.error = ""
        self.on_change: Callable[[dict], None] = lambda status: None
        self._lock = threading.RLock()
        self._cancel = threading.Event()

    # ---- storage ---------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path and self.path.exists() else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.token), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("couldn't save the Twitch token: %s", exc)

    @property
    def connected(self) -> bool:
        return bool(self.token.get("refresh_token") or self.token.get("access_token"))

    def user(self) -> tuple[str | None, str | None]:
        return self.token.get("user_id"), self.token.get("login")

    def status(self) -> dict:
        login = self.login
        return {"connected": self.connected, "login": self.token.get("login"),
                "scopes": self.token.get("scopes") or [], "error": self.error,
                "pending": {k: login[k] for k in ("user_code", "verification_uri", "expires_at")} if login else None}

    # ---- device-code login ------------------------------------------------
    def start_login(self, background: bool = True) -> dict:
        """Ask Twitch for a code to show the user; poll for the token in the background."""
        if not self.client_id:
            return {"ok": False, "error": "Add TWITCH_CLIENT_ID to the .env file first (Setup guide, Twitch step)."}
        try:
            r = self.http.post(f"{ID}/device", data={"client_id": self.client_id, "scopes": self.scopes})
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"Couldn't reach Twitch: {exc}"}
        if r.status_code != 200:
            return {"ok": False, "error": f"Twitch refused the login ({r.status_code}): {_message(r)}"}
        body = r.json()
        self._cancel.set()  # stop a previous flow's poller
        self._cancel = threading.Event()
        self.login = {"device_code": body["device_code"], "user_code": body["user_code"],
                      "verification_uri": body["verification_uri"], "interval": float(body.get("interval", 5)),
                      "expires_at": self.clock() + float(body.get("expires_in", 1800))}
        self.error = ""
        self.on_change(self.status())
        if background:
            threading.Thread(target=self._poll_login, args=(self._cancel,), name="twitch-login", daemon=True).start()
        return {"ok": True, "user_code": body["user_code"], "verification_uri": body["verification_uri"]}

    def poll_once(self) -> str:
        """One token request: ok | pending | slow_down | failed."""
        login = self.login
        if not login:
            return "failed"
        if self.clock() > login["expires_at"]:
            self.login, self.error = None, "The Twitch code expired. Say “connect Twitch” to get a new one."
            return "failed"
        data = {"client_id": self.client_id, "scopes": self.scopes, "device_code": login["device_code"],
                "grant_type": DEVICE_GRANT}
        if self.client_secret:
            data["client_secret"] = self.client_secret
        try:
            r = self.http.post(f"{ID}/token", data=data)
        except httpx.HTTPError:
            return "pending"  # a network blip: try again next interval
        if r.status_code == 200:
            self._store(r.json())
            self.login = None
            ok = self.validate(force=True)
            if not ok:
                return "failed"
            return "ok"
        msg = _message(r)
        if "pending" in msg:
            return "pending"
        if "slow" in msg:
            login["interval"] += 5
            return "slow_down"
        self.login, self.error = None, f"Twitch login failed: {msg or r.status_code}"
        return "failed"

    def _poll_login(self, cancel: threading.Event) -> None:
        while self.login and not cancel.is_set():
            if cancel.wait(self.login["interval"]):
                return
            result = self.poll_once()
            if result in ("ok", "failed"):
                self.on_change(self.status())
                return

    def cancel_login(self) -> None:
        self._cancel.set()
        self.login = None

    # ---- tokens ------------------------------------------------------------
    def _store(self, body: dict) -> None:
        with self._lock:
            self.token.update({
                "access_token": body["access_token"],
                "refresh_token": body.get("refresh_token") or self.token.get("refresh_token"),
                "expires_at": self.clock() + float(body.get("expires_in", 14000)),
                "scopes": body.get("scope") or self.token.get("scopes") or [],
            })
            self._save()

    def access_token(self) -> str | None:
        """A token good for at least another minute, refreshing it if needed."""
        with self._lock:
            if not self.connected:
                return None
            if self.token.get("expires_at", 0) - 60 < self.clock() and not self.refresh():
                return None
            return self.token.get("access_token")

    def refresh(self) -> bool:
        with self._lock:
            rt = self.token.get("refresh_token")
            if not rt:
                return False
            data = {"client_id": self.client_id, "grant_type": "refresh_token", "refresh_token": rt}
            if self.client_secret:
                data["client_secret"] = self.client_secret
            try:
                r = self.http.post(f"{ID}/token", data=data)
            except httpx.HTTPError as exc:
                self.error = f"Couldn't reach Twitch to refresh the login: {exc}"
                return False
            if r.status_code == 200:
                self._store(r.json())  # the old refresh token is spent: save the new one now
                self.error = ""
                return True
            if r.status_code in (400, 401):  # revoked, expired (30 days unused) or already used
                self.forget("Your Twitch login expired. Say “connect Twitch” to log in again.")
            return False

    def validate(self, force: bool = False) -> bool:
        """Twitch requires a validate call at startup and hourly; it also tells us who logged in."""
        with self._lock:
            if not self.connected:
                return False
            if not force and self.clock() - self.token.get("validated_at", 0) < 3600:
                return True
            token = self.access_token()
            if not token:
                return False
            try:
                r = self.http.get(f"{ID}/validate", headers={"Authorization": f"OAuth {token}"})
            except httpx.HTTPError:
                return True  # offline: keep the token, try again later
            if r.status_code == 401 and self.refresh():
                return self.validate(force=True)
            if r.status_code != 200:
                self.forget("Twitch no longer accepts the saved login. Say “connect Twitch” to log in again.")
                return False
            body = r.json()
            if body.get("client_id") and body["client_id"] != self.client_id:
                self.forget("The saved Twitch login belongs to a different app. Say “connect Twitch” again.")
                return False
            self.token.update({"login": body.get("login"), "user_id": body.get("user_id"),
                               "scopes": body.get("scopes") or self.token.get("scopes") or [],
                               "validated_at": self.clock()})
            self._save()
            return True

    def forget(self, why: str = "") -> None:
        with self._lock:
            self.token, self.error = {}, why
            if self.path:
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass
        self.on_change(self.status())

    def logout(self) -> dict:
        token = self.token.get("access_token")
        if token:
            try:
                self.http.post(f"{ID}/revoke", data={"client_id": self.client_id, "token": token})
            except httpx.HTTPError:
                pass
        self.forget()
        return {"ok": True}


class TwitchClient:
    def __init__(self, channel: str, client_id: str, client_secret: str = "", enabled: bool = True,
                 token_path: Path | None = None, http: httpx.Client | None = None, clock=time.time):
        self.channel = channel.strip().lower()
        if self.channel == "your_channel":  # the example config's placeholder
            self.channel = ""
        self.client_id, self.client_secret = client_id, client_secret
        self.http = http or httpx.Client(timeout=10)
        self.clock = clock
        self.auth = TwitchAuth(client_id, client_secret, token_path, self.http, clock=clock)
        self.configured = enabled and bool(client_id)
        self._token: tuple[str, float] | None = None
        self._user_id: str | None = None
        self._ids: dict[str, str] = {}  # login -> user id

    @property
    def enabled(self) -> bool:
        """Status can be read: logged in, or an app token from the Client ID + secret."""
        return self.configured and bool(self.auth.connected or (self.client_secret and self.channel))

    @property
    def login_name(self) -> str:
        return self.channel or (self.auth.token.get("login") or "")

    # ---- HTTP ------------------------------------------------------------
    def _app_token(self) -> str:
        if self._token and self._token[1] > self.clock() + 60:
            return self._token[0]
        resp = self.http.post(f"{ID}/token", params={
            "client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "client_credentials",
        })
        resp.raise_for_status()
        body = resp.json()
        self._token = (body["access_token"], self.clock() + int(body.get("expires_in", 3600)))
        return self._token[0]

    def _bearer(self, user: bool) -> str | None:
        token = self.auth.access_token() if (user or self.auth.connected) else None
        if token is None and not user and self.client_secret:
            token = self._app_token()
        return token

    def _call(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
              user: bool = True) -> httpx.Response:
        """One Helix call; a 401 refreshes the login once and retries."""
        for attempt in (1, 2):
            token = self._bearer(user)
            if token is None:
                raise NotConnected()
            r = self.http.request(method, f"{HELIX}/{path}", params=params, json=body,
                                  headers={"Client-Id": self.client_id, "Authorization": f"Bearer {token}"})
            if r.status_code == 401 and attempt == 1 and self.auth.connected and self.auth.refresh():
                continue
            return r
        return r

    def _get(self, path: str, params: dict) -> list[dict]:
        r = self._call("GET", path, params=params, user=False)
        r.raise_for_status()
        return r.json().get("data", [])

    def _user_id_of(self, login: str) -> str | None:
        login = login.lower().lstrip("@")
        if login not in self._ids:
            users = self._get("users", {"login": login})
            if not users:
                return None
            self._ids[login] = users[0]["id"]
        return self._ids[login]

    def broadcaster_id(self) -> str | None:
        if self._user_id is None:
            if self.channel and self.channel != self.auth.token.get("login"):
                self._user_id = self._user_id_of(self.channel)
            else:
                self.auth.validate()
                self._user_id = self.auth.token.get("user_id")
        return self._user_id

    def ids(self) -> tuple[str | None, str | None]:
        """(broadcaster, logged-in user): the same person unless you log in as an editor or mod."""
        self.auth.validate()
        return self.broadcaster_id(), self.auth.token.get("user_id")

    # ---- reads -----------------------------------------------------------
    def status(self) -> dict:
        if not self.configured:
            return {"enabled": False}
        auth = self.auth.status()
        if not self.enabled:
            return {"enabled": True, "needs_login": True, "auth": auth}
        channel = self.login_name
        try:
            data = self._get("streams", {"user_login": channel}) if channel else []
        except (httpx.HTTPError, NotConnected) as exc:
            return {"enabled": True, "error": str(exc), "auth": auth}
        if not data:
            return {"enabled": True, "channel": channel, "live": False, "auth": auth}
        s = data[0]
        started = datetime.fromisoformat(s["started_at"].replace("Z", "+00:00"))
        return {
            "enabled": True, "channel": channel, "live": True, "auth": auth,
            "viewers": s.get("viewer_count"), "title": s.get("title"), "game": s.get("game_name"),
            "uptime_s": (datetime.now(timezone.utc) - started).total_seconds(),
        }

    def channel_info(self) -> dict:
        """Title and category as they stand now (set before going live), for the pre-stream check."""
        if not self.enabled:
            return {"enabled": False}
        try:
            bid = self.broadcaster_id()
            if not bid:
                return {"enabled": True, "error": f"no Twitch channel called {self.login_name}"}
            data = self._get("channels", {"broadcaster_id": bid})
        except (httpx.HTTPError, NotConnected) as exc:
            return {"enabled": True, "error": str(exc)}
        c = data[0] if data else {}
        return {"enabled": True, "title": c.get("title") or "", "game": c.get("game_name") or ""}

    # ---- actions (need the login) -------------------------------------------
    def _act(self, what: str, scope: str, fn: Callable[[str], dict]) -> dict:
        if not self.configured:
            return {"ok": False, "error": "Twitch isn't set up. Add TWITCH_CLIENT_ID to .env and set twitch.enabled."}
        if not self.auth.connected:
            return {"ok": False, "error": f"To {what}, connect Twitch first: say “connect Twitch”.", "needs_login": True}
        granted = self.auth.token.get("scopes")
        if granted and scope not in granted:
            return {"ok": False, "error": f"Your Twitch login doesn't allow {NEEDS.get(scope, scope)}. "
                                          "Say “connect Twitch” to log in again and allow it."}
        try:
            bid = self.broadcaster_id()
            if not bid:
                return {"ok": False, "error": f"No Twitch channel called {self.login_name}."}
            return fn(bid)
        except NotConnected:
            return {"ok": False, "error": "Your Twitch login expired. Say “connect Twitch”.", "needs_login": True}
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"Couldn't reach Twitch: {exc}"}

    def create_clip(self, title: str | None = None, duration: float | None = None) -> dict:
        def run(bid: str) -> dict:
            params: dict = {"broadcaster_id": bid}
            if title:
                params["title"] = title[:100]
            if duration:
                params["duration"] = max(5.0, min(60.0, float(duration)))
            r = self._call("POST", "clips", params=params)
            if r.status_code == 404:
                return {"ok": False, "error": "Clips only work while you're live."}
            if r.status_code != 202:
                return _fail(r, "make the clip")
            clip = r.json()["data"][0]
            return {"ok": True, "id": clip["id"], "url": f"https://clips.twitch.tv/{clip['id']}",
                    "edit_url": clip.get("edit_url"), "title": title}
        return self._act("make a clip", "clips:edit", run)

    def create_marker(self, description: str = "") -> dict:
        def run(bid: str) -> dict:
            r = self._call("POST", "streams/markers", body={"user_id": bid, "description": (description or "")[:140]})
            if r.status_code == 404:
                return {"ok": False, "error": "Markers only work while you're live (with VODs turned on)."}
            if r.status_code != 200:
                return _fail(r, "add the marker")
            m = r.json()["data"][0]
            return {"ok": True, "position_s": m.get("position_seconds"), "description": m.get("description")}
        return self._act("add a marker", "channel:manage:broadcast", run)

    def find_category(self, name: str) -> dict | None:
        games = self._get("games", {"name": name})
        if games:
            return games[0]
        found = self._get("search/categories", {"query": name, "first": 10})
        if not found:
            return None
        exact = next((g for g in found if g["name"].lower() == name.lower()), None)
        return exact or found[0]

    def set_channel(self, title: str | None = None, category: str | None = None) -> dict:
        def run(bid: str) -> dict:
            body: dict = {}
            game = None
            if title is not None:
                if not title.strip():
                    return {"ok": False, "error": "The title can't be empty."}
                body["title"] = title.strip()[:140]
            if category:
                game = self.find_category(category)
                if not game:
                    return {"ok": False, "error": f"No Twitch category matches {category}."}
                body["game_id"] = game["id"]
            if not body:
                return {"ok": False, "error": "Tell me the new title or category."}
            r = self._call("PATCH", "channels", params={"broadcaster_id": bid}, body=body)
            if r.status_code != 204:
                return _fail(r, "change the channel")
            return {"ok": True, "title": body.get("title"), "category": game["name"] if game else None}
        return self._act("change the title or category", "channel:manage:broadcast", run)

    def start_ad(self, length: int = 60) -> dict:
        def run(bid: str) -> dict:
            r = self._call("POST", "channels/commercial", body={"broadcaster_id": bid, "length": max(30, min(180, int(length)))})
            if r.status_code == 429 or r.status_code == 400:
                return _fail(r, "run an ad", extra=" Ads need you live, and affiliate or partner.")
            if r.status_code != 200:
                return _fail(r, "run an ad")
            d = r.json()["data"][0]
            return {"ok": True, "length": d.get("length"), "retry_after": d.get("retry_after"), "message": d.get("message")}
        return self._act("run an ad", "channel:edit:commercial", run)

    def shoutout(self, user: str) -> dict:
        def run(bid: str) -> dict:
            target = self._user_id_of(user)
            if not target:
                return {"ok": False, "error": f"There's no Twitch user called {user}."}
            me = self.auth.token.get("user_id") or bid
            r = self._call("POST", "chat/shoutouts",
                           params={"from_broadcaster_id": bid, "to_broadcaster_id": target, "moderator_id": me})
            if r.status_code == 429:
                return {"ok": False, "error": "Twitch allows one shoutout every 2 minutes, and the same person once an hour."}
            if r.status_code != 204:
                return _fail(r, f"shout out {user}")
            return {"ok": True, "user": user}
        return self._act("shout someone out", "moderator:manage:shoutouts", run)

    def create_poll(self, title: str, choices: list[str], seconds: int = 120) -> dict:
        def run(bid: str) -> dict:
            opts = [c.strip()[:25] for c in choices if c and c.strip()][:5]
            if len(opts) < 2:
                return {"ok": False, "error": "A poll needs at least two choices."}
            r = self._call("POST", "polls", body={"broadcaster_id": bid, "title": title.strip()[:60],
                                                  "choices": [{"title": c} for c in opts],
                                                  "duration": max(15, min(1800, int(seconds)))})
            if r.status_code != 200:
                return _fail(r, "start the poll", extra=" Polls need affiliate or partner.")
            return {"ok": True, "title": title, "choices": opts, "seconds": max(15, min(1800, int(seconds)))}
        return self._act("start a poll", "channel:manage:polls", run)

    def send_chat(self, message: str) -> dict:
        def run(bid: str) -> dict:
            me = self.auth.token.get("user_id") or bid
            r = self._call("POST", "chat/messages", body={"broadcaster_id": bid, "sender_id": me, "message": message[:500]})
            if r.status_code != 200:
                return _fail(r, "send that to chat")
            d = r.json()["data"][0]
            if not d.get("is_sent"):
                why = (d.get("drop_reason") or {}).get("message") or "Twitch dropped it"
                return {"ok": False, "error": f"Chat didn't take it: {why}."}
            return {"ok": True, "message": message}
        return self._act("send chat messages", "user:write:chat", run)

    def subscribe_eventsub(self, sub_type: str, version: str, condition: dict, session_id: str) -> dict:
        try:
            r = self._call("POST", "eventsub/subscriptions", body={
                "type": sub_type, "version": version, "condition": condition,
                "transport": {"method": "websocket", "session_id": session_id}})
        except (httpx.HTTPError, NotConnected) as exc:
            return {"ok": False, "error": str(exc)}
        if r.status_code in (202, 409):  # 409: already subscribed on this session
            return {"ok": True}
        return {"ok": False, "error": f"{r.status_code} {_message(r)}"}


class NotConnected(Exception):
    def __str__(self) -> str:
        return "not logged in to Twitch"


def _message(r: httpx.Response) -> str:
    try:
        body = r.json()
        return str(body.get("message") or body.get("error") or "")
    except ValueError:
        return r.text[:200]


def _fail(r: httpx.Response, what: str, extra: str = "") -> dict:
    return {"ok": False, "error": f"Twitch wouldn't {what} ({r.status_code}): {_message(r) or 'no reason given'}.{extra}",
            "status": r.status_code}
