"""Vesper 2.1: the diagnostic report, skill self-tests, the crash guard, Setup → Skills, and startup speed."""

import json
import re
import socket
import sys
import time
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from assistant import diagnostics as diag
from assistant import doctor
from assistant.brain.router import Intent, RouterContext, route
from assistant.skills import MODULES
from assistant.brain.tools import Tool, obj
from assistant.skills import CRASHED, LazyClient, Skill, Skills, check, gmail_checks


# ---- a few test skills -----------------------------------------------------------------------------------------

class Flaky(Skill):
    name, title = "flaky", "Flaky"
    poll_s = 60

    def __init__(self, svc):
        super().__init__(svc)
        self.fail_tick = self.fail_route = self.fail_say = False
        self.stopped = 0
        self.started = 0

    def tools(self):
        return [Tool("flaky_ping", "Ping.", obj(), lambda: {"ok": True}, tier=0)]

    def tick(self):
        if self.fail_tick:
            raise RuntimeError("the API moved")
        self.publish({"ok": True})

    def route(self, t):
        if self.fail_route:
            raise ValueError("bad regex")
        return Intent("tool", "flaky_ping", {}) if t == "flaky ping" else None

    def say(self, tool, args, result):
        if self.fail_say:
            raise KeyError("x")
        return "Pong."

    def actions(self):
        return {"boom": lambda b: 1 / 0}

    def selftest(self):
        return [check("Service", "pass", "answers")]

    def start(self, runtime):
        self.started += 1

    def stop(self):
        self.stopped += 1


class Slow(Skill):
    name, title = "slow", "Slow"

    def selftest(self):
        time.sleep(3)
        return [check("Service", "pass", "late")]


class Crashy(Skill):
    name, title = "crashy", "Crashy"

    def selftest(self):
        raise OSError("socket closed")


@pytest.fixture
def fake_mod(monkeypatch):
    mod = types.ModuleType("assistant.skills.fake21")
    for cls in (Flaky, Slow, Crashy):
        cls.__module__ = mod.__name__
        setattr(mod, cls.__name__, cls)
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    return mod


@pytest.fixture
def sk(svc, fake_mod):
    s = Skills(svc, ["fake21", "diagnostics"])
    svc.skills = s
    return s


def notices(svc):
    seen = []
    svc.bus.on(lambda e: seen.append(e["data"]) if e["type"] == "notice" else None)
    return seen


# ---- the crash guard ------------------------------------------------------------------------------------------

def test_a_failing_tick_switches_the_skill_off_after_five_in_a_row(sk, svc):
    seen = notices(svc)
    flaky = sk["flaky"]
    flaky.fail_tick = True
    for _ in range(4):
        with pytest.raises(RuntimeError):
            sk.tick(flaky)
    assert sk.active("flaky")
    with pytest.raises(RuntimeError):
        sk.tick(flaky)
    assert not sk.active("flaky") and sk.off["flaky"] == CRASHED and flaky.stopped == 1
    assert [n["text"] for n in seen] == [
        "Flaky was switched off after 5 errors: RuntimeError: the API moved. It's back after a restart or "
        "Setup → Skills → Turn on."]
    assert seen[0]["level"] == "error" and seen[0]["key"] == "skill-off:flaky"
    sk.tick(flaky)  # switched off: the tick does nothing (and raises nothing)
    assert "flaky_ping" not in [t.name for t in sk.tools()] and route("flaky ping", RouterContext(skill_route=sk.route)) is None
    row = next(r for r in sk.overview() if r["name"] == "flaky")
    assert row["state"] == "crashed" and row["last_error"] == "RuntimeError: the API moved" and row["where"] == "tick"
    assert row["errors"] == 5


def test_a_success_resets_the_count(sk):
    flaky = sk["flaky"]
    for i in range(12):
        flaky.fail_tick = i % 4 != 3
        try:
            sk.tick(flaky)
        except RuntimeError:
            pass
    assert sk.active("flaky")


def test_route_and_say_that_keep_raising_switch_off_too(sk):
    flaky = sk["flaky"]
    flaky.fail_route = True
    for _ in range(5):
        assert sk.route("anything") is None  # the other skills still route
    assert sk.off.get("flaky") == CRASHED
    sk.switch_on("flaky", persist=False)
    flaky.fail_route, flaky.fail_say = False, True
    for _ in range(5):
        assert sk.say("flaky_ping", {}, {"ok": True}) is None
    assert sk.off.get("flaky") == CRASHED


def test_a_failing_action_is_an_answer_not_a_500(sk):
    r = sk.action("flaky", "boom", {})
    assert r["ok"] is False and "ZeroDivisionError" in r["error"]
    assert sk.health["flaky"].where == "action"


def test_turn_back_on_restarts_it_and_refreshes_the_tools(sk):
    changed, started = [], []
    sk.on_change = lambda: changed.append(1)
    sk.starter = lambda s: started.append(s.name)
    flaky = sk["flaky"]
    flaky.fail_tick = True
    for _ in range(5):
        with pytest.raises(RuntimeError):
            sk.tick(flaky)
    r = sk.switch_on("flaky", persist=False)
    assert r == {"ok": True, "name": "flaky", "state": "on", "reload": False}
    assert sk.active("flaky") and started == ["flaky"] and len(changed) == 2
    assert sk.health["flaky"].in_row == {}


def test_setup_toggle_is_saved_and_survives_a_restart(sk, svc, cfg):
    assert sk.set_enabled("flaky", False)["ok"]
    saved = yaml.safe_load((cfg.data_dir / "settings.yaml").read_text())
    assert saved["skills"]["flaky"]["enabled"] is False
    assert Skills(svc, ["fake21"]).off == {"flaky": "off"}  # the next start leaves it out
    assert sk.set_enabled("flaky", True)["ok"]
    assert yaml.safe_load((cfg.data_dir / "settings.yaml").read_text())["skills"]["flaky"]["enabled"] is True


def test_a_skill_off_in_config_can_be_turned_on_live(svc, cfg, fake_mod):
    cfg["skills"]["flaky"] = {"enabled": False}
    s = Skills(svc, ["fake21"])
    assert s.get("flaky") is None and s.off == {"flaky": "off"}
    assert [r["state"] for r in s.overview() if r["name"] == "flaky"] == ["off"]
    started = []
    s.starter = lambda skill: started.append(skill.name)
    r = s.switch_on("flaky")
    assert r["ok"] and r["reload"] is True, r
    assert s.active("flaky") and started == ["flaky"]


def test_diagnostics_itself_cant_be_switched_off(sk):
    sk["diagnostics"].essential = True
    sk._failed(sk["diagnostics"], "tick", RuntimeError("x"))
    assert sk.switch_off("diagnostics")["ok"] is False
    for _ in range(9):
        sk._failed(sk["diagnostics"], "tick", RuntimeError("x"))
    assert sk.active("diagnostics")
    assert sk.set_enabled("ghost", False)["ok"] is False


def test_a_skill_that_wont_import_is_listed_as_failed(svc, monkeypatch):
    def boom(name, *a, **k):
        if name.endswith("skills.weather"):
            raise SyntaxError("bad line")
        return real(name, *a, **k)
    import importlib
    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", boom)
    s = Skills(svc, ["weather", "habits"])
    assert [x.name for x in s] == ["habits"] and "SyntaxError" in s.errors["weather"]
    row = next(r for r in s.overview() if r["name"] == "weather")
    assert row["state"] == "failed" and row["where"] == "load"


# ---- self-tests and the health check ----------------------------------------------------------------------

def test_selftests_run_side_by_side_with_a_time_limit(sk):
    t0 = time.perf_counter()
    out = sk.selftests(timeout=0.5)
    assert time.perf_counter() - t0 < 2  # Slow's 3 s didn't hold the rest up
    assert out["flaky"] == [{"name": "Service", "status": "pass", "detail": "answers", "fix": ""}]
    assert out["slow"][0]["status"] == "warn" and "didn't answer within" in out["slow"][0]["detail"]
    assert out["crashy"][0]["status"] == "fail" and "OSError: socket closed" in out["crashy"][0]["detail"]
    assert out["diagnostics"] == []


def test_doctor_groups_skill_checks_with_a_docs_pointer(sk, svc, cfg):
    sk["slow"].selftest = lambda: [check("Bot", "fail", "token rejected", "Paste a new token")]
    result = doctor.run_doctor(cfg, svc, test_mic=False, load_model=False, check_ports=False, network=False, skills=True)
    rows = {c["name"]: c for c in result["checks"]}
    assert rows["Skills · Flaky · Service"]["status"] == "pass"
    bot = rows["Skills · Slow · Bot"]
    assert bot["status"] == "fail" and bot["fix"] == "Paste a new token. See docs/VESPER-2.md Part B."
    assert rows["Skills · Crashy"]["status"] == "fail"
    assert doctor.skill_fix("phone", "") == "See docs/VESPER-2.md Part E."
    assert doctor.skill_fix("mcp", "Connect it.") == "Connect it. See docs/VESPER-2.md Part J."


def test_doctor_runs_checks_in_parallel_and_a_stuck_one_is_a_warning(cfg, monkeypatch):
    monkeypatch.setattr(doctor, "check_browser", lambda: time.sleep(5))
    monkeypatch.setattr(doctor, "check_claude_cli", lambda cfg: (_ for _ in ()).throw(RuntimeError("bad PATH")))
    t0 = time.perf_counter()
    result = doctor.run_doctor(cfg, network=False, test_mic=False, load_model=False, check_ports=False, timeout=1)
    assert time.perf_counter() - t0 < 3
    rows = {c["name"]: c for c in result["checks"]}
    assert rows["Chrome"]["status"] == "warn" and "no answer within 1s" in rows["Chrome"]["detail"]
    assert rows["Claude Code CLI"] == {"name": "Claude Code CLI", "status": "fail", "detail": "RuntimeError: bad PATH", "fix": ""}
    assert [c["name"] for c in result["checks"]][:2] == ["Python", "config.yaml"]  # order kept


def test_crashed_and_failed_skills_show_in_the_health_check(sk):
    sk.errors["ghost"] = "ImportError: no module named winrt"
    sk.modules.append("ghost")
    sk.titles["ghost"] = "Ghost"
    sk.switch_off("slow", reason=CRASHED)
    rows = {c.name: c for c in doctor.check_skills(sk, timeout=1)}
    assert rows["Skills · Ghost"].status == "fail" and "didn't load" in rows["Skills · Ghost"].detail
    assert rows["Skills · Slow"].status == "fail" and "Setup → Skills → Turn on" in rows["Skills · Slow"].fix


# ---- each skill's own self-test (network faked) -----------------------------------------------------------

def mock(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_gmail_checks_per_account(svc):
    svc.google = SimpleNamespace(configured=True, accounts=lambda: [
        {"email": "jon@talent-sdk.com", "missing": [], "error": None},
        {"email": "jwill736@gmail.com", "missing": ["Gmail"], "error": None},
        {"email": "old@x.com", "missing": [], "error": "token revoked"}])
    rows = gmail_checks(svc)
    assert [(r["name"], r["status"]) for r in rows] == [
        ("Gmail · jon@talent-sdk.com", "pass"), ("Gmail · jwill736@gmail.com", "warn"), ("Gmail · old@x.com", "fail")]
    assert gmail_checks(svc, per_account=False) == [check("Gmail", "warn", "1 of 3 accounts can read mail",
                                                          "Setup → Google accounts → Reconnect, and tick the Gmail box.")]
    svc.google = SimpleNamespace(configured=False)
    assert gmail_checks(svc)[0]["status"] == "skip"
    assert svc.skills["inbox_zero"].selftest()[0]["status"] == "skip"
    for name in ("bills", "travel", "people"):
        assert svc.skills[name].selftest()[0]["name"] == "Gmail"


def test_phone_selftest(svc, monkeypatch):
    from assistant.skills.phone import Phone

    calls = []

    def handler(req):
        calls.append(req.url.path)
        ok = "GOOD" in req.url.path
        return httpx.Response(200 if ok else 401, json={"ok": ok, "result": {"username": "VesperBot"}} if ok else
                              {"ok": False, "description": "Unauthorized"})
    p = Phone(svc, http=mock(handler))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert p.selftest()[0]["status"] == "skip" and not calls
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:GOOD")
    r = p.selftest()[0]
    assert r["status"] == "warn" and "@VesperBot answers; no phone paired" in r["detail"]
    p.state["chat_id"] = 42
    assert p.selftest()[0]["status"] == "pass"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:BAD")
    r = p.selftest()[0]
    assert r["status"] == "fail" and "Unauthorized" in r["detail"] and "123456:BAD" not in r["detail"]


def test_spotify_selftest_refreshes_the_token(svc):
    from assistant.skills.spotify import Spotify

    answer = {"code": 200}

    def handler(req):
        if answer["code"] == 200:
            return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        return httpx.Response(400, json={"error": "invalid_grant"})
    s = Spotify(svc, http=mock(handler))
    s.tokens = {}
    assert s.selftest()[0]["status"] == "skip"
    s.tokens = {"refresh_token": "r1", "access_token": "old", "expires_at": time.time() + 999}
    assert s.selftest() == [check("Spotify", "pass", "signed in (token refreshed)")] and s.tokens["access_token"] == "new"
    answer["code"] = 400
    r = s.selftest()[0]
    assert r["status"] == "fail" and "Connect again" in r["detail"]


def test_home_selftest(svc, monkeypatch):
    from assistant.skills.home import Home

    def handler(req):
        if req.url.port == 8123:
            return httpx.Response(200 if req.headers["authorization"] == "Bearer good" else 401, json={"message": "API running."})
        if req.url.host == "192.168.1.50":
            return httpx.Response(200, json={"numberOfLights": 1, "lights": [{"on": 1, "brightness": 40, "temperature": 200}]})
        raise httpx.ConnectError("no route")
    h = Home(svc, http=mock(handler))
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    assert h.selftest()[0]["status"] == "skip"
    monkeypatch.setenv("HA_URL", "http://homeassistant.local:8123")
    monkeypatch.setenv("HA_TOKEN", "good")
    h.cfg["key_lights"] = [{"ip": "192.168.1.50", "name": "Desk"}, {"ip": "192.168.1.51", "name": "Fill"}]
    rows = {r["name"]: r["status"] for r in h.selftest()}
    assert rows == {"Home Assistant": "pass", "Key Light · Desk": "pass", "Key Light · Fill": "fail"}
    monkeypatch.setenv("HA_TOKEN", "bad")
    assert h.selftest()[0]["status"] == "fail"


def test_youtube_and_discord_selftests(svc, monkeypatch):
    from assistant.skills.discord import Discord
    from assistant.skills.youtube import YouTube

    def yt(req):
        if req.url.params["key"] == "AIza" + "x" * 35:
            return httpx.Response(200, json={"items": [{"id": "UC1"}]})
        return httpx.Response(400, json={"error": {"errors": [{"reason": "keyInvalid"}], "message": "API key not valid"}})
    y = YouTube(svc, http=mock(yt))
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert y.selftest()[0]["status"] == "skip"
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIza" + "x" * 35)
    assert y.selftest()[0]["status"] == "pass"
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIza" + "y" * 35)
    assert y.selftest()[0]["status"] == "fail"

    def dc(req):
        assert req.method == "GET"  # the self-test never posts
        return httpx.Response(200, json={"name": "Vesper", "channel_id": "1"}) if req.url.path.endswith("/good") else \
            httpx.Response(404, json={})
    d = Discord(svc, http=mock(dc))
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert d.selftest()[0]["status"] == "skip"
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/good")
    assert d.selftest() == [check("Discord", "pass", "webhook “Vesper” works")]
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/gone")
    assert d.selftest()[0]["status"] == "fail"


def test_mcp_selftest_reports_each_server(svc, monkeypatch):
    from assistant.skills import mcp

    monkeypatch.setattr(mcp, "sdk_problem", lambda: None)
    m = mcp.MCP(svc)
    assert m.selftest() == []
    for name, state, on in (("files", "connected", True), ("github", "error", True), ("slow", "connecting", True),
                            ("old", "off", False)):
        srv = mcp.Server({"name": name, "enabled": on})
        srv.state, srv.error, srv.tools = state, "401 Unauthorized" if state == "error" else "", [{"name": "read"}]
        m.servers[name] = srv
    assert [(r["name"], r["status"]) for r in m.selftest()] == [
        ("files", "pass"), ("github", "fail"), ("slow", "warn"), ("old", "skip")]
    monkeypatch.setattr(mcp, "sdk_problem", lambda: "pip install mcp")
    assert m.selftest()[0]["status"] == "fail"


def test_memory_selftest_checks_the_embedding_model(svc):
    from assistant.memory import Embedder
    from assistant.skills.memory import MemorySkill

    def handler(req):
        model = json.loads(req.content)["model"]
        if model == "embeddinggemma":
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})
        return httpx.Response(404, json={"error": "model not found"})
    m = MemorySkill(svc, embedder=Embedder("http://ollama:11434", "embeddinggemma", http=mock(handler)))
    m.memory.embedder.available = False  # it was down at startup
    assert m.selftest()[0]["status"] == "pass" and m.memory.embedder.available is None  # used again
    m.memory.embedder.model = "nomic-embed-text"
    r = m.selftest()[0]
    assert r["status"] == "warn" and "ollama pull nomic-embed-text" in r["fix"]
    assert svc.skills["memory"].selftest()[0]["status"] == "skip"  # embed_model "" in the test config


def test_screen_selftest_finds_a_vision_model_or_ocr(svc, monkeypatch):
    from assistant.skills import screen as sc

    s = sc.Screen(svc, window=lambda: None, capture_fn=lambda *a: None, ocr=None)
    s.brain = SimpleNamespace(refresh=lambda: SimpleNamespace(model="qwen3:8b", kind="ollama"),
                              found={"models": [{"name": "qwen3:8b", "caps": ["tools"]},
                                                {"name": "qwen3-vl:8b", "caps": ["vision", "tools"]}]})
    assert s.selftest() == [check("Vision", "pass", "sees with qwen3-vl:8b")]
    s.brain.found["models"].pop()
    monkeypatch.setattr(sc, "IS_WINDOWS", False)
    assert s.selftest()[0]["status"] == "fail"
    monkeypatch.setattr(sc, "IS_WINDOWS", True)
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "winrt" else None)
    assert s.selftest()[0]["status"] == "warn" and "Windows OCR" in s.selftest()[0]["detail"]


def test_clipboard_janitor_and_weather_selftests(svc):
    from assistant.skills.weather import Weather

    clip = svc.skills["clipboard"]
    assert clip.selftest()[0]["status"] == "fail"  # no clipboard (faked off in tests)
    clip.board = SimpleNamespace(read=lambda: "secret thing copied")
    assert clip.selftest() == [check("Clipboard", "pass", "readable")]  # never the text
    jan = svc.skills["janitor"]
    assert jan.selftest()[0]["status"] == "warn"
    jan.runner = lambda args: "NVIDIA GeForce RTX 5090, 581.15\n"
    assert jan.selftest() == [check("nvidia-smi", "pass", "NVIDIA GeForce RTX 5090, 581.15")]
    w = Weather(svc, http=mock(lambda req: httpx.Response(200, json={"current": {"temperature_2m": 61}})))
    assert w.selftest()[0]["status"] == "warn"  # no place: no IP lookup either
    w.cfg["place"] = {"name": "Decatur, GA", "lat": 33.7, "lon": -84.3}
    assert w.selftest() == [check("Forecast", "pass", "works for Decatur, GA")]


# ---- secrets never leave ------------------------------------------------------------------------------------

def test_redactor_hides_secrets_tokens_and_emails(tmp_path):
    r = diag.Redactor(["hunter2-very-secret"], home="C:\\Users\\Jon")
    text = ("token=abc123 key hunter2-very-secret; bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ "
            "https://discord.com/api/webhooks/123/abcDEF-xyz AIzaSyA1234567890abcdefghijklmnopqrstu "
            "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U "
            "sk-ant-api03-abcdefghijklmnop ya29.a0AfB_byC jwill736@gmail.com jon@talent-sdk.com "
            "C:\\Users\\Jon\\Vesper\\data https://x.y/?code=4/0Ab&state=s3cr3t")
    out = r(text)
    for secret in ("abc123", "hunter2", "AAHdqTcv", "abcDEF-xyz", "AIzaSyA", "eyJhbGci", "sk-ant-api03", "ya29.a0",
                   "jwill736", "jon@talent", "C:\\Users\\Jon", "4/0Ab", "s3cr3t"):
        assert secret not in out, secret
    assert "jw***@gmail.com" in out and "jo***@talent-sdk.com" in out and "~\\Vesper\\data" in out
    assert r("Calendar · Work: loads, 3 events") == "Calendar · Work: loads, 3 events"  # ordinary text untouched


def test_redactor_reads_env_and_saved_sign_ins(cfg, monkeypatch):
    (cfg.root / ".env").write_text("TELEGRAM_BOT_TOKEN=999:zzz-short\nSOME_FLAG=enabled\n", encoding="utf-8")
    (cfg.data_dir / "google_tokens.json").write_text(json.dumps(
        {"jwill736@gmail.com": {"access_token": "acc-token-1", "refresh_token": "ref-token-2", "scope": "email"}}))
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-id-777")
    r = diag.Redactor.for_cfg(cfg)
    out = r("999:zzz-short acc-token-1 ref-token-2 client-id-777 enabled")
    assert out == "[hidden] [hidden] [hidden] [hidden] enabled"  # an ordinary word in .env stays


# ---- the report ---------------------------------------------------------------------------------------------

@pytest.fixture
def live(cfg, svc, monkeypatch):
    from assistant.runtime import Runtime

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ")
    (cfg.root / ".env").write_text("TELEGRAM_BOT_TOKEN=123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ\n")
    rt = Runtime(cfg, services=svc)
    phone = svc.skills["phone"]
    phone.http = mock(lambda req: httpx.Response(200, json={"ok": True, "result": {"username": "VesperBot"}}))
    logs = cfg.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    lines = [f"2026-10-21 09:{i // 60:02d}:{i % 60:02d},000 INFO    MainThread x: fine {i}" for i in range(100)]
    lines += [f"2026-10-21 10:00:{i:02d},000 WARNING MainThread y: warn {i} from jwill736@gmail.com" for i in range(55)]
    lines += ["2026-10-21 10:01:00,000 ERROR   phone z: Telegram said 401 for bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ",
              "Traceback (most recent call last):", "  File x"]
    (logs / "assistant.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    svc.bus.publish("inbox_zero", {"owed": [{"subject": "Contract for Dana – private"}]}, sticky=True)
    return rt


def test_report_has_every_section_and_no_secrets(live, cfg, svc):
    rt = live
    rt.timings.update({"hud_ready_s": 1.8, "skill_load_ms": {"memory": 12.0}})
    now = datetime(2026, 10, 21, 10, 5, tzinfo=timezone.utc)
    rep = diag.run(cfg, svc, rt, timeout=8, now=now, gpu_runner=lambda args: "NVIDIA GeForce RTX 5090, 581.15, 32607, 2048, 41, 3\n")
    files = rep["files"]
    assert files["md"].endswith("diagnostics-2026-10-21.md") and files["json"].endswith("diagnostics-2026-10-21.json")
    md = open(files["md"], encoding="utf-8").read()
    js = json.loads(open(files["json"], encoding="utf-8").read())
    assert js["summary"] == rep["summary"] and js["gpu"]["cards"][0]["name"] == "NVIDIA GeForce RTX 5090"
    for heading in ("## Needs attention", "## Vesper", "## This PC", "## Graphics card", "## Models", "## Connections",
                    "## Skills", "## All checks", "## Startup", "## Watchdog", "## Recent warnings and errors"):
        assert heading in md, heading
    assert "RTX 5090" in md and "32" in md and "hud_ready_s | 1.8" in md
    skills = {s["name"]: s for s in js["skills"]}
    assert len(skills) == len(MODULES) and skills["phone"]["status"].startswith("Telegram: bot @VesperBot answers")
    assert skills["weather"]["state"] == "on" and skills["diagnostics"]["state"] == "on"
    conn = {c["name"]: c["status"] for c in js["connections"]}
    assert conn["Telegram"] == "warn" and conn["Spotify"] == "skip" and "Google" in conn
    assert len(js["log"]) == 50 and js["log"][-1].startswith("2026-10-21 10:01:00,000 ERROR")
    assert not any("INFO" in ln or "Traceback" in ln for ln in js["log"])
    for text in (md, json.dumps(js)):
        assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ" not in text and "jwill736@" not in text
        assert "Contract for Dana" not in text  # no message text, ever
        assert app_token(cfg) not in text
    assert "jw***@gmail.com" in md
    assert any(c["name"].startswith("Skills · Phone") for c in js["checks"])


def app_token(cfg):
    from assistant.server import load_token

    return load_token(cfg.data_dir)


def test_report_survives_a_stuck_part(cfg, svc, monkeypatch):
    monkeypatch.setattr(diag, "gpu_info", lambda runner=None: time.sleep(10))
    t0 = time.perf_counter()
    rep = diag.run(cfg, svc, None, timeout=1, write=False)
    assert time.perf_counter() - t0 < 6
    assert rep["gpu"]["error"] == "no answer in time" and "files" not in rep
    assert rep["watchdog"] is None and rep["mode"].startswith("separate")


def test_brief_and_latest(cfg, svc):
    rep = diag.run(cfg, svc, None, timeout=5)
    b = diag.latest(cfg.data_dir)
    assert b["summary"] == rep["summary"] and b["files"]["md"] == rep["files"]["md"]
    assert all(c["status"] in ("fail", "warn") for c in b["attention"])
    order = [c["status"] for c in b["attention"]]
    assert order == sorted(order, key=lambda s: s != "fail")


def test_cli_writes_the_report_and_prefers_the_running_copy(cfg, monkeypatch):
    printed = []
    assert diag.cli(cfg, ask_running=False, out=printed.append) == 0
    text = "\n".join(printed)
    assert "Report: " in text and "diagnostics-" in text and re.search(r"\d+ passed, \d+ warnings, \d+ failed", text)
    asked = []

    def fake_post(url, headers=None, json=None, timeout=None):
        asked.append((url, headers))
        return httpx.Response(200, json={"ok": True, "summary": {"pass": 9, "verdict": "pass"}, "attention": [],
                                         "files": {"md": "C:/x/diagnostics-2026-10-21.md", "json": "j"}})
    monkeypatch.setattr(httpx, "post", fake_post)
    (cfg.data_dir / "api_token").write_text("tok")
    printed.clear()
    assert diag.cli(cfg, "127.0.0.1", 8765, out=printed.append) == 0
    assert asked[0][0] == "http://127.0.0.1:8765/api/skill/diagnostics/run" and asked[0][1] == {"x-assistant-token": "tok"}
    assert "C:/x/diagnostics-2026-10-21.md" in "\n".join(printed)


def test_main_has_a_diagnose_switch(cfg, monkeypatch, tmp_path):
    import assistant.__main__ as main
    import assistant.diagnostics as d

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"data_dir": str(cfg.data_dir)}))
    seen = []
    monkeypatch.setattr(d, "cli", lambda c, host, port: seen.append((host, port)) or 0)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: tmp_path / "x.log")  # the test run's logging stays
    monkeypatch.setattr(sys, "argv", ["assistant", "--config", str(cfg_path), "--diagnose"])
    with pytest.raises(SystemExit) as e:
        main.main()
    assert e.value.code == 0 and seen and seen[0][0] == "127.0.0.1"


# ---- the diagnostics skill: voice, HUD -------------------------------------------------------------------

@pytest.mark.parametrize("said", ["run diagnostics", "Friday, run diagnostics", "run a diagnostic report", "run a self test",
                                  "diagnose yourself", "do a full self-check"])
def test_diagnostics_routes(svc, said):
    intent = route(said, RouterContext(skill_route=Skills(svc, ["diagnostics"]).route))
    assert (intent.kind, intent.tool) == ("tool", "run_diagnostics"), said


def test_spoken_reply(svc):
    d = svc.skills["diagnostics"]
    said = d.say("run_diagnostics", {}, {"ok": True, "summary": {"pass": 40, "warn": 2, "fail": 1},
                                          "files": {"md": "C:/V/data/diagnostics-2026-10-21.md"}})
    assert said == ("Diagnostics done: 40 passed, 2 warnings, 1 failed. The report is diagnostics-2026-10-21.md in the "
                    "Vesper data folder; Setup shows the summary.")
    assert d.say("run_diagnostics", {}, {"ok": True, "summary": {"pass": 5}, "files": {}}).startswith(
        "Diagnostics done: all 5 checks passed.")


def test_hud_runs_opens_and_toggles(cfg, svc):
    from assistant.runtime import Runtime
    from assistant.server import create_app

    opened = []
    d = svc.skills["diagnostics"]
    d.opener = lambda path, reveal=False: opened.append((path.name, reveal))
    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=False)
    with TestClient(app) as c:
        c.headers["X-Assistant-Token"] = app.state.token
        assert "/static/skills/diagnostics.js" in c.get("/").text
        assert c.post("/api/skill/diagnostics/open", json={}).json()["ok"] is False  # nothing yet
        r = c.post("/api/skill/diagnostics/run", json={}).json()
        assert r["ok"] and set(r["summary"]) >= {"pass", "warn", "fail", "verdict"} and r["files"]["md"].endswith(".md")
        assert c.post("/api/skill/diagnostics/open", json={"what": "folder"}).json()["ok"]
        assert opened == [(r["files"]["md"].replace("\\", "/").rsplit("/", 1)[-1], True)]
        doc = svc.bus.latest["doctor"]["data"]
        assert doc["checks"] and doc["summary"]["pass"] == r["summary"]["pass"]
        st = c.get("/api/skill/diagnostics").json()
        assert st["last"]["files"] == r["files"] and any(s["name"] == "weather" for s in st["skills"])
        off = c.post("/api/skill/diagnostics/skill", json={"name": "weather", "on": False}).json()
        assert off == {"ok": True, "name": "weather", "state": "off"}
        state = c.get("/api/state").json()["skills"]
        assert "weather" not in [x["name"] for x in state["loaded"]]
        assert next(x for x in state["health"] if x["name"] == "weather")["state"] == "off"
        assert "get_weather" not in rt.assistant.tools.tools  # the brain can't call it now
        back = c.post("/api/skill/diagnostics/skill", json={"name": "weather", "on": True}).json()
        assert back["ok"] and "get_weather" in rt.assistant.tools.tools
        assert c.post("/api/skill/diagnostics/skill", json={"name": "diagnostics", "on": False}).json()["ok"] is False


def test_voice_command_runs_the_report(cfg, svc):
    from assistant.brain.assistant import Assistant

    svc.skills["diagnostics"].run_report = lambda cfg, svc, rt: {
        "summary": {"pass": 3, "warn": 0, "fail": 0, "verdict": "pass"}, "checks": [],
        "files": {"md": "/d/diagnostics-2026-10-21.md", "json": "/d/x.json"}}
    reply = Assistant(svc).handle("run diagnostics")["reply"]
    assert reply.startswith("Diagnostics done: all 3 checks passed.")


# ---- startup speed ------------------------------------------------------------------------------------------

def test_no_skill_touches_the_network_while_it_is_made(svc, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network during skill construction")
    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    s = Skills(svc)
    assert not s.errors, s.errors
    assert len(list(s)) == len(MODULES)
    weather = s["weather"]
    assert isinstance(weather.http, LazyClient) and weather.http._client is None  # not even a client made yet


def test_lazy_client_makes_one_client_on_first_use():
    lc = LazyClient(timeout=3, transport=httpx.MockTransport(lambda req: httpx.Response(204)))
    assert lc._client is None
    assert lc.get("http://x/").status_code == 204 and isinstance(lc._client, httpx.Client)
    first = lc._client
    lc.get("http://x/")
    assert lc._client is first


def test_startup_budget_build_services_and_create_app(cfg):
    from assistant.bus import EventBus
    from assistant.runtime import Runtime
    from assistant.server import create_app
    from assistant.services import build_services
    from assistant.storage import Storage

    t0 = time.perf_counter()
    svc = build_services(cfg, EventBus(), Storage(":memory:"))
    rt = Runtime(cfg, services=svc)
    create_app(rt, start_background=False)
    took = time.perf_counter() - t0
    assert took < 5, f"startup took {took:.1f}s"
    assert rt.timings["runtime_s"] >= 0 and len(svc.skills.timings) == len(MODULES)


def test_skills_start_in_the_background(cfg, svc, monkeypatch):
    """A slow skill start (the MCP package takes seconds to import) never keeps the HUD from answering."""
    from assistant.runtime import Runtime
    from assistant.server import create_app

    gate = {"started": False}

    def slow_start(runtime):
        time.sleep(1.5)
        gate["started"] = True
    svc.skills["weather"].start = slow_start
    svc.skills["weather"].status = lambda: {"ok": True}
    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=True)
    t0 = time.perf_counter()
    with TestClient(app) as c:
        answered = time.perf_counter() - t0
        assert c.get("/").status_code == 200
        assert answered < 1.45 and not gate["started"]  # the HUD answered before the slow start finished
        deadline = time.time() + 10
        while "skill_panels_s" not in rt.timings and time.time() < deadline:
            time.sleep(0.05)
        assert gate["started"] and rt.timings["skill_start_ms"]["weather"] >= 1400
        assert "hud_ready_s" in rt.timings and "start_s" in rt.timings
        assert "skill · Weather" in rt.supervisor.parts
        assert json.loads((cfg.data_dir / diag.STARTUP_FILE).read_text())["skill_start_ms"]["weather"] >= 1400


def test_the_report_shows_what_the_mic_heard_and_why_it_was_ignored():
    heard = [{"ts": 1_790_000_000, "text": "Vesper, what time is it?", "ignored": "voice not recognised", "score": 0.41,
              "stt_ms": 120},
             {"ts": 1_790_000_060, "text": "why is my drive full", "wake": False, "armed": False, "stt_ms": 90},
             {"ts": 1_790_000_120, "text": "Vesper, what time is it?", "wake": True, "armed": False, "stt_ms": 95}]
    rt = SimpleNamespace(
        listener=SimpleNamespace(recent_heard=heard, wake_words=["vesper"]),
        speaker=SimpleNamespace(status=lambda: {"engine": "supertonic", "first_audio_ms": 310, "underruns": 3},
                                out=SimpleNamespace(latency="high")),
        verifier=SimpleNamespace(status=lambda: {"mode": "strict", "threshold": 0.5, "enrolled_threshold": 0.52,
                                                 "last_score": 0.41, "learned": 1}))
    v = diag.voice_report(rt)
    assert v["speaker"]["underruns"] == 3 and v["profile"]["threshold"] == 0.5 and len(v["heard"]) == 3
    md = diag.to_markdown({"summary": {}, "checks": [], "voice": v})
    section = md.split("## Voice · what the mic heard and why")[1].split("## ")[0]
    assert "glitches (sound card ran dry) 3, buffer high" in section and "threshold 0.5 (calibrated 0.52)" in section
    assert "| voice not recognised | 0.41 |" in section and "| no name |" in section and "| ran |" in section
    assert diag.voice_report(None) is None and diag.voice_report(SimpleNamespace(listener=None)) is None
