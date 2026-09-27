from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from assistant.integrations.calendars import CalendarHub, free_blocks

TZ = ZoneInfo("America/New_York")

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:standup
DTSTART;TZID=America/New_York:20260921T093000
DTEND;TZID=America/New_York:20260921T094500
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:review
DTSTART:20260928T150000Z
DTEND:20260928T160000Z
SUMMARY:Client review
LOCATION:Zoom
END:VEVENT
BEGIN:VEVENT
UID:holiday
DTSTART;VALUE=DATE:20260928
DTEND;VALUE=DATE:20260929
SUMMARY:Team offsite
END:VEVENT
BEGIN:VEVENT
UID:cancelled
DTSTART:20260928T180000Z
DTEND:20260928T190000Z
STATUS:CANCELLED
SUMMARY:Cancelled thing
END:VEVENT
END:VCALENDAR
"""


def hub(tmp_path):
    work = tmp_path / "work.ics"
    work.write_text(ICS)
    return CalendarHub([{"name": "Work", "url": str(work), "profile": "work"},
                        {"name": "Broken", "url": str(tmp_path / "missing.ics"), "profile": "personal"}], "America/New_York")


def test_merges_recurring_all_day_and_skips_cancelled(tmp_path):
    h = hub(tmp_path)
    day = datetime(2026, 9, 28, tzinfo=TZ)
    events = h.events(day, day + timedelta(days=1))
    titles = [e["title"] for e in events]
    assert titles == ["Team offsite", "Standup", "Client review"]
    standup = events[1]
    assert standup["start"].startswith("2026-09-28T09:30") and not standup["all_day"]
    assert events[2]["start"].startswith("2026-09-28T11:00")  # 15:00Z shown in New York time
    assert events[0]["all_day"]


def test_bad_calendar_reports_error_without_breaking_others(tmp_path):
    h = hub(tmp_path)
    h.refresh(force=True)
    status = {s["name"]: s for s in h.status()}
    assert status["Work"]["ok"] and not status["Broken"]["ok"]
    assert "FileNotFoundError" in status["Broken"]["error"]


def test_profile_filter(tmp_path):
    h = hub(tmp_path)
    day = datetime(2026, 9, 28, tzinfo=TZ)
    assert h.events(day, day + timedelta(days=1), profile="personal") == []


def test_free_blocks_between_meetings():
    ev = lambda s, e: {"start": f"2026-09-28T{s}:00-04:00", "end": f"2026-09-28T{e}:00-04:00", "all_day": False}
    blocks = free_blocks([ev("10:00", "11:00"), ev("11:15", "12:00"), ev("16:30", "18:30")], date(2026, 9, 28), TZ)
    spans = [(b["start"][11:16], b["end"][11:16], b["minutes"]) for b in blocks]
    assert spans == [("09:00", "10:00", 60), ("12:00", "16:30", 270)]  # 15-min gap dropped, day ends at 18:00
