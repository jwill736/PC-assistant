"""Event bus: background threads publish, dashboard websockets subscribe.

Integrations run in plain threads (psutil, OBS, audio are all blocking), while
the dashboard lives on the asyncio loop. ``publish`` is safe from any thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Callable

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, history: int = 200):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[asyncio.Queue] = set()
        self._overlays: set[asyncio.Queue] = set()  # OBS Browser sources: they get events, but nobody is looking
        self._lock = threading.Lock()
        self._listeners: list[Callable[[dict], None]] = []
        self.latest: dict[str, dict] = {}  # last payload per event type, for new clients
        self.recent: deque[dict] = deque(maxlen=history)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def client_count(self) -> int:
        """Open HUDs (someone may be looking): what system stats, the level meters and "the HUD was open" go by. The
        OBS overlay's sockets aren't counted: OBS keeps its Browser sources loaded whenever it's open."""
        with self._lock:
            return len(self._clients) - len(self._overlays)

    def subscribe(self, kind: str = "hud") -> asyncio.Queue:
        """``kind``: "hud" (the HUD, the mini HUD, the phone) or "overlay" (an OBS Browser source)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._clients.add(q)
            if kind == "overlay":
                self._overlays.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._clients.discard(q)
            self._overlays.discard(q)

    def on(self, callback: Callable[[dict], None]) -> None:
        """Register an in-process listener (called on the publishing thread)."""
        self._listeners.append(callback)

    def publish(self, event_type: str, data: Any = None, *, sticky: bool = False, transient: bool = False) -> None:
        """``transient``: a high-rate, throwaway event (the orb's audio level) that only goes to connected HUDs:
        not kept in ``recent`` and not passed to in-process listeners."""
        event = {"type": event_type, "ts": time.time(), "data": data}
        if transient:
            return self._send(event)
        if sticky:
            self.latest[event_type] = event
        else:
            self.recent.append(event)
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:  # a bad listener must not break publishers
                log.exception("bus listener failed for %s", event_type)
        self._send(event)

    def _send(self, event: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        payload = json.dumps(event, default=str)
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            loop.call_soon_threadsafe(_offer, q, payload)


def _offer(q: asyncio.Queue, payload: str) -> None:
    if q.full():  # slow client: drop the oldest frame rather than block
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(payload)
