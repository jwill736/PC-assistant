"""J's rhythm, learned from the activity log: when work usually starts and ends, lunch, which nights he streams and
when, how long he goes before a break, and bedtime.

Pure functions over activity segments ({start, end, category: work|stream|other|idle, ...}); the memory skill shows
the result as "Your rhythm" and, only when J clicks "Use these", turns it into settings (nudges quiet hours and break
length, the planner's work hours). A "day" runs from 05:00 to 05:00, so a late night belongs to the evening before.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

DAY_START_H = 5  # a day runs 05:00 -> 05:00
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _off(ts: float, tz) -> tuple:
    """(day, minutes since that day's 05:00) for a timestamp."""
    dt = datetime.fromtimestamp(ts, tz)
    base = dt - timedelta(hours=DAY_START_H)
    day = base.date()
    return day, base.hour * 60 + base.minute + base.second / 60


def hhmm(minutes_from_day_start: float, step: int = 5) -> str:
    """Minutes after 05:00 -> '09:10' (rounded to ``step`` minutes, wrapped past midnight)."""
    m = int(round((minutes_from_day_start + DAY_START_H * 60) / step) * step) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def _p(values: list[float], q: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))]


def infer(segments: list[dict], tz, now: float, min_days: int = 3) -> dict:
    """What J's days usually look like, with how many days each answer rests on."""
    by_day: dict = defaultdict(list)
    for s in segments:
        if s.get("category") == "idle" or s["end"] - s["start"] < 30:
            continue
        day, a = _off(s["start"], tz)
        _, b = _off(s["end"], tz)
        if b < a:  # crosses 05:00: count it in its starting day
            b = 24 * 60
        by_day[day].append((a, b, s.get("category") or "other"))
    days = {d: sorted(v) for d, v in by_day.items() if sum(b - a for a, b, _ in v) >= 30}
    if len(days) < min_days:
        return {"ok": False, "days": len(days), "error": f"Not enough to go on yet: {len(days)} day"
                f"{'s' if len(days) != 1 else ''} of activity so far, it needs {min_days}. Leave Vesper running."}
    first, last = [], []
    work_start, work_end, work_days = [], [], 0
    lunch_start, lunch_len = [], []
    streams: dict = defaultdict(list)
    stretches = []
    seen_weekday: dict = defaultdict(int)
    for day, segs in days.items():
        seen_weekday[day.weekday()] += 1
        first.append(segs[0][0])
        last.append(max(b for _, b, _ in segs))
        work = [(a, b) for a, b, c in segs if c == "work"]
        if sum(b - a for a, b in work) >= 60:
            work_days += 1
            work_start.append(work[0][0])
            work_end.append(max(b for _, b in work))
            # lunch: the longest pause between 11:00 and 14:30, 20 min to 2 h
            lo, hi = (11 - DAY_START_H) * 60, (14.5 - DAY_START_H) * 60
            busy: list[list[float]] = []
            for a, b in sorted((max(a, lo), min(b, hi)) for a, b, _ in segs if b > lo and a < hi):
                if busy and a <= busy[-1][1]:
                    busy[-1][1] = max(busy[-1][1], b)
                else:
                    busy.append([a, b])
            gaps = [(busy[i + 1][0] - busy[i][1], busy[i][1]) for i in range(len(busy) - 1)
                    if 20 <= busy[i + 1][0] - busy[i][1] <= 120]
            if gaps:
                g, at = max(gaps)
                lunch_start.append(at)
                lunch_len.append(g)
            # how long J works before stopping for 5+ minutes
            run_a, run_b = work[0]
            for a, b in work[1:] + [(1e9, 1e9)]:
                if a - run_b <= 5:
                    run_b = max(run_b, b)
                    continue
                if run_b - run_a >= 20:
                    stretches.append(run_b - run_a)
                run_a, run_b = a, b
        st = [(a, b) for a, b, c in segs if c == "stream"]
        if sum(b - a for a, b in st) >= 30:
            streams[day.weekday()].append((st[0][0], max(b for _, b in st)))
    out: dict = {"ok": True, "days": len(days), "weeks": round(len(days) / 7, 1), "computed": now,
                 "since": min(days).isoformat()}
    out["wake"] = {"time": hhmm(median(first), 15), "days": len(first)}
    out["bedtime"] = {"time": hhmm(median(last), 15), "days": len(last)}
    if work_days >= max(2, min_days - 1):
        out["work"] = {"start": hhmm(median(work_start), 15), "end": hhmm(median(work_end), 15), "days": work_days}
    if len(lunch_start) >= 2:
        out["lunch"] = {"start": hhmm(median(lunch_start), 15), "minutes": int(round(median(lunch_len) / 5) * 5),
                        "days": len(lunch_start)}
    if streams:
        nights = sum(len(v) for v in streams.values())
        usual = [wd for wd, v in streams.items() if len(v) >= 2 and len(v) >= 0.5 * seen_weekday[wd]]
        if not usual and nights >= 2:
            usual = [wd for wd, v in streams.items() if len(v) >= 1]
        if usual:
            all_st = [x for wd in usual for x in streams[wd]]
            out["stream"] = {"days": [WEEKDAYS[wd] for wd in sorted(usual)], "start": hhmm(median(a for a, _ in all_st), 15),
                             "end": hhmm(median(b for _, b in all_st), 15), "nights": nights}
    if len(stretches) >= 3:
        out["stretch_minutes"] = int(round(_p(stretches, 0.75)))
    out["suggest"] = suggest(out)
    return out


def suggest(r: dict) -> dict:
    """The settings this rhythm points to (applied only when J says so)."""
    s: dict = {}
    if r.get("bedtime") and r.get("wake"):
        bed = _mins(r["bedtime"]["time"])
        wake = _mins((r.get("work") or {}).get("start") or r["wake"]["time"])
        wake = min(wake, _mins(r["wake"]["time"])) if r.get("wake") else wake
        s["quiet_hours"] = f"{_fmt(bed - 15)}-{_fmt(wake)}"
    if r.get("stretch_minutes"):
        s["break_after_minutes"] = max(45, min(120, int(round(r["stretch_minutes"] / 15) * 15)))
    if r.get("work"):
        s["work_hours"] = {"start": r["work"]["start"], "end": r["work"]["end"]}
    return s


def _mins(hm: str) -> int:
    h, m = (int(x) for x in hm.split(":"))
    return h * 60 + m


def _fmt(m: int) -> str:
    m %= 24 * 60
    return f"{m // 60:02d}:{m % 60:02d}"


__all__ = ["infer", "suggest", "hhmm"]
