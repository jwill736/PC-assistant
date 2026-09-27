"""Configuration loading.

Settings live in ``config.yaml`` (copied from ``config.example.yaml``); secrets
(API keys, OBS password, calendar tokens) live in ``.env`` or the real
environment and are referenced from the YAML by variable name, so the YAML can
be shared without leaking anything.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "assistant": {
        "name": "Jarvis",
        "wake_words": [],  # the name and "hey <name>" are always included
        "user_name": "boss",
        "timezone": None,  # None = system local time
        "work_hours": {"start": "09:00", "end": "18:00"},
    },
    "server": {"host": "127.0.0.1", "port": 8765, "open_window": True},
    "tray": {"enabled": True},
    "macros": {},
    "data_dir": "data",
    "claude": {
        "enabled": True,
        "model": "claude-opus-5",
        "api_key_env": "ANTHROPIC_API_KEY",
        # Voice commands want speed; briefings want depth.
        "command_effort": "low",
        "briefing_effort": "high",
        "history_turns": 12,
        "history_idle_minutes": 10,
        "server_fallbacks": True,
    },
    "voice": {
        "enabled": True,
        "stt_model": "base.en",
        "stt_device": "auto",  # auto | cpu | cuda
        "input_device": None,
        "push_to_talk_hotkey": "ctrl+alt+j",
        "follow_up_seconds": 8,
        "speak_typed": False,  # also speak replies to commands typed in the dashboard
        # After voice calibration: off | log (score only) | strict (ignore voices that aren't yours)
        "speaker_check": "strict",
        "min_rms": 350,
        "silence_ms": 800,
        "max_utterance_s": 15,
        "tts": {"engine": "pyttsx3", "rate": 190, "voice_hint": ""},
    },
    "goals": {"north_star": "", "this_week": []},
    "profiles": {
        "work": {
            "label": "Work",
            "apps": [],
            "title_keywords": [],
            "launch": {"apps": [], "urls": [], "obs_scene": None},
            "close_apps": [],
        },
        "stream": {
            "label": "Stream",
            "apps": ["obs64", "obs", "streamlabs"],
            "title_keywords": ["twitch.tv", "kick.com", "youtube studio"],
            "launch": {"apps": [], "urls": [], "obs_scene": None},
            "close_apps": [],
        },
    },
    "apps": {},
    "sites": {
        "youtube": "https://www.youtube.com",
        "gmail": "https://mail.google.com",
        "calendar": "https://calendar.google.com",
        "github": "https://github.com",
        "twitch": "https://www.twitch.tv",
        "claude": "https://claude.ai",
    },
    "calendars": [],
    "news": {"feeds": [], "refresh_minutes": 15, "max_items": 40},
    "projects": {
        "scan_dirs": [],
        "claude_dir": "~/.claude",
        "github": {"user": "", "token_env": "GITHUB_TOKEN"},
    },
    "obs": {
        "enabled": True,
        "host": "localhost",
        "port": 4455,
        "password_env": "OBS_PASSWORD",
        "scene_aliases": {},
    },
    "twitch": {
        "enabled": False,
        "channel": "",
        "client_id_env": "TWITCH_CLIENT_ID",
        "client_secret_env": "TWITCH_CLIENT_SECRET",
    },
    "tracking": {"enabled": True, "sample_seconds": 5, "idle_seconds": 300},
    "optimizer": {"protected_processes": [], "heavy_process_mb": 1500},
    "jobs": {
        "claude_code_cmd": "claude",
        "permission_mode": "acceptEdits",
        "max_concurrent": 2,
        "timeout_minutes": 30,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """Return ``base`` updated recursively with ``override`` (neither is mutated)."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_env_file(path: Path) -> None:
    """Minimal ``.env`` reader: KEY=VALUE lines; real env vars win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


class Config(dict):
    """The merged settings dict plus a couple of helpers."""

    root: Path = ROOT
    path: Path | None = None  # the config.yaml this was loaded from (may not exist)

    def get_path(self, dotted: str, default: Any = None) -> Any:
        node: Any = self
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def secret(self, env_name: str | None) -> str:
        return os.environ.get(env_name or "", "") if env_name else ""

    @property
    def data_dir(self) -> Path:
        path = Path(self["data_dir"]).expanduser()
        if not path.is_absolute():
            path = self.root / path
        path.mkdir(parents=True, exist_ok=True)
        return path


DISCOVERED_FILE = "config.discovered.yaml"
CALIBRATION_FILE = "calibration.yaml"  # in data_dir, written by voice calibration


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def _union(*lists) -> list:
    seen, out = set(), []
    for items in lists:
        for item in items or []:
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def load_config(path: str | os.PathLike | None = None) -> Config:
    load_env_file(ROOT / ".env")
    cfg_path = Path(path) if path else Path(os.environ.get("ASSISTANT_CONFIG", ROOT / "config.yaml"))
    root = cfg_path.parent.resolve()  # even before config.yaml exists (first run), scan output lives beside it
    user = _read_yaml(cfg_path)
    # Machine-written layers sit between the defaults and the user's own config:
    # what the PC scan found, then what voice calibration measured.
    discovered = _read_yaml(root / DISCOVERED_FILE)
    data_dir = Path(user.get("data_dir") or DEFAULTS["data_dir"]).expanduser()
    calibration = _read_yaml((data_dir if data_dir.is_absolute() else root / data_dir) / CALIBRATION_FILE)
    merged = Config(deep_merge(deep_merge(deep_merge(DEFAULTS, discovered), calibration), user))
    merged.root = root
    merged.path = cfg_path
    # An app/site the user defines replaces the scanned one outright; blending a
    # scanned process name into a hand-written entry could close the wrong program.
    for section in ("apps", "sites"):
        for name, spec in (user.get(section) or {}).items():
            merged[section][name] = copy.deepcopy(spec)
    # Lists where "both" is the right answer: found repo folders plus the user's,
    # detected work/stream apps plus the user's.
    merged["projects"]["scan_dirs"] = _union(user.get("projects", {}).get("scan_dirs"),
                                             discovered.get("projects", {}).get("scan_dirs"))
    for name, prof in merged["profiles"].items():
        prof["apps"] = _union((user.get("profiles") or {}).get(name, {}).get("apps"),
                              (discovered.get("profiles") or {}).get(name, {}).get("apps"),
                              DEFAULTS["profiles"].get(name, {}).get("apps"))
    # Wake words: the name, "hey <name>", the user's extras and every spelling
    # calibration heard Whisper use for this voice.
    name = str(merged["assistant"]["name"]).lower()
    merged["assistant"]["wake_words"] = [w.lower() for w in _union(
        [name, f"hey {name}"], user.get("assistant", {}).get("wake_words"),
        calibration.get("assistant", {}).get("wake_words"))]
    return merged
