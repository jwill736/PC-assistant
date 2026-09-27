import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from assistant import discovery, doctor
from assistant.runtime import Runtime
from assistant.server import create_app


def by_name(result):
    return {c["name"]: c for c in result["checks"]}


def test_offline_doctor_reports_with_fixes(cfg):
    result = doctor.run_doctor(cfg, network=False, test_mic=False, load_model=False)
    checks = by_name(result)
    assert checks["Python"]["status"] == "pass"
    assert checks["North-star goal"]["status"] == "warn" and "north_star" in checks["North-star goal"]["fix"]
    assert checks["OBS"]["status"] == "skip"  # disabled in the test config
    assert checks["Voice"]["status"] == "skip"
    text = doctor.format_report(None, result)
    assert "[PASS] Python" in text and "-> Set goals.north_star" in text


def test_claude_check_states(cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert doctor.check_claude(cfg).status == "warn"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    request = httpx.Request("GET", "https://api.anthropic.com/v1/models/claude-opus-5")

    def fake_client(error):
        class Models:
            def retrieve(self, model):
                if error:
                    raise error
                return object()

        class Client:
            def __init__(self, **kw):
                self.models = Models()

        return Client

    monkeypatch.setattr(anthropic, "Anthropic", fake_client(None))
    assert doctor.check_claude(cfg).status == "pass"
    monkeypatch.setattr(anthropic, "Anthropic", fake_client(
        anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)))
    assert doctor.check_claude(cfg).status == "fail"
    monkeypatch.setattr(anthropic, "Anthropic", fake_client(
        anthropic.NotFoundError("no model", response=httpx.Response(404, request=request), body=None)))
    check = doctor.check_claude(cfg)
    assert check.status == "warn" and "claude.model" in check.fix


def test_calendar_check_flags_broken_feed(cfg, tmp_path):
    cfg["calendars"] = [{"name": "Gone", "url": str(tmp_path / "missing.ics")}]
    [check] = doctor.check_calendars(cfg)
    assert check.status == "fail"
    assert "Secret iCal" in check.fix


@pytest.fixture
def client(cfg, svc, tmp_path, monkeypatch):
    cfg.path = tmp_path / "config.yaml"
    rt = Runtime(cfg, services=svc)
    canned = {"scanned_at": 1.0, "duration_s": 0.1, "platform": "test",
              "findings": [{"area": "streaming", "name": "OBS WebSocket", "status": "action", "detail": "off", "fix": "Enable it"}],
              "summary": {"action": 1}, "obs": {}, "suggested": {"sites": {"stripe": "https://dashboard.stripe.com"}}}
    monkeypatch.setattr(discovery, "discover", lambda cfg: canned)
    app = create_app(rt, start_background=False)
    with TestClient(app) as c:
        c.headers["X-Assistant-Token"] = app.state.token
        yield c, rt


def test_rescan_endpoint_writes_and_hot_applies(client):
    c, rt = client
    report = c.post("/api/discovery/run").json()
    assert report["summary"] == {"action": 1} and "suggested" not in report
    assert rt.svc.browser.resolve("stripe") == "https://dashboard.stripe.com"  # usable without a restart
    assert c.get("/api/discovery").json()["findings"][0]["name"] == "OBS WebSocket"
    assert c.get("/api/state").json()["discovery"]["summary"] == {"action": 1}


def test_doctor_endpoint(client):
    c, _rt = client
    result = c.post("/api/doctor").json()
    names = {x["name"] for x in result["checks"]}
    assert {"Python", "Claude API", "OBS", "News feeds", "Chrome"} <= names
    assert "HUD port" not in names  # the server itself holds the port
