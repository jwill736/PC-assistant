"""Shared bits for the stream skills (co-pilot, moments, raid planner, privacy): the HUD's one-tap cards, running a
tool on J's click, the stream on the calendar right now, and "live, with a short grace for blips".

A card is a question on the HUD ("Switch category to Hades II?") with a Yes and a Not now. It is never spoken: on air,
Vesper's voice goes out to viewers. J's click on Yes is the yes a tier-2 action needs, so the tool runs through the
ToolBox with ``confirmed_by="hud"`` (the audit log says so) and the HUD gets the usual ``tool`` event.

Not a skill itself (it isn't in MODULES).
"""

from __future__ import annotations

import itertools
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Callable

STREAM_TITLE = re.compile(r"^\s*(?:twitch\s+)?(?:stream(?:ing)?|live(?:stream)?|going live)\b", re.I)
_ids = itertools.count(1)


class Cards:
    """The questions one skill has on the HUD right now. ``once``: a key is asked at most once (per Cards object)."""

    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self.items: list[dict] = []
        self.asked: set[str] = set()
        self._lock = threading.RLock()

    def add(self, kind: str, text: str, *, yes: str = "Yes", no: str = "Not now", data: dict | None = None,
            ttl: float = 300, key: str | None = None, once: bool = True) -> dict | None:
        key = key or f"{kind}:{text}"
        with self._lock:
            if once and key in self.asked:
                return None
            self.asked.add(key)
            now = self.clock()
            card = {"id": f"{kind}-{next(_ids)}", "kind": kind, "text": text, "yes": yes, "no": no, "data": data or {},
                    "ts": now, "expires": now + ttl, "key": key}
            self.items = [c for c in self.items if c["key"] != key] + [card]
            return card

    def live(self) -> list[dict]:
        now = self.clock()
        with self._lock:
            self.items = [c for c in self.items if c["expires"] > now]
            return [dict(c) for c in self.items]

    def take(self, card_id: str) -> dict | None:
        """The card J answered (gone from the HUD), or None if it already expired or was answered."""
        with self._lock:
            hit = next((c for c in self.items if c["id"] == card_id), None)
            if hit is not None:
                self.items.remove(hit)
            return hit if hit is not None and hit["expires"] > self.clock() else None

    def drop(self, kind: str | None = None, **match: Any) -> None:
        with self._lock:
            self.items = [c for c in self.items if not ((kind is None or c["kind"] == kind)
                                                        and all(c["data"].get(k) == v for k, v in match.items()))]


def run_on_click(runner: Callable[..., dict] | None, bus: Any, name: str, args: dict, what: str = "") -> dict:
    """Run a tool because J clicked Yes on the HUD: the click is the confirmation (audited as confirmed_by=hud)."""
    if runner is None:
        return {"ok": False, "error": "Vesper is still starting up."}
    result = runner(name, args, confirmed_by="hud", context={"source": "hud", "utterance": what or None})
    if not isinstance(result, dict):
        result = {"ok": True, "result": result}
    bus.publish("tool", {"name": name, "args": args, "result": result})
    return result


def is_stream_event(e: dict) -> bool:
    return not e.get("all_day") and (e.get("profile") == "stream" or bool(STREAM_TITLE.match(e.get("title") or "")))


def stream_events(svc: Any, now: float) -> list[dict]:
    """Stream blocks on the calendar from yesterday to tomorrow, with ``start_ts``/``end_ts`` (a late stream crosses
    midnight)."""
    base = datetime.fromtimestamp(now, svc.tz)
    try:
        events = svc.calendars.agenda(3, start=base - timedelta(days=1))
    except TypeError:  # a calendar without the start argument
        events = svc.calendars.agenda(2)
    except Exception:
        return []
    out = []
    for e in events or []:
        if not is_stream_event(e):
            continue
        try:
            start = datetime.fromisoformat(e["start"]).timestamp()
            end = datetime.fromisoformat(e["end"]).timestamp() if e.get("end") else start + 3 * 3600
        except (KeyError, TypeError, ValueError):
            continue
        out.append({**e, "start_ts": start, "end_ts": end})
    return sorted(out, key=lambda e: e["start_ts"])


def stream_event_now(svc: Any, now: float, early_s: float = 1800) -> dict | None:
    """The stream block going on now (or starting within ``early_s``: going live a bit early still counts)."""
    hits = [e for e in stream_events(svc, now) if e["start_ts"] - early_s <= now < e["end_ts"]]
    return min(hits, key=lambda e: abs(e["start_ts"] - now)) if hits else None


class LiveState:
    """``svc.live()`` with a grace period, so a dropped connection for a minute doesn't end the stream.
    ``update()`` returns "start", "end" or None."""

    def __init__(self, svc: Any, clock: Callable[[], float], grace_s: float = 90):
        self.svc, self.clock, self.grace_s = svc, clock, grace_s
        self.live, self.since, self._off_since = False, 0.0, 0.0

    def raw(self) -> bool:
        try:
            return bool(self.svc.live and self.svc.live())
        except Exception:
            return self.live

    def update(self) -> str | None:
        now, on = self.clock(), self.raw()
        if on:
            self._off_since = 0.0
            if not self.live:
                self.live, self.since = True, now
                return "start"
            return None
        if self.live:
            self._off_since = self._off_since or now
            if now - self._off_since >= self.grace_s:
                self.live = False
                return "end"
        return None

    @property
    def ended_at(self) -> float:
        return self._off_since or self.clock()


def clock_ts(sec: float | None) -> str:
    if sec is None:
        return ""
    h, r = divmod(int(max(0, sec)), 3600)
    return f"{h:02d}:{r // 60:02d}:{r % 60:02d}"
