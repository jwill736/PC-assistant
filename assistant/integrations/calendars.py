"""Multiple calendars merged into one agenda.

Each calendar is an iCal feed — Google ("Secret address in iCal format"),
Outlook ("Publish calendar" -> ICS link), iCloud (public calendar link), or a
local .ics file. Read-only by design: no OAuth app to register.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, tzinfo
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

log = logging.getLogger(__name__)

# Categorical slots in fixed order, validated for colour-blind separation on the HUD surface.
PALETTE = ["#6f9bff", "#b48cff", "#c9b27c", "#4fc1a6", "#e7799a", "#5fb7d9", "#d9a441", "#9ea3aa"]  # HUD series colours


def local_tz(name: str | None = None) -> tzinfo:
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def _to_dt(value, tz: tzinfo) -> tuple[datetime, bool]:
    """Normalize an iCal DTSTART/DTEND value to an aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz), False
        return value.astimezone(tz), False
    if isinstance(value, date):
        return datetime.combine(value, dtime.min, tzinfo=tz), True
    raise TypeError(f"unsupported date value {value!r}")


class CalendarHub:
    def __init__(self, sources: list[dict], tz_name: str | None = None, refresh_minutes: int = 10,
                 google=None, google_sources: Callable[[], list[dict]] | None = None):
        self.tz = local_tz(tz_name)
        # Google calendars read through a signed-in account (integrations/google.py): {account, id, name, profile}
        self.google = google
        self._google_sources = google_sources or (lambda: [])
        self._gevents: dict[str, list[dict]] = {}
        self.sources = []
        for i, src in enumerate(sources or []):
            if not src.get("url") and not src.get("url_env"):
                continue
            self.sources.append({
                "name": src.get("name") or f"Calendar {i + 1}",
                "url": src.get("url", ""),
                "url_env": src.get("url_env"),
                "profile": src.get("profile", "personal"),
                "color": src.get("color") or PALETTE[i % len(PALETTE)],
            })
        self.refresh_s = refresh_minutes * 60
        self._cals: dict[str, icalendar.Calendar] = {}
        self._errors: dict[str, str] = {}
        self._fetched = 0.0
        self._lock = threading.Lock()

    def set_sources(self, sources: list[dict]) -> None:
        """The iCal calendars changed (the HUD turned the failing links off)."""
        with self._lock:
            self.sources = [s for s in CalendarHub(sources).sources]
            for name in list(self._cals):
                if name not in {s["name"] for s in self.sources}:
                    self._cals.pop(name, None)
                    self._errors.pop(name, None)
            self._fetched = 0.0

    def google_sources(self) -> list[dict]:
        out = []
        try:
            chosen = self._google_sources() or []
        except Exception:
            chosen = []
        for i, src in enumerate(chosen):
            if src.get("account") and src.get("id"):
                out.append({"name": src.get("name") or src["id"], "account": src["account"], "id": src["id"],
                            "profile": src.get("profile", "personal"),
                            "color": src.get("color") or PALETTE[(len(self.sources) + i) % len(PALETTE)],
                            "key": f"google:{src['account']}:{src['id']}"})
        return out

    def _refresh_google(self) -> None:
        if self.google is None:
            return
        now = datetime.now(self.tz)
        start, end = now - timedelta(days=8), now + timedelta(days=62)  # yesterday's recap to two months out
        keep = set()
        for src in self.google_sources():
            keep.add(src["key"])
            try:
                self._gevents[src["key"]] = self.google.events(src["account"], src["id"], start, end, self.tz)
                self._errors.pop(src["key"], None)
            except Exception as exc:
                self._errors[src["key"]] = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("google calendar %s failed: %s", src["name"], type(exc).__name__)
        for key in list(self._gevents):
            if key not in keep:
                self._gevents.pop(key, None)

    def _source_url(self, src: dict) -> str:
        import os

        url = os.environ.get(src["url_env"], "") if src.get("url_env") else src["url"]
        return url.replace("webcal://", "https://", 1)

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.time() - self._fetched < self.refresh_s:
                return
            for src in self.sources:
                url = self._source_url(src)
                if not url:  # link not added to .env yet: not connected, not broken (and never read "." as a file)
                    self._errors.pop(src["name"], None)
                    continue
                try:
                    if url.startswith(("http://", "https://")):
                        resp = httpx.get(url, timeout=15, follow_redirects=True)
                        resp.raise_for_status()
                        raw = resp.content
                    else:
                        raw = Path(url).expanduser().read_bytes()
                    self._cals[src["name"]] = icalendar.Calendar.from_ical(raw)
                    self._errors.pop(src["name"], None)
                except Exception as exc:
                    # Never log the URL: Google's secret iCal address is a credential.
                    self._errors[src["name"]] = type(exc).__name__ + (f": {exc}" if not url.startswith("http") else "")
                    log.warning("calendar %s failed: %s", src["name"], type(exc).__name__)
            self._refresh_google()
            self._fetched = time.time()

    def events(self, start: datetime, end: datetime, profile: str | None = None) -> list[dict]:
        self.refresh()
        out = []
        for src in self.sources:
            if profile and src["profile"] != profile:
                continue
            cal = self._cals.get(src["name"])
            if cal is None:
                continue
            try:
                occurrences = recurring_ical_events.of(cal).between(start, end)
            except Exception:
                log.exception("expanding %s failed", src["name"])
                continue
            for ev in occurrences:
                try:
                    s, all_day = _to_dt(ev.get("DTSTART").dt, self.tz)
                    e_raw = ev.get("DTEND")
                    if e_raw is not None:
                        e, _ = _to_dt(e_raw.dt, self.tz)
                    elif ev.get("DURATION") is not None:
                        e = s + ev.get("DURATION").dt
                    else:
                        e = s + (timedelta(days=1) if all_day else timedelta(hours=1))
                except (TypeError, AttributeError):
                    continue
                if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                    continue
                out.append({
                    "calendar": src["name"],
                    "profile": src["profile"],
                    "color": src["color"],
                    "title": str(ev.get("SUMMARY", "(no title)")),
                    "start": s.isoformat(),
                    "end": e.isoformat(),
                    "all_day": all_day,
                    "location": str(ev.get("LOCATION", "") or ""),
                    "description": str(ev.get("DESCRIPTION", "") or "")[:280],
                })
        for src in self.google_sources():
            if profile and src["profile"] != profile:
                continue
            for ev in self._gevents.get(src["key"], []):
                if ev["end"] <= start or ev["start"] >= end:
                    continue
                out.append({"calendar": src["name"], "profile": src["profile"], "color": src["color"],
                            "title": ev["title"], "start": ev["start"].isoformat(), "end": ev["end"].isoformat(),
                            "all_day": ev["all_day"], "location": ev["location"], "description": ev["description"]})
        out.sort(key=lambda x: (x["start"], not x["all_day"]))
        return out

    def agenda(self, days: int = 1, profile: str | None = None, start: datetime | None = None) -> list[dict]:
        base = start or datetime.now(self.tz)
        day0 = datetime.combine(base.date(), dtime.min, tzinfo=self.tz)
        return self.events(day0, day0 + timedelta(days=days), profile)

    def status(self) -> list[dict]:
        """``missing``: the .env name to fill in when the link isn't there yet (not an error: just not connected)."""
        return [{"name": s["name"], "profile": s["profile"], "color": s["color"], "kind": "ical",
                 "ok": s["name"] in self._cals and s["name"] not in self._errors,
                 "error": self._errors.get(s["name"]),
                 "missing": (s.get("url_env") or "url") if not self._source_url(s) else None} for s in self.sources] + [
            {"name": g["name"], "profile": g["profile"], "color": g["color"], "kind": "google", "account": g["account"],
             "ok": g["key"] in self._gevents and g["key"] not in self._errors, "error": self._errors.get(g["key"]),
             "missing": None} for g in self.google_sources()]


def free_blocks(events: list[dict], day: date, tz: tzinfo, start: str = "09:00", end: str = "18:00",
                min_minutes: int = 30, now: datetime | None = None) -> list[dict]:
    """Open stretches inside working hours, after ``now``, between timed events."""
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    cursor = datetime.combine(day, dtime(sh, sm), tzinfo=tz)
    stop = datetime.combine(day, dtime(eh, em), tzinfo=tz)
    if now and now > cursor:
        cursor = now.replace(second=0, microsecond=0)
    busy = sorted(
        (datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"]))
        for e in events if not e["all_day"]
    )
    blocks = []
    for bs, be in busy:
        if be <= cursor:
            continue
        if bs > cursor:
            gap_end = min(bs, stop)
            if (gap_end - cursor).total_seconds() >= min_minutes * 60:
                blocks.append({"start": cursor.isoformat(), "end": gap_end.isoformat(),
                               "minutes": int((gap_end - cursor).total_seconds() // 60)})
        cursor = max(cursor, be)
        if cursor >= stop:
            break
    if stop > cursor and (stop - cursor).total_seconds() >= min_minutes * 60:
        blocks.append({"start": cursor.isoformat(), "end": stop.isoformat(),
                       "minutes": int((stop - cursor).total_seconds() // 60)})
    return blocks
