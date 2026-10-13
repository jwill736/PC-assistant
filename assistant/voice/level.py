"""Audio level for the HUD's orb: a small ``level`` event, only while audio is live.

``{"src": "mic" | "tts", "rms": 0..1}`` at most ``MAX_HZ`` times a second, while you're talking to Vesper
(armed / hearing) or while it speaks. Never sticky, never kept in the bus history, never sent to in-process
listeners, and skipped entirely when no HUD is connected. It must never slow an audio thread: the time check
comes first, the RMS is one numpy pass over a few hundred samples, and a call that finds the meter busy is dropped.
"""

from __future__ import annotations

import math
import threading
import time

MAX_HZ = 20
FLOOR_DB, CEIL_DB = -60.0, -12.0  # quiet room .. loud speech, mapped to 0..1


def rms01(samples, full_scale: float | None = None) -> float:
    """Perceptual 0..1 loudness of a block of samples (int16 or float -1..1)."""
    try:
        import numpy as np

        arr = np.asarray(samples)
        if not arr.size:
            return 0.0
        scale = full_scale or (32768.0 if arr.dtype.kind in "iu" else 1.0)
        rms = float(np.sqrt(np.mean(np.square(arr, dtype=np.float32)))) / scale
    except ImportError:  # tests without numpy
        vals = [float(x) for x in samples]
        if not vals:
            return 0.0
        scale = full_scale or (32768.0 if any(abs(v) > 1.5 for v in vals) else 1.0)
        rms = math.sqrt(sum(v * v for v in vals) / len(vals)) / scale
    if rms <= 0:
        return 0.0
    db = 20 * math.log10(rms)
    return round(min(1.0, max(0.0, (db - FLOOR_DB) / (CEIL_DB - FLOOR_DB))), 3)


class LevelMeter:
    """Throttled ``level`` events for one source ("mic" or "tts")."""

    def __init__(self, bus, src: str, max_hz: float = MAX_HZ, clock=time.monotonic):
        self.bus, self.src = bus, src
        self.gap = 1.0 / max_hz
        self._clock = clock
        self._next = 0.0
        self._busy = threading.Lock()
        self._live = False

    def wanted(self) -> bool:
        """Is a HUD connected, and is the next sample due? (Cheap: no audio is touched.)"""
        if not getattr(self.bus, "client_count", 0):
            return False
        return self._clock() >= self._next

    def feed(self, samples) -> None:
        """Offer a block of audio; most calls return at the first line."""
        if not self.wanted():
            return
        self.send(lambda: rms01(samples))

    def send(self, measure) -> None:
        """Publish ``measure()`` (a 0..1 level, or None to skip) if not throttled; drops the sample if busy."""
        if not self._busy.acquire(blocking=False):
            return
        try:
            now = self._clock()
            if now < self._next:
                return
            self._next = now + self.gap
            rms = measure()
            if rms is None:
                return
            self._publish(float(rms))
            self._live = True
        except Exception:  # a level is decoration: never let it break audio
            pass
        finally:
            self._busy.release()

    def end(self) -> None:
        """One final zero so the orb settles at once (only if anything was sent)."""
        if self._live and getattr(self.bus, "client_count", 0):
            self._live = False
            try:
                self._publish(0.0)
            except Exception:
                pass

    def _publish(self, rms: float) -> None:
        data = {"src": self.src, "rms": round(rms, 3)}
        try:
            self.bus.publish("level", data, transient=True)
        except TypeError:  # a bus without transient events
            self.bus.publish("level", data)
