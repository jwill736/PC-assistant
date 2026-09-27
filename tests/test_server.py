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


def test_voice_endpoints_with_voice_off(client):
    c, _ = client
    v = c.get("/api/voice").json()
    assert v["enabled"] is False and v["profile"]["enrolled"] is False
    assert v["wake_words"] == ["friday", "hey friday"]
    assert c.post("/api/voice/calibrate").json()["ok"] is False
    assert c.post("/api/voice/calibrate/cancel").json() == {"ok": True}
    assert c.post("/api/voice/speaker_check", json={"mode": "log"}).json()["ok"] is False
    assert c.delete("/api/voice/profile").json() == {"ok": True, "deleted": False}
    assert "voice_profile" in c.get("/api/state").json()


@pytest.fixture
def macro_client(cfg, svc):
    cfg["macros"] = {"note": {"say": ["log it"], "steps": [{"command": "add task Clip the raid"}]},
                     "end": {"say": ["end of stream"], "steps": [{"obs_control": {"action": "stop_stream"}}]}}
    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=False)
    with TestClient(app) as c:
        c.headers["X-Assistant-Token"] = app.state.token
        yield c, svc


def test_macro_endpoints(macro_client, monkeypatch):
    c, svc = macro_client
    listed = {m["name"]: m for m in c.get("/api/macros").json()}
    assert listed["note"] == {"name": "note", "triggers": ["log it"], "steps": 1, "needs_yes": False}
    assert listed["end"]["needs_yes"] is True
    assert c.post("/api/macros/note/run").json()["kind"] == "macro"
    assert svc.storage.list_tasks()[0]["title"] == "Clip the raid"
    stopped = []
    monkeypatch.setattr(svc.obs, "control", lambda action: stopped.append(action) or {"ok": True})
    assert c.post("/api/macros/end/run").json()["kind"] == "macro"  # a click already confirmed it
    assert stopped == ["stop_stream"]
    assert c.post("/api/macros/nope/run").status_code == 404


def test_wake_model_upload_and_delete(client, monkeypatch):
    import sys
    from types import SimpleNamespace

    from assistant.voice import wakeword

    c, _ = client
    blob = b"\x08" * 5000
    monkeypatch.setattr(wakeword, "available", lambda: False)
    assert "Install the wake word runtime" in c.post("/api/wakewords?name=vesper", content=blob).json()["error"]

    shape = [[1, 16, 96]]
    fake_ort = SimpleNamespace(InferenceSession=lambda path, providers: SimpleNamespace(
        get_inputs=lambda: [SimpleNamespace(shape=shape[0])]))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setattr(wakeword, "available", lambda: True)
    assert "10 KB" in c.post("/api/wakewords?name=vesper", content=b"tiny").json()["error"]
    shape[0] = [1, 1536]
    assert "16×96" in c.post("/api/wakewords?name=vesper", content=blob).json()["error"]
    shape[0] = ["batch", 16, 96]
    r = c.post("/api/wakewords?name=Clip That.onnx&threshold=0.68", content=blob).json()
    assert r["ok"] and r["name"] == "clip_that"
    models = c.get("/api/voice").json()["wake_models"]
    assert [(m["name"], m["threshold"]) for m in models] == [("clip_that", 0.68)]
    assert c.delete("/api/wakewords/clip_that").json()["ok"] is True
    assert c.get("/api/voice").json()["wake_models"] == []
    assert c.delete("/api/wakewords/clip_that").json()["ok"] is False
