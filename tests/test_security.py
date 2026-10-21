"""Security review of Vesper 2.0 (docs/SECURITY.md): who may drive the HUD's API, sign-in callbacks, skill actions
never handing out secrets, the phone bridge, tool servers (MCP), clipboard/screen privacy, secrets never kept in
memory or logs, and .env writes that can't be made to add a line."""

import json
import os
import webbrowser
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_skill_phone import ME, STRANGER, TOKEN, pair, tg  # noqa: F401  (fixture: a fake Telegram)

from assistant import privacy
from assistant.brain.assistant import Assistant
from assistant.firstrun import set_env_value
from assistant.runtime import Runtime
from assistant.server import create_app, origin_ok, token_exempt
from assistant.skills import _kit
from assistant.skills import clipboard as clip_mod
from assistant.skills import mcp as mcp_mod
from assistant.skills import screen as screen_mod

# ---- the HUD's API: token, Host and Origin ---------------------------------------------------------------------


@pytest.fixture
def hud(cfg, svc):
    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=False)
    with TestClient(app) as c:
        c.rt, c.app = rt, app
        c.h = {"x-assistant-token": app.state.token}
        yield c


def test_every_api_route_needs_the_token_except_the_sign_in_callbacks(hud):
    open_routes = []
    for r in hud.app.routes:
        if not isinstance(r, APIRoute) or not r.path.startswith("/api"):
            continue
        path = r.path.replace("{name}", "spotify").replace("{action}", "connect")
        for method in r.methods:
            got = hud.request(method, path, json={})
            if got.status_code != 401:
                open_routes.append((method, r.path))
    assert sorted(open_routes) == [("GET", "/api/google/callback"), ("GET", "/api/skill/{name}/callback")]


def test_only_a_get_to_an_exact_callback_path_skips_the_token():
    assert token_exempt("GET", "/api/skill/spotify/callback")
    for method, path in [("POST", "/api/skill/spotify/callback"), ("PUT", "/api/skill/spotify/callback"),
                         ("HEAD", "/api/skill/spotify/callback"), ("GET", "/api/skill/spotify/callback/"),
                         ("GET", "/api/skill/spotify/connect"), ("GET", "/api/skill/Spotify/callback"),
                         ("GET", "/api/skill/spotify/callback/x"), ("GET", "/api/skill/../state/callback")]:
        assert not token_exempt(method, path), (method, path)


def test_a_web_page_cant_drive_vesper(hud):
    """DNS rebinding (a foreign Host) and a page's own fetch (a foreign Origin) are refused even with the token."""
    assert hud.get("/api/state", headers=hud.h).status_code == 200
    assert hud.get("/api/state", headers={**hud.h, "host": "evil.example:8765"}).status_code == 403
    assert hud.get("/", headers={"host": "vesper.evil.example"}).status_code == 403  # the page carrying the token
    for origin in ("https://evil.example", "null", "http://127.0.0.1.evil.example:8765", "file://"):
        r = hud.post("/api/skill/mcp/add", json={"name": "x", "target": "calc.exe"}, headers={**hud.h, "origin": origin})
        assert r.status_code == 403, origin
    assert hud.post("/api/command", json={"text": "what time is it"},
                    headers={**hud.h, "origin": "http://127.0.0.1:8765"}).status_code == 200
    assert origin_ok(None) and origin_ok("http://localhost:8765") and origin_ok("http://[::1]:8765")


def test_the_live_feed_needs_the_token_and_a_local_page(hud):
    from starlette.websockets import WebSocketDisconnect

    tok = hud.app.state.token
    for url, headers in [("/ws?token=nope", {}), (f"/ws?token={tok}", {"origin": "https://evil.example"})]:
        with pytest.raises(WebSocketDisconnect):
            with hud.websocket_connect(url, headers=headers) as ws:
                ws.receive_text()
    with hud.websocket_connect(f"/ws?token={tok}", headers={"origin": "http://127.0.0.1:8765"}):
        pass


def test_mcp_command_lines_from_the_hud_need_the_token(hud, monkeypatch):
    """Adding a tool server runs a command on this PC: the token (only on Vesper's own page) is the gate."""
    added = []
    monkeypatch.setattr(mcp_mod.MCP, "add", lambda self, body: added.append(body) or {"ok": True})
    bad = [hud.post("/api/skill/mcp/add", json={"name": "x", "target": "calc.exe"}),
           hud.post("/api/skill/mcp/add", json={"name": "x", "target": "calc.exe"},
                    headers={"x-assistant-token": "guess"}),
           hud.post("/api/skill/mcp/add", json={"name": "x", "target": "calc.exe"}, headers={**hud.h, "host": "x.io"})]
    assert [r.status_code for r in bad] == [401, 401, 403] and added == []
    assert hud.post("/api/skill/mcp/add", json={"name": "x", "target": "npx srv"}, headers=hud.h).json()["ok"]
    assert len(added) == 1


# ---- sign-in callbacks: single-use state, for every skill that has one -------------------------------------------

def _callback_skills(svc):
    return [s for s in svc.skills if callable(getattr(s, "callback", None))]


def test_every_callback_skill_starts_its_sign_in_with_a_state(svc):
    skills = _callback_skills(svc)
    assert [s.name for s in skills] == ["spotify"]  # add the new one's setup to _ready() below
    for s in skills:
        assert callable(getattr(s, "begin", None)), f"{s.name}: a callback needs begin() to issue the state"


def _ready(skill, monkeypatch):
    """Configure one callback skill so begin() works, with its token endpoint faked (and counted)."""
    calls = []

    def answer(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})

    if skill.name == "spotify":
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "0123456789abcdef0123456789abcdef")
        skill.http = httpx.Client(transport=httpx.MockTransport(answer))
        skill.opener = lambda url: None
        skill._save = lambda: None
    return calls


def _state(skill):
    return parse_qs(urlparse(skill.begin()["url"]).query)["state"][0]


def test_a_missing_wrong_replayed_or_stale_state_is_refused(hud, svc, monkeypatch):
    for skill in _callback_skills(svc):
        calls = _ready(skill, monkeypatch)
        base = f"/api/skill/{skill.name}/callback"
        good = _state(skill)
        for query in ("", "?code=abc", "?state=&code=abc", "?state=forged&code=abc", f"?state={good[:-1]}x&code=abc"):
            r = hud.get(base + query)
            assert r.status_code == 400, (skill.name, query)
        assert calls == []  # a refused state never reaches the service's token endpoint
        assert hud.get(f"{base}?state={good}&code=abc").status_code == 200 and len(calls) == 1
        assert hud.get(f"{base}?state={good}&code=abc").status_code == 400  # replayed
        assert len(calls) == 1
        stale = _state(skill)
        clock = skill.clock
        monkeypatch.setattr(skill, "clock", lambda: clock() + 3600)
        assert hud.get(f"{base}?state={stale}&code=abc").status_code == 400  # expired
        assert len(calls) == 1
        assert hud.post(f"{base}?state={stale}&code=abc").status_code == 401  # only GET skips the token
        page = hud.get(f"{base}?error=<script>alert(1)</script>").text
        assert "<script>" not in page  # what the service says back is shown escaped


# ---- skill actions and panels never hand out secrets -------------------------------------------------------------

SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-" + "S" * 40,
    "HA_TOKEN": "eyJhbGciOiJIUzI1NiJ9.SECRETHAPAYLOAD0123.SECRETHASIG0123456",
    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123456789/SECRETWEBHOOKTOKEN0123456789",
    "YOUTUBE_API_KEY": "AIza" + "Y" * 35,
    "TELEGRAM_BOT_TOKEN": "123456789:AAH" + "T" * 32,
    "NTFY_TOPIC": "ntfyxx-SECRETTOPIC0123456789",
    "MCP_NOTION_TOKEN": "secret_NOTIONTOKEN0123456789abcdef",
    "GOOGLE_CLIENT_SECRET": "GOCSPX-SECRETGOOGLE0123456789",
    "OBS_PASSWORD": "SECRETOBSPASSWORD99",
    "TWITCH_CLIENT_SECRET": "SECRETTWITCH0123456789abcdef",
    "GITHUB_TOKEN": "ghp_" + "G" * 36,
    "SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef",
}
SPOTIFY_TOKENS = {"access_token": "SECRETSPOTIFYACCESS0123456789", "refresh_token": "SECRETSPOTIFYREFRESH0123456789",
                  "expires_at": 9e12, "scope": "user-read-playback-state"}
DESTRUCTIVE = {"disconnect", "unpair", "new_code", "remove", "remove_light", "clear", "forget", "delete", "undo",
               "ntfy", "token", "client", "webhook", "ha", "setup"}
BODIES = {  # a plausible body for actions that do nothing with {}
    ("mcp", "add"): {"name": "x", "target": "https://example.invalid/mcp"},
    ("discord", "post"): {"text": "hello"}, ("home", "toggle"): {"target": "key light"},
}


def _leaks(value) -> list[str]:
    text = json.dumps(value, default=str)
    hidden = [*[v for k, v in SECRETS.items() if k != "SPOTIFY_CLIENT_ID"], SPOTIFY_TOKENS["access_token"],
              SPOTIFY_TOKENS["refresh_token"], "SECRETWEBHOOKTOKEN", "SECRETTOPIC"]
    return [s for s in hidden if s in text]


@pytest.fixture
def secret_world(svc, cfg, monkeypatch, tmp_path):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HA_URL", "http://homeassistant.local:8123")

    def no_network(self, request, **kw):  # every call fails, and its error message carries the URL (with any key)
        raise httpx.ConnectError(f"can't reach {request.url} (headers {dict(request.headers)})", request=request)

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)
    sk = svc.skills
    sk["spotify"].tokens = dict(SPOTIFY_TOKENS)
    sk["spotify"].opener = lambda url: None
    sk["spotify"]._save = lambda: None
    sk["home"].discover = lambda wait=3.0: {"ok": False, "error": "no network in tests"}
    sk["home"].cfg["key_lights"] = [{"ip": "192.168.1.40", "name": "Key Light"}]
    sk["youtube"].cfg["channel"] = "@jvesper"
    sk["weather"].cfg["place"] = {"name": "Atlanta", "lat": 33.7, "lon": -84.4}
    sk["janitor"].cfg["downloads"] = str(tmp_path / "Downloads")
    sk["janitor"].list_drives = lambda: []
    sk["janitor"].runner = lambda args: ""
    sk["screen"].capture = lambda *a, **k: None
    sk["screen"].window = lambda: None

    class Board:
        def __init__(self):
            self.text = "hello"

        def sequence(self):
            return None

        def read(self):
            return self.text

        def write(self, text):
            self.text = text
            return True

    sk["clipboard"].board = Board()
    mcp = sk["mcp"]
    mcp.servers["notion"] = mcp_mod.Server(mcp_mod.normalize({
        "name": "notion", "command": "npx", "args": ["-y", "@notionhq/notion-mcp-server"],
        "env": {"NOTION_TOKEN": "${MCP_NOTION_TOKEN}"}}))
    mcp.connect = lambda name, wait=0: {"ok": False, "state": "error", "error": "not in tests"}
    mcp._save = lambda: None
    for s in sk:  # no saving of settings into the developer's real data folder
        monkeypatch.setattr(_kit, "save_setting", lambda skill, key, value: skill.cfg.__setitem__(key, value))
    seen = []
    svc.bus.on(lambda ev: seen.append(ev))
    svc.bus_seen = seen
    return svc


def test_no_skill_action_or_panel_hands_out_a_secret(secret_world, cfg):
    svc = secret_world
    assert _leaks({"x": SECRETS["HA_TOKEN"]}) and _leaks(f"key={SECRETS['YOUTUBE_API_KEY']}")  # the detector works
    checked = []
    for skill in svc.skills:
        st = skill.status()
        assert not _leaks(st), (skill.name, "status", _leaks(st))
        # the ones that forget a sign-in or a setting go last, so the rest run while everything is connected
        for action in sorted(skill.actions(), key=lambda a: a in DESTRUCTIVE):
            try:
                out = svc.skills.action(skill.name, action, BODIES.get((skill.name, action), {}))
            except Exception as exc:  # a crash is no leak, but say which
                out = {"crashed": f"{type(exc).__name__}: {exc}"}
            assert not _leaks(out), (skill.name, action, _leaks(out))
            checked.append(f"{skill.name}/{action}")
        st = skill.status()
        assert not _leaks(st), (skill.name, "status after actions", _leaks(st))
    assert len(checked) >= 100  # every action of every skill
    assert not _leaks([e.get("data") for e in svc.bus_seen])  # nor anything sent to the HUD while they ran
    rt = Runtime(cfg, services=svc)
    assert not _leaks(rt.state())


# ---- the phone bridge ---------------------------------------------------------------------------------------------

def test_guessing_the_pairing_code_locks_pairing_for_longer_each_time(tg):  # noqa: F811
    p = tg.p
    now = [1000.0]
    p.clock = lambda: now[0]
    code = p.state["code"]
    notices = []
    p.svc.bus.on(lambda ev: notices.append(ev["data"]) if ev["type"] == "notice" else None)
    for i in range(5):
        tg.msg(f"/start {(int(code) + 1 + i) % 1_000_000:06d}", chat=STRANGER)
    p.poll_once(timeout=0)
    assert p.state["code"] != code and p.state["lockouts"] == 1 and p._locked_until == 1600
    assert not p.chat_id and [n["key"] for n in notices] == ["phone:guessing"]  # J hears about it
    tg.msg(f"/start {p.state['code']}", chat=STRANGER)  # even the right code is refused while locked
    p.poll_once(timeout=0)
    assert not p.chat_id
    now[0] = 1601
    code = p.state["code"]
    for _ in range(5):
        tg.msg("/start 000000" if code != "000000" else "/start 111111", chat=STRANGER)
    p.poll_once(timeout=0)
    assert p.state["lockouts"] == 2 and p._locked_until == 1601 + 1200  # doubled
    from assistant.skills import phone as ph

    again = ph.Phone(p.svc, http=p.http, idle=lambda: 0, clock=lambda: now[0])  # a restart keeps the lockout
    assert again._locked_until == p._locked_until and again.state["lockouts"] == 2


def test_a_wrong_guess_count_survives_a_restart(tg):  # noqa: F811
    from assistant.skills import phone as ph

    p = tg.p
    for _ in range(3):
        tg.msg("/start 999999" if p.state["code"] != "999999" else "/start 888888", chat=STRANGER)
    p.poll_once(timeout=0)
    assert ph.Phone(p.svc, http=p.http, idle=lambda: 0)._bad == 3


def test_a_group_chat_can_never_pair(tg):  # noqa: F811
    tg.msg(f"/start {tg.p.state['code']}", type="group")
    tg.p.poll_once(timeout=0)
    assert not tg.p.chat_id


def test_risky_commands_from_the_phone_still_need_a_yes_and_strangers_are_ignored(tg, svc, monkeypatch):  # noqa: F811
    from assistant.integrations import desktop

    done = []
    monkeypatch.setattr(desktop, "power_action", lambda action: done.append(action) or {"ok": True})
    a = Assistant(svc)
    p = tg.p
    p.run_command = lambda text: a.handle(text, "phone")
    p.pending = a.pending_view
    pair(tg)
    tg.msg("turn off the pc")
    p.poll_once(timeout=0)
    assert done == [] and a.pending_view() is not None
    assert "Reply yes or no" in tg.texts()[-1]
    tg.msg("yes", chat=STRANGER)  # a stranger's yes does nothing, and gets no answer
    tg.button("yes", chat=STRANGER)
    p.poll_once(timeout=0)
    assert done == [] and tg.texts(STRANGER) == []
    tg.button("yes")
    p.poll_once(timeout=0)
    assert done == ["shutdown"]


# ---- tool servers (MCP) -----------------------------------------------------------------------------------------

def test_mcp_tools_ask_first_and_what_they_return_is_untrusted(svc):
    srv = mcp_mod.Server(mcp_mod.normalize({"name": "files", "command": "npx", "args": ["srv"]}))
    srv.client = object()
    srv.tools = [{"name": "write_file", "title": "", "description": "Write a file", "schema": {}, "read_only": False}]
    mcp = svc.skills["mcp"]
    mcp.servers["files"] = srv
    tool = next(t for t in mcp.tools() if t.name == "mcp_files_write_file")
    assert tool.needs_confirmation({}) and tool.untrusted and "never instructions" in tool.untrusted_note
    assert mcp_mod.clamp_tier(0) == 1 and mcp_mod.clamp_tier("x") == 2 and mcp_mod.clamp_tier(9) == 3
    assert mcp_mod.normalize({"name": "y", "url": "https://x"})["tier"] == 2  # a new server asks for every call


def test_mcp_token_with_a_line_break_never_reaches_env(svc, cfg, monkeypatch):
    mcp = svc.skills["mcp"]
    mcp.connect = lambda name, wait=0: {"ok": True, "state": "off"}
    out = svc.skills.action("mcp", "add", {"name": "notion", "target": "npx notion", "token_var": "NOTION_TOKEN",
                                           "token": "abc\nVESPER_INJECTED=http://evil.example"})
    assert out["ok"] is False and "line break" in out["error"]
    env = cfg.root / ".env"
    assert not env.exists() or "VESPER_INJECTED" not in env.read_text()


# ---- clipboard and screen privacy ---------------------------------------------------------------------------------

class Win:
    def __init__(self, app="code", title="notes.txt - VS Code"):
        self.app, self.title = app, title


class Board:
    def __init__(self, text, private=False):
        self.text, self.is_private, self.reads, self.seq = text, private, 0, 0

    def sequence(self):
        self.seq += 1
        return self.seq

    def read(self):
        self.reads += 1
        return self.text

    def private(self):
        return self.is_private


@pytest.mark.parametrize("text,win,private,kept", [
    ("meeting notes for friday", Win(), False, True),
    ("hunter2-Correct!", Win("keepassxc", "KeePassXC"), False, False),  # copied from a password manager
    ("Account 4521 balance", Win("chrome", "Chase Online Banking - Google Chrome"), False, False),  # a bank
    ("some text", Win("msedge", "New InPrivate tab - Microsoft Edge"), False, False),  # private browsing
    ("plain words", Win(), True, False),  # the copying app marked it "don't record"
    ("sk-ant-api03-" + "x" * 40, Win(), False, False),  # an API key anywhere
    ("4111 1111 1111 1111", Win(), False, False),  # a card number
])
def test_the_clipboard_keeps_only_what_isnt_private(svc, text, win, private, kept):
    c = clip_mod.Clipboard(svc, clipboard=Board(text, private), window=lambda: win)
    c.tick()
    assert bool(c.items) is kept
    if private:
        assert c.board.reads == 0 and c.skipped == 1  # never even read


def test_your_own_deny_list_covers_the_clipboard_too(svc):
    c = clip_mod.Clipboard(svc, clipboard=Board("payroll numbers"), window=lambda: Win("excel", "Payroll 2026.xlsx"))
    c.cfg["deny_titles"] = ["payroll"]
    c.tick()
    assert c.items == []


def test_windows_private_clipboard_marks_are_honoured():
    class User32:
        def __init__(self, marked):
            self.marked = marked

        def RegisterClipboardFormatW(self, name):  # noqa: N802 (the Win32 name)
            return {"ExcludeClipboardContentFromMonitorProcessing": 49001, "Clipboard Viewer Ignore": 49002}[name]

        def IsClipboardFormatAvailable(self, fmt):  # noqa: N802
            return fmt in self.marked

    assert clip_mod.WinClipboard(User32({49001}), object()).private()
    assert clip_mod.WinClipboard(User32({49002}), object()).private()
    assert not clip_mod.WinClipboard(User32(set()), object()).private()
    assert not clip_mod.WinClipboard(object(), object()).private()  # an old Windows without them: just not private


@pytest.mark.parametrize("app,title", [("1Password", "1Password"), ("chrome", "Wells Fargo – Sign On"),
                                       ("firefox", "Private Browsing — Mozilla Firefox"),
                                       ("credentialuibroker", "Windows Security")])
def test_the_screen_never_looks_at_private_windows(svc, app, title):
    shots = []
    s = screen_mod.Screen(svc, window=lambda: Win(app, title), capture_fn=lambda *a, **k: shots.append(1))
    out = s.look("what's this", explicit=True)
    assert out["ok"] is False and out.get("private") and shots == []


def test_the_screen_isnt_looked_at_on_air_unless_asked(svc):
    shots = []
    s = screen_mod.Screen(svc, window=lambda: Win(), capture_fn=lambda *a, **k: shots.append(1))
    svc.live = lambda: True
    assert s.look("what's this", explicit=False)["ok"] is False and shots == []


# ---- secrets are never kept -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["the wifi password is hunter2", "my api key is sk-ant-api03-" + "k" * 30,
                                  "card 4111 1111 1111 1111", "door code: 4512", "my pin is 9931",
                                  "token ghp_" + "a" * 36])
def test_secret_values_are_recognised_and_redacted(text):
    assert privacy.has_secret_value(text)
    red = privacy.redact(text)
    assert "[secret]" in red and not privacy.has_secret_value(red)


@pytest.mark.parametrize("text", ["the wifi password is on the fridge", "my password manager is 1Password",
                                  "call dana at 3", "the meeting moved to thursday", "pin the tab"])
def test_talking_about_secrets_is_not_a_secret(text):
    assert not privacy.has_secret_value(text) and privacy.redact(text) == text


def test_memory_never_learns_a_secret(svc):
    mem = svc.skills["memory"].memory
    for bad in ("J's wifi password is hunter2", "J's API key is sk-ant-api03-" + "z" * 30,
                "J's card number is 4111 1111 1111 1111", "J's door code is 4512", "J keeps his passwords in 1Password"):
        assert mem.add(bad) is None, bad
    assert mem.add("J plays Valorant on Fridays") is not None


def test_a_note_with_a_password_in_it_is_refused_and_the_log_is_blanked(svc):
    a = Assistant(svc)
    out = a.handle("remember that the wifi password is hunter2")
    assert "don't keep" in out["reply"]
    assert all("hunter2" not in n["text"] for n in svc.storage.list_notes())
    assert all("hunter2" not in r["text"] for r in svc.storage.recent_log(20))  # the conversation log
    assert all("hunter2" not in json.dumps(e) for e in a.tools.audit.recent)  # the action log
    a.handle("remember that the wifi password is on the fridge")  # about a password, without it: fine
    assert any("on the fridge" in n["text"] for n in svc.storage.list_notes())


def test_people_notes_refuse_secrets(svc):
    svc.storage.execute("INSERT INTO people_contacts(email, name, account, count) VALUES ('dana@x.com', 'Dana', 'work', 1)")
    out = svc.skills["people"].remember_about("dana", "Dana's garage code is 7731")
    assert out["ok"] is False
    assert svc.storage.query("SELECT * FROM people_notes") == []


# ---- .env writes ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["abc\nFOO=bar", "abc\r\nFOO=bar", "abc\rFOO=bar", "abc\x00", "abc FOO=bar",
                                   "abc\x0bFOO=bar", "abc\x85FOO=bar"])
def test_a_value_with_a_line_break_never_adds_a_line(value):
    with pytest.raises(ValueError):
        set_env_value("A=1\n", "HA_TOKEN", value)


def test_set_env_value_still_trims_and_replaces():
    assert set_env_value("A=1\nK=old\n", "K", "  new  ") == "A=1\nK=new\n"
    with pytest.raises(ValueError):
        set_env_value("", "BAD NAME", "x")


def test_pasting_a_poisoned_token_on_the_hud_changes_nothing(svc, cfg, monkeypatch):
    monkeypatch.setattr(svc.skills["home"], "http", httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"message": "API running."}))))
    env = cfg.root / ".env"
    env.write_text("HA_TOKEN=old\n")
    poisoned = "x" * 30 + "\nVESPER_INJECTED=http://evil.example"
    out = svc.skills.action("home", "ha", {"url": "http://homeassistant.local:8123", "token": poisoned})
    assert out["ok"] is False and "line break" in out["error"]
    assert env.read_text() == "HA_TOKEN=old\n" and "VESPER_INJECTED" not in os.environ
    with pytest.raises(ValueError):
        _kit.save_secret(svc, "HA_TOKEN", poisoned)


def test_a_poisoned_google_secret_is_refused(cfg, svc):
    rt = Runtime(cfg, services=svc)
    out = rt.set_google_client("123-abc.apps.googleusercontent.com", "GOCSPX-abcdefghij\nVESPER_INJECTED=1")
    assert out["ok"] is False and not (cfg.root / ".env").exists()
    assert "VESPER_INJECTED" not in os.environ
