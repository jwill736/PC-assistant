"""Planner: plan my day, shut down for the day, and the weekly review.

- "plan my day" / "plan tomorrow": meetings from the calendars, the stream tonight, and open tasks laid into the free
  blocks (due today first, then priority 1, goal-related first; 45 minutes each unless the title says "(30m)" / "~2h").
  The model, when there is one, only re-orders the tasks and writes a headline; the times are always worked out here.
- "am i overbooked": meetings + must-do tasks (due today or P1) against the work hours, and what to push.
- "what should i work on now": the block you're in and what's next.
- "shut down for the day": what got done, focus hours, open tasks due today roll to the next workday, and an offer to
  switch to stream mode when a stream is on tonight (a HUD button; nothing switches on its own).
- "weekly review" (and a nudge on Friday at 4 PM): hours by category, tasks done, meetings, streams, wins, leaks and
  next week's top three.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta

from ..brain.router import Intent
from ..brain.speech import clock, duration
from ..brain.tools import Tool, obj
from ..integrations.activity import IDLE, summarize
from ..integrations.calendars import free_blocks
from . import Skill, words

PLAN_SCHEMA = {"type": "object", "properties": {
    "order": {"type": "array", "items": {"type": "integer"}, "description": "task ids, most important first"},
    "headline": {"type": "string", "description": "one short line that frames the day"}}, "required": ["order", "headline"]}
REVIEW_SCHEMA = {"type": "object", "properties": {
    "wins": {"type": "array", "items": {"type": "string"}}, "leaks": {"type": "array", "items": {"type": "string"}},
    "next_week": {"type": "array", "items": {"type": "string"}}}, "required": ["wins", "leaks", "next_week"]}


def estimate(task: dict, default: int = 45) -> int:
    """Minutes for a task: '(30m)', '~2h', '[90 min]' in the title, else the default."""
    m = re.search(r"[(\[~]\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minutes|h|hr|hrs|hours)\b", task.get("title") or "")
    if not m:
        return int(default)
    n = float(m.group(1))
    return max(5, int(n * 60 if m.group(2).startswith("h") else n))


def goal_words(goals: dict) -> set[str]:
    north = goals.get("north_star") or []
    lines = (north if isinstance(north, list) else [north]) + list(goals.get("this_week") or [])
    return {w for line in lines for w in words(str(line)) if len(w) > 3}


def order_tasks(tasks: list[dict], today: str, goals: set[str], prefer: list[int] | None = None) -> list[dict]:
    if prefer:
        rank = {tid: i for i, tid in enumerate(prefer)}
        return sorted(tasks, key=lambda t: rank.get(t["id"], len(rank) + t["priority"]))
    return sorted(tasks, key=lambda t: (0 if t.get("due") and t["due"] <= today else 1, t["priority"],
                                        0 if words(t["title"]) & goals else 1, t.get("due") or "9999", t["created"]))


def place(tasks: list[dict], blocks: list[dict], default: int, min_chunk: int = 15) -> tuple[list[dict], list[dict]]:
    """Lay tasks into free blocks in order (a long task may split across blocks). Returns (placed, left over)."""
    room = [[datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])] for b in blocks]
    placed, left = [], []
    for t in tasks:
        need = estimate(t, default)
        parts = []
        for slot in room:
            span = int((slot[1] - slot[0]).total_seconds() // 60)
            if need <= 0:
                break
            if span < min(min_chunk, need):
                continue
            take = min(need, span)
            parts.append((slot[0], slot[0] + timedelta(minutes=take)))
            slot[0] += timedelta(minutes=take)
            need -= take
        if not parts:
            left.append({**_task_view(t, default)})
            continue
        for i, (s, e) in enumerate(parts):
            placed.append({"kind": "task", "title": t["title"], "start": s.isoformat(), "end": e.isoformat(),
                           "minutes": int((e - s).total_seconds() // 60), "task_id": t["id"], "priority": t["priority"],
                           "due": t.get("due"), "profile": t.get("profile"),
                           **({"part": f"{i + 1}/{len(parts)}"} if len(parts) > 1 else {})})
        if need > 0:
            left.append({**_task_view(t, default), "minutes": need, "partial": True})
    gaps = [{"kind": "free", "title": "Open", "start": s.isoformat(), "end": e.isoformat(),
             "minutes": int((e - s).total_seconds() // 60)} for s, e in room if (e - s).total_seconds() >= 15 * 60]
    return placed + gaps, left


def _task_view(t: dict, default: int) -> dict:
    return {"id": t["id"], "title": t["title"], "priority": t["priority"], "due": t.get("due"),
            "minutes": estimate(t, default)}


def _at(day: date, hm: str, tz) -> datetime:
    h, m = (int(x) for x in hm.split(":"))
    return datetime.combine(day, dtime(h, m), tzinfo=tz)


def _mins(a: str, b: str) -> int:
    return int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() // 60)


def build_plan(events: list[dict], tasks: list[dict], now: datetime, day: date, hours: dict, goals: dict, *,
               default: int = 45, prep: int = 30, prefer: list[int] | None = None) -> dict:
    """The whole plan, worked out without a model (``prefer``: a task order the model chose)."""
    tz = now.tzinfo
    ws, we = _at(day, hours["start"], tz), _at(day, hours["end"], tz)
    cursor = now if day == now.date() else None
    timed = [e for e in events if not e["all_day"]]
    streams = [e for e in timed if e.get("profile") == "stream"]
    meetings = [e for e in timed if e.get("profile") != "stream"]
    blocks = [{"kind": "meeting", "title": e["title"], "start": e["start"], "end": e["end"],
               "minutes": _mins(e["start"], e["end"]), "calendar": e.get("calendar")} for e in meetings]
    blocks += [{"kind": "stream", "title": e["title"], "start": e["start"], "end": e["end"],
                "minutes": _mins(e["start"], e["end"])} for e in streams]
    tonight = next((e for e in streams if datetime.fromisoformat(e["start"]) >= (cursor or ws)), None)
    busy = list(timed)
    if tonight and prep:
        s = datetime.fromisoformat(tonight["start"])
        p = {"kind": "stream", "title": "Stream prep", "start": (s - timedelta(minutes=prep)).isoformat(),
             "end": s.isoformat(), "minutes": prep, "all_day": False}
        blocks.append(p)
        busy.append(p)
    today = day.isoformat()
    ordered = order_tasks(tasks, today, goal_words(goals), prefer)
    work = [t for t in ordered if t.get("profile") != "stream"][:8]
    work_free = free_blocks(busy, day, tz, hours["start"], hours["end"], min_minutes=15, now=cursor)
    planned, left = place(work, work_free, default)
    stream_tasks = [t for t in ordered if t.get("profile") == "stream"][:4]
    if tonight and stream_tasks:
        eve = free_blocks(busy, day, tz, hours["end"], datetime.fromisoformat(tonight["start"]).strftime("%H:%M"),
                          min_minutes=15, now=cursor)
        more, left2 = place(stream_tasks, eve, default)
        planned += [b for b in more if b["kind"] == "task"]
        left += left2
    else:
        left += [_task_view(t, default) for t in stream_tasks]
    blocks = sorted(blocks + planned, key=lambda b: b["start"])
    # load: meetings in work hours + must-do tasks (due today or P1) against the work day
    def inside(e):
        s, e2 = max(datetime.fromisoformat(e["start"]), ws), min(datetime.fromisoformat(e["end"]), we)
        return max(0, int((e2 - s).total_seconds() // 60))
    must = [t for t in work if (t.get("due") and t["due"] <= today) or t["priority"] == 1]
    meet_min = sum(inside(e) for e in meetings)
    task_min = sum(estimate(t, default) for t in must)
    cap = int((we - ws).total_seconds() // 60)
    free_left = sum(b["minutes"] for b in work_free)
    over = max(0, meet_min + task_min - cap, task_min - free_left if cursor else 0)
    push, freed = [], 0
    for t in sorted(must, key=lambda t: (1 if t.get("due") and t["due"] <= today else 0, -t["priority"])):
        if freed >= over:
            break
        push.append(t["title"])
        freed += estimate(t, default)
    return {"ok": True, "date": today, "hours": hours, "blocks": blocks,
            "all_day": [e["title"] for e in events if e["all_day"]], "unplanned": left,
            "load": {"meetings_min": meet_min, "tasks_min": task_min, "capacity_min": cap, "free_min": free_left,
                     "over_min": over, "overbooked": over > 0},
            "push": push if over else [],
            "stream": {"title": tonight["title"], "start": tonight["start"]} if tonight else None}


def next_workday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


class Planner(Skill):
    name, title = "planner", "Day plan"
    poll_s = 300
    delay_s = 20

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        self.plan: dict | None = None
        self._order: tuple[str, list[int], str] | None = None  # (date, task ids, headline) from the model
        self.shutdown: dict | None = None
        self.review: dict | None = None
        self.run_command = None

    def start(self, runtime) -> None:
        self.run_command = lambda text: runtime.command(text, source="planner")

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.svc.tz)

    def _hours(self) -> dict:
        return dict(self.svc.cfg["assistant"].get("work_hours") or {"start": "09:00", "end": "18:00"})

    def _events(self, day: date) -> tuple[list[dict], str | None]:
        try:
            events = self.svc.calendars.agenda(1, start=datetime.combine(day, dtime.min, tzinfo=self.svc.tz))
            if not events and not self.svc.calendars.status():
                return [], "No calendar connected (Setup → Google), so meetings aren't in the plan."
            return events, None
        except Exception as exc:
            return [], f"The calendar didn't load ({type(exc).__name__})."

    # ---- plan my day -------------------------------------------------------------------------------------
    def plan_day(self, day: str = "today", polish: bool = True) -> dict:
        now = self.now()
        d = now.date() + timedelta(days=1 if str(day).lower().startswith("tomorrow") else 0)
        events, cal_note = self._events(d)
        tasks = self.svc.storage.list_tasks()
        default = int(self.setting("default_estimate", 45))
        kw = {"default": default, "prep": int(self.setting("stream_prep_minutes", 30))}
        goals = self.svc.cfg.get("goals") or {}
        prefer, headline, by = None, "", "local"
        if self._order and self._order[0] == d.isoformat():
            _, prefer, headline = self._order
            by = "model"
        if polish and tasks:
            draft = build_plan(events, tasks, now, d, self._hours(), goals, **kw)
            ask = self.svc.think(self._prompt(draft, tasks, goals, default), schema=PLAN_SCHEMA)
            if isinstance(ask, dict) and isinstance(ask.get("order"), list):
                ids = {t["id"] for t in tasks}
                prefer = [int(i) for i in ask["order"] if isinstance(i, (int, float)) and int(i) in ids]
                headline, by = str(ask.get("headline") or "")[:160], "model"
                self._order = (d.isoformat(), prefer, headline)
        plan = build_plan(events, tasks, now, d, self._hours(), goals, prefer=prefer, **kw)
        notes = [n for n in (cal_note,) if n]
        if by == "local":
            notes.append("No model connected: tasks are ordered by due date, priority and your goals." if polish
                         else "Ordered by due date, priority and your goals.")
        if not tasks:
            notes.append("No open tasks: add some (“add a task …”) and I'll slot them in.")
        plan.update(headline=headline, by=by, notes=notes, generated=self.clock(), day=d.strftime("%A"))
        if d == now.date():
            self.plan = plan
        return plan

    def _prompt(self, draft: dict, tasks: list[dict], goals: dict, default: int) -> str:
        meetings = [f"{b['start'][11:16]}-{b['end'][11:16]} {b['title']}" for b in draft["blocks"] if b["kind"] != "task"]
        rows = [f"{t['id']}: {t['title']} (P{t['priority']}, due {t.get('due') or '-'}, {estimate(t, default)} min, "
                f"{t.get('profile')})" for t in tasks[:25]]
        return ("Order these open tasks for today, most important first. Due today or overdue comes first, then what "
                "moves the goals. Return task ids only.\nGoals: " + str(goals) + "\nCalendar: " + "; ".join(meetings)
                + "\nTasks:\n" + "\n".join(rows))

    def current(self) -> dict:
        if not self.plan or self.plan["date"] != self.now().date().isoformat():
            self.plan_day(polish=False)
        return self.plan

    def what_next(self) -> dict:
        plan, now = self.current(), self.now().isoformat()
        at = next((b for b in plan["blocks"] if b["start"] <= now < b["end"] and b["kind"] != "free"), None)
        nxt = next((b for b in plan["blocks"] if b["start"] >= (at or {}).get("end", now) and b["kind"] == "task"), None)
        return {"ok": True, "now": at, "next": nxt, "unplanned": plan["unplanned"][:3]}

    def overbooked(self) -> dict:
        p = self.current()
        return {"ok": True, **p["load"], "push": p["push"], "stream": p["stream"]}

    # ---- shut down for the day -------------------------------------------------------------------------------
    def _day_activity(self, d: date) -> dict:
        start = datetime.combine(d, dtime.min, tzinfo=self.svc.tz)
        try:
            segs = self.svc.activity.segments(start.timestamp(), min(self.clock(), (start + timedelta(days=1)).timestamp()))
        except Exception:
            segs = []
        return summarize(segs, d.isoformat())

    def shutdown_day(self) -> dict:
        now = self.now()
        day0 = datetime.combine(now.date(), dtime.min, tzinfo=self.svc.tz)
        done = self.svc.storage.tasks_done_between(day0.timestamp(), now.timestamp())
        act = self._day_activity(now.date())
        focus = self.svc.skills.get("focus") if self.svc.skills is not None else None
        sessions = focus.today().get("minutes", 0) if focus is not None else 0
        deep = sum(s["minutes"] for s in act["focus_sessions"])
        to = next_workday(now.date()).isoformat()
        carried = []
        for t in self.svc.storage.list_tasks():
            if t.get("due") and t["due"] <= now.date().isoformat() and t.get("profile") != "stream":
                self.svc.storage.update_task(t["id"], due=to)
                carried.append(t["title"])
        plan = self.plan_day(polish=False)
        stream = plan["stream"]
        offer = {"label": "Switch to stream mode", "command": "start stream mode"} if stream else None
        self.shutdown = {"ok": True, "date": now.date().isoformat(), "at": self.clock(), "done": [t["title"] for t in done],
                         "work_min": round(act["by_category"].get("work", 0) / 60), "deep_min": deep,
                         "focus_min": sessions, "active_min": round(act["active_seconds"] / 60), "carried": carried,
                         "carried_to": to, "open": len(self.svc.storage.list_tasks()), "stream": stream, "offer": offer,
                         "tracked": bool(act["active_seconds"])}
        return self.shutdown

    def stream_mode(self) -> dict:
        if not self.run_command:
            return {"ok": False, "error": "Vesper is still starting."}
        out = self.run_command("start stream mode") or {}
        if self.shutdown:
            self.shutdown["offer"] = None
        return {"ok": True, "reply": out.get("reply") if isinstance(out, dict) else None}

    # ---- weekly review -------------------------------------------------------------------------------------
    def weekly_review(self) -> dict:
        now = self.now()
        week0 = datetime.combine(now.date() - timedelta(days=now.weekday()), dtime.min, tzinfo=self.svc.tz)
        cats, deep, switches, active_days = defaultdict(float), [], [], 0
        d = week0.date()
        while d <= now.date():
            s = self._day_activity(d)
            for k, v in s["by_category"].items():
                cats[k] += v
            deep += s["focus_sessions"]
            if s["switches_per_hour"]:
                switches.append(s["switches_per_hour"])
            active_days += bool(s["active_seconds"])
            d += timedelta(days=1)
        done = self.svc.storage.tasks_done_between(week0.timestamp(), now.timestamp())
        try:
            evs = [e for e in self.svc.calendars.agenda(7, start=week0) if not e["all_day"] and e["start"] < now.isoformat()]
        except Exception:
            evs = []
        streams = [e for e in evs if e.get("profile") == "stream"]
        hours = {k: round(v / 3600, 1) for k, v in sorted(cats.items(), key=lambda kv: -kv[1]) if v >= 360}
        active_h = round(sum(v for k, v in cats.items() if k != IDLE) / 3600, 1)
        sw = round(sum(switches) / len(switches)) if switches else 0
        wins = [w for w in (done and f"{_n(len(done), 'task')} closed",
                            deep and f"{_n(len(deep), 'deep-work session')}, longest {max(x['minutes'] for x in deep)} min",
                            hours.get("stream") and f"{hours['stream']} h on stream") if w]
        leaks = [x for x in (hours.get("other", 0) >= 3 and f"{hours['other']} h on things outside work and stream",
                             hours.get(IDLE, 0) >= 5 and f"{hours.get(IDLE)} h idle at the PC",
                             sw > 30 and f"about {sw} app switches an hour") if x]
        tops = order_tasks(self.svc.storage.list_tasks(), now.date().isoformat(), goal_words(self.svc.cfg.get("goals") or {}))
        nxt = [t["title"] for t in tops[:3]] or list((self.svc.cfg.get("goals") or {}).get("this_week") or [])[:3]
        r = {"ok": True, "week_of": week0.date().isoformat(), "hours": hours, "active_h": active_h,
             "days_tracked": active_days, "tasks_done": len(done), "done_titles": [t["title"] for t in done[:8]],
             "meetings": len(evs) - len(streams), "streams": len(streams), "deep_sessions": len(deep),
             "wins": wins, "leaks": leaks, "next_week": nxt, "by": "local", "at": self.clock()}
        ask = self.svc.think("Write J's weekly review from this data: 2-3 wins, 1-3 time leaks (cite numbers), and next "
                             "week's top 3 moves tied to the goals. Never invent numbers.\nGoals: "
                             f"{self.svc.cfg.get('goals')}\nData: {r}", schema=REVIEW_SCHEMA)
        if isinstance(ask, dict) and all(isinstance(ask.get(k), list) for k in ("wins", "leaks", "next_week")):
            r.update({k: [str(x)[:140] for x in ask[k]][:3] for k in ("wins", "leaks", "next_week")}, by="model")
        self.review = r
        return r

    # ---- skill surface ---------------------------------------------------------------------------------------
    def tick(self) -> None:
        try:
            self.plan_day(polish=False)
            now = self.now()
            day, at = int(self.setting("review_weekday", 4)), str(self.setting("review_time", "16:00"))
            if now.weekday() == day and now.strftime("%H:%M") >= at:
                self.nudge("It's the end of the week. Want your weekly review? Say “weekly review”.",
                           key=f"planner:review:{now.strftime('%G-%V')}", expires_in=4 * 3600,
                           action={"label": "Weekly review", "command": "weekly review"})
        except Exception as exc:
            self.plan = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "blocks": [], "date": ""}
        self.publish()

    def status(self) -> dict:
        plan = self.plan if self.plan and self.plan.get("date") == self.now().date().isoformat() else None
        if plan is None:
            try:
                plan = self.current()
            except Exception as exc:
                plan = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "blocks": []}
        today = self.now().date().isoformat()
        return {"plan": plan, "now": self.clock(), "shutdown": self.shutdown if (self.shutdown or {}).get("date") == today else None,
                "review": self.review}

    def actions(self):
        return {"replan": lambda b: self.plan_day(polish=True), "shutdown": lambda b: self.shutdown_day(),
                "review": lambda b: self.weekly_review(), "stream_mode": lambda b: self.stream_mode()}

    def briefing(self):
        p = self.plan_day(polish=False)
        tasks = [b for b in p["blocks"] if b["kind"] == "task"]
        if not tasks and not p["load"]["overbooked"]:
            return None
        return {"first": [f"{_t(b['start'])} {b['title']}" for b in tasks[:3]],
                "overbooked": f"{duration(p['load']['over_min'] * 60)} over; push {', '.join(p['push'])}" if p["push"] else None,
                "stream": _t(p["stream"]["start"]) if p["stream"] else None}

    def tools(self):
        return [
            Tool("plan_day", "Plan the user's day (or tomorrow): meetings, stream, and open tasks time-blocked into free "
                             "time, with an overbooked warning. day: today or tomorrow.",
                 obj({"day": {"type": "string", "enum": ["today", "tomorrow"]}}), self.plan_day),
            Tool("what_next", "What the user should be doing right now and next, from today's plan.", obj(), self.what_next),
            Tool("am_i_overbooked", "Whether today's meetings plus must-do tasks fit the work hours, and what to push.",
                 obj(), self.overbooked),
            Tool("shutdown_day", "End the work day: recap what got done and focus time, roll open tasks due today to the "
                                 "next workday, and offer stream mode if a stream is on tonight.", obj(),
                 self.shutdown_day, tier=1),
            Tool("weekly_review", "This week in review: hours by category, tasks done, meetings, streams, wins, leaks and "
                                  "next week's top 3.", obj(), self.weekly_review),
        ]

    def route(self, t: str):
        m = re.fullmatch(r"(?:plan|schedule|time ?block) (?:out )?(?:my |the )?(day|today|tomorrow)(?: for me)?|"
                         r"(?:make|build|give me) (?:me )?(?:a |my )?(?:day )?plan(?: for)?(?: (today|tomorrow))?|re-?plan(?: my day)?", t)
        if m:
            return Intent("tool", "plan_day", {"day": "tomorrow" if "tomorrow" in t else "today"})
        if re.fullmatch(r"what should i (?:be )?(?:work(?:ing)? on|do|focus on)(?: (?:now|next|right now))?|what'?s next on my plan", t):
            return Intent("tool", "what_next")
        if re.fullmatch(r"am i (?:over ?booked|over ?committed|too busy)(?: today)?|(?:do|will) i have (?:enough )?time today|"
                        r"is my day (?:too )?(?:full|packed)", t):
            return Intent("tool", "am_i_overbooked")
        if re.fullmatch(r"(?:shut ?down|wrap up|close out|end) (?:for the day|my (?:work ?)?day|the (?:work ?)?day|work)|"
                        r"i'?m (?:done|finished) (?:for the day|with work)(?: for (?:the day|today))?|end of (?:my )?work ?day", t):
            return Intent("tool", "shutdown_day")
        if re.fullmatch(r"(?:my |the |do (?:my|the) |run (?:my|the) |give me (?:my|the) )?(?:weekly|week'?s|week in) review|"
                        r"how did (?:my|this) week go|review (?:my|the|this) week", t):
            return Intent("tool", "weekly_review")
        return None

    def say(self, tool, args, result):
        if result.get("ok") is False:
            return result.get("error")
        live, r = bool(self.svc.live and self.svc.live()), result
        push = f" Push {' and '.join(r.get('push', [])[:2])} to tomorrow?" if r.get("push") and not live else ""
        if tool == "plan_day":
            tasks = [b for b in r["blocks"] if b["kind"] == "task"]
            head = f"{_n(sum(b['kind'] == 'meeting' for b in r['blocks']), 'meeting')} and {_n(len(tasks), 'task block')}"
            if live:
                return f"{head}. The plan's on the HUD."
            out = (r["headline"] + " " if r.get("headline") else "") + head
            out += f". First: {tasks[0]['title']} at {_t(tasks[0]['start'])}." if tasks else "."
            if r["load"]["overbooked"]:
                out += f" You're {duration(r['load']['over_min'] * 60)} over." + push
            return out + (f" Stream at {_t(r['stream']['start'])}." if r["stream"] else "")
        if tool == "what_next":
            at, nxt = r["now"], r["next"]
            if live or not (at or nxt):
                return "It's on the HUD plan." if live else "Nothing else planned. Say “plan my day” to lay out your tasks."
            return " ".join(([f"Right now: {at['title']} until {_t(at['end'])}."] if at else [])
                            + ([f"{'Then' if at else 'Next'}: {nxt['title']} at {_t(nxt['start'])}."] if nxt else []))
        if tool == "am_i_overbooked":
            line = (f"{duration(r['meetings_min'] * 60) if r['meetings_min'] else 'No time'} in meetings and "
                    f"{duration(r['tasks_min'] * 60) if r['tasks_min'] else 'nothing'} of must-do tasks")
            return f"{line}: that's {duration(r['over_min'] * 60)} over.{push}" if r["overbooked"] else \
                f"You're fine: {line}, inside your work hours."
        if tool == "shutdown_day":
            out = f"Shutting down. You closed {_n(len(r['done']), 'task')}"
            if r["tracked"]:
                deep = max(r["deep_min"], r["focus_min"])
                out += f", {duration(r['work_min'] * 60)} on work" + (f", {duration(deep * 60)} of it deep" if deep else "")
            out += "."
            if r["carried"]:
                c = len(r["carried"])
                out += f" {_n(c, 'task')} roll{'s' if c == 1 else ''} to {date.fromisoformat(r['carried_to']).strftime('%A')}."
            if r["stream"]:
                out += f" Stream at {_t(r['stream']['start'])}. Want stream mode? Say “start stream mode”, or tap the button on the HUD."
            return out
        if tool == "weekly_review":
            work = f", {r['hours']['work']} on work" if r["hours"].get("work") else ""
            out = (f"This week: {r['active_h']} hours active{work}, {_n(r['tasks_done'], 'task')} done, "
                   f"{_n(r['meetings'], 'meeting')}, {_n(r['streams'], 'stream')}.")
            out += f" Win: {r['wins'][0]}." if r["wins"] else ""
            out += f" Leak: {r['leaks'][0]}." if r["leaks"] else ""
            return out + (f" Next week, start with {r['next_week'][0]}." if r["next_week"] and not live else "")
        return None


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _t(iso: str) -> str:
    return clock(datetime.fromisoformat(iso))
