"""Customisable layout, the mini HUD, the phone over home Wi-Fi (PIN, cookie, lockout) and the OBS overlay."""

import asyncio
import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from assistant import overlay, surfaces
from assistant.config import SETTINGS_FILE
from assistant.phone import COOKIE, LOCK_S, MAX_FAILS, NET_FAILS, LanGate, PhoneAccess, split_host
from assistant.runtime import Runtime
from assistant.server import create_app, hud_version

LAN_BASE = "http://192.168.1.23:8766"
PHONE_IP = ("192.168.1.50", 50123)
WEB = Path(__file__).resolve().parent.parent / "assistant" / "web"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def build(cfg, svc, remote=False):
    if remote:
        cfg["hud"] = {"remote": {"enabled": True}}
    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=False)
    phone = app.state.phone
    phone._addresses, phone._hostname = (lambda: ["192.168.1.23"]), (lambda: "GamingPC")
    phone.clock = Clock()
    return rt, app, phone


@pytest.fixture
def pc(cfg, svc):
    rt, app, phone = build(cfg, svc)
    with TestClient(app) as c:
        c.headers["X-Assistant-Token"] = app.state.token
        yield c, app, rt


@pytest.fixture
def lan(cfg, svc):
    """The same app twice: on loopback (install token) and on the phone listener (PIN session)."""
    rt, app, phone = build(cfg, svc, remote=True)
    with TestClient(app) as local, TestClient(LanGate(app), base_url=LAN_BASE, client=PHONE_IP, follow_redirects=False) as remote:
        local.headers["X-Assistant-Token"] = app.state.token
        yield local, remote, app, phone, rt


def sign_in(remote, phone):
    phone.clock.t += 2
    r = remote.post("/pin/login", json={"pin": phone.pin})
    assert r.status_code == 200 and r.json() == {"ok": True}
    sid = phone.session(r.cookies.get(COOKIE))
    assert sid
    return r.cookies.get(COOKIE), phone.csrf(sid)


# ---- layout ---------------------------------------------------------------------------------------------------

def test_layout_saved_to_settings_and_cleaned(pc, cfg):
    c, app, rt = pc
    assert c.get("/api/hud/layout").json() == {"layout": {}}
    body = {"view": "command", "by": "tab1", "layout": {
        "order": ["agenda", "briefing", "agenda", "BAD KEY!", "inbox#2"],
        "spans": {"agenda": 6, "briefing": 5, "inbox#2": "12", "news": 12},
        "hidden": ["news", "<script>"]}}
    r = c.post("/api/hud/layout", json=body).json()
    lay = {"order": ["agenda", "briefing", "inbox#2"], "spans": {"agenda": 6, "news": 12}, "hidden": ["news"]}
    assert r == {"ok": True, "layout": {"command": lay}}
    saved = yaml.safe_load((cfg.data_dir / SETTINGS_FILE).read_text(encoding="utf-8"))
    assert saved["hud"]["layout"] == {"command": lay}  # survives a cleared browser cache
    assert c.get("/api/hud/layout").json() == {"layout": {"command": lay}}
    ev = rt.bus.latest["hud_layout"]["data"]
    assert ev == {"layout": {"command": lay}, "by": "tab1"}  # other windows follow; the sender ignores its own


def test_layout_reset_tab_and_all(pc):
    c, _, _ = pc
    for view in ("command", "work"):
        c.post("/api/hud/layout", json={"view": view, "layout": {"order": ["a", "b"]}})
    assert set(c.get("/api/hud/layout").json()["layout"]) == {"command", "work"}
    assert set(c.post("/api/hud/layout", json={"view": "work", "reset": True}).json()["layout"]) == {"command"}
    assert c.post("/api/hud/layout", json={"reset_all": True}).json()["layout"] == {}
    assert c.post("/api/hud/layout", json={"view": "../etc", "layout": {}}).status_code == 400


def test_layout_needs_the_token(pc):
    c, _, _ = pc
    assert c.get("/api/hud/layout", headers={"X-Assistant-Token": "nope"}).status_code == 401
    assert c.post("/api/hud/layout", json={"reset_all": True}, headers={"X-Assistant-Token": ""}).status_code == 401


def test_index_loads_the_new_scripts_versioned(pc):
    c, _, _ = pc
    html = c.get("/").text
    for name in ("qr.js", "layout.js", "surfaces.js", "surfaces.css"):
        assert f"/static/{name}?v=" in html, name
    assert 'id="layout-btn"' in html
    assert html.index("/static/app.js") < html.index("/static/layout.js") < html.index("/static/surfaces.js")


def test_hud_version_follows_the_new_pages():
    assert len(hud_version()) == 10
    files = {p.name for p in WEB.glob("*.*")}
    assert {"mini.html", "overlay.html", "pin.html", "mini.css", "overlay.css", "surfaces.css"} <= files


# ---- mini HUD ---------------------------------------------------------------------------------------------------

def test_mini_page_has_token_and_name(pc):
    c, app, _ = pc
    html = c.get("/mini").text
    assert app.state.token in html and "Friday mini" in html and "/static/mini.js?v=" in html
    assert c.get("/mini", headers={"Host": "evil.example:8765"}).status_code == 403


def test_mini_open_launches_a_window(pc, monkeypatch):
    c, _, _ = pc
    calls = []
    monkeypatch.setattr(surfaces, "open_mini", lambda url, data_dir: calls.append(url) or True)
    r = c.post("/api/hud/mini/open").json()
    assert r == {"ok": True, "window": True, "url": "http://testserver/mini"} and calls == ["http://testserver/mini"]


def test_open_mini_sizes_a_chrome_app_window(tmp_path, monkeypatch):
    seen = []

    class FakeBrowser:
        def app_window(self, url, size, position, profile):
            seen.append((url, size, position, Path(profile).name))
            return True

    monkeypatch.setattr(surfaces, "mini_position", lambda size=surfaces.MINI_SIZE: (1500, 72))
    assert surfaces.open_mini("http://127.0.0.1:8765/mini", tmp_path, browser=FakeBrowser()) is True
    assert seen == [("http://127.0.0.1:8765/mini", surfaces.MINI_SIZE, (1500, 72), "chrome-mini")]


def test_app_window_flags(monkeypatch):
    from assistant.integrations import browser as br

    popen = []
    monkeypatch.setattr(br.subprocess, "Popen", lambda args, **kw: popen.append(args))
    b = br.Browser()
    b.chrome = "chrome"
    b.app_window("http://x/mini", size=(380, 560), position=(10, 20), profile="/d/chrome-mini")
    assert popen[0] == ["chrome", "--app=http://x/mini", "--window-size=380,560", "--window-position=10,20",
                        "--user-data-dir=/d/chrome-mini", "--no-first-run", "--no-default-browser-check"]
    b.app_window("http://x/")
    assert popen[1] == ["chrome", "--app=http://x/", "--window-size=1600,960"]  # the big HUD, as before


def test_tray_opens_the_mini_hud(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from assistant.bus import EventBus
    from assistant.tray import Tray

    class Cfg(dict):
        data_dir = tmp_path

    rt = SimpleNamespace(bus=EventBus(), listener=None, cfg=Cfg(assistant={"name": "Friday"}),
                         supervisor=SimpleNamespace(snapshot=lambda: []),
                         assistant=SimpleNamespace(tools=SimpleNamespace(guard=SimpleNamespace(hands_off=False))),
                         svc=SimpleNamespace(activity=SimpleNamespace(paused=False)))
    opened = []
    monkeypatch.setattr(surfaces, "open_mini", lambda url, data_dir: opened.append((url, data_dir)))
    items = {i.label: i for i in Tray(rt, "http://127.0.0.1:8765/", lambda: None).menu_items() if not i.separator}
    items["Open mini HUD"].action()
    assert opened == [("http://127.0.0.1:8765/mini", tmp_path)]


# ---- phone over home Wi-Fi -----------------------------------------------------------------------------------------

def test_phone_listener_is_off_by_default(pc, cfg, svc):
    c, app, _ = pc
    assert c.get("/api/hud/remote").json()["enabled"] is False
    with TestClient(LanGate(app), base_url=LAN_BASE, client=PHONE_IP) as remote:
        assert remote.get("/pin").status_code == 403
        assert remote.get("/api/state").status_code == 403


def test_phone_without_pin_gets_nothing(lan):
    _, remote, app, phone, _ = lan
    assert remote.get("/api/state").status_code == 401
    assert remote.get("/api/state", headers={"X-Assistant-Token": app.state.token}).status_code == 401  # not even with it
    r = remote.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/pin"
    page = remote.get("/pin")
    assert page.status_code == 200 and "6-digit PIN" in page.text and app.state.token not in page.text
    assert remote.get("/static/styles.css").status_code == 200  # the PIN page's own styles


def test_phone_pin_gives_a_signed_cookie_and_its_own_token(lan):
    local, remote, app, phone, _ = lan
    phone.clock.t += 2
    r = remote.post("/pin/login", json={"pin": phone.pin})
    header = r.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=strict" in header and f"max-age={30 * 86400}" in header
    cookie = r.cookies.get(COOKIE)
    csrf = phone.csrf(phone.session(cookie))
    assert remote.get("/api/state").status_code == 401  # the cookie alone isn't enough (CSRF)
    assert remote.get("/api/state", headers={"X-Assistant-Token": app.state.token}).status_code == 401
    st = remote.get("/api/state", headers={"X-Assistant-Token": csrf})
    assert st.status_code == 200 and st.json()["assistant"]["name"] == "Friday"
    html = remote.get("/").text
    assert csrf in html and app.state.token not in html and 'data-remote="phone"' in html
    # the phone's session never opens the loopback listener
    local.cookies.set(COOKIE, cookie)
    assert local.get("/api/state", headers={"X-Assistant-Token": csrf}).status_code == 401
    assert local.get("/api/state").status_code == 200


def test_phone_websocket_needs_cookie_and_session_token(lan):
    _, remote, app, phone, rt = lan
    cookie, csrf = sign_in(remote, phone)
    host = {"host": "192.168.1.23:8766"}  # websocket_connect doesn't follow base_url
    head = {**host, "cookie": f"{COOKIE}={cookie}", "origin": LAN_BASE}
    with pytest.raises(Exception):
        with remote.websocket_connect(f"/ws?token={csrf}", headers=host) as ws:  # no cookie
            ws.receive_text()
    with pytest.raises(Exception):
        with remote.websocket_connect(f"/ws?token={app.state.token}", headers=head) as ws:  # the install token
            ws.receive_text()
    with pytest.raises(Exception):
        with remote.websocket_connect(f"/ws?token={csrf}", headers={**head, "origin": "http://evil.example"}) as ws:
            ws.receive_text()
    with remote.websocket_connect(f"/ws?token={csrf}", headers=head) as ws:
        rt.bus.publish("announce", {"text": "hello phone"})
        assert json.loads(ws.receive_text())["type"] == "announce"


def test_wrong_pins_lock_the_phone_out(lan):
    _, remote, _, phone, rt = lan
    wrong = "000000" if phone.pin != "000000" else "111111"
    for i in range(MAX_FAILS - 1):
        phone.clock.t += 2
        r = remote.post("/pin/login", json={"pin": wrong})
        assert r.status_code == 401 and r.json()["left"] == MAX_FAILS - 1 - i
    phone.clock.t += 2
    r = remote.post("/pin/login", json={"pin": wrong})
    assert r.status_code == 429 and "15 minutes" in r.json()["error"] and int(r.headers["retry-after"]) == LOCK_S
    phone.clock.t += 2
    assert remote.post("/pin/login", json={"pin": phone.pin}).status_code == 429  # even the right one, for now
    notice = [e for e in rt.bus.recent if e["type"] == "notice"]
    assert notice and "192.168.1.50" in notice[-1]["data"]["text"]
    phone.clock.t += LOCK_S + 1
    assert remote.post("/pin/login", json={"pin": phone.pin}).status_code == 200


def test_pin_tries_are_rate_limited(lan):
    _, remote, _, phone, _ = lan
    phone.clock.t += 2
    assert remote.post("/pin/login", json={"pin": "12"}).status_code == 401
    r = remote.post("/pin/login", json={"pin": phone.pin})  # same second
    assert r.status_code == 429 and r.json()["error"] == "One try a second."


def test_whole_network_lockout(cfg, svc):
    phone = PhoneAccess(cfg, svc.bus, "tok", clock=Clock(), addresses=lambda: ["192.168.1.23"])
    wrong = "000000" if phone.pin != "000000" else "111111"
    for i in range(NET_FAILS):
        phone.clock.t += 1.1
        phone.attempt(f"192.168.1.{100 + i // 4}", wrong)  # 4 tries each from 5 devices: none locked alone
    phone.clock.t += 2
    r = phone.attempt("192.168.1.200", phone.pin)
    assert r["status"] == 429 and "network" in r["error"]


def test_phone_cookie_tampered_expired_or_revoked(cfg, svc):
    phone = PhoneAccess(cfg, svc.bus, "install-token", clock=Clock())
    cookie = phone.issue()
    sid = phone.session(cookie)
    assert sid and phone.csrf(sid) != "install-token"
    s, exp, sig = cookie.split(".")
    assert phone.session(f"{s}.{int(exp) + 999}.{sig}") is None  # can't stretch the expiry
    assert phone.session(f"{s}x.{exp}.{sig}") is None
    assert phone.session(cookie[:-1] + ("0" if cookie[-1] != "0" else "1")) is None
    assert PhoneAccess(cfg, svc.bus, "other-install", clock=Clock()).session(cookie) is None  # signed with the token
    phone.clock.t += 31 * 86400
    assert phone.session(cookie) is None
    phone.clock.t -= 31 * 86400
    phone.new_pin()
    assert phone.session(cookie) is None  # a new PIN signs every phone out
    cookie = phone.issue()
    phone.sign_out_all()
    assert phone.session(cookie) is None


def test_phone_listener_checks_host_and_client(lan):
    _, remote, _, phone, _ = lan
    cookie, csrf = sign_in(remote, phone)
    head = {"X-Assistant-Token": csrf}
    assert remote.get("/api/state", headers={**head, "Host": "evil.example:8766"}).status_code == 403
    assert remote.get("/api/state", headers={**head, "Host": "127.0.0.1:8766"}).status_code == 403
    assert remote.get("/api/state", headers={**head, "Host": "gamingpc.local:8766"}).status_code == 200
    assert remote.get("/api/state", headers={**head, "Host": "192.168.1.23:9999"}).status_code == 403
    app = remote.app
    with TestClient(app, base_url=LAN_BASE, client=("8.8.8.8", 4000)) as outside:
        assert outside.get("/pin").status_code == 403


def test_pc_only_routes_are_closed_to_the_phone(lan):
    _, remote, _, phone, _ = lan
    cookie, csrf = sign_in(remote, phone)
    head = {"X-Assistant-Token": csrf}
    for path in ("/api/quit", "/api/hud/remote", "/api/hud/overlay", "/api/hud/mini/open"):
        assert remote.post(path, headers=head, json={}).status_code == 403, path
    assert remote.get("/api/hud/remote", headers=head).status_code == 403
    assert remote.get("/overlay").status_code == 403
    assert remote.get("/mini").status_code == 200


def test_remote_settings_from_the_pc(pc, cfg, monkeypatch):
    c, app, _ = pc
    phone = app.state.phone
    started = []

    async def fake_start(a):
        started.append(a)

    async def fake_stop():
        started.append("stop")

    monkeypatch.setattr(phone, "start", fake_start)
    monkeypatch.setattr(phone, "stop", fake_stop)
    r = c.post("/api/hud/remote", json={"enabled": True}).json()
    assert r["enabled"] is True and len(r["pin"]) == 6 and r["pin"].isdigit()
    assert r["urls"] == ["http://192.168.1.23:8766/", "http://gamingpc.local:8766/"]
    assert started == [app]
    saved = yaml.safe_load((cfg.data_dir / SETTINGS_FILE).read_text(encoding="utf-8"))
    assert saved["hud"]["remote"]["enabled"] is True
    pin = r["pin"]
    old = phone.session(phone.issue())
    assert old and c.post("/api/hud/remote", json={"new_pin": True}).json()["ok"]
    assert phone.session(phone.issue()) and pin.isdigit()
    assert c.post("/api/hud/remote", json={"enabled": False}).json()["enabled"] is False and started[-1] == "stop"
    assert "pin" not in app.state.phone.bus.latest["hud_remote"]["data"]  # the PIN never goes out on the bus


def test_phone_listener_starts_and_stops(cfg, svc):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cfg["hud"] = {"remote": {"enabled": True, "bind": "127.0.0.1", "port": port}}
    phone = PhoneAccess(cfg, svc.bus, "tok", addresses=lambda: ["192.168.1.23"])

    async def run():
        async def app(scope, receive, send):
            pass
        await phone.start(app)
        for _ in range(100):
            if phone.running:
                break
            await asyncio.sleep(0.02)
        assert phone.running and phone.status()["running"]
        await phone.stop()
        assert not phone.running
    asyncio.run(run())


def test_phone_listener_reports_a_busy_port(cfg, svc):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    cfg["hud"] = {"remote": {"enabled": True, "bind": "127.0.0.1", "port": port}}
    phone = PhoneAccess(cfg, svc.bus, "tok", addresses=lambda: ["192.168.1.23"])

    async def run():
        async def app(scope, receive, send):
            pass
        await phone.start(app)
        await asyncio.wait_for(phone.task, 5)
    try:
        asyncio.run(run())
    finally:
        s.close()
    assert phone.error and str(port) in phone.error and not phone.running


def test_no_home_network(cfg, svc):
    phone = PhoneAccess(cfg, svc.bus, "tok", addresses=lambda: [])
    asyncio.run(phone.start(None))
    assert "home network" in phone.error


def test_split_host():
    assert split_host("192.168.1.5:8766") == ("192.168.1.5", 8766)
    assert split_host("[fe80::1]:80") == ("fe80::1", 80)
    assert split_host("GamingPC") == ("gamingpc", None)
    assert split_host(None) == ("", None)


# ---- OBS overlay ----------------------------------------------------------------------------------------------------

PRIVATE = ["dana@acme.com", "Therapy appointment", "PRIVATE QUESTION", "secretuser", "Call the doctor", "my-laptop",
           "spotify:track:abc", "viewer wrote this", "evil.example"]


def seed_private(rt):
    bus = rt.bus
    bus.publish("spotify", {"connected": True, "now": {"playing": True, "track": "Midnight City", "artist": "M83", "album": "Hurry Up",
                                                       "art": "https://i.scdn.co/image/abc", "progress_ms": 1000, "duration_ms": 200000,
                                                       "device": "my-laptop", "uri": "spotify:track:abc", "at": 1}}, sticky=True)
    bus.publish("stream_copilot", {"live": True, "now": {"subs": 3, "follows": 12, "bits": 50, "viewers": 99},
                                   "mood": {"score": 0.4, "label": "hyped", "rate": 30},
                                   "questions": [{"user": "secretuser", "text": "PRIVATE QUESTION"}],
                                   "greet": [{"user": "secretuser"}]}, sticky=True)
    bus.publish("automations", {"items": [{"id": "t1", "kind": "timer", "label": "Call the doctor", "at": 9e12, "total_s": 300},
                                          {"id": "r1", "kind": "reminder", "text": "dana@acme.com", "at": 9e12}]}, sticky=True)
    bus.publish("calendar", {"events": [{"title": "Therapy appointment", "start": "2999-01-01T10:00:00", "profile": "personal"}]},
                sticky=True)
    rt.twitch_feed.add({"kind": "follow", "user": "nightowl", "text": "viewer wrote this"})
    rt.twitch_feed.add({"kind": "chat", "user": "secretuser", "text": "viewer wrote this"})


def test_overlay_has_its_own_key(pc):
    c, app, _ = pc
    key = c.get("/api/hud/overlay").json()["key"]
    assert key and key != app.state.token
    assert c.get("/overlay?panels=goals").status_code == 401
    assert c.get(f"/overlay?panels=goals&key={app.state.token}").status_code == 401
    page = c.get(f"/overlay?panels=goals&key={key}")
    assert page.status_code == 200 and page.headers["referrer-policy"] == "no-referrer" and app.state.token not in page.text
    assert c.get(f"/overlay/state?key={app.state.token}").status_code == 401
    # the overlay key opens nothing else
    assert c.get("/api/state", headers={"X-Assistant-Token": key}).status_code == 401
    with pytest.raises(Exception):
        with c.websocket_connect(f"/ws?token={key}") as ws:
            ws.receive_text()


def test_overlay_state_is_whitelisted(pc):
    c, _, rt = pc
    seed_private(rt)
    key = c.get("/api/hud/overlay").json()["key"]
    c.post("/api/hud/overlay", json={"goals": [{"kind": "subs", "target": 10}, {"kind": "nope", "target": 5}]})
    st = c.get(f"/overlay/state?key={key}").json()
    text = json.dumps(st)
    for word in PRIVATE:
        assert word not in text, word
    assert st["nowplaying"]["track"] == "Midnight City" and st["nowplaying"]["art"] == "https://i.scdn.co/image/abc"
    assert st["mood"] == {"live": True, "score": 0.4, "label": "hyped", "rate": 30}
    assert st["goals"]["items"] == [{"kind": "subs", "label": "Sub goal", "current": 3, "target": 10}]
    assert st["timers"]["items"] == [{"at": 9e12, "total_s": 300}]
    assert st["countdown"]["at"] is None  # a personal calendar event is never counted down
    assert [f["user"] for f in st["followers"]] == ["nightowl"]


def test_overlay_socket_sends_only_overlay_events(pc):
    c, _, rt = pc
    key = c.get("/api/hud/overlay").json()["key"]
    with c.websocket_connect(f"/overlay/ws?key={key}") as ws:
        first = json.loads(ws.receive_text())
        assert first["type"] == "snapshot" and set(first["data"]) == {"nowplaying", "mood", "goals", "timers", "countdown", "followers"}
        rt.bus.publish("assistant_said", {"text": "dana@acme.com said the contract is late"})
        rt.bus.publish("user_said", {"text": "Therapy appointment"})
        rt.bus.publish("twitch_event", {"kind": "chat", "user": "secretuser", "text": "viewer wrote this"})
        rt.bus.publish("twitch_event", {"kind": "raid", "user": "BigRaider", "amount": 42, "text": "viewer wrote this"})
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "follower" and msg["data"]["user"] == "BigRaider" and "text" not in msg["data"]
        rt.bus.publish("stream_copilot", {"live": True, "mood": {"score": -0.6, "label": "restless", "rate": 3},
                                          "questions": [{"text": "PRIVATE QUESTION"}]}, sticky=True)
        got = {}
        for _ in range(2):
            m = json.loads(ws.receive_text())
            got[m["type"]] = m["data"]
        assert got["mood"]["label"] == "restless" and "PRIVATE" not in json.dumps(got)


def test_overlay_new_link_retires_the_old_one(pc):
    c, _, rt = pc
    old = c.get("/api/hud/overlay").json()["key"]
    with c.websocket_connect(f"/overlay/ws?key={old}") as ws:
        ws.receive_text()
        new = c.post("/api/hud/overlay/rotate").json()["key"]
        assert new != old
        rt.bus.publish("spotify", {"now": {"track": "x"}}, sticky=True)
        with pytest.raises(Exception):
            ws.receive_text()
    assert c.get(f"/overlay/state?key={old}").status_code == 401
    assert c.get(f"/overlay/state?key={new}").status_code == 200


def test_overlay_settings_goals_and_countdown(pc, cfg):
    c, _, rt = pc
    r = c.post("/api/hud/overlay", json={"goals": [{"kind": "follows", "target": 50, "label": "Road to 50"}, {"kind": "subs", "target": "x"}],
                                         "countdown_min": 5, "countdown_label": "Back soon"}).json()
    conf = r["settings"]
    assert conf["goals"] == [{"kind": "follows", "target": 50, "label": "Road to 50"}]
    assert conf["countdown"]["label"] == "Back soon" and conf["countdown"]["at"] > 0
    saved = yaml.safe_load((cfg.data_dir / SETTINGS_FILE).read_text(encoding="utf-8"))
    assert saved["hud"]["overlay"]["goals"][0]["target"] == 50
    assert rt.bus.recent[-1]["type"] == "hud_overlay"
    assert c.post("/api/hud/overlay", json={"countdown_min": 0}).json()["settings"]["countdown"] == {}
    assert c.post("/api/hud/overlay", json={"countdown_min": "soon"}).status_code == 400
    assert c.post("/api/hud/overlay", json={"goals": []}, headers={"X-Assistant-Token": "bad"}).status_code == 401


def test_overlay_socket_refuses_bad_key_and_foreign_host(pc):
    c, _, _ = pc
    key = c.get("/api/hud/overlay").json()["key"]
    for url, head in (("/overlay/ws?key=nope", {}), (f"/overlay/ws?key={key}", {"host": "evil.example:8765"})):
        with pytest.raises(Exception):
            with c.websocket_connect(url, headers=head) as ws:
                ws.receive_text()


def test_overlay_projections():
    assert overlay.parse_panels("music, copilot,ticker,bogus,goals,goals") == ["nowplaying", "mood", "followers", "goals"]
    assert overlay.parse_panels("") == ["nowplaying", "goals", "mood"]
    assert overlay.now_playing({"now": {"track": "a", "art": "https://evil.example/x.png"}})["art"] is None
    assert overlay.now_playing({"now": {"track": None}}) == {"playing": False}
    assert overlay.now_playing(None) == {"playing": False}
    assert overlay.follower({"kind": "redemption", "user": "x", "text": "hi"}) is None
    assert overlay.follower({"kind": "gift", "user": None, "amount": 5, "ts": 3}) == {"kind": "gift", "user": None, "amount": 5, "ts": 3}
    assert overlay.goals({"live": False, "now": {"subs": 9}}, {"goals": [{"kind": "subs", "target": 10}]})["items"][0]["current"] == 0
    cal = {"events": [{"start": "2999-01-01T20:00:00", "profile": "stream", "title": "Stream: secret sponsor"},
                      {"start": "2999-01-01T19:00:00", "profile": "work", "title": "x"}]}
    import datetime as dt
    soon = dt.datetime.fromisoformat("2999-01-01T19:30:00").timestamp()
    cd = overlay.countdown(cal, {}, now=soon)
    assert cd["label"] == "Stream starts" and cd["source"] == "calendar" and "secret" not in json.dumps(cd)
    assert overlay.countdown(cal, {"countdown": {"at": soon + 60, "label": "Starting soon"}}, now=soon)["source"] == "manual"
    assert overlay.timers({"items": [{"kind": "timer", "at": 100, "label": "x"}]}, now=1000) == {"items": []}
    assert overlay.event_type('{"type": "spotify", "ts": 1}') == "spotify"
    assert overlay.event_type('{"ts": 1, "type": "calendar"}') == "calendar"


# ---- the QR code ----------------------------------------------------------------------------------------------------

# "http://192.168.1.23:8766/", level M, mask 2: made by the python-qrcode reference library.
GOLDEN = [
    "#######...###.#...#######", "#.....#..#.#.#.#..#.....#", "#.###.#.##......#.#.###.#", "#.###.#.##.###....#.###.#",
    "#.###.#.#.###.#...#.###.#", "#.....#.#..###.##.#.....#", "#######.#.#.#.#.#.#######", "........###...##.........",
    "#.#####..##..#.##.#####..", ".#.#.#...#..###..#.....#.", ".######.#..#.#.#..##.#.##", "#...#..####.#...#...#...#",
    "#..##.##.#.###....###.###", "#..#.#.##....##......#.#.", "#.....#.#..##..#.#.#.#.##", "#...##.#.#.#...#.....#..#",
    "#.#..###.#####..#####.#..", "........###.#.#.#...###..", "#######..#...##.#.#.#####", "#.....#.####..###...##.##",
    "#.###.#.#..###.########..", "#.###.#.#.###.###.###.###", "#.###.#.#.#.###..#....#.#", "#.....#...##....#.####..#",
    "#######.###..#.#.########",
]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_qr_encoder_matches_the_reference():
    script = ("const Q = require(process.argv[1]);"
              "console.log(JSON.stringify(Q.matrix('http://192.168.1.23:8766/', { ecl: 'M', mask: 2 }).map(r => r.map(b => b ? '#' : '.').join(''))));"
              "const a = Q.matrix('http://192.168.1.23:8766/'); console.log(a.length);")
    # stdin closed: on a Windows runner node otherwise sat on the inherited console handle until the timeout
    out = subprocess.run([shutil.which("node"), "-e", script, str(WEB / "qr.js")], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=120, check=True).stdout.splitlines()
    assert json.loads(out[0]) == GOLDEN
    assert out[1] == "25"


def test_new_web_files_only_write_text():
    """Every string in the new pages goes in through textContent / h(), never as HTML."""
    for name in ("layout.js", "surfaces.js", "mini.js", "overlay.js", "qr.js"):
        js = (WEB / name).read_text(encoding="utf-8")
        assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "outerHTML" not in js, name
