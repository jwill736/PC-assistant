import json
import subprocess
from pathlib import Path

import pytest

from assistant import discovery
from assistant.config import load_config
from assistant.discovery import SystemPaths, spoken_form

STREAM_KEY = "live_123456789_SECRETSTREAMKEY"


@pytest.fixture
def fake_pc(tmp_path) -> SystemPaths:
    """A Windows-shaped tree: %APPDATA%, %LOCALAPPDATA%, Program Files, ProgramData, home."""
    env = {
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "ProgramFiles": str(tmp_path / "PF"),
        "ProgramFiles(x86)": str(tmp_path / "PF86"),
        "ProgramData": str(tmp_path / "PD"),
        "SystemDrive": str(tmp_path / "C"),
    }
    home = tmp_path / "home"
    home.mkdir()
    return SystemPaths(env=env, home=home)


def touch(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_obs(p: SystemPaths, enabled=True):
    base = Path(p.env["APPDATA"]) / "obs-studio"
    touch(base / "plugin_config/obs-websocket/config.json", json.dumps(
        {"server_enabled": enabled, "server_port": 4460, "auth_required": True, "server_password": "hunter2"}))
    touch(base / "user.ini", "[Basic]\nProfileDir=Main\nSceneCollectionFile=Streaming\n")
    touch(base / "basic/scenes/Streaming.json", json.dumps({"name": "Streaming", "scene_order": [
        {"name": "🔴 Starting Soon"}, {"name": "Just Chatting"}, {"name": "BRB ☕"}, {"name": "Gameplay_Main"}]}))
    touch(base / "basic/scenes/Old.json", json.dumps({"name": "Old", "scene_order": [{"name": "Unused"}]}))
    touch(base / "basic/profiles/Main/service.json", json.dumps(
        {"type": "rtmp_common", "settings": {"service": "Twitch", "server": "auto", "key": STREAM_KEY}}))
    return base


def test_spoken_form():
    assert spoken_form("🔴 Starting Soon") == "starting soon"
    assert spoken_form("Gameplay_Main") == "gameplay main"
    assert spoken_form("Q&A — Live!") == "q&a live"


def test_obs_scan_reads_scenes_platform_and_never_the_key(fake_pc):
    make_obs(fake_pc)
    findings, suggested, info = discovery.scan_obs(fake_pc)
    assert info["scenes"] == ["🔴 Starting Soon", "Just Chatting", "BRB ☕", "Gameplay_Main"]
    assert info["platform"] == "Twitch"
    assert suggested["obs"]["port"] == 4460
    assert suggested["obs"]["scene_aliases"] == {"starting soon": "🔴 Starting Soon", "brb": "BRB ☕",
                                                 "gameplay main": "Gameplay_Main"}
    blob = json.dumps([findings and [f.__dict__ for f in findings], suggested, info])
    assert STREAM_KEY not in blob and "hunter2" not in blob
    assert discovery.read_obs_websocket(fake_pc)["password"] == "hunter2"


def test_obs_websocket_disabled_is_an_action(fake_pc):
    make_obs(fake_pc, enabled=False)
    findings, _s, _i = discovery.scan_obs(fake_pc)
    ws = next(f for f in findings if f.name == "OBS WebSocket")
    assert ws.status == "action" and "Enable" in ws.fix


def test_games_from_steam_and_epic(fake_pc):
    steam = Path(fake_pc.env["ProgramFiles(x86)"]) / "Steam"
    lib2 = fake_pc.home / "SteamLibrary"
    touch(steam / "steamapps/libraryfolders.vdf",
          '"libraryfolders"\n{\n "0"\n {\n  "path"  "%s"\n }\n "1"\n {\n  "path"  "%s"\n }\n}\n'
          % (str(steam).replace("\\", "\\\\"), str(lib2).replace("\\", "\\\\")))
    touch(steam / "steamapps/appmanifest_730.acf", '"AppState"\n{\n "appid" "730"\n "name" "Counter-Strike 2"\n}')
    touch(steam / "steamapps/appmanifest_228980.acf", '"AppState"\n{\n "appid" "228980"\n "name" "Steamworks Common Redistributables"\n}')
    touch(lib2 / "steamapps/appmanifest_1086940.acf", '"AppState"\n{\n "appid" "1086940"\n "name" "Baldur\'s Gate 3"\n}')
    touch(Path(fake_pc.env["ProgramData"]) / "Epic/EpicGamesLauncher/Data/Manifests/x.item",
          json.dumps({"DisplayName": "Fortnite", "AppName": "Fortnite"}))
    findings, games = discovery.scan_games(fake_pc)
    assert games["counter strike 2"] == {"path": "steam://rungameid/730"}
    assert games["baldur's gate 3"]["path"] == "steam://rungameid/1086940"
    assert games["fortnite"]["path"].startswith("com.epicgames.launcher://apps/Fortnite")
    assert not any("redistributable" in g for g in games)
    assert "2 Steam + 1 Epic" in findings[0].detail


def test_bookmarks_bar_becomes_sites(fake_pc):
    chrome = Path(fake_pc.env["LOCALAPPDATA"]) / "Google/Chrome/User Data"
    bar = {"children": [
        {"type": "url", "name": "Twitch Dashboard", "url": "https://dashboard.twitch.tv/u/me"},
        {"type": "url", "name": "📅 Calendar", "url": "https://calendar.google.com/calendar/u/0/r"},
        {"type": "folder", "name": "Work", "children": [
            {"type": "url", "name": "Stripe", "url": "https://dashboard.stripe.com"},
            {"type": "folder", "name": "Deep", "children": [{"type": "url", "name": "Too deep", "url": "https://x.y"}]}]},
        {"type": "url", "name": "js", "url": "javascript:alert(1)"},
    ]}
    touch(chrome / "Default/Bookmarks", json.dumps({"roots": {"bookmark_bar": bar}}))
    findings, sites, cal = discovery.scan_browsers(fake_pc)
    assert sites == {"twitch dashboard": "https://dashboard.twitch.tv/u/me",
                     "calendar": "https://calendar.google.com/calendar/u/0/r",
                     "stripe": "https://dashboard.stripe.com"}
    assert cal and "3 bookmarks-bar sites" in findings[0].detail


def test_known_apps_by_path_and_by_shortcut(fake_pc, tmp_path):
    touch(Path(fake_pc.env["LOCALAPPDATA"]) / "Discord/Update.exe")
    touch(Path(fake_pc.env["ProgramFiles"]) / "obs-studio/bin/64bit/obs64.exe")
    lnk = touch(tmp_path / "startmenu/Adobe Premiere Pro 2026.lnk")
    findings, apps, by_area = discovery.scan_apps(fake_pc, {"adobe premiere pro 2026": lnk, "notepad": tmp_path / "n.lnk"})
    assert apps["discord"]["args"] == "--processStart Discord.exe" and apps["discord"]["process"] == "discord"
    assert apps["obs"]["cwd"].endswith("64bit")
    assert apps["premiere"] == {"path": str(lnk), "process": "adobe premiere pro"}
    assert "chrome" not in apps
    assert set(by_area) == {"chat", "streaming", "creative"}


def git(repo: Path, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_code_scan_finds_repos_and_github_user(fake_pc):
    for name, owner in [("alpha", "someuser"), ("beta", "someuser"), ("gamma", "someorg")]:
        repo = fake_pc.home / "source/repos" / name
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        git(repo, "remote", "add", "origin", f"https://github.com/{owner}/{name}.git")
    findings, suggested = discovery.scan_code(fake_pc)
    assert suggested["projects"]["github"]["user"] == "someuser"
    assert suggested["projects"]["scan_dirs"] == [str(fake_pc.home / "source/repos")]
    assert findings[0].detail.startswith("3 repos found")


def test_full_discover_writes_config_that_merges_under_user(fake_pc, cfg, tmp_path):
    make_obs(fake_pc)
    touch(Path(fake_pc.env["LOCALAPPDATA"]) / "slack/slack.exe")
    result = discovery.discover(cfg, fake_pc, shortcut_index={})
    assert result["summary"] and result["findings"][0]["status"] in {"action", "missing"}  # problems first
    root = tmp_path / "root"
    root.mkdir()
    discovery.write_discovered(result, root, root / "data")
    assert (root / "data/discovery.json").exists()
    assert "hunter2" not in (root / "config.discovered.yaml").read_text(encoding="utf-8")

    (root / "config.yaml").write_text(
        "apps:\n  slack: {path: my-slack.exe}\nprojects:\n  scan_dirs: [D:/mine]\n"
        "profiles:\n  work:\n    apps: [excel]\nobs:\n  scene_aliases: {brb: My BRB}\n", encoding="utf-8")
    merged = load_config(root / "config.yaml")
    assert merged["apps"]["slack"] == {"path": "my-slack.exe"}                  # user wins
    assert "obs" in merged["apps"] or "starting soon" in merged["obs"]["scene_aliases"]
    assert merged["obs"]["scene_aliases"]["brb"] == "My BRB"                   # user wins
    assert merged["obs"]["scene_aliases"]["starting soon"] == "🔴 Starting Soon"  # discovered fills in
    assert merged["profiles"]["work"]["apps"][:2] == ["excel", "slack"]      # lists are unioned
    assert merged["projects"]["scan_dirs"][0] == "D:/mine"
    assert merged.path == root / "config.yaml"


def test_obs_password_falls_back_to_obs_config(monkeypatch, cfg, fake_pc):
    from assistant import services

    make_obs(fake_pc)
    monkeypatch.setattr(discovery, "SystemPaths", lambda: fake_pc)
    monkeypatch.delenv("OBS_PASSWORD", raising=False)
    assert services.obs_password(cfg) == "hunter2"
    monkeypatch.setenv("OBS_PASSWORD", "from-env")
    assert services.obs_password(cfg) == "from-env"


def test_stale_detection(tmp_path):
    assert discovery.is_stale(tmp_path)
    (tmp_path / "config.discovered.yaml").write_text("{}\n")
    assert not discovery.is_stale(tmp_path)
    assert discovery.is_stale(tmp_path, max_age_s=-1)
