"""Tool servers (MCP): names, tiers, the saved list, the panel without the SDK, and a real round trip to a tiny server."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant.brain.tools import ToolBox
from assistant.skills import Skills
from assistant.skills import mcp as m

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


# ---- pure helpers ------------------------------------------------------------------------------------------

def test_tool_names_are_safe_short_and_stable():
    assert m.tool_name("files", "read_file") == "mcp_files_read_file"
    assert m.tool_name("home", "HassTurnOn") == "mcp_home_hassturnon"
    assert m.tool_name("my-docs", "search.pages v2") == "mcp_my-docs_search_pages_v2"
    long = m.tool_name("github", "x" * 100)
    assert len(long) == 64 and long == m.tool_name("github", "x" * 100) and long != m.tool_name("github", "x" * 99)
    import re
    assert re.fullmatch(r"[a-z0-9_-]{1,64}", long)


def test_command_lines_keep_windows_paths():
    assert m.split_command(r'npx -y @modelcontextprotocol/server-filesystem "C:\Users\J\My Docs"') == \
        ("npx", ["-y", "@modelcontextprotocol/server-filesystem", r"C:\Users\J\My Docs"])
    assert m.split_command("") == ("", [])
    assert m.join_command("npx", ["-y", r"C:\My Docs"]) == r'npx -y "C:\My Docs"'


def test_tiers_default_to_asking_and_can_be_lowered_per_server_or_tool():
    assert m.tier_for({}, "x") == 2
    assert m.tier_for({"tier": 1}, "x") == 1
    assert m.tier_for({"tier": 1, "tiers": {"delete": 3}}, "delete") == 3
    assert m.tier_for({"tier": 2, "tiers": {"search": 1}}, "search") == 1
    assert m.tier_for({"tier": 0}, "x") == 1 and m.tier_for({"tier": "junk"}, "x") == 2  # never below 1


def test_expand_fills_secrets_from_the_environment(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "abc")
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    missing = []
    assert m.expand("Bearer ${HA_TOKEN}", missing) == "Bearer abc" and not missing
    assert m.expand("${NOPE_TOKEN}", missing) == "" and missing == ["NOPE_TOKEN"]


def test_results_become_text_and_errors_are_not_ok():
    res = SimpleNamespace(content=[SimpleNamespace(type="text", text="5"), SimpleNamespace(type="image", mime_type="image/png")],
                          structured_content={"result": 5}, is_error=False)
    assert m.shape_result("fake", "add", res) == {"ok": True, "server": "fake", "tool": "add",
                                                  "text": "5\n[image image/png]", "data": {"result": 5}}
    bad = m.shape_result("fake", "boom", SimpleNamespace(content=[SimpleNamespace(type="text", text="kaboom")], is_error=True))
    assert bad["ok"] is False and bad["error"] == "kaboom"
    assert m.describe_error(ExceptionGroup("x", [FileNotFoundError(2, "nope", "npx")])).startswith("Couldn't start it: npx")


# ---- config and the panel ------------------------------------------------------------------------------------

def test_the_server_list_is_saved_and_tokens_go_to_env(svc, cfg, monkeypatch):
    monkeypatch.setattr(m, "sdk_problem", lambda: "pip install mcp")
    monkeypatch.setenv("MCP_HOME_TOKEN", "")  # add() sets these; monkeypatch puts them back after
    monkeypatch.setenv("MCP_NOTION_TOKEN", "")
    sk = m.MCP(svc)
    out = sk.add({"name": "Home", "target": "http://ha.local:8123/api/mcp", "token": "secret-123", "tier": 1, "wait": 0})
    assert out["ok"] and out["name"] == "home"
    out = sk.add({"name": "notion", "target": "npx -y @notionhq/notion-mcp-server", "token": "ntn_1", "token_var": "NOTION_TOKEN"})
    saved = json.loads((cfg.data_dir / "mcp_servers.json").read_text())
    assert "secret-123" not in json.dumps(saved) and "ntn_1" not in json.dumps(saved)
    assert saved[0]["headers_env"] == {"Authorization": "Bearer ${MCP_HOME_TOKEN}"} and saved[0]["tier"] == 1
    assert saved[1]["command"] == "npx" and saved[1]["env"] == {"NOTION_TOKEN": "${MCP_NOTION_TOKEN}"}
    env = (cfg.root / ".env").read_text()
    assert "MCP_HOME_TOKEN=secret-123" in env and "MCP_NOTION_TOKEN=ntn_1" in env
    again = m.MCP(svc)  # a fresh start reads the same list
    assert list(again.servers) == ["home", "notion"] and again.servers["home"].conf["transport"] == "http"
    assert again.set_tier("home", 3)["ok"] and m.MCP(svc).servers["home"].conf["tier"] == 3
    assert again.remove("notion")["ok"] and list(m.MCP(svc).servers) == ["home"]
    assert sk.add({"name": "x", "target": "npx thing", "token": "t"})["ok"] is False  # where would the token go?
    assert sk.add({"name": "", "target": ""})["ok"] is False


def test_claude_desktop_config_format_is_read(svc, cfg):
    (cfg.data_dir / "mcp_servers.json").write_text(json.dumps(
        {"mcpServers": {"files": {"command": "npx", "args": ["-y", "pkg", "C:\\Docs"]}, "web": {"url": "https://x/mcp"}}}))
    sk = m.MCP(svc)
    assert sk.servers["files"].conf["transport"] == "stdio" and sk.servers["web"].conf["transport"] == "http"
    assert sk.servers["files"].conf["tier"] == 2


def test_without_the_sdk_the_skill_loads_and_says_what_to_install(svc, cfg, monkeypatch):
    monkeypatch.setattr(m, "sdk_problem", lambda: "pip install mcp")
    (cfg.data_dir / "mcp_servers.json").write_text(json.dumps([{"name": "files", "command": "npx", "args": []}]))
    sk = Skills(svc, ["mcp"])
    s = sk["mcp"]
    s.start(None)
    st = s.status()
    assert st["sdk"] == "pip install mcp" and st["servers"][0]["state"] == "error"
    assert "pip install mcp" in st["servers"][0]["error"] and s.tools() == []
    assert s.connect("files")["ok"] is False and s.call("files", "x")["ok"] is False
    assert {p["id"] for p in st["presets"]} >= {"files", "home", "github", "notion"}


def test_missing_token_is_a_status_not_a_crash(svc, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.delenv("MCP_GH_TOKEN", raising=False)
    sk = m.MCP(svc)
    sk.servers["gh"] = m.Server(m.normalize({"name": "gh", "url": "https://example.invalid/mcp",
                                             "headers_env": {"Authorization": "Bearer ${MCP_GH_TOKEN}"}}))
    out = sk.connect("gh", wait=10)
    assert out["ok"] is False and "MCP_GH_TOKEN" in out["error"]
    assert sk.status()["servers"][0]["missing"] == ["MCP_GH_TOKEN"]
    sk.close()


# ---- a real server over stdio --------------------------------------------------------------------------------

@pytest.fixture
def live(svc):
    pytest.importorskip("mcp")
    sk = Skills(svc, ["mcp"])
    svc.skills = sk
    s = sk["mcp"]
    box = ToolBox(svc)
    s.runtime = SimpleNamespace(assistant=SimpleNamespace(tools=box))
    yield s, box
    s.close()


def test_a_connected_server_gives_the_brain_its_tools(live):
    s, box = live
    out = s.add({"name": "fake", "target": f'"{sys.executable}" "{FAKE}"', "wait": 30})
    assert out["ok"] and out["state"] == "connected", out
    names = {n for n in box.tools if n.startswith("mcp_")}
    assert names == {"mcp_fake_add", "mcp_fake_echo-text", "mcp_fake_boom"}
    add = box.tools["mcp_fake_add"]
    assert add.untrusted and add.untrusted_label == "an MCP server" and add.tier_for({}) == 2
    assert add.description.startswith("[fake tool server] Add two numbers")
    assert add.definition()["input_schema"]["required"] == ["a", "b"]
    # tier 2: nothing runs without a yes; with one, the server answers
    assert box.run("mcp_fake_add", {"a": 2, "b": 3})["blocked"]
    r = box.run("mcp_fake_add", {"a": 2, "b": 3}, confirmed_by="hud")
    assert r["ok"] and r["text"] == "5" and r["data"] == {"result": 5}
    assert box.run("mcp_fake_echo-text", {"text": "hi"}, confirmed_by="hud")["text"] == "hi"
    assert box.run("mcp_fake_boom", {}, confirmed_by="hud")["ok"] is False
    # lower one tool to tier 1, hide another
    s.set_tier("fake", 1, tool="add")
    s.offer("fake", "boom", False)
    assert box.tools["mcp_fake_add"].tier_for({}) == 1 and "mcp_fake_boom" not in box.tools
    st = s.status()["servers"][0]
    assert st["state"] == "connected" and st["total"] == 3 and st["offered"] == 2 and st["picked"]
    assert s.refresh("fake")["ok"]
    # disconnect: the tools leave the brain
    assert s.set_enabled("fake", False)["ok"]
    assert not any(n.startswith("mcp_") for n in box.tools)
    assert s.call("fake", "add", a=1, b=1)["ok"] is False


def test_a_server_that_wont_start_shows_why(live):
    s, box = live
    out = s.add({"name": "ghost", "target": "definitely-not-a-real-command-xyz --serve", "wait": 30})
    assert out["state"] == "error" and out["error"]
    assert s.status()["servers"][0]["state"] == "error"
    assert not any(n.startswith("mcp_") for n in box.tools)
