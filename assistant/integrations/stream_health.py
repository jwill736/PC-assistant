"""Stream health while live: dropped frames by cause, reconnects, and a mic that isn't reaching the stream.

OBS reports three different kinds of lost frames, and each has a different fix:

========  ==============================  =============================================
class     OBS counter                     what to do
========  ==============================  =============================================
network   stream output skipped frames    lower the bitrate 200-500 kbps (Twitch's advice)
encoder   stats outputSkippedFrames       GPU encoder (NVENC), faster preset, lower res
render    stats renderSkippedFrames       the GPU is maxed: cap the game's FPS
========  ==============================  =============================================

Totals only ever grow, so rates are computed over the last minute, not since the
stream started. Thresholds follow the research: up to 0.1% is invisible, above
0.5% shows artifacts, above 2% degrades noticeably.

The mic check avoids the obvious false alarm: with a noise gate, an OBS mic is
*supposed* to read silence between sentences. So it only speaks up when the
assistant's own microphone hears you talking while OBS's mic stays silent or
is muted.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

WARN_PCT, CRIT_PCT, CLEAR_PCT = 0.5, 2.0, 0.1
CLASSES = ("network", "encoder", "render")
ADVICE = {
    "network": "Your upload can't keep up: lower the bitrate by 200 to 500 kbps{bitrate}.",
    "encoder": "The encoder is overloaded: switch to the GPU encoder (NVENC), a faster preset, or a lower output resolution.",
    "render": "OBS can't draw frames in time, so the GPU is maxed: cap the game's frame rate or close heavy browser sources.",
}
LABEL = {"network": "Dropped frames", "encoder": "Encoder skipping frames", "render": "Frames missed while rendering"}


@dataclass
class Alert:
    kind: str       # network | encoder | render | reconnecting | reconnected | offline | obs_lost | mic_* | recovered
    level: str      # critical | warning | good | info
    text: str       # spoken and shown
    speak: bool = True
    ts: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


def level_for(pct: float | None) -> str:
    if pct is None:
        return "unknown"
    if pct > CRIT_PCT:
        return "critical"
    if pct > WARN_PCT:
        return "warning"
    if pct > CLEAR_PCT:
        return "notice"
    return "ok"


class StreamHealth:
    """Fed the OBS status every few seconds; returns the alerts worth saying."""

    def __init__(self, window_s: float = 60, min_frames: int = 300, repeat_s: float = 180, clock=time.time):
        self.window_s, self.min_frames, self.repeat_s = window_s, min_frames, repeat_s
        self.clock = clock
        self.samples: deque = deque()
        self.levels = {c: "unknown" for c in CLASSES}
        self.rates: dict[str, float | None] = {c: None for c in CLASSES}
        self._last_alert: dict[str, float] = {}
        self._clear_since: dict[str, float] = {}
        self.was_live = False
        self.was_reconnecting = False
        self.was_connected = True
        self.live_since: float | None = None
        self.expected_stop_at = 0.0  # set when the assistant itself ends the stream
        self.recent: deque = deque(maxlen=20)

    # ---- the one entry point ----------------------------------------------
    def update(self, status: dict) -> list[Alert]:
        now = self.clock()
        alerts: list[Alert] = []
        connected = bool(status.get("connected"))
        stream = status.get("streaming") or {}
        live = connected and bool(stream.get("active"))

        if self.was_live and not connected and self.was_connected:
            alerts.append(Alert("obs_lost", "critical", "I lost the connection to OBS while you were live. Check that OBS is still running."))
        elif self.was_live and connected and not live:
            mins = round((now - (self.live_since or now)) / 60)
            unexpected = now - self.expected_stop_at > 60
            alerts.append(Alert("offline", "critical" if unexpected else "info",
                                ("The stream just went offline" if unexpected else "Stream ended")
                                + (f" after {_duration(mins)}." if mins else "."), speak=unexpected))
        if live and not self.was_live:
            self.live_since, self.samples = now, deque()
            self.levels = {c: "unknown" for c in CLASSES}
        reconnecting = live and bool(stream.get("reconnecting"))
        if reconnecting and not self.was_reconnecting:
            alerts.append(Alert("reconnecting", "critical", "The stream is reconnecting: your internet connection to Twitch dropped."))
        elif self.was_reconnecting and live and not reconnecting:
            alerts.append(Alert("reconnected", "good", "The stream reconnected."))
        self.was_live, self.was_reconnecting, self.was_connected = live, reconnecting, connected

        if live:
            stats = status.get("stats") or {}
            self.samples.append((now, _num(stream.get("dropped_frames")), _num(stream.get("total_frames")),
                                 _num(stats.get("encoder_skipped")), _num(stats.get("encoder_total")),
                                 _num(stats.get("render_skipped")), _num(stats.get("render_total"))))
            while self.samples and now - self.samples[0][0] > self.window_s:
                self.samples.popleft()
            alerts += self._rate_alerts(now, stream.get("kbps"))
        for a in alerts:
            a.ts = now
            self.recent.appendleft(a.as_dict())
        return alerts

    def _rate_alerts(self, now: float, kbps) -> list[Alert]:
        if len(self.samples) < 2:
            return []
        first, last = self.samples[0], self.samples[-1]
        out = []
        for i, cls in enumerate(CLASSES):
            d_bad, d_total = last[1 + 2 * i] - first[1 + 2 * i], last[2 + 2 * i] - first[2 + 2 * i]
            if d_total < self.min_frames or d_bad < 0:  # too little data, or OBS reset its counters
                continue
            pct = round(d_bad / d_total * 100, 2)
            self.rates[cls] = pct
            new, old = level_for(pct), self.levels[cls]
            self.levels[cls] = new
            if new in ("warning", "critical"):
                self._clear_since.pop(cls, None)
                escalated = new == "critical" and old != "critical"
                if escalated or new != old and old not in ("warning", "critical") \
                        or now - self._last_alert.get(cls, -1e9) >= self.repeat_s:
                    self._last_alert[cls] = now
                    bitrate = f" (you're sending {kbps} kbps now)" if cls == "network" and kbps else ""
                    out.append(Alert(cls, new, f"{LABEL[cls]}: {pct}% over the last minute. "
                                                + ADVICE[cls].format(bitrate=bitrate)))
            elif cls in self._last_alert and new == "ok":
                since = self._clear_since.setdefault(cls, now)
                if now - since >= self.window_s:  # stayed clean for a whole window
                    del self._last_alert[cls]
                    self._clear_since.pop(cls, None)
                    out.append(Alert("recovered", "good", f"{LABEL[cls]} has cleared up."))
        return out

    def note_commanded_stop(self) -> None:
        self.expected_stop_at = self.clock()

    def snapshot(self) -> dict:
        return {"live": self.was_live, "reconnecting": self.was_reconnecting, "window_s": self.window_s,
                "classes": {c: {"pct": self.rates[c], "level": self.levels[c]} for c in CLASSES},
                "alerts": list(self.recent)}


class MicWatch:
    """Levels from OBS's InputVolumeMeters event (every 50 ms) for the mic input, cross-checked against
    what the assistant's own microphone heard."""

    SILENT_DB = -55.0   # below this for the whole window: nothing is reaching OBS
    CLIP_DB = -1.0      # a peak this close to 0 dBFS is clipping

    def __init__(self, source: str = "", window_s: float = 60, repeat_s: float = 300, clock=time.time):
        self.source = source.lower()
        self.window_s, self.repeat_s, self.clock = window_s, repeat_s, clock
        self.name: str | None = None
        self.peaks: deque = deque()   # (t, peak dB) at most one per 0.5 s
        self.clips: deque = deque()
        self._last_alert: dict[str, float] = {}
        self._lock = threading.Lock()  # OBS's event thread writes 20x a second; the poll thread reads

    def pick(self, names: list[str]) -> str | None:
        """The configured source, else OBS's default "Mic/Aux", else the first name with "mic" in it."""
        low = {n.lower(): n for n in names}
        if self.source:
            return low.get(self.source) or next((n for k, n in low.items() if self.source in k), None)
        return low.get("mic/aux") or next((n for k, n in low.items() if "mic" in k or "microphone" in k), None)

    def on_meters(self, inputs: list[dict]) -> None:
        """One InputVolumeMeters payload: [{"inputName": ..., "inputLevelsMul": [[mag, peak, input_peak], ...]}]."""
        if self.name is None:
            self.name = self.pick([i.get("inputName", "") for i in inputs])
        entry = next((i for i in inputs if i.get("inputName") == self.name), None)
        if entry is None:
            return
        channels = entry.get("inputLevelsMul") or []
        peak = max((ch[1] for ch in channels if len(ch) > 1), default=0.0)
        db = 20 * math.log10(peak) if peak > 0 else -100.0
        now = self.clock()
        with self._lock:
            if not self.peaks or now - self.peaks[-1][0] >= 0.5:
                self.peaks.append((now, db))
            elif db > self.peaks[-1][1]:
                self.peaks[-1] = (self.peaks[-1][0], db)
            if db >= self.CLIP_DB:
                self.clips.append(now)
            while self.peaks and now - self.peaks[0][0] > self.window_s:
                self.peaks.popleft()
            while self.clips and now - self.clips[0] > 10:
                self.clips.popleft()

    def loudest(self) -> float | None:
        with self._lock:
            return max((db for _, db in self.peaks), default=None)

    def _coverage(self) -> float:
        with self._lock:
            return self.clock() - self.peaks[0][0] if self.peaks else 0.0

    def _clip_count(self) -> int:
        with self._lock:
            return len(self.clips)

    def check(self, live: bool, muted: bool | None, you_spoke: int) -> list[Alert]:
        """``you_spoke``: utterances the assistant's own mic heard in the last window."""
        if not live or self.name is None:
            return []
        now = self.clock()
        out = []
        covered = self._coverage() >= self.window_s * 0.8
        if you_spoke >= 2 and muted:
            out.append(("mic_muted", "critical", f"You're talking, but {self.name} is muted in OBS."))
        elif you_spoke >= 2 and covered and (self.loudest() or -100) < self.SILENT_DB:
            out.append(("mic_silent", "critical", f"You're talking, but {self.name} isn't picking anything up in OBS. "
                                                  "Check the device in its properties."))
        if self._clip_count() >= 5:
            out.append(("mic_clipping", "warning", f"{self.name} is clipping. Turn its gain down a little."))
        alerts = []
        for kind, level, text in out:
            if now - self._last_alert.get(kind, -1e9) >= self.repeat_s:
                self._last_alert[kind] = now
                alerts.append(Alert(kind, level, text, ts=now))
        return alerts

    def snapshot(self) -> dict:
        loud = self.loudest()
        return {"source": self.name, "peak_db": round(loud, 1) if loud is not None else None,
                "clipping": self._clip_count() >= 5}


def _num(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _duration(mins: int) -> str:
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m" if h else f"{m} minute{'s' if m != 1 else ''}"
