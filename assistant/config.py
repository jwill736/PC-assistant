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
        "name": "Vesper",
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
    # Which model answers open questions. auto: Claude when ANTHROPIC_API_KEY is set, else a model running on
    # this PC (Ollama, LM Studio, llama.cpp, Jan: found on their default ports). local: never Claude.
    "brain": {
        "provider": "auto",
        "local": {
            "enabled": True,
            "url": "",        # only for a non-default server, e.g. http://127.0.0.1:11500
            "model": "",      # empty = automatic (see stream_model below); a name pins that model
            "temperature": 0.3,
            # Small models choose better from a short list; Claude always gets every tool.
            "tools": ["open_app", "close_app", "focus_window", "web_search", "set_volume", "media_control",
                      "obs_status", "obs_switch_scene", "obs_set_mute", "calendar", "list_tasks", "add_task",
                      "complete_task", "remember", "recall", "news", "activity", "projects", "system_status",
                      "prestream_check", "twitch_status", "twitch_marker", "twitch_clip", "free_model_memory",
                      "search_library", "read_document", "open_document"],
            # Which model: "" = the strongest that passed the test on this PC (Setup → Brain → Test my models),
            # else the strongest that fits the graphics card. While you're live: stream_model ("auto" = the
            # smallest that passed; "same" = don't switch), and the big one is unloaded.
            "stream_model": "auto",
            "auto_test": True,     # test your models once by itself, and again when the list changes
            "context": 8192,       # Ollama context size: room for the instructions, the tool list and the chat
            "keep_alive": "15m",   # how long Ollama keeps the model loaded after a question
        },
    },
    "voice": {
        "enabled": True,
        # Speech-to-text: auto | parakeet | parakeet-large | moonshine | whisper (see voice/stt.py)
        "stt_engine": "auto",
        "stt_threads": 2,
        "stt_model": "base.en",  # whisper engine only
        "stt_device": "auto",    # whisper engine only: auto | cpu | cuda
        "input_device": None,
        "push_to_talk_hotkey": "ctrl+alt+j",
        "follow_up_seconds": 8,
        "speak_typed": False,  # also speak replies to commands typed in the dashboard
        # After voice calibration: off | log (score only) | strict (ignore voices that aren't yours)
        "speaker_check": "strict",
        # Voice detection: auto | silero | energy. Silero ends an utterance after endpoint_ms of silence.
        "vad": "auto",
        "vad_threshold": 0.6,
        "endpoint_ms": 400,
        "min_rms": 350,          # energy gate only
        "silence_ms": 800,       # energy gate only
        "max_utterance_s": 15,
        "corrections": {},       # {"vrb": "BRB"}: fix words the recogniser keeps getting wrong
        # Trained wake words (data/models/wakewords/*.onnx, see training/wake_words.ipynb)
        "wake_mode": "auto",     # auto | acoustic | hybrid | transcript
        "wake_threshold": 0.5,
        "hard_triggers": {},     # {"clip_that": "save the replay"}: trained phrase -> command
        "barge_in": True,        # "stop" / the name interrupts a spoken reply
        # Speech output: auto (= supertonic) | supertonic | kokoro | pyttsx3 (Windows SAPI) | browser | none
        "tts": {"engine": "auto", "voice": "", "speed": 1.0, "threads": 2, "output_device": None,
                "stream": True,  # speak Claude's reply sentence by sentence as it streams in
                "rate": 190, "voice_hint": ""},  # rate / voice_hint: pyttsx3 and browser only
    },
    # PC control safety (see brain/policy.py): kill switch, per-request budget, toast confirmations
    "pc_control": {"kill_hotkey": "ctrl+alt+k", "max_steps": 25, "max_failures": 3, "toast_confirm": True},
    "goals": {"north_star": "", "this_week": []},
    # The second brain's library: your documents, read on this PC and searchable by voice (see library.py)
    "library": {
        "enabled": True,
        "folders": [],         # empty = the usual places: Documents, Desktop, OneDrive, Google Drive, Dropbox
        "whole_pc": False,     # also every drive in this PC, ranked after your folders (programs and games skipped)
        "exclude": [],         # patterns to leave out, e.g. "*/Archive/*"
        "max_file_mb": 25,
        "max_files": 20000,
        "refresh_minutes": 30,  # look for new and changed files this often (never while you're live)
    },
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
        # While live: alerts for dropped frames (by cause), reconnects and a mic that isn't reaching the stream
        "health_alerts": True,
        "speak_alerts": True,     # say them too (route voice.tts.output_device to headphones to keep them off stream)
        "mic_source": "",         # your mic's OBS input; empty = "Mic/Aux" or the first input with "mic" in its name
        "prestream_minutes": 15,  # run the pre-stream check this long before a stream on your calendar; 0 = off
    },
    "twitch": {
        "enabled": True,          # does nothing until TWITCH_CLIENT_ID is in .env
        "channel": "",            # empty = the account you log in with
        "client_id_env": "TWITCH_CLIENT_ID",
        "client_secret_env": "TWITCH_CLIENT_SECRET",  # only for a Confidential app; a Public one needs none
        "events": True,           # live follows, subs, raids, cheers and chat (EventSub)
        # Said out loud. Only names and numbers are ever spoken, never what a viewer wrote.
        "callouts": {"raid": True, "sub": True, "gift": True, "cheer_min": 100, "follow": False,
                     "redemption": False, "hype_train": True},
        "auto_markers": True,     # drop a stream marker at chat spikes, raids, hype trains and "clip that"
        "spike_ratio": 3.0,       # chat this many times faster than its recent pace is a highlight
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


def goal_lines(goals: dict | None) -> list[str]:
    """goals.north_star as a list: one goal per area ("Streaming: …", "Work: …") or a single line."""
    value = (goals or {}).get("north_star")
    items = value if isinstance(value, list) else [value]
    return [str(g).strip() for g in items if g and str(g).strip()]


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
SETTINGS_FILE = "settings.yaml"        # in data_dir, choices made in the HUD / --bench-voice --apply


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def save_setting(cfg: Config, dotted: str, value: Any) -> None:
    """Remember a choice made in the HUD: data/settings.yaml, which wins over config.yaml."""
    path = cfg.data_dir / SETTINGS_FILE
    settings = _read_yaml(path)
    node, target = settings, cfg
    *parents, leaf = dotted.split(".")
    for part in parents:
        node = node.setdefault(part, {})
        target = target.setdefault(part, {})
    node[leaf] = value
    target[leaf] = value
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# Written by the assistant when you change a setting in the HUD. Delete a line to fall back\n"
                   "# to config.yaml.\n" + yaml.safe_dump(settings, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


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
    data_path = data_dir if data_dir.is_absolute() else root / data_dir
    calibration = _read_yaml(data_path / CALIBRATION_FILE)
    # Choices made in the HUD (voice, speaker check) or by --bench-voice --apply sit *above*
    # config.yaml: they're newer than the file, and config.example.yaml spells most of them out.
    settings = _read_yaml(data_path / SETTINGS_FILE)
    merged = deep_merge(deep_merge(DEFAULTS, discovered), calibration)
    merged = Config(deep_merge(deep_merge(merged, user), settings))
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
