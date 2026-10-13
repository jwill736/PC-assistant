"""Spotify skill: PKCE sign-in, token refresh, player control, search, routes, and the stream-safe heads-up."""

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import spotify as spm

CID = "0123456789abcdef0123456789abcdef"
PLAYER = {"is_playing": True, "progress_ms": 30000, "shuffle_state": False,
          "device": {"name": "J-PC", "volume_percent": 60},
          "context": {"type": "playlist", "uri": "spotify:playlist:PL1"},
          "item": {"id": "T1", "uri": "spotify:track:T1", "name": "Midnight City", "duration_ms": 240000,
                   "artists": [{"name": "M83"}],
                   "album": {"name": "Hurry Up", "images": [{"url": "big"}, {"url": "mid"}, {"url": "small"}]}}}
SEARCH = {"tracks": {"items": [{"type": "track", "uri": "spotify:track:S1", "name": "Lofi Beats Song",
                                "artists": [{"name": "Someone"}]}]},
          "playlists": {"items": [None, {"type": "playlist", "uri": "spotify:playlist:LOFI", "name": "lofi beats",
                                         "owner": {"display_name": "Spotify"}}]},
          "artists": {"items": [{"type": "artist", "uri": "spotify:artist:A1", "name": "Daft Punk"}]}}


class Fake:
    def __init__(self):
        self.calls, self.token_posts = [], []
        self.player = dict(PLAYER)
        self.status = {}  # path -> forced status code
        self.refresh_reply = {"access_token": "AT2", "expires_in": 3600}
        self.expired_tokens = set()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "accounts.spotify.com":
            form = parse_qs(req.content.decode())
            self.token_posts.append({k: v[0] for k, v in form.items()})
            if form["grant_type"][0] == "authorization_code":
                return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600,
                                                 "scope": spm.SCOPES})
            if isinstance(self.refresh_reply, int):
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Refresh token revoked"})
            return httpx.Response(200, json=self.refresh_reply)
        self.calls.append((req.method, req.url.path, dict(req.url.params), json.loads(req.content or b"null")))
        if req.headers["authorization"].split()[-1] in self.expired_tokens:
            return httpx.Response(401, json={"error": {"status": 401, "message": "The access token expired"}})
        path = req.url.path.removeprefix("/v1")
        if path in self.status:
            code = self.status[path]
            msg = {404: "Player command failed: No active device found",
                   403: "Player command failed: Premium required"}.get(code, "nope")
            return httpx.Response(code, json={"error": {"status": code, "message": msg}}, headers={"Retry-After": "7"})
        if path == "/me/player":
            return httpx.Response(200, json=self.player) if self.player else httpx.Response(204)
        if path == "/search":
            return httpx.Response(200, json=SEARCH)
        if path.startswith("/playlists/"):
            return httpx.Response(200, json={"name": "Stream Safe Bangers" if path.endswith("PL1") else "Other"})
        return httpx.Response(204)


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture
def sp(svc, fake, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "")
    monkeypatch.setattr("assistant.config.save_setting", lambda cfg, key, value: None)
    now = [1000.0]
    opened = []
    s = spm.Spotify(svc, http=httpx.Client(transport=httpx.MockTransport(fake)), clock=lambda: now[0], opener=opened.append)
    s.later = lambda fn: None
    s.now_, s.opened = now, opened
    return s


def connect(sp):
    assert sp.set_client(CID)["ok"]
    url = sp.connect()["url"]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    ok, title, _ = sp.callback({"state": q["state"], "code": "C0DE"})
    assert ok, title
    return q


def test_setup_steps_and_client_id_goes_to_env(sp, cfg):
    st = sp.status()
    assert not st["configured"] and "developer.spotify.com" in st["next_step"]
    assert st["redirect"] == "http://127.0.0.1:8765/api/skill/spotify/callback"
    assert sp.set_client("not-an-id")["ok"] is False
    assert sp.connect()["ok"] is False  # nothing to sign in with yet
    assert sp.set_client(CID)["ok"]
    assert f"SPOTIFY_CLIENT_ID={CID}" in (cfg.root / ".env").read_text()
    assert sp.status()["next_step"].startswith("Click Connect") and not sp.status()["connected"]
    assert sp.tick() is None and sp.now is None  # not connected: no calls


def test_pkce_sign_in_with_a_single_use_state(sp, fake):
    q = connect(sp)
    assert q["code_challenge_method"] == "S256" and q["client_id"] == CID and q["scope"] == spm.SCOPES
    assert q["redirect_uri"] == "http://127.0.0.1:8765/api/skill/spotify/callback" and sp.opened
    post = fake.token_posts[-1]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(post["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == q["code_challenge"] and post["code"] == "C0DE" and "client_secret" not in post
    assert post["redirect_uri"] == q["redirect_uri"]
    assert sp.connected and sp.status()["next_step"] is None
    again = sp.callback({"state": q["state"], "code": "C0DE"})  # replayed: the state was used up
    assert again[0] is False and "expired" in again[2]
    assert sp.callback({"state": "made-up", "code": "x"})[0] is False
    assert sp.callback({"error": "access_denied"})[2] == "You cancelled on Spotify's page."
    saved = json.loads((sp.svc.cfg.data_dir / "spotify_tokens.json").read_text())
    assert saved["refresh_token"] == "RT1"


def test_a_sign_in_link_expires(sp):
    sp.set_client(CID)
    q = {k: v[0] for k, v in parse_qs(urlparse(sp.begin()["url"]).query).items()}
    sp.now_[0] += 700
    assert sp.callback({"state": q["state"], "code": "x"})[0] is False


def test_refresh_keeps_the_old_refresh_token_and_handles_revocation(sp, fake):
    connect(sp)
    sp.now_[0] += 4000  # access token expired
    assert sp.now_playing()["track"] == "Midnight City"
    post = fake.token_posts[-1]
    assert post["grant_type"] == "refresh_token" and post["refresh_token"] == "RT1" and post["client_id"] == CID
    assert sp.tokens["access_token"] == "AT2" and sp.tokens["refresh_token"] == "RT1"
    sp.now_[0] += 4000
    fake.refresh_reply = 400
    out = sp.now_playing()
    assert out["ok"] is False and "Connect again" in out["error"] and not sp.connected


def test_a_dropped_token_is_refreshed_once(sp, fake):
    connect(sp)
    fake.expired_tokens.add("AT1")
    assert sp.now_playing()["ok"] and fake.token_posts[-1]["grant_type"] == "refresh_token"


def test_now_playing_and_controls(sp, fake):
    connect(sp)
    n = sp.now_playing()
    assert (n["track"], n["artist"], n["art"], n["volume"], n["context"]) == ("Midnight City", "M83", "mid", 60, "spotify:playlist:PL1")
    assert sp.say("spotify_now_playing", {}, n) == "Midnight City by M83."
    assert sp.control("next")["ok"] and fake.calls[-1][:2] == ("POST", "/v1/me/player/next")
    assert sp.control("pause")["ok"] and sp.now["playing"] is False
    assert sp.say("spotify_control", {"action": "pause"}, {}) == "Paused."
    assert sp.control("dance")["ok"] is False
    assert sp.volume(change=-15) == {"ok": True, "level": 45} and fake.calls[-1][2] == {"volume_percent": "45"}
    assert sp.volume(150)["level"] == 100
    assert sp.shuffle(True)["ok"] and fake.calls[-1][2] == {"state": "true"}
    fake.status["/me/player/next"] = 404
    assert "No Spotify device is active" in sp.control("next")["error"]
    fake.status["/me/player/pause"] = 403
    assert "Premium" in sp.control("pause")["error"]
    fake.status["/me/player/play"] = 429
    assert "7 seconds" in sp.control("play")["error"]
    fake.player = None
    assert sp.now_playing()["playing"] is False and sp.say("spotify_now_playing", {}, sp.now) == "Nothing's playing on Spotify."


def test_play_search_picks_the_right_kind(sp, fake):
    connect(sp)
    out = sp.play_search("lofi beats")
    assert out["type"] == "playlist" and fake.calls[-1][3] == {"context_uri": "spotify:playlist:LOFI"}
    assert sp.say("spotify_play", {}, out) == "Playing the lofi beats playlist."
    out = sp.play_search("daft punk")
    assert out["type"] == "artist" and fake.calls[-1][3] == {"context_uri": "spotify:artist:A1"}
    out = sp.play_search("lofi beats", "track")
    assert fake.calls[-1][3] == {"uris": ["spotify:track:S1"]} and sp.say("spotify_play", {}, out) == "Playing Lofi Beats Song by Someone."
    assert sp.play_search("")["ok"] is False


def test_service_down_never_crashes(svc, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", CID)

    def down(req):
        raise httpx.ConnectError("offline")
    s = spm.Spotify(svc, http=httpx.Client(transport=httpx.MockTransport(down)))
    s.tokens = {"access_token": "x", "refresh_token": "y", "expires_at": 0}
    assert s.now_playing()["ok"] is False and s.control("next")["ok"] is False
    s.tokens["expires_at"] = 1e12
    assert "didn't answer" in s.now_playing()["error"]
    s.tick()
    assert "didn't answer" in s.status()["error"]


@pytest.mark.parametrize("said,tool,args", [
    ("what's playing", "spotify_now_playing", {}),
    ("what song is this", "spotify_now_playing", {}),
    ("play lofi beats", "spotify_play", {"query": "lofi beats", "kind": "auto"}),
    ("play my chill playlist", "spotify_play", {"query": "chill", "kind": "playlist"}),
    ("play songs by daft punk", "spotify_play", {"query": "daft punk", "kind": "artist"}),
    ("next song", "spotify_control", {"action": "next"}),
    ("skip", "spotify_control", {"action": "next"}),
    ("pause the music", "spotify_control", {"action": "pause"}),
    ("pause", "spotify_control", {"action": "pause"}),
    ("previous song", "spotify_control", {"action": "previous"}),
    ("turn the music down", "spotify_volume", {"change": -15}),
    ("turn up the music", "spotify_volume", {"change": 15}),
    ("music volume to 30", "spotify_volume", {"level": 30}),
    ("shuffle off", "spotify_shuffle", {"on": False}),
])
def test_routes_when_connected(svc, said, tool, args):
    sk = Skills(svc, ["spotify"])
    sk["spotify"].tokens = {"access_token": "x", "refresh_token": "y"}
    sk["spotify"].now = {"playing": True, "track": "x"}
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


@pytest.mark.parametrize("said,tool", [
    ("pause", "media_control"), ("next song", "media_control"), ("play an ad", "twitch_ad"),
    ("play lofi on youtube", "web_search"),
])
def test_generic_commands_stay_generic_when_not_connected(svc, said, tool):
    sk = Skills(svc, ["spotify"])
    assert route(said, RouterContext(skill_route=sk.route)).tool == tool


def test_bare_pause_is_left_alone_when_spotify_is_not_playing(svc):
    sk = Skills(svc, ["spotify"])
    sk["spotify"].tokens = {"access_token": "x"}
    sk["spotify"].now = {"playing": False, "track": None}
    assert route("pause", RouterContext(skill_route=sk.route)).tool == "media_control"
    assert route("pause the music", RouterContext(skill_route=sk.route)).tool == "spotify_control"


def test_stream_safe_heads_up_only_while_live(sp, fake, svc, monkeypatch):
    nudges = []
    svc.nudge = lambda text, **kw: nudges.append((text, kw))
    live = [False]
    svc.live = lambda: live[0]
    connect(sp)
    sp.tick()
    assert not nudges  # not live
    live[0] = True
    sp.tick()
    text, kw = nudges[-1]
    assert text == "Heads up: 'Midnight City' may not be stream-safe." and kw["priority"] == "urgent" and kw["speak"] is False
    sp.tick()
    assert len(nudges) == 1  # once per track per stream
    assert sp.status()["live"] and not sp.status()["safe"]
    # marked by name in config: the playlist's name is looked up once
    sp.cfg["stream_safe"] = ["stream safe bangers"]
    fake.player = {**PLAYER, "item": {**PLAYER["item"], "id": "T2", "name": "Other Song"}}
    sp.tick()
    assert len(nudges) == 1 and sp.status()["safe"]
    # marked from the HUD by id
    sp.cfg["stream_safe"] = []
    assert sp.mark_safe()["ok"] and sp.setting("stream_safe") == ["PL1"]
    fake.player = {**PLAYER, "item": {**PLAYER["item"], "id": "T3"}}
    sp.tick()
    assert len(nudges) == 1
    fake.player = {**PLAYER, "context": None, "item": {**PLAYER["item"], "id": "T4", "name": "Loose Track"}}
    sp.tick()
    assert nudges[-1][0] == "Heads up: 'Loose Track' may not be stream-safe."
    assert sp.mark_safe()["ok"] is False  # not a playlist
    live[0] = False
    sp.tick()
    assert sp._warned == set()


def test_the_callback_route_needs_no_token_but_only_for_get(svc, cfg, sp, monkeypatch):
    from fastapi.testclient import TestClient

    from assistant.runtime import Runtime
    from assistant.server import create_app

    sk = Skills(svc, ["weather"])
    sk.all["spotify"] = sp
    svc.skills = sk
    sk["weather"].forecast = lambda fresh=False: {"ok": False, "error": "nowhere"}
    sp.set_client(CID)
    state = parse_qs(urlparse(sp.begin()["url"]).query)["state"][0]
    app = create_app(Runtime(cfg, services=svc), start_background=False)
    with TestClient(app) as c:
        r = c.get(f"/api/skill/spotify/callback?state={state}&code=abc")
        assert r.status_code == 200 and "Spotify connected" in r.text and sp.connected
        assert c.get(f"/api/skill/spotify/callback?state={state}&code=abc").status_code == 400  # single use
        assert c.post("/api/skill/spotify/callback", json={}).status_code == 401
        assert c.get("/api/skill/spotify").status_code == 401
        assert c.get("/api/skill/weather/callback").status_code == 404  # no sign-in there
        assert c.get("/api/skill/spotify/callback?error=access_denied").status_code == 400
        assert '/static/skills/spotify.js' in c.get("/").text
