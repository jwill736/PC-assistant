"""Spotify: what's playing, play / pause / skip / volume / shuffle, and "play lofi beats", by voice and on the HUD.

Sign-in is Spotify's own page (Authorization Code with PKCE, so no secret): paste your app's Client ID on the HUD,
click Connect, approve. It comes back to http://127.0.0.1:8765/api/skill/spotify/callback, which must be listed as a
Redirect URI on the app in Spotify's developer dashboard ("localhost" is refused there). The Client ID goes in .env
(SPOTIFY_CLIENT_ID); tokens in data/spotify_tokens.json. Playback control needs Premium and Spotify open somewhere.

While you're live, a track that isn't from a playlist you marked stream-safe (skills.spotify.stream_safe: playlist
ids, links or names; "Mark stream-safe" on the HUD) puts a heads-up on the HUD. Never spoken: it would go out on air.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import webbrowser
from urllib.parse import urlencode

import httpx

from ..brain.router import NUMBER, Intent, number
from ..brain.tools import Tool, obj
from . import Skill, check, lazy_client
from ._kit import LiveEdge, callback_url, pkce, save_secret, save_setting

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private"
CLIENT_ENV = "SPOTIFY_CLIENT_ID"
PENDING_S = 600
COMMANDS = {"play": ("PUT", "/me/player/play"), "pause": ("PUT", "/me/player/pause"),
            "next": ("POST", "/me/player/next"), "previous": ("POST", "/me/player/previous")}
PLAYLISTY = re.compile(r"\b(?:playlist|mix|beats|vibes|radio|music|songs|chill|lofi|lo-fi|focus|study|workout|hits)\b")


class SpotifyError(Exception):
    pass


class Spotify(Skill):
    name, title = "spotify", "Spotify"
    poll_s = 10
    game_essential = True  # the stream-safe music check runs while live
    delay_s = 10

    def __init__(self, svc, http: httpx.Client | None = None, clock=time.time, opener=webbrowser.open):
        super().__init__(svc)
        self.http = http or lazy_client(timeout=10)
        self.clock, self.opener = clock, opener
        self.later = lambda fn: threading.Timer(0.8, fn).start()  # Spotify takes a moment to catch up after a change
        self._lock = threading.RLock()
        self._pending: dict[str, dict] = {}
        self._names: dict[str, str] = {}  # playlist id -> name
        self._warned: set[str] = set()  # tracks flagged this stream
        self._live = LiveEdge(svc)
        self.tokens: dict = self._load()
        self.now: dict | None = None
        self.error: str | None = None

    # ---- setup ---------------------------------------------------------------------------------------
    @property
    def client_id(self) -> str:
        return self.svc.cfg.secret(CLIENT_ENV)

    @property
    def connected(self) -> bool:
        return bool(self.tokens.get("refresh_token") or self.tokens.get("access_token"))

    def _path(self):
        return self.svc.cfg.data_dir / "spotify_tokens.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, AttributeError, KeyError):
            return {}

    def _save(self) -> None:
        tmp = self._path().with_suffix(".tmp")
        tmp.write_text(json.dumps(self.tokens, indent=1), encoding="utf-8")
        tmp.replace(self._path())

    def set_client(self, client_id: str) -> dict:
        cid = (client_id or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", cid):
            return {"ok": False, "error": "That isn't a Spotify Client ID: it's 32 letters and digits, on your app's "
                                          "Settings page in the Spotify developer dashboard."}
        save_secret(self.svc, CLIENT_ENV, cid)
        return {"ok": True}

    def begin(self) -> dict:
        """Spotify's sign-in address (PKCE: a fresh verifier and a single-use state each time)."""
        if not self.client_id:
            return {"ok": False, "error": "Paste your Spotify app's Client ID first."}
        verifier, challenge = pkce()
        state = secrets.token_urlsafe(24)
        with self._lock:
            now = self.clock()
            self._pending = {s: p for s, p in self._pending.items() if now - p["at"] < PENDING_S}
            self._pending[state] = {"verifier": verifier, "at": now}
        params = {"client_id": self.client_id, "response_type": "code", "redirect_uri": callback_url(self.svc, self.name),
                  "scope": SCOPES, "state": state, "code_challenge_method": "S256", "code_challenge": challenge}
        return {"ok": True, "url": f"{AUTH_URL}?{urlencode(params)}"}

    def connect(self) -> dict:
        out = self.begin()
        if out["ok"]:
            try:
                self.opener(out["url"])
            except Exception:
                pass
        return out

    def callback(self, params: dict) -> tuple[bool, str, str]:
        """Spotify sent the browser back: trade the code for tokens (server.py's /api/skill/spotify/callback)."""
        fail = "Spotify sign-in didn't finish"
        if params.get("error"):
            return False, fail, "You cancelled on Spotify's page." if params["error"] == "access_denied" else \
                f"Spotify said: {params['error']}"
        with self._lock:
            pending = self._pending.pop(params.get("state") or "", None)
        if not pending or self.clock() - pending["at"] > PENDING_S:
            return False, fail, "That sign-in link expired or wasn't started here. Click Connect on the HUD again."
        try:
            r = self.http.post(TOKEN_URL, data={
                "grant_type": "authorization_code", "code": params.get("code", ""), "client_id": self.client_id,
                "redirect_uri": callback_url(self.svc, self.name), "code_verifier": pending["verifier"]})
        except httpx.HTTPError as exc:
            return False, fail, f"Couldn't reach Spotify ({type(exc).__name__})."
        if r.status_code != 200:
            return False, fail, f"Spotify refused the sign-in: {_reason(r)}"
        self._store(r.json())
        self.error = None
        return True, "Spotify connected", "Vesper can see and control your Spotify now. You can close this tab."

    def _store(self, tok: dict) -> None:
        with self._lock:
            self.tokens = {"access_token": tok["access_token"],
                           "refresh_token": tok.get("refresh_token") or self.tokens.get("refresh_token"),
                           "expires_at": self.clock() + int(tok.get("expires_in", 3600)) - 60,
                           "scope": tok.get("scope") or self.tokens.get("scope", "")}
            self._save()

    def disconnect(self) -> dict:
        with self._lock:
            self.tokens, self.now = {}, None
            self._save()
        return {"ok": True}

    # ---- the API -------------------------------------------------------------------------------------
    def _access(self, force: bool = False) -> str:
        with self._lock:
            if not self.connected:
                raise SpotifyError("Spotify isn't connected: Setup → Spotify → Connect.")
            if not force and self.clock() < self.tokens.get("expires_at", 0):
                return self.tokens["access_token"]
            if not self.tokens.get("refresh_token"):
                raise SpotifyError("Spotify signed you out: click Connect again.")
            r = self.http.post(TOKEN_URL, data={"grant_type": "refresh_token", "client_id": self.client_id,
                                                "refresh_token": self.tokens["refresh_token"]})
            if r.status_code != 200:
                if "invalid_grant" in r.text:  # revoked, or the app's Client ID changed
                    self.tokens = {}
                    self._save()
                    raise SpotifyError("Spotify ended the sign-in: click Connect again.")
                raise SpotifyError(f"Couldn't refresh the Spotify sign-in: {_reason(r)}")
            self._store(r.json())
            return self.tokens["access_token"]

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> httpx.Response:
        r = None
        for force in (False, True):  # a 401: the token was dropped early, refresh once
            r = self.http.request(method, API + path, params=params, json=body,
                                  headers={"Authorization": f"Bearer {self._access(force)}"})
            if r.status_code != 401:
                break
        return r

    def _try(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        """(response, None), or (None, a plain-English error) when Spotify can't be asked."""
        try:
            return self._call(method, path, params, body), None
        except SpotifyError as exc:
            return None, {"ok": False, "error": str(exc)}
        except httpx.HTTPError as exc:
            return None, {"ok": False, "error": f"Spotify didn't answer ({type(exc).__name__})."}

    def _do(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        r, err = self._try(method, path, params, body)
        return err or ({"ok": True} if r.status_code in (200, 202, 204) else {"ok": False, "error": _problem(r)})

    def now_playing(self) -> dict:
        r, err = self._try("GET", "/me/player", {"additional_types": "track,episode"})
        if err:
            return err
        if r.status_code == 204 or (r.status_code == 200 and not r.content):
            self.now = {"ok": True, "playing": False, "track": None, "at": self.clock()}
        elif r.status_code != 200:
            return {"ok": False, "error": _problem(r)}
        else:
            self.now = shape(r.json(), self.clock())
        return self.now

    def _refresh(self) -> None:
        if self.now_playing().get("ok"):
            self.publish()

    def control(self, action: str) -> dict:
        if action not in COMMANDS:
            return {"ok": False, "error": f"I can play, pause, skip or go back, not {action}."}
        out = self._do(*COMMANDS[action])
        if out["ok"]:
            if self.now and action in ("play", "pause"):
                self.now["playing"] = action == "play"
            self.later(self._refresh)
        return {**out, "action": action}

    def volume(self, level: int | None = None, change: int | None = None) -> dict:
        if level is None:
            cur = (self.now or {}).get("volume")
            cur = self.now_playing().get("volume") if cur is None else cur
            if cur is None:
                return {"ok": False, "error": "Nothing's playing on Spotify, so there's no volume to change."}
            level = cur + int(change or 0)
        level = max(0, min(100, int(level)))
        out = self._do("PUT", "/me/player/volume", {"volume_percent": level})
        if out["ok"] and self.now:
            self.now["volume"] = level
        return {**out, "level": level}

    def shuffle(self, on: bool = True) -> dict:
        out = self._do("PUT", "/me/player/shuffle", {"state": "true" if on else "false"})
        if out["ok"] and self.now:
            self.now["shuffle"] = bool(on)
        return {**out, "on": bool(on)}

    def play_search(self, query: str, kind: str = "auto") -> dict:
        query = (query or "").strip()
        if not query:
            return {"ok": False, "error": "Play what?"}
        kind = kind if kind in ("track", "playlist", "artist", "album") else "auto"
        r, err = self._try("GET", "/search", {"q": query, "limit": 3,
                                              "type": "track,playlist,artist" if kind == "auto" else kind})
        if err:
            return err
        if r.status_code != 200:
            return {"ok": False, "error": _problem(r)}
        pick = choose(r.json(), query, kind)
        if not pick:
            return {"ok": False, "error": f"Spotify found nothing for {query}."}
        body = {"uris": [pick["uri"]]} if pick["type"] == "track" else {"context_uri": pick["uri"]}
        out = self._do("PUT", "/me/player/play", body=body)
        if out["ok"]:
            self.later(self._refresh)
        return {**out, **pick, "query": query}

    # ---- stream-safe music ---------------------------------------------------------------------------
    def marks(self) -> set[str]:
        out = set()
        for x in self.setting("stream_safe", []) or []:
            m = re.search(r"playlist[:/]([A-Za-z0-9]+)", str(x))
            out |= {m.group(1)} if m else {str(x).strip(), str(x).strip().lower()}  # an id, or a name
        return out

    def safe(self, now: dict | None, fetch: bool = False) -> bool:
        ctx = (now or {}).get("context") or ""
        if not ctx.startswith("spotify:playlist:"):
            return False
        pid, marks = ctx.rsplit(":", 1)[-1], self.marks()
        if pid in marks:
            return True
        if fetch and pid not in self._names:
            try:
                r = self._call("GET", f"/playlists/{pid}", {"fields": "name"})
                self._names[pid] = r.json().get("name", "") if r.status_code == 200 else ""
            except (SpotifyError, httpx.HTTPError, ValueError):
                return False
        return bool(self._names.get(pid)) and self._names[pid].lower() in marks

    def mark_safe(self, on: bool = True) -> dict:
        ctx = (self.now or {}).get("context") or ""
        if not ctx.startswith("spotify:playlist:"):
            return {"ok": False, "error": "Play a playlist first: only a playlist can be marked stream-safe."}
        pid = ctx.rsplit(":", 1)[-1]
        kept = [x for x in self.setting("stream_safe", []) or [] if pid not in str(x)]
        save_setting(self, "stream_safe", kept + [pid] if on else kept)
        return {"ok": True, "id": pid, "safe": on}

    def _dmca(self, now: dict) -> None:
        if not now.get("playing") or not now.get("id") or now["id"] in self._warned or self.safe(now, fetch=True):
            return
        self._warned.add(now["id"])
        self.nudge(f"Heads up: '{now.get('track')}' may not be stream-safe.", priority="urgent", speak=False,
                   key=f"spotify:dmca:{now['id']}", expires_in=600)

    # ---- skill surface -------------------------------------------------------------------------------
    def tick(self) -> None:
        if self._live() == "end":
            self._warned.clear()
        if self.connected and self.client_id:
            out = self.now_playing()
            self.error = None if out.get("ok") else out.get("error")
            if self._live.was and out.get("ok"):
                self._dmca(out)
        self.publish()

    def selftest(self) -> list[dict]:
        if not self.connected:
            return [check("Spotify", "skip", "not connected", "Optional: Setup → Spotify → Connect")]
        try:
            self._access(force=True)  # a fresh sign-in token: proves the saved sign-in still works
        except SpotifyError as exc:
            return [check("Spotify", "fail", str(exc), "Setup → Spotify → Connect")]
        except httpx.HTTPError as exc:
            return [check("Spotify", "warn", f"Spotify didn't answer ({type(exc).__name__})", "Check the internet")]
        return [check("Spotify", "pass", "signed in (token refreshed)")]

    def status(self) -> dict:
        step = None if self.connected else (
            "Make an app at developer.spotify.com/dashboard with the Redirect URI below, then paste its Client ID."
            if not self.client_id else "Click Connect and approve on Spotify's page.")
        return {"configured": bool(self.client_id), "connected": self.connected, "next_step": step,
                "redirect": callback_url(self.svc, self.name), "error": self.error, "now": self.now,
                "live": self._live.was, "safe": self.safe(self.now), "stream_safe": self.setting("stream_safe", []) or []}

    def actions(self):
        return {"client": lambda b: self.set_client(str(b.get("client_id") or "")),
                "connect": lambda b: self.connect(), "disconnect": lambda b: self.disconnect(),
                "control": lambda b: self.control(str(b.get("action") or "")),
                "volume": lambda b: self.volume(int(b.get("level", 50))),
                "shuffle": lambda b: self.shuffle(bool(b.get("on", True))),
                "play": lambda b: self.play_search(str(b.get("query") or ""), str(b.get("kind") or "auto")),
                "mark_safe": lambda b: self.mark_safe(bool(b.get("on", True))),
                "refresh": lambda b: self.now_playing()}

    def tools(self):
        return [
            Tool("spotify_now_playing", "What's playing on the user's Spotify (track, artist, device, volume).", obj(),
                 self.now_playing),
            Tool("spotify_control", "Spotify playback: play (resume), pause, next or previous track.",
                 obj({"action": {"type": "string", "enum": list(COMMANDS)}}, ["action"]), self.control, tier=1),
            Tool("spotify_volume", "Spotify's own volume: level 0-100, or change (e.g. -15 for 'turn the music down').",
                 obj({"level": {"type": "integer"}, "change": {"type": "integer"}}), self.volume, tier=1),
            Tool("spotify_shuffle", "Turn Spotify shuffle on or off.", obj({"on": {"type": "boolean"}}, ["on"]),
                 self.shuffle, tier=1),
            Tool("spotify_play", "Search Spotify and play the best match: a song, playlist, artist or album "
                                 "(kind: track, playlist, artist, album, or auto to guess).",
                 obj({"query": {"type": "string"}, "kind": {"type": "string",
                                                            "enum": ["auto", "track", "playlist", "artist", "album"]}},
                     ["query"]), self.play_search, tier=1),
        ]

    def route(self, t: str):
        if re.fullmatch(r"what(?:'s| is) (?:playing|this song|this track|that song)(?: on spotify)?|what song is "
                        r"(?:this|playing)|what am i listening to|now playing|who(?:'s| is) (?:singing|playing) this", t):
            return Intent("tool", "spotify_now_playing")
        # "put on my list …" is a task (the core router's), never a song
        if not self.connected or re.search(r"\byoutube\b|\b(?:ads?|commercial)\b", t) \
                or re.match(r"put on (?:my|the) (?:to-?do )?list\b", t):
            return None
        now = self.now or {}
        active = bool(now.get("playing"))
        if re.fullmatch(r"(?:pause|stop) (?:the )?(?:music|song|spotify)" + ("|pause" if active else ""), t):
            return Intent("tool", "spotify_control", {"action": "pause"})
        # A bare "play" while Spotify already plays stays here: the generic play/pause media key would pause it.
        if re.fullmatch(r"(?:play|resume|unpause|start) (?:the |some )?(?:music|song|spotify)"
                        + ("|play|resume|unpause" if now.get("track") or active else ""), t):
            return Intent("tool", "spotify_control", {"action": "play"})
        if re.fullmatch(r"(?:next|skip)(?: the)? (?:song|track)|skip (?:this )?song|next one"
                        + ("|next|skip|skip (?:this|it)" if active else ""), t):
            return Intent("tool", "spotify_control", {"action": "next"})
        if re.fullmatch(r"(?:play )?(?:the )?(?:previous|last) (?:song|track)(?: again)?|go back (?:a|one) (?:song|track)", t):
            return Intent("tool", "spotify_control", {"action": "previous"})
        m = re.fullmatch(r"(?:turn|bring) (?:the )?music (down|up)|(?:turn|bring) (down|up) (?:the )?music|music (down|up)"
                         r"|(?:make )?(?:the )?music (quieter|louder)|(lower|raise) (?:the )?music", t)
        if m:
            word = next(g for g in m.groups() if g)
            return Intent("tool", "spotify_volume", {"change": 15 if word in ("up", "louder", "raise") else -15})
        m = re.fullmatch(rf"(?:set )?(?:the )?(?:music|spotify)(?: volume)?(?: to| at)? {NUMBER}(?: percent| %|%)?", t)
        if m and number(m.group(1)) is not None:
            return Intent("tool", "spotify_volume", {"level": min(100, number(m.group(1)))})
        m = re.fullmatch(r"(?:turn )?shuffle (on|off)|turn (on|off) shuffle|shuffle(?: my| the)? (?:music|songs|it)", t)
        if m:
            return Intent("tool", "spotify_shuffle", {"on": (m.group(1) or m.group(2) or "on") == "on"})
        m = re.fullmatch(r"(?:play|put on) (?:the |my )?playlist (.+)|(?:play|put on) (?:my |the )?(.+?) playlist", t)
        if m:
            return Intent("tool", "spotify_play", {"query": m.group(1) or m.group(2), "kind": "playlist"})
        m = re.fullmatch(r"(?:play|put on) (?:some )?(?:songs?|music|something) by (.+?)(?: on spotify)?", t)
        if m:
            return Intent("tool", "spotify_play", {"query": m.group(1), "kind": "artist"})
        m = re.fullmatch(r"(?:play|put on) (?:some )?(.+?)(?: on spotify)?", t)
        apps = {str(a).lower() for a in (self.svc.cfg.get("apps") or {})}  # "play valorant": the game, not a song
        if m and not re.fullmatch(r"(?:the |some )?(?:music|song|spotify|video|media|it|that|something)", m.group(1)) \
                and m.group(1) not in apps:
            return Intent("tool", "spotify_play", {"query": m.group(1), "kind": "auto"})
        return None

    def say(self, tool, args, result):
        if tool == "spotify_now_playing":
            if not result.get("track"):
                return "Nothing's playing on Spotify."
            line = f"{result['track']} by {result['artist']}." if result.get("artist") else f"{result['track']}."
            return line if result.get("playing") else f"Paused: {line}"
        if tool == "spotify_control":
            return {"play": "Playing.", "pause": "Paused.", "next": "Skipped.", "previous": "Going back."}.get(args.get("action"))
        if tool == "spotify_volume":
            return f"Music at {result.get('level')} percent."
        if tool == "spotify_shuffle":
            return "Shuffle on." if result.get("on") else "Shuffle off."
        if tool == "spotify_play":
            if result.get("type") == "playlist":
                return f"Playing the {result['name']} playlist."
            return f"Playing {result['name']}" + (f" by {result['by']}." if result.get("by") and result["type"] == "track" else ".")
        return None


# ---- shaping -----------------------------------------------------------------------------------------------

def shape(p: dict, at: float) -> dict:
    item, device, ctx = p.get("item") or {}, p.get("device") or {}, p.get("context") or {}
    album = item.get("album") or {}
    images = album.get("images") or item.get("images") or []
    art = (images[1] if len(images) > 1 else images[0])["url"] if images else None  # ~300 px
    artist = ", ".join(a.get("name", "") for a in item.get("artists") or []) or (item.get("show") or {}).get("name", "")
    return {"ok": True, "playing": bool(p.get("is_playing")), "id": item.get("id"), "uri": item.get("uri"),
            "track": item.get("name"), "artist": artist, "album": album.get("name") or "", "art": art,
            "progress_ms": p.get("progress_ms") or 0, "duration_ms": item.get("duration_ms") or 0, "at": at,
            "shuffle": bool(p.get("shuffle_state")), "volume": device.get("volume_percent"),
            "device": device.get("name") or "", "context": ctx.get("uri") or "", "context_type": ctx.get("type") or ""}


def choose(data: dict, query: str, kind: str) -> dict | None:
    """The best search result: an exact artist or song name wins; "lofi beats"-style words lean to a playlist."""
    found = {k: [x for x in (data.get(k + "s") or {}).get("items") or [] if x] for k in ("track", "playlist", "artist", "album")}
    q = re.sub(r"[^a-z0-9 ]", "", query.lower()).strip()
    name = lambda x: re.sub(r"[^a-z0-9 ]", "", (x.get("name") or "").lower()).strip()  # noqa: E731
    if kind == "auto":
        exact = [x for x in found["artist"] if name(x) == q] or [x for x in found["track"] if name(x) == q]
        order = ["playlist", "track", "artist"] if PLAYLISTY.search(q) else ["track", "playlist", "artist"]
        x = exact[0] if exact else next((found[k][0] for k in order if found[k]), None)
    else:
        x = found[kind][0] if found[kind] else None
    if not x:
        return None
    by = ", ".join(a.get("name", "") for a in x.get("artists") or []) or (x.get("owner") or {}).get("display_name", "")
    return {"type": x.get("type") or kind, "uri": x["uri"], "name": x.get("name", ""), "by": by}


def _reason(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or r.status_code)
    return str(data.get("error_description") or err or r.status_code)


def _problem(r: httpx.Response) -> str:
    """What a failed player call means, in words."""
    why = _reason(r)
    if r.status_code == 404:
        return "No Spotify device is active: open Spotify on this PC or your phone and play something once."
    if r.status_code == 403 and "premium" in why.lower():
        return "Spotify only lets Premium accounts control playback."
    if r.status_code == 429:
        return f"Spotify asked me to slow down; try again in {r.headers.get('Retry-After', 'a few')} seconds."
    return f"Spotify said: {why}"
