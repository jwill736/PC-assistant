import pytest
from fastapi.testclient import TestClient

from assistant.runtime import Runtime
from assistant.server import create_app


@pytest.fixture
def client(cfg, svc):
    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=False)
    with TestClient(app) as c:
        c.headers["X-Assistant-Token"] = app.state.token
        yield c, app


def test_index_injects_token_and_name(client):
    c, app = client
    html = c.get("/").text
    assert app.state.token in html and "Friday HUD" in html


def test_api_requires_token(client):
    c, _ = client
    assert c.get("/api/state", headers={"X-Assistant-Token": "nope"}).status_code == 401


def test_rejects_foreign_host_header(client):
    c, _ = client
    assert c.get("/api/state", headers={"Host": "evil.example:8765"}).status_code == 403


def test_state_and_command_roundtrip(client):
    c, _ = client
    st = c.get("/api/state").json()
    assert st["assistant"]["name"] == "Friday" and st["assistant"]["claude"] is False
    r = c.post("/api/command", json={"text": "add task Record the intro"}).json()
    assert r["reply"] == "Added: Record the intro."
    tasks = c.get("/api/tasks").json()
    assert tasks[0]["title"] == "Record the intro"
    done = c.patch(f"/api/tasks/{tasks[0]['id']}", json={"status": "done"}).json()
    assert done["status"] == "done" and done["done_at"]


def test_websocket_needs_token(client):
    c, app = client
    with pytest.raises(Exception):
        with c.websocket_connect("/ws?token=wrong") as ws:
            ws.receive_text()
    with c.websocket_connect(f"/ws?token={app.state.token}") as ws:
        c.post("/api/command", json={"text": "what time is it"})
        types = {__import__("json").loads(ws.receive_text())["type"] for _ in range(3)}
        assert "user_said" in types


def test_unknown_tool_404(client):
    c, _ = client
    assert c.post("/api/tool/format_c_drive", json={}).status_code == 404
