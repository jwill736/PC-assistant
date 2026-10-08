"""Find everything on this PC the assistant can connect to.

Runs locally (at first launch, then daily, or via ``--doctor``) and produces:

* ``findings``  — one row per thing found or missing, with the fix, for the
  HUD Setup view and the doctor report.
* ``suggested`` — a config fragment written to ``config.discovered.yaml`` and
  merged *under* ``config.yaml`` (the user's own settings always win).

Secrets are never copied anywhere. OBS's WebSocket password is read straight
from OBS's own config at connect time (see ``read_obs_websocket``), and the
stream key in OBS's service.json is never read into memory beyond parsing.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import DISCOVERED_FILE

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
STALE_AFTER_S = 24 * 3600

CONNECTED, FOUND, ACTION, MISSING = "connected", "found", "action", "missing"


@dataclass
class Finding:
    area: str
    name: str
    status: str  # connected | found | action | missing
    detail: str = ""
    fix: str = ""


@dataclass
class SystemPaths:
    """Where to look. Real values by default; tests point these at a temp tree."""

    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    home: Path = field(default_factory=Path.home)

    def var(self, name: str) -> Path | None:
        value = self.env.get(name)
        return Path(value) if value else None

    def expand(self, template: str) -> Path | None:
        """Expand %VAR% placeholders using ``env``; None if any are unset."""
        missing = False

        def sub(m: re.Match) -> str:
            nonlocal missing
            value = self.env.get(m.group(1))
            if value is None:
                missing = True
                return ""
            return value

        out = re.sub(r"%([^%]+)%", sub, template)
        # Forward slashes work as separators on Windows and POSIX alike.
        return None if missing else Path(out.replace("\\", "/"))

    def first_existing(self, *candidates: Path | None) -> Path | None:
        for c in candidates:
            if c is not None and c.exists():
                return c
        return None


# ---------------------------------------------------------------------------
# Known apps: spoken alias -> where it usually installs + its process name.
# `hints` match Start Menu shortcut names when the exe isn't at a fixed path.
# ---------------------------------------------------------------------------
KNOWN_APPS: dict[str, dict[str, Any]] = {
    "chrome": {"paths": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe", r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"], "process": "chrome", "hints": ["google chrome"], "area": "browser"},
    "edge": {"paths": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"], "process": "msedge", "hints": ["microsoft edge"], "area": "browser"},
    "brave": {"paths": [r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe", r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"], "process": "brave", "hints": ["brave"], "area": "browser"},
    "firefox": {"paths": [r"%ProgramFiles%\Mozilla Firefox\firefox.exe"], "process": "firefox", "hints": ["firefox"], "area": "browser"},
    "obs": {"paths": [r"%ProgramFiles%\obs-studio\bin\64bit\obs64.exe"], "process": "obs64", "hints": ["obs studio"], "area": "streaming", "cwd_parent": True},
    "streamlabs": {"paths": [r"%ProgramFiles%\Streamlabs OBS\Streamlabs OBS.exe"], "process": "streamlabs obs", "hints": ["streamlabs"], "area": "streaming"},
    "streamerbot": {"paths": [], "process": "streamer.bot", "hints": ["streamer.bot", "streamerbot"], "area": "streaming"},
    "stream deck": {"paths": [r"%ProgramFiles%\Elgato\StreamDeck\StreamDeck.exe"], "process": "streamdeck", "hints": ["stream deck"], "area": "streaming"},
    "wave link": {"paths": [r"%ProgramFiles%\Elgato\WaveLink\WaveLink.exe"], "process": "wavelink", "hints": ["wave link"], "area": "audio"},
    "voicemeeter": {"paths": [r"%ProgramFiles(x86)%\VB\Voicemeeter\voicemeeter8x64.exe", r"%ProgramFiles(x86)%\VB\Voicemeeter\voicemeeterpro_x64.exe", r"%ProgramFiles(x86)%\VB\Voicemeeter\voicemeeter_x64.exe"], "process": "voicemeeter8x64", "hints": ["voicemeeter"], "area": "audio"},
    "voicemod": {"paths": [r"%ProgramFiles%\Voicemod V3\Voicemod.exe", r"%ProgramFiles%\Voicemod Desktop\VoicemodDesktop.exe"], "process": "voicemod", "hints": ["voicemod"], "area": "audio"},
    "discord": {"paths": [r"%LOCALAPPDATA%\Discord\Update.exe"], "args": "--processStart Discord.exe", "process": "discord", "hints": ["discord"], "area": "chat"},
    "slack": {"paths": [r"%LOCALAPPDATA%\slack\slack.exe", r"%ProgramFiles%\Slack\slack.exe"], "process": "slack", "hints": ["slack"], "area": "chat"},
    "teams": {"paths": [], "process": "ms-teams", "hints": ["microsoft teams", "teams"], "area": "chat"},
    "zoom": {"paths": [r"%APPDATA%\Zoom\bin\Zoom.exe", r"%ProgramFiles%\Zoom\bin\Zoom.exe"], "process": "zoom", "hints": ["zoom workplace", "zoom"], "area": "chat"},
    "spotify": {"paths": [r"%APPDATA%\Spotify\Spotify.exe"], "process": "spotify", "hints": ["spotify"], "area": "media"},
    "steam": {"paths": [r"%ProgramFiles(x86)%\Steam\steam.exe"], "process": "steam", "hints": ["steam"], "area": "games"},
    "epic games": {"paths": [r"%ProgramFiles(x86)%\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe"], "process": "epicgameslauncher", "hints": ["epic games launcher"], "area": "games"},
    "valorant": {"paths": [r"%SystemDrive%\Riot Games\Riot Client\RiotClientServices.exe"], "args": "--launch-product=valorant --launch-patchline=live", "process": "valorant", "hints": ["valorant"], "area": "games"},
    "league of legends": {"paths": [r"%SystemDrive%\Riot Games\Riot Client\RiotClientServices.exe"], "args": "--launch-product=league_of_legends --launch-patchline=live", "process": "leagueclient", "hints": ["league of legends"], "area": "games"},
    "battle.net": {"paths": [r"%ProgramFiles(x86)%\Battle.net\Battle.net Launcher.exe"], "process": "battle.net", "hints": ["battle.net"], "area": "games"},
    "vscode": {"paths": [r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe", r"%ProgramFiles%\Microsoft VS Code\Code.exe"], "process": "code", "hints": ["visual studio code"], "area": "work"},
    "cursor": {"paths": [r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe"], "process": "cursor", "hints": ["cursor"], "area": "work"},
    "claude": {"paths": [r"%LOCALAPPDATA%\AnthropicClaude\claude.exe"], "process": "claude", "hints": ["claude"], "area": "work"},
    "chatgpt": {"paths": [], "process": "chatgpt", "hints": ["chatgpt"], "area": "work"},
    "notion": {"paths": [r"%LOCALAPPDATA%\Programs\Notion\Notion.exe"], "process": "notion", "hints": ["notion"], "area": "work"},
    "obsidian": {"paths": [r"%LOCALAPPDATA%\Programs\Obsidian\Obsidian.exe"], "process": "obsidian", "hints": ["obsidian"], "area": "work"},
    "excel": {"paths": [r"%ProgramFiles%\Microsoft Office\root\Office16\EXCEL.EXE"], "process": "excel", "hints": ["excel"], "area": "work"},
    "word": {"paths": [r"%ProgramFiles%\Microsoft Office\root\Office16\WINWORD.EXE"], "process": "winword", "hints": ["word"], "area": "work"},
    "outlook": {"paths": [r"%ProgramFiles%\Microsoft Office\root\Office16\OUTLOOK.EXE"], "process": "outlook", "hints": ["outlook (classic)", "outlook"], "area": "work"},
    "terminal": {"paths": [r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe"], "process": "windowsterminal", "hints": ["terminal"], "area": "work"},
    "docker": {"paths": [r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe"], "process": "docker desktop", "hints": ["docker desktop"], "area": "work"},
    "davinci resolve": {"paths": [r"%ProgramFiles%\Blackmagic Design\DaVinci Resolve\Resolve.exe"], "process": "resolve", "hints": ["davinci resolve"], "area": "creative"},
    "premiere": {"paths": [], "process": "adobe premiere pro", "hints": ["adobe premiere pro", "premiere pro"], "area": "creative"},
    "photoshop": {"paths": [], "process": "photoshop", "hints": ["adobe photoshop", "photoshop"], "area": "creative"},
    "after effects": {"paths": [], "process": "afterfx", "hints": ["adobe after effects", "after effects"], "area": "creative"},
    "capcut": {"paths": [r"%LOCALAPPDATA%\CapCut\Apps\CapCut.exe"], "process": "capcut", "hints": ["capcut"], "area": "creative"},
    "nvidia app": {"paths": [], "process": "nvidia app", "hints": ["nvidia app", "geforce experience"], "area": "system"},
    "g hub": {"paths": [r"%ProgramFiles%\LGHUB\lghub.exe"], "process": "lghub", "hints": ["logitech g hub"], "area": "system"},
}

APP_PROFILE_HINTS = {"work": "work", "chat": "work", "streaming": "stream", "audio": "stream", "creative": "stream"}


def scan_apps(paths: SystemPaths, shortcut_index: dict[str, Path]) -> tuple[list[Finding], dict, dict[str, list[str]]]:
    findings, apps = [], {}
    by_area: dict[str, list[str]] = {}
    for alias, spec in KNOWN_APPS.items():
        exe = paths.first_existing(*(paths.expand(p) for p in spec["paths"]))
        entry: dict[str, Any] | None = None
        if exe is not None:
            entry = {"path": str(exe), "process": spec["process"]}
            if spec.get("args"):
                entry["args"] = spec["args"]
            if spec.get("cwd_parent"):
                entry["cwd"] = str(exe.parent)
        else:
            names = sorted(shortcut_index, key=len)
            match = next((n for h in spec["hints"] for n in names if n == h), None) \
                or next((n for h in spec["hints"] for n in names if n.startswith(h)), None)
            if match:
                entry = {"path": str(shortcut_index[match]), "process": spec["process"]}
        if entry is None:
            continue
        apps[alias] = entry
        by_area.setdefault(spec["area"], []).append(alias)
    for area, names in sorted(by_area.items()):
        findings.append(Finding("apps", area.title(), FOUND, ", ".join(sorted(names))))
    findings.append(Finding("apps", "Start Menu", FOUND if shortcut_index else MISSING,
                            f"{len(shortcut_index)} shortcuts indexed — any of them opens by name"))
    return findings, apps, by_area


# ---------------------------------------------------------------------------
# OBS
# ---------------------------------------------------------------------------

def obs_config_dir(paths: SystemPaths) -> Path | None:
    appdata = paths.var("APPDATA")
    return paths.first_existing(
        appdata / "obs-studio" if appdata else None,
        paths.home / ".config" / "obs-studio",
        paths.home / "Library" / "Application Support" / "obs-studio",
    )


def read_obs_websocket(paths: SystemPaths | None = None) -> dict:
    """OBS's own WebSocket settings (enabled/port/password). {} if not found."""
    base = obs_config_dir(paths or SystemPaths())
    if base is None:
        return {}
    cfg_file = base / "plugin_config" / "obs-websocket" / "config.json"
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        "enabled": bool(data.get("server_enabled")),
        "port": int(data.get("server_port") or 4455),
        "auth_required": bool(data.get("auth_required", True)),
        "password": data.get("server_password") or "",
    }


def _ini_value(path: Path, section: str, key: str) -> str | None:
    if not path.exists():
        return None
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path, encoding="utf-8-sig")
        return parser.get(section, key, fallback=None)
    except (configparser.Error, UnicodeDecodeError):
        return None


def spoken_form(name: str) -> str:
    """What someone would *say* for a scene name: no emoji, symbols or casing."""
    s = re.sub(r"[^\w\s'&+-]", " ", name.lower())
    s = s.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


def scan_obs(paths: SystemPaths) -> tuple[list[Finding], dict, dict]:
    findings: list[Finding] = []
    suggested: dict = {}
    info: dict = {"installed": False}
    base = obs_config_dir(paths)
    if base is None:
        findings.append(Finding("streaming", "OBS Studio", MISSING, "No OBS settings folder found.",
                                "Install OBS 28+ (obsproject.com) if you stream or record."))
        return findings, suggested, info
    info["installed"] = True

    ws = read_obs_websocket(paths)
    if not ws:
        findings.append(Finding("streaming", "OBS WebSocket", ACTION, "WebSocket server settings not found.",
                                "OBS → Tools → WebSocket Server Settings → Enable WebSocket server."))
    elif not ws["enabled"]:
        findings.append(Finding("streaming", "OBS WebSocket", ACTION, f"Installed but switched off (port {ws['port']}).",
                                "OBS → Tools → WebSocket Server Settings → tick Enable WebSocket server."))
    else:
        auth = "password read automatically from OBS" if ws["auth_required"] and ws["password"] else "no password"
        findings.append(Finding("streaming", "OBS WebSocket", CONNECTED, f"Enabled on port {ws['port']} ({auth})."))
    if ws:
        suggested["obs"] = {"enabled": True, "port": ws["port"]}

    # Scene collections
    current = (_ini_value(base / "user.ini", "Basic", "SceneCollectionFile")
               or _ini_value(base / "global.ini", "Basic", "SceneCollectionFile"))
    collections = {}
    for f in sorted((base / "basic" / "scenes").glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        order = [s.get("name") for s in data.get("scene_order", []) if s.get("name")]
        if not order:
            order = [s.get("name") for s in data.get("sources", []) if s.get("id") == "scene" and s.get("name")]
        collections[f.stem] = {"name": data.get("name", f.stem), "scenes": order}
    if collections:
        key = current if current in collections else next(iter(collections))
        scenes = collections[key]["scenes"]
        info.update(collection=collections[key]["name"], scenes=scenes)
        aliases = {}
        for s in scenes:
            spoken = spoken_form(s)
            if spoken and spoken != s.lower():
                aliases[spoken] = s
        if aliases:
            suggested.setdefault("obs", {})["scene_aliases"] = aliases
        findings.append(Finding("streaming", "OBS scenes", FOUND,
                                f"{len(scenes)} scenes in “{collections[key]['name']}”: {', '.join(scenes[:8])}"
                                + (" …" if len(scenes) > 8 else "")))

    # Streaming destination (service name only — never the key)
    profile = (_ini_value(base / "user.ini", "Basic", "ProfileDir")
               or _ini_value(base / "global.ini", "Basic", "ProfileDir"))
    service_files = sorted((base / "basic" / "profiles").glob("*/service.json"))
    if profile:
        service_files.sort(key=lambda p: p.parent.name != profile)
    for sf in service_files[:1]:
        platform = _service_platform(sf)
        if platform:
            info["platform"] = platform
            findings.append(Finding("streaming", "Streaming destination", FOUND, f"OBS streams to {platform}."))
    return findings, suggested, info


def _service_platform(service_file: Path) -> str | None:
    try:
        data = json.loads(service_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    settings = data.get("settings") or {}
    service = str(settings.get("service") or "").strip()
    server = str(settings.get("server") or "").lower()
    for name in ("Twitch", "YouTube", "Kick", "Facebook", "TikTok", "Restream"):
        if name.lower() in service.lower() or name.lower() in server:
            return name
    return service or ("Custom RTMP" if server else None)


# ---------------------------------------------------------------------------
# Games: Steam + Epic
# ---------------------------------------------------------------------------
_VDF_PATH = re.compile(r'"path"\s+"([^"]+)"')
_ACF_FIELD = re.compile(r'"(appid|name)"\s+"([^"]*)"')
SKIP_GAMES = re.compile(r"redistributable|proton|steam linux runtime|steamworks|soundtrack|dedicated server|sdk", re.I)


def steam_root(paths: SystemPaths) -> Path | None:
    if IS_WINDOWS and paths.env is os.environ:  # pragma: no cover - registry is Windows-only
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                p = Path(winreg.QueryValueEx(key, "SteamPath")[0])
                if p.exists():
                    return p
        except OSError:
            pass
    pf86 = paths.var("ProgramFiles(x86)")
    return paths.first_existing(pf86 / "Steam" if pf86 else None,
                                paths.home / ".steam" / "steam", paths.home / ".local" / "share" / "Steam",
                                paths.home / "Library" / "Application Support" / "Steam")


def scan_games(paths: SystemPaths) -> tuple[list[Finding], dict]:
    games: dict[str, dict] = {}
    root = steam_root(paths)
    steam_count = 0
    if root is not None:
        libraries = {root}
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            text = vdf.read_text(encoding="utf-8", errors="replace")
            libraries |= {Path(p.replace("\\\\", "\\")) for p in _VDF_PATH.findall(text)}
        for lib in libraries:
            for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
                fields = dict(_ACF_FIELD.findall(acf.read_text(encoding="utf-8", errors="replace")))
                name, appid = fields.get("name"), fields.get("appid")
                if name and appid and not SKIP_GAMES.search(name):
                    games[spoken_form(name)] = {"path": f"steam://rungameid/{appid}"}
                    steam_count += 1
    epic_count = 0
    program_data = paths.var("ProgramData")
    manifests = program_data / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests" if program_data else None
    if manifests and manifests.is_dir():
        for item in manifests.glob("*.item"):
            try:
                data = json.loads(item.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name, app = data.get("DisplayName"), data.get("AppName")
            if name and app and not SKIP_GAMES.search(name):
                games.setdefault(spoken_form(name), {
                    "path": f"com.epicgames.launcher://apps/{app}?action=launch&silent=true"})
                epic_count += 1
    findings = []
    if steam_count or epic_count:
        sample = ", ".join(sorted(games)[:6])
        findings.append(Finding("games", "Installed games", FOUND,
                                f"{steam_count} Steam + {epic_count} Epic games open by name (e.g. {sample})."))
    return findings, games


# ---------------------------------------------------------------------------
# Browsers: bookmarks bar -> voice-openable sites
# ---------------------------------------------------------------------------
BROWSER_DIRS = {
    "Chrome": [r"%LOCALAPPDATA%\Google\Chrome\User Data", "~/.config/google-chrome", "~/Library/Application Support/Google/Chrome"],
    "Edge": [r"%LOCALAPPDATA%\Microsoft\Edge\User Data", "~/.config/microsoft-edge"],
    "Brave": [r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data", "~/.config/BraveSoftware/Brave-Browser"],
}
CALENDAR_HOSTS = ("calendar.google.com", "outlook.office.com/calendar", "outlook.live.com/calendar", "icloud.com/calendar")


def _resolve_dir(paths: SystemPaths, template: str) -> Path | None:
    if template.startswith("~/"):
        return paths.home / template[2:]
    return paths.expand(template)


def scan_browsers(paths: SystemPaths, limit: int = 40) -> tuple[list[Finding], dict, list[str]]:
    sites: dict[str, str] = {}
    calendar_links: list[str] = []
    findings = []
    for browser, templates in BROWSER_DIRS.items():
        root = paths.first_existing(*(_resolve_dir(paths, t) for t in templates))
        if root is None:
            continue
        profiles = [p for p in [root / "Default", *sorted(root.glob("Profile *"))] if (p / "Bookmarks").exists()]
        count = 0
        for prof in profiles:
            try:
                data = json.loads((prof / "Bookmarks").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            bar = (data.get("roots") or {}).get("bookmark_bar") or {}
            for node in _walk_bookmarks(bar.get("children") or [], depth=0):
                url = node.get("url", "")
                if any(h in url for h in CALENDAR_HOSTS):
                    calendar_links.append(url)
                alias = spoken_form(node.get("name", ""))[:40]
                if alias and url.startswith("http") and alias not in sites and len(sites) < limit:
                    sites[alias] = url
                    count += 1
        findings.append(Finding("browser", f"{browser} bookmarks", FOUND,
                                f"{count} bookmarks-bar sites open by voice (“open <bookmark name>”)."
                                if count else f"{browser} found; bookmarks bar is empty."))
    return findings, sites, calendar_links


def _walk_bookmarks(nodes: list, depth: int):
    for n in nodes:
        if n.get("type") == "url":
            yield n
        elif n.get("type") == "folder" and depth < 1:
            yield from _walk_bookmarks(n.get("children") or [], depth + 1)


# ---------------------------------------------------------------------------
# Code: git repos, GitHub user, Claude Code
# ---------------------------------------------------------------------------
REPO_DIRS = ["source/repos", "Documents/GitHub", "Projects", "projects", "dev", "code", "src", "repos", "git",
             "Desktop", "Documents"]
_GH_REMOTE = re.compile(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$")


def scan_code(paths: SystemPaths, claude_dir: str = "~/.claude") -> tuple[list[Finding], dict]:
    from .integrations.projects import ClaudeSessions, find_repos

    roots = [str(paths.home / d) for d in REPO_DIRS if (paths.home / d).is_dir()]
    repos = find_repos(roots, max_depth=2)
    sessions = ClaudeSessions(str(paths.home / ".claude") if claude_dir == "~/.claude" else claude_dir)
    claude_projects = sessions.by_project()
    for p in claude_projects:
        if p["cwd"] and (Path(p["cwd"]) / ".git").exists() and Path(p["cwd"]) not in repos:
            repos.append(Path(p["cwd"]))
    owners: Counter = Counter()
    for r in repos:
        try:
            url = subprocess.run(["git", "-C", str(r), "remote", "get-url", "origin"], capture_output=True,
                                 text=True, timeout=5, creationflags=NO_WINDOW).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        m = _GH_REMOTE.search(url)
        if m:
            owners[m.group(1)] += 1
    suggested: dict = {}
    parents = sorted({str(r.parent) for r in repos})
    if parents:
        suggested["projects"] = {"scan_dirs": parents}
    if owners:
        suggested.setdefault("projects", {})["github"] = {"user": owners.most_common(1)[0][0]}
    findings = [Finding("projects", "Git repos", FOUND if repos else MISSING,
                        f"{len(repos)} repos found" + (f" (GitHub: {owners.most_common(1)[0][0]})" if owners else ""),
                        "" if repos else "Clone your repos under ~/source/repos or add projects.scan_dirs.")]
    cli = shutil.which("claude")
    findings.append(Finding("projects", "Claude Code sessions", FOUND if claude_projects else MISSING,
                            f"{len(claude_projects)} projects with recent sessions"))
    findings.append(Finding("projects", "Claude Code CLI", CONNECTED if cli else ACTION,
                            "Background coding jobs available" if cli else "Not on PATH — background coding jobs are off.",
                            "" if cli else "npm install -g @anthropic-ai/claude-code, then `claude` once to sign in."))
    return findings, suggested


# ---------------------------------------------------------------------------
# Hardware: mics + GPU
# ---------------------------------------------------------------------------
PREFERRED_MICS = re.compile(r"wave|yeti|shure|rode|go ?xlr|elgato|audient|focusrite|scarlett|sm7b|voicemeeter", re.I)


def scan_audio() -> list[Finding]:
    try:
        import sounddevice as sd
    except ImportError:
        return [Finding("voice", "Microphone", ACTION, "Voice packages not installed.",
                        "pip install -r requirements-voice.txt")]
    try:
        devices = sd.query_devices()
        default_in = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
    except Exception as exc:
        return [Finding("voice", "Microphone", ACTION, f"Couldn't list audio devices: {exc}")]
    inputs = [(i, d["name"]) for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]
    if not inputs:
        return [Finding("voice", "Microphone", MISSING, "No input devices.", "Plug in or enable a microphone.")]
    default_name = next((n for i, n in inputs if i == default_in), inputs[0][1])
    better = next((n for _i, n in inputs if PREFERRED_MICS.search(n) and n != default_name), None)
    detail = f"Default input: {default_name} ({len(inputs)} inputs)."
    fix = f"Your {better} looks like the real mic — set voice.input_device to its name." if better and not PREFERRED_MICS.search(default_name) else ""
    return [Finding("voice", "Microphone", ACTION if fix else FOUND, detail, fix)]


def scan_gpu() -> tuple[list[Finding], dict]:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return [Finding("system", "GPU", FOUND, "No NVIDIA GPU detected — speech recognition runs on CPU.")], {}
    try:
        name = subprocess.run([smi, "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True,
                              timeout=5, creationflags=NO_WINDOW).stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        name = "NVIDIA GPU"
    cuda = 0
    try:
        import ctranslate2

        cuda = ctranslate2.get_cuda_device_count()
    except Exception:
        pass
    if cuda:
        return [Finding("system", "GPU", CONNECTED, f"{name} (stats + NVENC in the HUD). Speech runs on the CPU "
                        "with Parakeet; if you switch to stt_engine: whisper it will use CUDA (small.en).")], \
            {"voice": {"stt_device": "cuda", "stt_model": "small.en"}}
    return [Finding("system", "GPU", FOUND, f"{name} (stats + NVENC in the HUD). Speech recognition runs on the "
                    "CPU with Parakeet (~60 ms per command), so it doesn't compete with the game or NVENC.")], {}


# ---------------------------------------------------------------------------
# Accounts & keys already configured
# ---------------------------------------------------------------------------

LOCAL_AI_INSTALLS = {
    "Ollama": ["%LOCALAPPDATA%/Programs/Ollama/ollama.exe", "%ProgramFiles%/Ollama/ollama.exe"],
    "LM Studio": ["%LOCALAPPDATA%/Programs/LM Studio/LM Studio.exe", "%LOCALAPPDATA%/LM-Studio/LM Studio.exe"],
    "Jan": ["%LOCALAPPDATA%/Programs/Jan/Jan.exe"],
}


def scan_local_ai(paths: SystemPaths, cfg: dict, detector=None) -> list[Finding]:
    """A model server running on this PC (the brain without a Claude key), or one installed but not running."""
    from .brain import local_llm

    lcfg = (cfg.get("brain") or {}).get("local") or {}
    if not lcfg.get("enabled", True):
        return [Finding("ai", "Local AI", FOUND, "Turned off in config (brain.local.enabled: false).")]
    found = (detector or local_llm.detect)(extra_url=lcfg.get("url") or None)
    if found and found["models"]:
        # the same choice the brain makes: your pin, else the model test's winner on this PC, else the strongest
        # that fits the card the test measured
        pick = local_llm.LocalBrain.for_report(lcfg, getattr(cfg, "data_dir", None)).choose(found["models"])
        names = [m["name"] for m in found["models"]]
        label = local_llm.LABELS.get(found["kind"], found["kind"])
        return [Finding("ai", f"Local AI ({label})", CONNECTED,
                        f"{len(names)} model{'s' if len(names) != 1 else ''}: {', '.join(names[:5])}"
                        f"{'…' if len(names) > 5 else ''}. Answers with {pick}.")]
    if found:
        return [Finding("ai", "Local AI", ACTION, f"{local_llm.LABELS.get(found['kind'], found['kind'])} is running "
                        "with no models.", "Download one, e.g. in a terminal: ollama pull llama3.1:8b")]
    installed = [name for name, cands in LOCAL_AI_INSTALLS.items()
                 if paths.first_existing(*(paths.expand(c) for c in cands))]
    if installed:
        return [Finding("ai", "Local AI", ACTION, f"{installed[0]} is installed but not running.",
                        f"Start {installed[0]}; Vesper finds it by itself within a minute.")]
    return [Finding("ai", "Local AI", MISSING, "No local model server found (Ollama, LM Studio, Jan, llama.cpp).",
                    "Optional: install Ollama (ollama.com) and run: ollama pull llama3.1:8b")]


def scan_accounts(cfg: dict, calendar_links: list[str], local_ai: bool = False) -> list[Finding]:
    env = os.environ
    out = []
    key = env.get(cfg["claude"].get("api_key_env", "ANTHROPIC_API_KEY"), "")
    if key:
        out.append(Finding("ai", "Claude API", CONNECTED, "API key set"))
    elif local_ai:  # a local model already answers open questions: the key is an upgrade, not a gap
        out.append(Finding("ai", "Claude API", FOUND, "No key: your local model answers instead.",
                           "Optional: add ANTHROPIC_API_KEY to .env for stronger plans and background research."))
    else:
        out.append(Finding("ai", "Claude API", ACTION, "No key and no local model: only the built-in commands work.",
                           "Add ANTHROPIC_API_KEY to .env (console.anthropic.com → API keys), or run a local model."))
    cals = [c for c in cfg.get("calendars") or [] if c.get("url") or env.get(c.get("url_env") or "", "")]
    hint = " You use Google/Outlook calendar in the browser — each calendar has a private iCal link." if calendar_links and not cals else ""
    out.append(Finding("calendars", "Calendars", CONNECTED if cals else ACTION, f"{len(cals)} connected.{hint}",
                       "" if cals else "Google Calendar → Settings → each calendar → Integrate calendar → "
                       "Secret address in iCal format → paste into .env (CAL_WORK_ICS …)."))
    tw = cfg.get("twitch") or {}
    if tw.get("enabled") and env.get(tw.get("client_id_env", ""), ""):
        out.append(Finding("streaming", "Twitch stats", CONNECTED, f"Channel {tw.get('channel')}"))
    gh = cfg["projects"]["github"]
    token = env.get(gh.get("token_env", "GITHUB_TOKEN"), "")
    out.append(Finding("projects", "GitHub API", CONNECTED if token else FOUND,
                       "Token set (private repos + PRs)" if token else "Public data only.",
                       "" if token else "Optional: add a fine-grained GITHUB_TOKEN (read-only) to .env for private repos and PRs."))
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def discover(cfg: dict, paths: SystemPaths | None = None, shortcut_index: dict[str, Path] | None = None) -> dict:
    paths = paths or SystemPaths()
    started = time.time()
    if shortcut_index is None:
        from .integrations.desktop import AppLauncher

        shortcut_index = AppLauncher().shortcut_index()
    findings: list[Finding] = []
    suggested: dict = {}

    def run(label, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # one broken scanner must not sink the rest
            log.exception("discovery: %s failed", label)
            findings.append(Finding("system", label, ACTION, f"Scanner error: {type(exc).__name__}: {exc}"))
            return None

    res = run("apps", scan_apps, paths, shortcut_index)
    apps: dict = {}
    if res:
        f, apps, by_area = res
        findings += f
        suggested["apps"] = dict(apps)
        # Detected work/stream apps sharpen activity categorisation.
        prof_apps: dict[str, list[str]] = {}
        for area, names in by_area.items():
            prof = APP_PROFILE_HINTS.get(area)
            if prof:
                prof_apps.setdefault(prof, []).extend(KNOWN_APPS[n]["process"] for n in names)
        if prof_apps:
            suggested["profiles"] = {p: {"apps": sorted(set(a))} for p, a in prof_apps.items()}
    obs_info: dict = {}
    res = run("OBS", scan_obs, paths)
    if res:
        f, s, obs_info = res
        findings += f
        _merge(suggested, s)
    res = run("games", scan_games, paths)
    if res:
        f, games = res
        findings += f
        for name, spec in games.items():
            suggested.setdefault("apps", {}).setdefault(name, spec)
    calendar_links: list[str] = []
    res = run("browsers", scan_browsers, paths)
    if res:
        f, sites, calendar_links = res
        findings += f
        if sites:
            suggested["sites"] = sites
    res = run("code", scan_code, paths, cfg["projects"].get("claude_dir", "~/.claude"))
    if res:
        f, s = res
        findings += f
        _merge(suggested, s)
    if cfg["voice"].get("enabled", True):
        findings += run("audio", scan_audio) or []
    else:
        findings.append(Finding("voice", "Voice", FOUND, "Turned off in config (voice.enabled: false)."))
    res = run("GPU", scan_gpu)
    if res:
        f, s = res
        findings += f
        _merge(suggested, s)
    local = run("local AI", scan_local_ai, paths, cfg) or []
    findings += local
    findings += run("accounts", scan_accounts, cfg, calendar_links, any(f.status == CONNECTED for f in local)) or []
    order = {ACTION: 0, MISSING: 1, FOUND: 2, CONNECTED: 3}
    findings.sort(key=lambda x: (order.get(x.status, 9), x.area, x.name))
    return {
        "scanned_at": time.time(),
        "duration_s": round(time.time() - started, 2),
        "platform": sys.platform,
        "findings": [asdict(f) for f in findings],
        "summary": dict(Counter(f.status for f in findings)),
        "obs": {k: v for k, v in obs_info.items() if k != "password"},
        "suggested": suggested,
    }


def _merge(into: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            _merge(into[k], v)
        else:
            into[k] = v


def write_discovered(result: dict, root: Path, data_dir: Path) -> Path:
    path = root / DISCOVERED_FILE
    header = ("# Generated by the assistant's PC scan on "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(result['scanned_at']))}. Rewritten on every scan.\n"
              "# It sits UNDER config.yaml: anything you set there wins. Copy an entry into config.yaml to change it.\n")
    path.write_text(header + yaml.safe_dump(result["suggested"], sort_keys=True, allow_unicode=True), encoding="utf-8")
    data_dir.mkdir(parents=True, exist_ok=True)
    report = {k: v for k, v in result.items() if k != "suggested"}
    (data_dir / "discovery.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return path


def load_report(data_dir: Path) -> dict | None:
    try:
        return json.loads((data_dir / "discovery.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_stale(root: Path, max_age_s: float = STALE_AFTER_S) -> bool:
    path = root / DISCOVERED_FILE
    return not path.exists() or time.time() - path.stat().st_mtime > max_age_s
