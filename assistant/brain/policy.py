"""What the assistant may do on its own, and a record of everything it did.

Every action has a risk tier (from the research's PC-control plan):

====  ==========================================================  =================================
tier  covers                                                      confirmation
====  ==========================================================  =================================
T0    read-only: status, calendar, where am I                     none
T1    reversible: volume, brightness, open apps, switch scenes    none
T2    may lose work or change settings: close apps                a "yes" (voice, HUD or toast)
T3    irreversible or public: delete, shut down, go live/end      a "yes" per action, target read
      stream, run code                                            back; by voice, only in your voice
====  ==========================================================  =================================

``Guard`` is the kill switch plus the per-command step budget; ``AuditLog``
appends one JSON line per action to ``data/logs/actions-YYYY-MM.jsonl``.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

TIER_NAMES = {0: "read-only", 1: "reversible", 2: "needs a yes", 3: "needs a yes, every time"}
CONFIRM_FROM = 2


class Guard:
    """The kill switch ("hands off") and the step/failure budget for one command."""

    def __init__(self, max_steps: int = 25, max_failures: int = 3):
        self.max_steps, self.max_failures = max_steps, max_failures
        self._hands_off = threading.Event()
        self.reason = ""
        self.since = 0.0
        self.steps = 0
        self.failures = 0
        self.in_command = False  # the budget counts actions within one request, not HUD clicks between them

    @property
    def hands_off(self) -> bool:
        return self._hands_off.is_set()

    def stop(self, reason: str = "kill switch") -> None:
        self.reason, self.since = reason, time.time()
        self._hands_off.set()

    def resume(self) -> None:
        self.reason, self.since = "", 0.0
        self._hands_off.clear()

    def new_command(self) -> None:
        self.steps = self.failures = 0
        self.in_command = True

    def end_command(self) -> None:
        self.in_command = False

    @property
    def exhausted(self) -> bool:
        return self.in_command and (self.steps >= self.max_steps or self.failures >= self.max_failures)

    def check(self, tier: int) -> str | None:
        """None if the action may run now, else why not."""
        if tier >= 1 and self.hands_off:
            return "PC control is paused (kill switch). Say “resume control” or press Resume in the HUD."
        if self.in_command and self.steps >= self.max_steps:
            return f"Stopped: that's {self.max_steps} actions for one request."
        if self.in_command and self.failures >= self.max_failures:
            return f"Stopped after {self.failures} failed actions in a row."
        return None

    def record(self, ok: bool) -> None:
        if self.in_command:
            self.steps += 1
            self.failures = 0 if ok else self.failures + 1

    def status(self) -> dict:
        return {"hands_off": self.hands_off, "reason": self.reason, "since": self.since or None,
                "max_steps": self.max_steps, "max_failures": self.max_failures}


_SECRETISH = ("password", "token", "secret", "key")


def _clip(value, limit: int = 200):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {k: ("[redacted]" if any(s in k.lower() for s in _SECRETISH) else _clip(v, limit)) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit) for v in value[:20]]
    return value


class AuditLog:
    """Append-only JSON lines, one file per month; the last few hundred also kept in memory for the HUD."""

    def __init__(self, folder: Path | None, keep: int = 300, clock=time.time):
        self.folder = Path(folder) if folder else None
        self.recent: deque[dict] = deque(maxlen=keep)
        self.clock = clock
        self._lock = threading.Lock()
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=True)
            self._load_tail()

    def path(self, ts: float | None = None) -> Path | None:
        if not self.folder:
            return None
        return self.folder / f"actions-{time.strftime('%Y-%m', time.localtime(ts or self.clock()))}.jsonl"

    def write(self, **entry) -> dict:
        entry = {"ts": round(self.clock(), 3), **{k: _clip(v) for k, v in entry.items() if v is not None}}
        with self._lock:
            self.recent.append(entry)
            path = self.path(entry["ts"])
            if path:
                try:
                    with path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                except OSError:
                    pass  # the in-memory copy still shows in the HUD
        return entry

    def tail(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self.recent)[-limit:][::-1]

    def _load_tail(self) -> None:
        path = self.path()
        if not path or not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-self.recent.maxlen:]
        except OSError:
            return
        for line in lines:
            try:
                self.recent.append(json.loads(line))
            except ValueError:
                continue
