"""The pre-stream check: run it by voice ("am I ready to stream?") or automatically
before a stream on your calendar. Each item is one of good / warning / critical,
with the fix in the same sentence.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

MIN_FREE_GB = 20  # an hour of 1080p60 recording is roughly 10-20 GB


def _item(name: str, level: str, detail: str) -> dict:
    return {"name": name, "level": level, "detail": detail}


def run_checklist(svc, mic=None, system_snapshot: dict | None = None) -> dict:
    """``mic``: the MicWatch (live levels from OBS), if the event connection is up."""
    items: list[dict] = []
    obs = svc.obs
    st = obs.status() if obs.enabled else {"connected": False, "enabled": False}
    if not st.get("enabled", True):
        items.append(_item("OBS", "critical", "OBS control is turned off in config.yaml (obs.enabled)."))
    elif not st.get("connected"):
        items.append(_item("OBS", "critical", st.get("error") or "OBS isn't running or its WebSocket server is off."))
    else:
        items.append(_item("OBS", "good", f"connected, on {st.get('current_scene')}"))
        start_scene = (svc.cfg["profiles"].get("stream", {}).get("launch") or {}).get("obs_scene")
        if start_scene:
            found = obs.match_scene(start_scene)
            items.append(_item("Start scene", "good" if found else "warning",
                               f"{found} is ready" if found else f"No scene called {start_scene}; check profiles.stream.launch.obs_scene."))
        audio = st.get("audio") or []
        mic_name = mic.name if mic and mic.name else next(
            (a["name"] for a in audio if a["name"].lower() == "mic/aux" or "mic" in a["name"].lower()), None)
        mic_state = next((a for a in audio if a["name"] == mic_name), None)
        if mic_state is None:
            items.append(_item("Mic", "warning", "I can't find a mic input in OBS. Set obs.mic_source in config.yaml."))
        elif mic_state.get("muted"):
            items.append(_item("Mic", "critical", f"{mic_name} is muted in OBS."))
        else:
            level = mic.snapshot().get("peak_db") if mic else None
            items.append(_item("Mic", "good", f"{mic_name} is live" + (f", peaking at {level} dB" if level is not None else "")))
        replay = obs.replay_buffer_active()
        items.append(_item("Replay buffer", "good" if replay else "warning",
                           "running, so “clip that” works" if replay else
                           "off, so “clip that” can't save anything. Say “start replay buffer”."
                           if replay is False else "not set up in OBS (Settings → Output → Replay Buffer)."))
        folder = obs.record_directory()
        if folder:
            try:
                free = shutil.disk_usage(Path(folder)).free / 1e9
                items.append(_item("Recording space", "good" if free >= MIN_FREE_GB else "warning",
                                   f"{free:.0f} GB free in {folder}" + ("" if free >= MIN_FREE_GB else
                                                                         f"; under {MIN_FREE_GB} GB, recordings may stop")))
            except OSError:
                pass
        if st.get("streaming", {}).get("active"):
            items.append(_item("Stream", "info", "already live"))

    snap = system_snapshot
    if snap:
        cpu = (snap.get("cpu") or {}).get("percent")
        gpus = snap.get("gpus") or []
        gpu = max((g.get("util") or 0 for g in gpus), default=None)
        busy = [f"CPU {cpu:.0f}%" if cpu is not None and cpu >= 80 else "", f"GPU {gpu:.0f}%" if gpu is not None and gpu >= 85 else ""]
        busy = [b for b in busy if b]
        items.append(_item("PC load", "warning" if busy else "good",
                           f"already busy ({', '.join(busy)}): close what you don't need" if busy else
                           f"CPU {cpu:.0f}%" + (f", GPU {gpu:.0f}%" if gpu is not None else "") if cpu is not None else "fine"))

    tw = svc.twitch
    if getattr(tw, "enabled", False):
        info = tw.channel_info()
        if info.get("error"):
            items.append(_item("Twitch", "warning", f"couldn't read the channel: {info['error']}"))
        else:
            items.append(_item("Twitch title", "good" if info.get("title") else "warning",
                               f"“{info.get('title')}” in {info.get('game') or 'no category'}" if info.get("title")
                               else "No stream title set."))

    worst = "critical" if any(i["level"] == "critical" for i in items) else \
        "warning" if any(i["level"] == "warning" for i in items) else "good"
    good = sum(1 for i in items if i["level"] == "good")
    checked = sum(1 for i in items if i["level"] != "info")
    problems = [i for i in items if i["level"] in ("critical", "warning")]
    if not problems:
        spoken = f"Pre-stream check: all {checked} good. You're ready."
    else:
        fixes = " ".join(f"{p['name']}: {p['detail']}" for p in problems[:3])
        spoken = f"Pre-stream check: {good} of {checked} good. {fixes}"
    return {"ok": True, "level": worst, "items": items, "spoken": spoken, "ts": datetime.now().timestamp()}


def next_stream_event(events: list[dict], now: datetime, lead_minutes: int, seen: set) -> dict | None:
    """The stream-calendar event starting within ``lead_minutes`` that hasn't been checked yet."""
    horizon = now + timedelta(minutes=lead_minutes)
    for ev in events:
        if ev.get("all_day") or ev.get("profile") != "stream":
            continue
        try:
            start = datetime.fromisoformat(ev["start"])
        except (KeyError, ValueError):
            continue
        key = f"{ev.get('title')}@{ev['start']}"
        if now <= start <= horizon and key not in seen:
            seen.add(key)
            return ev
    return None
