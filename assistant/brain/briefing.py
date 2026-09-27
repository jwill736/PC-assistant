"""Morning briefing and end-of-day recap.

``gather_*`` collects hard data from every integration. Claude turns it into a
prioritised plan when an API key is set; otherwise ``fallback_*`` produces a
plain but complete version so "good morning" always works.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta

from ..integrations.calendars import free_blocks
from ..services import Services
from .speech import clock, duration

log = logging.getLogger(__name__)

BRIEFING_SCHEMA = {
    "type": "object",
    "properties": {
        "spoken": {"type": "string", "description": "What to say out loud: 60-120 words, no lists, no URLs."},
        "headline": {"type": "string", "description": "One line that frames the day."},
        "top_moves": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"move": {"type": "string"}, "why": {"type": "string"}, "when": {"type": "string"}},
                "required": ["move", "why", "when"],
                "additionalProperties": False,
            },
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "bullets": {"type": "array", "items": {"type": "string"}}},
                "required": ["title", "bullets"],
                "additionalProperties": False,
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["spoken", "headline", "top_moves", "sections", "risks"],
    "additionalProperties": False,
}


def _safe(label: str, fn, default):
    try:
        return fn()
    except Exception:
        log.exception("briefing: %s failed", label)
        return default


def _day_start(svc: Services, offset_days: int = 0) -> datetime:
    today = datetime.now(svc.tz).date() + timedelta(days=offset_days)
    return datetime.combine(today, dtime.min, tzinfo=svc.tz)


def gather_morning(svc: Services) -> dict:
    now = datetime.now(svc.tz)
    today0, yday0 = _day_start(svc), _day_start(svc, -1)
    hours = svc.cfg["assistant"]["work_hours"]
    events = _safe("calendar", lambda: svc.calendars.agenda(1), [])
    week = _safe("calendar week", lambda: svc.calendars.agenda(7), [])
    yday = _safe("activity", lambda: svc.activity.summary_for_day(yday0, svc.tz), {})
    yday.pop("timeline", None)
    projects = _safe("projects", svc.projects.summary, {})
    return {
        "now": now.strftime("%A %B %d %Y, %I:%M %p"),
        "user": svc.cfg["assistant"]["user_name"],
        "goals": svc.cfg["goals"],
        "calendar_today": events,
        "free_blocks_today": free_blocks(events, now.date(), svc.tz, hours["start"], hours["end"], now=now),
        "upcoming_streams": [e for e in week if e["profile"] == "stream"][:3],
        "calendar_errors": [c for c in svc.calendars.status() if c["error"]],
        "yesterday": yday,
        "yesterday_commits": _safe("commits", lambda: svc.projects.commits_between(yday0, today0), [])[-15:],
        "tasks_open": svc.storage.list_tasks()[:10],
        "tasks_done_yesterday": svc.storage.tasks_done_between(yday0.timestamp(), today0.timestamp()),
        "notes": svc.storage.list_notes(6),
        "claude_projects": (projects.get("claude") or [])[:6],
        "repos_with_uncommitted_work": [r for r in projects.get("repos", []) if r["dirty_files"]][:6],
        "open_prs": (projects.get("github") or {}).get("open_prs", [])[:6],
        "news": [{"source": h["source"], "title": h["title"]} for h in _safe("news", lambda: svc.news.headlines(limit=8), [])],
        "system": _safe("system", lambda: svc.system.analyze()["findings"], []),
        "jobs_finished_overnight": [j for j in svc.storage.list_jobs(10)
                                    if j["status"] in {"done", "failed"} and (j["finished"] or 0) > yday0.timestamp()],
    }


def gather_recap(svc: Services) -> dict:
    now = datetime.now(svc.tz)
    today0 = _day_start(svc)
    summary = _safe("activity", lambda: svc.activity.summary_for_day(now, svc.tz), {})
    summary.pop("timeline", None)
    tomorrow = _safe("calendar", lambda: svc.calendars.agenda(1, start=now + timedelta(days=1)), [])
    return {
        "now": now.strftime("%A %B %d %Y, %I:%M %p"),
        "user": svc.cfg["assistant"]["user_name"],
        "goals": svc.cfg["goals"],
        "activity_today": summary,
        "meetings_today": [e for e in _safe("calendar", lambda: svc.calendars.agenda(1), []) if not e["all_day"]],
        "commits_today": _safe("commits", lambda: svc.projects.commits_between(today0, now), []),
        "tasks_done_today": svc.storage.tasks_done_between(today0.timestamp(), now.timestamp()),
        "tasks_open": svc.storage.list_tasks()[:10],
        "jobs_today": [j for j in svc.storage.list_jobs(20) if j["created"] >= today0.timestamp()],
        "tomorrow": tomorrow,
    }


MORNING_INSTRUCTIONS = """Build {user}'s morning briefing from the JSON below.

Priorities, in order: (1) the north-star goal and this week's priorities, (2) hard commitments on the calendars,
(3) momentum from yesterday (commits, Claude Code sessions, unfinished work), (4) everything else.
- top_moves: the 3 highest-leverage things to do today, each tied to a specific free block or time, each with a
  one-line reason that connects to the goal. Time is money: favour moves that ship something or make money.
- sections: Schedule, Yesterday in numbers, Projects, Stream, News worth knowing, System. Skip a section if there's
  nothing real to say. Use the actual numbers from the data (hours, commits, dropped frames), not adjectives.
- risks: conflicts, overcommitment, stale work, a goal nobody's touching. Be blunt.
- spoken: greet {user} by name, then the day in 60-120 words; conversational, no lists, no URLs.
If the north-star goal is empty, say so in risks and recommend setting it — you can't prioritise without it.
Never invent events, numbers or projects that aren't in the data.

DATA:
{data}"""

RECAP_INSTRUCTIONS = """Write {user}'s end-of-day recap from the JSON below.
- sections: Where the time went (hours by category, top apps, focus sessions, context switches), Shipped (commits,
  tasks done, jobs), Meetings, Tomorrow.
- top_moves: the 3 things to line up for tomorrow morning, tied to the goals.
- risks: time leaks (idle, excessive switching, time in categories that don't serve the goal). Be blunt, cite numbers.
- spoken: 50-100 words, conversational, name the single biggest win and the single biggest leak.
Never invent data.

DATA:
{data}"""


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f}h"


def fallback_morning(data: dict, name: str) -> dict:
    events = [e for e in data["calendar_today"] if not e["all_day"]]
    free = data["free_blocks_today"]
    yday = data.get("yesterday") or {}
    tasks = data["tasks_open"]
    schedule = [f"{clock(datetime.fromisoformat(e['start']))} — {e['title']} ({e['calendar']})" for e in events] or ["Nothing scheduled."]
    free_line = [f"Open: {clock(datetime.fromisoformat(b['start']))}–{clock(datetime.fromisoformat(b['end']))} ({b['minutes']} min)" for b in free]
    cats = yday.get("by_category", {})
    yesterday = [f"{k}: {_hours(v)}" for k, v in cats.items()] or ["No activity recorded yesterday."]
    if yday.get("focus_sessions"):
        yesterday.append(f"{len(yday['focus_sessions'])} deep-work session(s), longest {max(s['minutes'] for s in yday['focus_sessions'])} min")
    if data["yesterday_commits"]:
        yesterday.append(f"{len(data['yesterday_commits'])} commit(s)")
    projects = [f"{p['project']}: last ask “{p['latest_ask'][:90]}”" for p in data["claude_projects"][:4]]
    projects += [f"{r['name']}: {r['dirty_files']} uncommitted file(s)" for r in data["repos_with_uncommitted_work"][:3]]
    moves = []
    for i, t in enumerate(tasks[:3]):
        slot = free[i] if i < len(free) else None
        moves.append({"move": t["title"], "why": f"priority {t['priority']} {t['profile']} task",
                      "when": f"{clock(datetime.fromisoformat(slot['start']))}" if slot else "when you can"})
    risks = []
    if not data["goals"].get("north_star"):
        risks.append("No north-star goal set in config.yaml — priorities are guesses until you set one.")
    risks += [f"Calendar '{c['name']}' failed to load" for c in data["calendar_errors"]]
    risks += [f["title"] for f in data["system"] if f["severity"] in {"high", "medium"}]
    now = datetime.now().astimezone()
    ahead = [e for e in events if datetime.fromisoformat(e["start"]) > now]
    if ahead:
        lead = "first up" if len(ahead) == len(events) else "next up"
        first = f"{lead} is {ahead[0]['title']} at {clock(datetime.fromisoformat(ahead[0]['start']))}"
    else:
        first = "nothing else on the calendar today" if events else "your calendar is clear"
    spoken = (f"Good morning {data['user']}. It's {now.strftime('%A')}; {first}. "
              f"You have {len(events)} event{'s' if len(events) != 1 else ''} and {duration(sum(b['minutes'] for b in free) * 60)} of open time. "
              + (f"Top task: {tasks[0]['title']}. " if tasks else "No open tasks — add some so I can plan your day. ")
              + (f"Heads up: {risks[0]}" if risks else "Let's get it."))
    return {
        "spoken": spoken,
        "headline": f"{len(events)} events · {duration(sum(b['minutes'] for b in free) * 60)} open · {len(tasks)} open tasks",
        "top_moves": moves,
        "sections": [s for s in [
            {"title": "Schedule", "bullets": schedule + free_line},
            {"title": "Yesterday in numbers", "bullets": yesterday},
            {"title": "Projects", "bullets": projects},
            {"title": "Stream", "bullets": [f"{e['title']} — {e['start'][:16].replace('T', ' ')}" for e in data["upcoming_streams"]]},
            {"title": "News", "bullets": [f"{n['source']}: {n['title']}" for n in data["news"][:5]]},
        ] if s["bullets"]],
        "risks": risks,
        "generated_by": "local",
    }


def fallback_recap(data: dict, name: str) -> dict:
    act = data.get("activity_today") or {}
    cats = act.get("by_category", {})
    active = act.get("active_seconds", 0)
    time_lines = [f"{k}: {_hours(v)}" for k, v in cats.items()] or ["No activity recorded."]
    if act.get("top_apps"):
        time_lines.append("Top apps: " + ", ".join(f"{a['app']} {_hours(a['seconds'])}" for a in act["top_apps"][:4]))
    if act.get("switches_per_hour"):
        time_lines.append(f"{act['switches_per_hour']} app switches per hour")
    shipped = [f"{c['repo']}: {c['message']}" for c in data["commits_today"][-8:]]
    shipped += [f"✔ {t['title']}" for t in data["tasks_done_today"]]
    tomorrow = [f"{clock(datetime.fromisoformat(e['start']))} — {e['title']}" for e in data["tomorrow"] if not e["all_day"]]
    work = cats.get("work", 0)
    spoken = (f"Day's recap, {data['user']}. {duration(active)} active, {duration(work)} of it on work"
              + (f", {len(data['commits_today'])} commits" if data["commits_today"] else "")
              + (f", {len(data['tasks_done_today'])} tasks closed" if data["tasks_done_today"] else "")
              + ". " + (f"Tomorrow starts with {data['tomorrow'][0]['title']}." if data["tomorrow"] else "Tomorrow's calendar is open."))
    return {
        "spoken": spoken,
        "headline": f"{_hours(active)} active · {_hours(work)} work · {len(data['commits_today'])} commits",
        "top_moves": [{"move": t["title"], "why": f"priority {t['priority']}", "when": "tomorrow"} for t in data["tasks_open"][:3]],
        "sections": [s for s in [
            {"title": "Where the time went", "bullets": time_lines},
            {"title": "Shipped", "bullets": shipped},
            {"title": "Tomorrow", "bullets": tomorrow},
        ] if s["bullets"]],
        "risks": [f"{_hours(cats.get('idle', 0))} idle"] if cats.get("idle", 0) > 3600 else [],
        "generated_by": "local",
    }
