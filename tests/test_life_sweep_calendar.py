"""Sweep regressions: the PC-clock zone follows DST (LIFE-1), and an iCal feed is expanded once per fetch (INTEG-7)."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from zoneinfo import ZoneInfo

from assistant.integrations import calendars
from assistant.integrations.calendars import CalendarHub, local_tz

NY = ZoneInfo("America/New_York")


def _new_york(ts):  # what Windows' clock says for New York, without touching the machine's zone
    return int(datetime.fromtimestamp(ts, timezone.utc).astimezone(NY).utcoffset().total_seconds())


def test_the_default_zone_follows_dst_like_the_real_one(monkeypatch):
    """With assistant.timezone unset (J's config) the zone was the fixed offset Vesper started with: after the clocks
    changed, the planner, routines, quiet hours and calendar times were an hour off until a restart."""
    monkeypatch.setattr(calendars, "_gmtoff", _new_york, raising=False)
    tz = local_tz(None)
    assert tz is local_tz() and tz is CalendarHub([]).tz  # one shared zone, so svc.tz and the calendars agree
    assert tz.utcoffset(datetime(2026, 7, 1, 12)) == timedelta(hours=-4)
    assert tz.utcoffset(datetime(2026, 12, 1, 12)) == timedelta(hours=-5)
    # "every weekday at 9 am", made before the change, is still 9 AM real time after it
    assert datetime(2026, 11, 2, 9, 0, tzinfo=tz).timestamp() == datetime(2026, 11, 2, 9, 0, tzinfo=NY).timestamp()
    # every 20 minutes across both changes: the same wall time and the same pass through the repeated hour
    t0 = datetime(2026, 3, 7, tzinfo=timezone.utc).timestamp()
    for ts in [t0 + i * 1200 for i in range(3 * 72)] + [t0 + 238 * 86400 + i * 1200 for i in range(3 * 72)]:
        a, b = datetime.fromtimestamp(ts, tz), datetime.fromtimestamp(ts, NY)
        assert (a.replace(tzinfo=None), a.fold, a.utcoffset()) == (b.replace(tzinfo=None), b.fold, b.utcoffset())
        for fold in (0, 1):
            w = b.replace(tzinfo=None, fold=fold)
            assert w.replace(tzinfo=tz).timestamp() == w.replace(tzinfo=NY).timestamp(), (w, fold)
    for wall in (datetime(2026, 3, 8, 2, 30), datetime(2026, 11, 1, 1, 30)):  # the skipped and the repeated hour
        for fold in (0, 1):
            assert (wall.replace(tzinfo=tz, fold=fold).timestamp()
                    == wall.replace(tzinfo=NY, fold=fold).timestamp()), (wall, fold)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset to switch the process zone")
def test_the_default_zone_reads_the_pc_clock(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        tz = local_tz(None)
        assert tz.utcoffset(datetime(2026, 7, 1, 12)) != tz.utcoffset(datetime(2026, 12, 1, 12))
        assert datetime(2026, 11, 3, 17, 0, tzinfo=tz).isoformat() == "2026-11-03T17:00:00-05:00"
    finally:
        monkeypatch.undo()
        time.tzset()


ICS = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:test",
                   "BEGIN:VEVENT", "UID:standup", "DTSTART;TZID=America/New_York:20260921T093000",
                   "DTEND;TZID=America/New_York:20260921T094500", "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
                   "SUMMARY:Standup", "END:VEVENT",
                   "BEGIN:VEVENT", "UID:ping", "DTSTART:20261104T170000Z", "SUMMARY:Ping", "END:VEVENT",
                   "BEGIN:VEVENT", "UID:off", "DTSTART;VALUE=DATE:20261105", "DTEND;VALUE=DATE:20261106",
                   "SUMMARY:Offsite", "END:VEVENT", "END:VCALENDAR"])


def test_agenda_doesnt_reexpand_the_feed_on_every_call(tmp_path, monkeypatch):
    """Expanding a busy feed is ~0.3 s of CPU, and nudges asks for today's agenda every 15 s, in games too."""
    p = tmp_path / "work.ics"
    p.write_text(ICS, encoding="utf-8")
    clock = {"t": datetime(2026, 11, 4, 8, 0, tzinfo=NY).timestamp()}
    hub = CalendarHub([{"name": "Work", "url": str(p), "profile": "work"}], "America/New_York",
                      clock=lambda: clock["t"])
    day = datetime(2026, 11, 4, tzinfo=NY)
    hub.refresh(force=True)
    wanted = hub._expand(hub.sources[0], hub._cals["Work"], day, day + timedelta(days=1))
    calls = []
    real = calendars.recurring_ical_events.of
    monkeypatch.setattr(calendars.recurring_ical_events, "of", lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(4):  # one in-game minute of nudges ticks
        today = hub.agenda(1)
        clock["t"] += 15
    assert calls == [], f"expanded the whole feed {len(calls)} times in a minute"
    assert today == [ev for _, _, ev in wanted]
    assert [e["title"] for e in today] == ["Standup", "Ping"]  # a DTSTART-only event counts from its start
    assert [e["title"] for e in hub.agenda(2)] == ["Standup", "Ping", "Offsite", "Standup"]
    today[0]["title"] = "changed by a caller"
    assert hub.agenda(1)[0]["title"] == "Standup"
    assert calls == []
    # a range outside the expanded window (now - 8 d … now + 62 d) still works, expanded on demand
    far = hub.events(datetime(2027, 3, 1, tzinfo=NY), datetime(2027, 3, 2, tzinfo=NY))
    assert [e["title"] for e in far] == ["Standup"] and len(calls) == 1
    clock["t"] += 600  # the next fetch re-expands once
    hub.agenda(1)
    assert len(calls) == 2
