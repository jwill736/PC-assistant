"""The installer's first-run questions: name, goal and where the model lives (this computer, another PC on
the home network, Claude, or later), written into config.yaml without losing its comments."""

import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from assistant import firstrun
from assistant.__main__ import quit_running
from assistant.brain import local_llm
from assistant.config import ROOT, load_config
from assistant.firstrun import Setup, candidate_urls, set_env_value, set_yaml_value
from assistant.runtime import Runtime
from assistant.server import create_app

EXAMPLE = (ROOT / "config.example.yaml").read_text(encoding="utf-8")


class FakeNetwork:
    """Model servers by (host, port): 'ollama' answers /api/tags + /api/show, 'lmstudio' /v1/models."""

    def __init__(self, servers=None, models=("llama3.1:8b",)):
        self.servers, self.models, self.seen = dict(servers or {}), list(models), []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(f"{req.url.host}:{req.url.port}{req.url.path}")
        kind = self.servers.get((req.url.host, req.url.port))
        if kind is None:
            raise httpx.ConnectError("refused", request=req)
        if kind == "ollama" and req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m, "details": {"parameter_size": "8B"}}
                                                        for m in self.models]})
        if kind == "ollama" and req.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        if kind == "lmstudio" and req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        return httpx.Response(404)


def client(net) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(net))


@pytest.fixture
def root(tmp_path):
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env")
    return tmp_path


def answers(*replies):
    """An input() that gives these answers in order, then fails the test if asked again."""
    queue = list(replies)

    def ask(prompt):
        assert queue, f"asked one question too many: {prompt!r}"
        return queue.pop(0)
    return ask


def setup(root, net, *replies, secret=None, out=None):
    out = out if out is not None else []
    return Setup(root, ask=answers(*replies), ask_secret=lambda _p: secret or "", say=out.append, http=client(net))


# ---- editing config.yaml in place ----------------------------------------------------------------------

def test_edits_change_only_their_lines_and_keep_every_comment():
    text = set_yaml_value(EXAMPLE, "assistant.user_name", "Jay O'Neil")
    text = set_yaml_value(text, "goals.north_star", '$5k/month from streaming by June 2027 # "for real"')
    text = set_yaml_value(text, "brain.local.url", "http://GAMING-PC:11434")
    before, after = EXAMPLE.split("\n"), text.split("\n")
    assert len(before) == len(after)
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(changed) == 3
    # the comment after the name stays in its column; the commented-out url line becomes the real one
    assert changed[0][1].index("# how it addresses you") == changed[0][0].index("# how it addresses you")
    assert changed[2] == ("    # url: http://GAMING-PC:11434   # your model on another PC, or a non-default port",
                          '    url: "http://GAMING-PC:11434"')
    data = yaml.safe_load(text)
    assert data["assistant"]["user_name"] == "Jay O'Neil"
    assert data["goals"]["north_star"] == '$5k/month from streaming by June 2027 # "for real"'
    assert data["brain"]["local"] == {"model": "", "stream_model": "auto", "url": "http://GAMING-PC:11434"}
    assert data["goals"]["this_week"] == yaml.safe_load(EXAMPLE)["goals"]["this_week"]


def test_adds_keys_and_sections_that_are_missing():
    text = set_yaml_value("assistant:\n  name: Vesper\n", "assistant.user_name", "J")
    assert text == 'assistant:\n  user_name: "J"\n  name: Vesper\n'
    text = set_yaml_value("voice:\n  enabled: true\n", "brain.local.url", "http://pc:11434")
    assert yaml.safe_load(text) == {"voice": {"enabled": True}, "brain": {"local": {"url": "http://pc:11434"}}}
    assert text.endswith("\n") and "\n\nbrain:\n" in text
    text = set_yaml_value("brain:\n  provider: auto\n", "brain.local.url", "x")
    assert yaml.safe_load(text)["brain"] == {"provider": "auto", "local": {"url": "x"}}


def test_refuses_what_it_cant_change_safely():
    with pytest.raises(ValueError):
        set_yaml_value("assistant:\n  work_hours: {start: '09:00'}\n", "assistant.work_hours.start", "10:00")
    with pytest.raises(ValueError):
        set_yaml_value("goals:\n  this_week:\n    - a\n", "goals.this_week", "b")


def test_quoted_values_with_hashes_and_comments():
    text = set_yaml_value('goals:\n  north_star: "a # b"   # keep me\n', "goals.north_star", "c")
    assert text == 'goals:\n  north_star: "c"       # keep me\n'  # the comment stays in its column


def test_env_file_line_is_replaced_or_added():
    assert set_env_value("A=1\nANTHROPIC_API_KEY=\nB=2\n", "ANTHROPIC_API_KEY", "sk-ant-x") == \
        "A=1\nANTHROPIC_API_KEY=sk-ant-x\nB=2\n"
    assert set_env_value("A=1\n", "ANTHROPIC_API_KEY", "k") == "A=1\nANTHROPIC_API_KEY=k\n"
    assert set_env_value("", "K", "v") == "K=v\n"


def test_candidate_urls():
    assert candidate_urls("gaming-pc") == ["http://gaming-pc:11434", "http://gaming-pc:1234"]
    assert candidate_urls(" 192.168.1.20:1234 ") == ["http://192.168.1.20:1234"]
    assert candidate_urls("http://pc:5000/") == ["http://pc:5000"]


# ---- the questions ---------------------------------------------------------------------------------------

def test_a_model_on_this_computer_is_found_and_nothing_else_is_asked(root):
    out = []
    net = FakeNetwork({("127.0.0.1", 11434): "ollama"})
    result = setup(root, net, "Jay", "", out=out).run()
    assert result["brain"] == "here"
    assert any("Found llama3.1:8b on Ollama on this computer" in line for line in out)
    cfg = load_config(root / "config.yaml")
    assert cfg["assistant"]["user_name"] == "Jay" and not cfg["brain"]["local"].get("url")


def test_llama_on_the_main_pc_by_name(root):
    """The laptop case: the model runs on the main PC; typing its name finds Ollama there and saves the URL."""
    out = []
    net = FakeNetwork({("gaming-pc", 11434): "ollama"}, models=["llama3.1:8b", "qwen2.5:14b"])
    result = setup(root, net, "", "", "1", "GAMING-PC", out=out).run()
    assert result["brain"] == "remote"
    assert any("Connected to http://GAMING-PC:11434: llama3.1:8b, qwen2.5:14b" in line for line in out)
    cfg = load_config(root / "config.yaml")
    assert cfg["brain"]["local"]["url"] == "http://GAMING-PC:11434"
    # and the brain then uses it: the remote server is tried first
    brain = local_llm.LocalBrain(cfg["brain"]["local"], http=client(net))
    assert brain.refresh().url == "http://GAMING-PC:11434" and brain.llm.model == "llama3.1:8b"


def test_the_hud_and_health_check_name_the_main_pc(root, cfg):
    """Connected: 'llama3.1:8b on Ollama at GAMING-PC'. Not reachable: the address and what to do on that PC."""
    from assistant.doctor import WARN, check_brain

    net = FakeNetwork({("gaming-pc", 11434): "ollama"})
    up = local_llm.LocalBrain({"url": "http://gaming-pc:11434"}, http=client(net))
    assert up.refresh().label == "llama3.1:8b on Ollama at GAMING-PC"
    assert up.status()["server"] == "Ollama at GAMING-PC"
    down = local_llm.LocalBrain({"url": "http://gaming-pc:11434/"}, http=client(FakeNetwork()))
    assert down.refresh() is None
    assert down.status()["configured_url"] == "http://gaming-pc:11434" and down.status()["server"] is None
    from assistant.brain.speech import _connections_summary

    spoken = _connections_summary({"brain": {"active": None, "local": down.status()}})
    assert spoken.startswith("I can't reach your model at gaming-pc") and "step 19" in spoken
    cfg["brain"] = {"provider": "auto", "local": {"enabled": True, "url": "http://gaming-pc:11434"}}
    check = check_brain(cfg, brain=down)
    assert check.status == WARN and "Can't reach http://gaming-pc:11434" in check.fix and "OLLAMA_HOST" in check.fix


def test_lm_studio_on_the_main_pc(root):
    net = FakeNetwork({("192.168.1.20", 1234): "lmstudio"}, models=["meta-llama-3.1-8b-instruct"])
    assert setup(root, net, "", "", "1", "192.168.1.20").run()["brain"] == "remote"
    assert load_config(root / "config.yaml")["brain"]["local"]["url"] == "http://192.168.1.20:1234"


def test_main_pc_not_reachable_yet_saves_it_and_says_what_to_do_there(root):
    out = []
    result = setup(root, FakeNetwork(), "", "", "1", "gaming-pc", out=out).run()
    assert result["brain"] == "remote-unreachable"
    text = "\n".join(out)
    assert "setx OLLAMA_HOST 0.0.0.0" in text and "Serve on Local Network" in text and "ipconfig" in text
    assert load_config(root / "config.yaml")["brain"]["local"]["url"] == "http://gaming-pc:11434"


def test_claude_key_goes_into_env_hidden(root):
    out = []
    result = setup(root, FakeNetwork(), "", "", "3", secret="sk-ant-api03-abcd1234", out=out).run()
    assert result == {"changes": {}, "brain": "claude", "claude_key": True}
    assert "ANTHROPIC_API_KEY=sk-ant-api03-abcd1234\n" in (root / ".env").read_text()
    assert not any("abcd1234" in line and "sk-ant" in line for line in out)  # only the last 4 characters shown


def test_a_wrong_key_is_not_saved(root):
    out = []
    assert setup(root, FakeNetwork(), "", "", "3", secret="hello", out=out).run()["brain"] == "later"
    assert "ANTHROPIC_API_KEY=\n" in (root / ".env").read_text()
    assert any("start with sk-ant-" in line for line in out)


def test_decide_later_and_closed_input_keep_everything(root):
    before = (root / "config.yaml").read_text()

    def closed(_prompt):
        raise EOFError

    out = []
    s = Setup(root, ask=closed, ask_secret=closed, say=out.append, http=client(FakeNetwork()))
    assert s.run()["brain"] == "later"
    assert (root / "config.yaml").read_text() == before
    assert any("Setup > Brain" in line for line in out)


def test_ollama_running_here_without_models_says_how_to_get_one(root):
    out = []
    net = FakeNetwork({("127.0.0.1", 11434): "ollama"}, models=[])
    setup(root, net, "", "", "4", out=out).run()
    assert any("has no models yet" in line and "ollama pull" in line for line in out)


def test_unattended_asks_nothing(root):
    out = []
    s = Setup(root, ask=answers(), ask_secret=answers(), say=out.append, http=client(FakeNetwork()), unattended=True)
    result = s.run(name="CI", goal="Ship it by Friday", brain_url="http://127.0.0.1:9")
    assert result["brain"] == "remote-unreachable"
    cfg = load_config(root / "config.yaml")
    assert cfg["assistant"]["user_name"] == "CI" and cfg["goals"]["north_star"] == "Ship it by Friday"
    assert cfg["brain"]["local"]["url"] == "http://127.0.0.1:9"


def test_a_hand_edited_config_falls_back_to_the_settings_file(root):
    (root / "config.yaml").write_text("assistant: {name: Vesper, user_name: J}\ndata_dir: data\n")
    out = []
    setup(root, FakeNetwork(), "Jay", "", "4", out=out).run()
    assert (root / "config.yaml").read_text() == "assistant: {name: Vesper, user_name: J}\ndata_dir: data\n"
    assert yaml.safe_load((root / "data" / "settings.yaml").read_text())["assistant"]["user_name"] == "Jay"
    assert load_config(root / "config.yaml")["assistant"]["user_name"] == "Jay"


def test_command_line(root, monkeypatch):
    monkeypatch.setattr(local_llm, "detect", lambda *a, **k: None)
    assert firstrun.main(["--root", str(root), "--unattended", "--name", "Jay", "--icon", str(root / "v.ico")]) == 0
    assert load_config(root / "config.yaml")["assistant"]["user_name"] == "Jay"


def test_icon_is_the_tray_mark(tmp_path):
    pytest.importorskip("PIL")
    assert firstrun.write_icon(tmp_path / "data" / "vesper.ico")
    assert (tmp_path / "data" / "vesper.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"  # ICO header


# ---- closing a running copy before an update ------------------------------------------------------------

def test_quit_endpoint_needs_the_token_and_closes_the_app(cfg, svc):
    app = create_app(Runtime(cfg, services=svc), start_background=False)
    calls = []
    with TestClient(app) as c:
        assert c.post("/api/quit").status_code == 401
        headers = {"X-Assistant-Token": app.state.token}
        assert c.post("/api/quit", headers=headers).status_code == 503  # no server to stop (tests)
        app.state.on_quit = lambda: calls.append(1)
        assert c.post("/api/quit", headers=headers).json() == {"ok": True, "pid": os.getpid()}
    assert calls == [1]


def test_quit_running_when_nothing_runs(tmp_path, monkeypatch):
    posted = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: posted.append(a))
    assert quit_running("127.0.0.1", 9, tmp_path, wait=0.1) is True
    assert posted == []


def test_quit_running_asks_with_the_token_and_waits(tmp_path, monkeypatch):
    import assistant.__main__ as main_mod

    (tmp_path / "api_token").write_text("tok\n")
    state = {"up": True}
    posted = []

    def post(url, headers, timeout):
        posted.append((url, headers))
        state["up"] = False

    monkeypatch.setattr(main_mod, "already_running", lambda h, p: state["up"])
    monkeypatch.setattr(httpx, "post", post)
    assert quit_running("127.0.0.1", 8765, tmp_path, wait=1) is True
    assert posted == [("http://127.0.0.1:8765/api/quit", {"x-assistant-token": "tok"})]


def test_config_example_still_loads():
    assert json.dumps(yaml.safe_load(EXAMPLE))  # the url comment edit kept it valid
