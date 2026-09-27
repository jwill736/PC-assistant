"""Where the day actually went.

Samples the foreground window every few seconds, labels it work / stream /
other / idle using the profiles in config.yaml, and stores merged segments in
SQLite. Recaps, focus-session detection and the dashboard timeline read from
here. Nothing leaves the machine.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta, tzinfo

from . import desktop
from ..storage import Storage

log = logging.getLogger(__name__)

IDLE = "idle"
OTHER = "other"


class Categorizer:
    def __init__(self, profiles: dict):
        self.rules = []
        for key, prof in (profiles or {}).items():
            self.rules.append((
                key,
                {desktop.normalize_app(a) for a in prof.get("apps") or []},
                [k.lower() for k in prof.get("title_keywords") or []],
            ))

    def __call__(self, app: str, title: str) -> str:
        app_n, title_l = desktop.normalize_app(app), (title or "").lower()
        if app_n == IDLE:
            return IDLE
        for key, _apps, keywords in self.rules:  # specific beats generic: titles first
            if any(k in title_l for k in keywords):
                return key
        for key, apps, _keywords in self.rules:
            if app_n in apps:
                return key
        return OTHER


class ActivityTracker:
    def __init__(self, storage: Storage, profiles: dict, sample_seconds: float = 5, idle_seconds: float = 300,
                 on_change=None):
        self.storage = storage
        self.categorize = Categorizer(profiles)
        self.sample = sample_seconds
        self.idle_threshold = idle_seconds
        self.on_change = on_change
        self.current: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- sampling -------------------------------------------------------
    def observe(self, app: str, title: str, now: float | None = None) -> None:
        now = now or time.time()
        category = self.categorize(app, title)
        with self._lock:
            cur = self.current
            same = cur and cur["app"] == app and cur["title"] == title
            fresh = cur and now - cur["end"] <= self.sample * 3
            if same and fresh and now - cur["start"] < 900:
                cur["end"] = now
                return
            if cur:
                if fresh:
                    cur["end"] = now  # close the old segment where the new one starts
                self._flush(cur)
            self.current = {"start": now, "end": now, "app": app, "title": title, "category": category}
        if self.on_change and not same:
            self.on_change(self.current)

    def _flush(self, seg: dict) -> None:
        if seg["end"] - seg["start"] >= 1:
            self.storage.add_activity(seg["start"], seg["end"], seg["app"], seg["title"], seg["category"])

    def tick(self) -> None:
        if desktop.idle_seconds() >= self.idle_threshold:
            self.observe(IDLE, "Away from keyboard")
            return
        win = desktop.active_window()
        if win is None or not win.title:
            self.observe(IDLE, "Locked / no window")
            return
        self.observe(win.app, win.title)

    def _run(self) -> None:
        while not self._stop.wait(self.sample):
            try:
                self.tick()
            except Exception:
                log.exception("activity sample failed")

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="activity", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self.current:
                self._flush(self.current)
                self.current = None

    # ---- reporting ------------------------------------------------------
    def segments(self, start: float, end: float) -> list[dict]:
        rows = self.storage.activity_between(start, end)
        with self._lock:
            if self.current and self.current["end"] > start:
                rows.append(dict(self.current))
        for r in rows:  # clip to the window
            r["start"], r["end"] = max(r["start"], start), min(r["end"], end)
        return [r for r in rows if r["end"] > r["start"]]

    def summary_for_day(self, day: datetime | None = None, tz: tzinfo | None = None) -> dict:
        tz = tz or datetime.now().astimezone().tzinfo
        base = (day or datetime.now(tz)).date()
        start = datetime.combine(base, dtime.min, tzinfo=tz)
        return summarize(self.segments(start.timestamp(), (start + timedelta(days=1)).timestamp()), base.isoformat())


def summarize(segments: list[dict], label: str = "") -> dict:
    by_cat: dict[str, float] = defaultdict(float)
    by_app: dict[tuple[str, str], float] = defaultdict(float)
    by_title: dict[tuple[str, str], float] = defaultdict(float)
    for s in segments:
        dur = s["end"] - s["start"]
        by_cat[s["category"]] += dur
        if s["category"] != IDLE:
            by_app[(s["app"], s["category"])] += dur
            by_title[(s["title"][:90], s["app"])] += dur
    active = [s for s in segments if s["category"] != IDLE]
    switches = sum(1 for a, b in zip(active, active[1:]) if a["app"] != b["app"])
    active_s = sum(s["end"] - s["start"] for s in active)
    return {
        "date": label,
        "active_seconds": round(active_s),
        "by_category": {k: round(v) for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])},
        "top_apps": [{"app": a, "category": c, "seconds": round(v)}
                     for (a, c), v in sorted(by_app.items(), key=lambda kv: -kv[1])[:10]],
        "top_titles": [{"title": t, "app": a, "seconds": round(v)}
                       for (t, a), v in sorted(by_title.items(), key=lambda kv: -kv[1])[:10]],
        "first_active": active[0]["start"] if active else None,
        "last_active": active[-1]["end"] if active else None,
        "context_switches": switches,
        "switches_per_hour": round(switches / (active_s / 3600), 1) if active_s > 600 else None,
        "focus_sessions": focus_sessions(segments),
        "timeline": timeline(segments),
    }


def focus_sessions(segments: list[dict], category: str = "work", min_minutes: int = 25, max_gap_s: int = 120) -> list[dict]:
    """Unbroken stretches in one category (short detours under ``max_gap_s`` allowed)."""
    sessions, cur = [], None
    for s in segments:
        if s["category"] == category:
            if cur and s["start"] - cur["end"] <= max_gap_s:
                cur["end"] = max(cur["end"], s["end"])
            else:
                if cur:
                    sessions.append(cur)
                cur = {"start": s["start"], "end": s["end"]}
        elif cur and s["end"] - s["start"] > max_gap_s:
            sessions.append(cur)
            cur = None
    if cur:
        sessions.append(cur)
    return [dict(x, minutes=round((x["end"] - x["start"]) / 60)) for x in sessions
            if x["end"] - x["start"] >= min_minutes * 60]


def timeline(segments: list[dict], min_block_s: int = 60) -> list[dict]:
    """Contiguous same-category blocks, with sub-minute blips folded into neighbours."""
    blocks: list[dict] = []
    for s in segments:
        if blocks and blocks[-1]["category"] == s["category"] and s["start"] - blocks[-1]["end"] < 120:
            blocks[-1]["end"] = max(blocks[-1]["end"], s["end"])
        elif blocks and s["end"] - s["start"] < min_block_s and s["start"] - blocks[-1]["end"] < 120:
            blocks[-1]["end"] = max(blocks[-1]["end"], s["end"])
        else:
            blocks.append({"start": s["start"], "end": s["end"], "category": s["category"]})
    return blocks
