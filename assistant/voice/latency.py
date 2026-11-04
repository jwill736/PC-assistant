"""How long you wait: from the moment you stop talking to the first word of the reply, per turn.

``TurnLatency`` listens on the bus: a "heard" event that was addressed to Vesper starts the clock (its time minus the
speech-to-text time, so it's when you stopped talking), the speaker's first audio for the next turn stops it, and the
"assistant_said" that follows says what answered: the fast path (a built-in command or a skill) or a model.
The HUD's Speaking voice panel shows the median of the last 20 turns.

``Prewarm`` gets the voice ready while you're still talking: the audio device opened and the common short replies
synthesised ahead (``Speaker.prewarm``).
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from typing import Callable

MODEL_KINDS = {"claude", "local"}
STALE_S = 60.0  # a reply that starts later than this wasn't waited for (or never came)


def _p50(values: list[float]) -> int | None:
    return round(statistics.median(values)) if values else None


class TurnLatency:
    def __init__(self, turn: Callable[[], int] = lambda: 0, keep: int = 20, clock: Callable[[], float] = time.time,
                 on_change: Callable[[], None] | None = None):
        self.turn, self.clock, self.on_change = turn, clock, on_change
        self.samples: deque[dict] = deque(maxlen=keep)
        self._open: dict | None = None
        self._lock = threading.Lock()

    # ---- inputs -------------------------------------------------------------------------------------------------
    def on_event(self, event: dict) -> None:
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "heard":
            if data.get("ignored") or data.get("interrupted") or not (data.get("wake") or data.get("armed") or data.get("trigger")):
                return
            stt = float(data.get("stt_ms") or 0)
            with self._lock:
                self._open = {"t0": float(event.get("ts") or self.clock()) - stt / 1000, "after": self.turn(),
                              "stt_ms": round(stt), "kind": None}
        elif kind == "assistant_said" and data.get("source") == "voice":
            k = "model" if data.get("kind") in MODEL_KINDS else "fast"
            with self._lock:
                if self._open is not None:
                    self._open["kind"] = k  # the fast path publishes before the first audio plays
                elif self.samples and self.samples[-1]["kind"] is None and self.clock() - self.samples[-1]["at"] < STALE_S:
                    self.samples[-1]["kind"] = k  # a streamed model reply: its first sentence played before this
                else:
                    return
            if self.on_change:
                self.on_change()

    def first_audio(self, turn: int, at: float | None = None) -> None:
        """The speaker started playing ``turn``'s first sentence."""
        at = self.clock() if at is None else at
        with self._lock:
            o = self._open
            if o is None or turn <= o["after"]:
                return
            self._open = None
            ms = (at - o["t0"]) * 1000
            if not 0 <= ms <= STALE_S * 1000:
                return
            self.samples.append({"ms": round(ms), "stt_ms": o["stt_ms"], "kind": o["kind"], "at": at})
        if self.on_change:
            self.on_change()

    # ---- the report -------------------------------------------------------------------------------------------
    def report(self) -> dict | None:
        with self._lock:
            s = list(self.samples)
        if not s:
            return None
        return {"n": len(s), "p50_ms": _p50([x["ms"] for x in s]),
                "fast_p50_ms": _p50([x["ms"] for x in s if x["kind"] == "fast"]),
                "model_p50_ms": _p50([x["ms"] for x in s if x["kind"] == "model"]),
                "stt_p50_ms": _p50([x["stt_ms"] for x in s]), "last_ms": s[-1]["ms"],
                "recent": [{"ms": x["ms"], "kind": x["kind"]} for x in s[-20:]]}


class Prewarm:
    """On the wake word, or as soon as you start talking to it, get the voice ready: at most every ``gap_s``.

    "Talking to it" is armed (the hotkey, a follow-up window, a conversation) or within ``exchange_s`` of a sentence it
    took as meant for it. Speech before that (an hour of talking to chat, with the name heard only in the words) no
    longer opens the speakers every 5 seconds: it kept the output stream running all the time J talked, ~50 audio
    callbacks a second (PERF-12). The first reply of an exchange then opens the device itself (50-100 ms)."""

    def __init__(self, speaker, gap_s: float = 5.0, clock: Callable[[], float] = time.monotonic,
                 exchange_s: float = 120.0):
        self.speaker, self.gap_s, self.clock, self.exchange_s = speaker, gap_s, clock, exchange_s
        self._last = -1e9
        self._talking_to_us = -1e9  # when a sentence was last meant for Vesper (or it was woken or armed)
        self.count = 0

    def on_event(self, event: dict) -> None:
        kind, data = event.get("type"), event.get("data") or {}
        state = data.get("state") if kind == "voice_state" else None
        if kind == "heard":
            if not data.get("ignored") and (data.get("wake") or data.get("armed") or data.get("trigger")):
                self._talking_to_us = self.clock()
            return
        if kind == "wake" or state == "armed":
            self._talking_to_us = self.clock()
        elif state != "hearing" or self.clock() - self._talking_to_us > self.exchange_s:
            return
        now = self.clock()
        if now - self._last < self.gap_s or self.speaker.speaking.is_set():
            return
        self._last = now
        self.count += 1
        self.speaker.prewarm()
