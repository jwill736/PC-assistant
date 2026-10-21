"""Focus blocks on the calendar: "protect 2 hours tomorrow for the Q3 deck", "block my afternoon for deep work".

- Finds free time across every calendar on the agenda (work, personal and stream all count as busy), inside the work
  hours (assistant.work_hours) or the part of the day asked for (morning / afternoon / evening).
- Creates a "busy" event called "🔒 <what>" on the right Google calendar: the work calendar for work, the personal
  one when the request says personal / gym / family / stream (skills.focusblocks.calendars picks others).
- It writes to J's calendar, so it's tier 2: Vesper says the exact day and time first and waits for his yes.
- The Day plan's blocks can go on the calendar too: "put my plan on my calendar", or the button under Day plan.

Needs the "Calendar events" box on Google's page (google.can); says which account and box when it's missing.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, time as dtime, timedelta

from ..brain.router import Intent, number
from ..brain.tools import Tool, obj
from ..integrations.calendars import free_blocks
from ..integrations.google import EVENTS_SCOPE, GoogleError
from . import Skill
from .people import account_kind, on_air

LOCK = "🔒"
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PARTS = {"morning": ("start", "12:00"), "afternoon": ("12:00", "end"), "evening": ("18:00", "22:00"),
         "day": ("start", "end"), "any": ("start", "end")}
PERSONAL = re.compile(r"\b(?:personal|gym|workout|family|kids?|doctor|dentist|errands?|stream(?:ing)?|twitch|"
                      r"video|editing|youtube|home)\b", re.I)
VERB = r"(?:protect|block(?: off| out)?|reserve|hold|save|carve out|set aside|book|put aside|guard)"
NOT_TIME = re.compile(r"\b(?:sites?|websites?|reddit|twitter|apps?|distractions?|notifications?|calls? from|"
                      r"youtube(?! (?:video|thumbnail|upload|script))|him|her|them|that (?:number|user|person))\b")
SPAN = re.compile(r"\b(?:\d+(?:\.\d+)?|an?|one|two|three|four|five|six|half an?|ninety|forty[- ]five|thirty)"
                  r"(?: and a half)? (?:hours?|hrs?|h|minutes?|mins?)\b(?: and a half)?")


def parse_minutes(text: str) -> int | None:
    m = re.search(r"\b(\d+(?:\.\d+)?|an?|one|two|three|four|five|six|half an?|ninety|forty[- ]five|thirty)"
                  r"( and a half)? (hours?|hrs?|h|minutes?|mins?)\b( and a half)?", text)
    if not m:
        return None
    n_raw = m.group(1)
    n = 0.5 if n_raw.startswith("half") else 1.0 if n_raw in ("a", "an", "one") else \
        float(n_raw) if re.fullmatch(r"\d+(?:\.\d+)?", n_raw) else float(number(n_raw.replace("-", " ")) or 0)
    if m.group(2) or m.group(4):
        n += 0.5
    return int(n * 60) if m.group(3).startswith("h") else int(n)


def parse_day(text: str, today: date) -> date | None:
    if re.search(r"\btomorrow\b", text):
        return today + timedelta(days=1)
    if re.search(r"\b(?:today|this (?:morning|afternoon|evening)|tonight)\b", text):
        return today
    m = re.search(r"\b(?:(next|on) )?(" + "|".join(DAYS) + r")\b", text)
    if m:  # "friday": the coming one (today if it's Friday); "next friday": never today
        ahead = (DAYS.index(m.group(2)) - today.weekday()) % 7
        return today + timedelta(days=ahead or (7 if m.group(1) == "next" else 0))
    m = re.search(r"\b(\d{4}-\d\d-\d\d)\b", text)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            return None
    return None


def parse_request(t: str) -> dict | None:
    """'protect 2 hours tomorrow for the q3 deck' -> {minutes, day, part, title, profile}; None if it isn't one."""
    m = re.fullmatch(rf"{VERB}(?: me)?(?: some)? (.+)", t)
    if not m:
        return None
    rest = m.group(1)
    if NOT_TIME.search(rest):  # "block youtube for 30 minutes" is the focus skill's
        return None
    minutes = parse_minutes(rest)
    part = next((p for p in ("morning", "afternoon", "evening") if re.search(rf"\b{p}\b", rest)), None)
    if minutes is None and part is None and not re.search(r"\b(?:focus|deep work)\b", rest):
        return None
    purpose = re.search(r"\b(?:for|to work on|to focus on|to do) (?!(?:me|my calendar|the calendar)\b)(.+)$", rest)
    what = purpose.group(1) if purpose else ""
    what = re.sub(r"\b(?:tomorrow|today|tonight|this (?:morning|afternoon|evening)|(?:next |on )?(?:" + "|".join(DAYS) +
                  r"))\b(?: (?:morning|afternoon|evening))?", "", SPAN.sub("", what))
    what = re.sub(r"\s{2,}", " ", what).strip(" ,")
    what = re.sub(r"^(?:the|my|a|an|some) ", "", what)
    if not what and re.search(r"\bdeep work\b", rest):
        what = "deep work"
    args = {"title": (what[:1].upper() + what[1:])[:80] if what else "Focus time"}
    if minutes:
        args["minutes"] = minutes
    if part:
        args["part"] = part
    day = re.search(r"\b(tomorrow|today|tonight|this (?:morning|afternoon|evening)|(?:next )?(?:" + "|".join(DAYS) + r"))\b", rest)
    if day:
        args["day"] = "today" if day.group(1).startswith(("this", "tonight")) else day.group(1)
    if PERSONAL.search(rest):
        args["profile"] = "personal"
    return args


def _hm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%I:%M %p").lstrip("0").replace(":00", "")


def span_text(start: str, end: str, today: date) -> str:
    s = datetime.fromisoformat(start)
    day = "today" if s.date() == today else "tomorrow" if s.date() == today + timedelta(days=1) else s.strftime("%A")
    a, b = _hm(start), _hm(end)
    if a[-2:] == b[-2:]:
        a = a[:-3]
    return f"{day} ({s.strftime('%a %b')} {s.day}), {a}–{b}"


class FocusBlocks(Skill):
    name, title = "focusblocks", "Focus blocks"
    poll_s = 0

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        self.runtime = None
        svc.storage.execute("CREATE TABLE IF NOT EXISTS focusblocks_made (id INTEGER PRIMARY KEY, account TEXT NOT NULL, "
                            "calendar TEXT NOT NULL, event_id TEXT, title TEXT NOT NULL, start TEXT NOT NULL, end_at TEXT NOT NULL, "
                            "link TEXT, ts REAL NOT NULL)")
        svc.bus.on(self._on_event)

    def start(self, runtime) -> None:
        self.runtime = runtime

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.svc.tz)

    # ---- which calendar -----------------------------------------------------------------------------------
    def calendar_for(self, profile: str) -> tuple[dict | None, str]:
        """({account, id, name}, "") for an area, or (None, what J should do)."""
        g = self.svc.google
        if g is None or not g.configured or not g.emails():
            return None, "No Google account is connected: Setup → Google accounts."
        want = (self.setting("calendars") or {}).get(profile)
        chosen = (self.svc.cfg.get("google") or {}).get("calendars") or []
        kind = "work" if profile == "work" else "personal"
        cands = [c for c in chosen if c.get("profile") == profile] + \
            [c for c in chosen if c.get("profile") != profile and account_kind(c.get("account", "")) == kind]
        cands += [{"account": e, "id": e, "name": e} for e in g.emails() if account_kind(e) == kind]
        cands += [{"account": e, "id": e, "name": e} for e in g.emails()] if profile != "work" else []
        cands.sort(key=lambda c: c.get("id") != c.get("account"))  # each account's own calendar first, stable otherwise
        if isinstance(want, dict) and want.get("account") and want.get("id"):
            cands.insert(0, want)  # skills.focusblocks.calendars.<area>: {account, id}
        for c in cands:
            if g.can(c["account"], EVENTS_SCOPE):
                return {"account": c["account"], "id": c["id"], "name": c.get("name") or c["id"]}, ""
        accts = sorted({c["account"] for c in cands if c.get("account") in g.emails()}) or g.emails()
        return None, g.need(EVENTS_SCOPE, "Putting time on your calendar", accts)

    # ---- finding the time ----------------------------------------------------------------------------------
    def _hours(self) -> dict:
        return dict(self.svc.cfg["assistant"].get("work_hours") or {"start": "09:00", "end": "18:00"})

    def find_slot(self, minutes: int | None, day: str, part: str) -> dict:
        now = self.now()
        d = parse_day(str(day or "").lower(), now.date()) or now.date()
        hours = self._hours()
        lo, hi = PARTS.get(part or "any", PARTS["any"])
        lo, hi = hours["start"] if lo == "start" else lo, hours["end"] if hi == "end" else hi
        if part == "afternoon" and hi <= lo:
            hi = "18:00"
        try:
            events = self.svc.calendars.agenda(1, start=datetime.combine(d, dtime.min, tzinfo=self.svc.tz))
        except Exception as exc:
            return {"ok": False, "error": f"The calendar didn't load ({type(exc).__name__})."}
        start_at = now + timedelta(minutes=(15 - now.minute % 15) % 15 or 0) if d == now.date() else None
        need = int(minutes or 0)
        blocks = free_blocks(events, d, self.svc.tz, lo, hi, min_minutes=max(15, need or 30), now=start_at)
        if not blocks:
            free = free_blocks(events, d, self.svc.tz, lo, hi, min_minutes=15, now=start_at)
            longest = max(free, key=lambda b: b["minutes"]) if free else None
            when = "today" if d == now.date() else "tomorrow" if d == now.date() + timedelta(days=1) else d.strftime("%A")
            window = f" {part}" if part in ("morning", "afternoon", "evening") else ""
            msg = f"No free {_dur(need) + ' ' if need else ''}stretch {when}{window}"
            msg += f" (the longest is {_dur(longest['minutes'])} at {_hm(longest['start'])})." if longest else "."
            return {"ok": False, "error": msg}
        if need:
            b = blocks[0]
            s = datetime.fromisoformat(b["start"])
            return {"ok": True, "start": s.isoformat(), "end": (s + timedelta(minutes=need)).isoformat()}
        b = max(blocks, key=lambda x: x["minutes"])  # "block my afternoon": the longest free stretch in it
        return {"ok": True, "start": b["start"], "end": b["end"]}

    # ---- the tool: tier 2 once there's a time to show ------------------------------------------------------------
    def _resolve(self, a: dict) -> bool:
        """Work out the exact time and calendar once, and pin them into the call (what J says yes to is what's made)."""
        if a.get("start") and a.get("end") and a.get("calendar"):
            return True
        cal, why = self.calendar_for(a.get("profile") or "work")
        if cal is None:
            a["_error"] = why
            return False
        slot = self.find_slot(a.get("minutes"), a.get("day") or "today", a.get("part") or "any")
        if not slot["ok"]:
            a["_error"] = slot["error"]
            return False
        a.update(start=slot["start"], end=slot["end"], calendar=cal["id"], account=cal["account"], calendar_name=cal["name"])
        return True

    def _tier(self, a: dict) -> int:
        return 2 if self._resolve(a) else 0  # nothing to write: the error comes straight back

    def _describe(self, a: dict) -> str:
        if not self._resolve(a):
            return a.get("_error") or "protect time"
        area = "work" if account_kind(a["account"]) == "work" else "personal"
        return (f"put “{LOCK} {a.get('title') or 'Focus time'}” on your {area} calendar "
                f"{span_text(a['start'], a['end'], self.now().date())}, shown as busy")

    def protect_time(self, minutes: int | None = None, day: str = "today", part: str = "", title: str = "Focus time",
                     profile: str = "work", start: str = "", end: str = "", calendar: str = "", account: str = "",
                     calendar_name: str = "", _error: str = "") -> dict:
        if _error and not (start and end):
            return {"ok": False, "error": _error}
        a = {"minutes": minutes, "day": day, "part": part, "title": title, "profile": profile, "start": start, "end": end,
             "calendar": calendar, "account": account}
        if not self._resolve(a):
            return {"ok": False, "error": a["_error"]}
        return self._create(a["account"], a["calendar"], a.get("title") or "Focus time", a["start"], a["end"])

    def _create(self, account: str, calendar: str, title: str, start: str, end: str) -> dict:
        s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
        clash = [x for x in self._events(s.date()) if not x.get("all_day")
                 and datetime.fromisoformat(x["start"]) < e and datetime.fromisoformat(x["end"]) > s
                 and not x["title"].startswith(LOCK)]
        if clash:
            return {"ok": False, "error": f"That time just got booked ({clash[0]['title']}). Ask again and I'll find another."}
        name = re.sub(r"^\W*🔒\s*", "", title).strip() or "Focus time"
        tzname = getattr(self.svc.tz, "key", None)
        body = {"summary": f"{LOCK} {name}", "transparency": "opaque", "description": "Focus time protected by Vesper.",
                "start": {"dateTime": s.isoformat(), **({"timeZone": tzname} if tzname else {})},
                "end": {"dateTime": e.isoformat(), **({"timeZone": tzname} if tzname else {})},
                "reminders": {"useDefault": False}, "extendedProperties": {"private": {"vesper": "focusblock"}}}
        try:
            ev = self.svc.google.insert_event(account, calendar, body)
        except GoogleError as exc:
            return {"ok": False, "error": str(exc)}
        self.svc.storage.execute("INSERT INTO focusblocks_made(account, calendar, event_id, title, start, end_at, link, ts) "
                                 "VALUES (?,?,?,?,?,?,?,?)", (account, calendar, ev.get("id"), f"{LOCK} {name}", start, end,
                                                               ev.get("htmlLink") or "", self.clock()))
        try:
            self.svc.calendars.refresh(force=True)
        except Exception:
            pass
        self.publish()
        return {"ok": True, "title": f"{LOCK} {name}", "start": start, "end": end, "account": account_kind(account),
                "calendar": calendar, "link": ev.get("htmlLink") or "", "id": ev.get("id")}

    def _events(self, d: date) -> list[dict]:
        try:
            return self.svc.calendars.agenda(1, start=datetime.combine(d, dtime.min, tzinfo=self.svc.tz))
        except Exception:
            return []

    # ---- the day plan's blocks -----------------------------------------------------------------------------------
    def plan_blocks(self) -> list[dict]:
        """Today's task blocks from the Day plan that haven't started, joined when back to back."""
        pl = self.svc.skills.get("planner") if self.svc.skills is not None else None
        if pl is None:
            return []
        try:
            plan = pl.current()
        except Exception:
            return []
        now = self.now().isoformat()
        out = []
        for b in plan.get("blocks") or []:
            if b.get("kind") != "task" or b["start"] < now:
                continue
            if out and out[-1]["task_id"] == b.get("task_id") and out[-1]["end"] == b["start"]:
                out[-1]["end"] = b["end"]
                continue
            out.append({"title": re.sub(r"\s*[(\[~]\s*\d+(?:\.\d+)?\s*(?:m|min|mins|minutes|h|hr|hrs|hours)\b[)\]]?", "",
                                        b["title"]).strip(), "start": b["start"], "end": b["end"],
                        "profile": "work" if (b.get("profile") or "work") == "work" else "personal", "task_id": b.get("task_id")})
        return out[:8]

    def _resolve_plan(self, a: dict) -> bool:
        if a.get("blocks"):
            return True
        blocks = self.plan_blocks()
        if not blocks:
            a["_error"] = "No task blocks left in today's plan. Say “plan my day” first."
            return False
        pinned = []
        for b in blocks:
            cal, why = self.calendar_for(b["profile"])
            if cal is None:
                a["_error"] = why
                return False
            pinned.append({**b, "account": cal["account"], "calendar": cal["id"]})
        a["blocks"] = pinned
        return True

    def calendar_plan(self, blocks: list | None = None, _error: str = "") -> dict:
        a = {"blocks": blocks or []}
        if not self._resolve_plan(a):
            return {"ok": False, "error": _error or a["_error"]}
        made, errors = [], []
        for b in a["blocks"]:
            r = self._create(b["account"], b["calendar"], b["title"], b["start"], b["end"])
            (made if r.get("ok") else errors).append(r if r.get("ok") else f"{b['title']}: {r.get('error')}")
        return {"ok": bool(made), "made": made, "errors": errors, "count": len(made),
                **({} if made else {"error": "; ".join(errors)})}

    def _describe_plan(self, a: dict) -> str:
        if not self._resolve_plan(a):
            return a.get("_error") or "put the plan on the calendar"
        parts = [f"{LOCK} {b['title']} {_hm(b['start'])}–{_hm(b['end'])}" for b in a["blocks"]]
        return f"put {len(parts)} block{'s' if len(parts) != 1 else ''} from today's plan on your calendar: " + "; ".join(parts)

    # ---- skill surface ---------------------------------------------------------------------------------------
    def tools(self):
        return [
            Tool("protect_time", "Block time on J's Google calendar for focused work (a busy '🔒 <title>' event in "
                                 "free time). minutes (empty: the longest free stretch), day (today, tomorrow, a "
                                 "weekday or YYYY-MM-DD), part (morning, afternoon, evening), title, profile (work or "
                                 "personal). J confirms the exact time first.",
                 obj({"minutes": {"type": "integer"}, "day": {"type": "string"}, "title": {"type": "string"},
                      "part": {"type": "string", "enum": ["morning", "afternoon", "evening", "any"]},
                      "profile": {"type": "string", "enum": ["work", "personal"]}}),
                 self.protect_time, tier=self._tier, confirm_text=self._describe),
            Tool("calendar_plan_blocks", "Put today's Day plan task blocks on J's Google calendar as busy focus "
                                         "blocks (J confirms the list first).", obj(), self.calendar_plan,
                 tier=lambda a: 2 if self._resolve_plan(a) else 0, confirm_text=self._describe_plan),
        ]

    def route(self, t: str):
        if re.fullmatch(r"(?:put|add) (?:these|those|the|my|today'?s) (?:plan |day plan )?(?:blocks |plan )?(?:on|to|in) "
                        r"(?:my|the) calendar|(?:put|add) my (?:day )?plan (?:on|to|in) (?:my|the) calendar|"
                        r"block (?:out )?my plan(?: on (?:my|the) calendar)?", t):
            return Intent("tool", "calendar_plan_blocks", {})
        args = parse_request(t)
        return Intent("tool", "protect_time", args) if args else None

    def say(self, tool, args, result):
        if tool == "calendar_plan_blocks":
            n = result.get("count", 0)
            return f"Done: {n} focus block{'s' if n != 1 else ''} on your calendar." + \
                (f" {len(result['errors'])} didn't fit." if result.get("errors") else "")
        if tool != "protect_time":
            return None
        when = span_text(result["start"], result["end"], self.now().date()).split(" (")[0]
        tm = f"{_hm(result['start'])} to {_hm(result['end'])}"
        if on_air(self.svc):
            return f"Done: {when} {tm} is blocked on your calendar."
        return f"Done: {result['title']} is on your {result['account']} calendar {when}, {tm}, shown as busy."

    def status(self) -> dict:
        rows = self.svc.storage.query("SELECT * FROM focusblocks_made WHERE end_at > ? ORDER BY start LIMIT 8",
                                      (self.now().isoformat(),))
        work, why_w = self.calendar_for("work")
        pers, why_p = self.calendar_for("personal")
        out = {"ok": bool(work or pers), "made": [] if on_air(self.svc) else
               [{"title": r["title"], "start": r["start"], "end": r["end_at"], "link": r["link"],
                 "account": account_kind(r["account"])} for r in rows],
               "calendars": {"work": work, "personal": pers}, "plan_blocks": len(self.plan_blocks())}
        if on_air(self.svc):
            out["live"] = True
        if not (work or pers):
            out["error"] = why_w or why_p
        elif not work or not pers:
            out["missing"] = why_w or why_p
        return out

    def actions(self):
        def ask(name: str, args: dict) -> dict:
            if self.runtime is None:
                return {"ok": False, "error": "Vesper isn't running yet."}
            out = self.runtime.assistant.run_tool(name, args)  # tier 2: parks it and waits for J's yes
            data = out.get("data") or {}
            if out.get("kind") == "pending":
                return {"ok": True, "pending": True, "reply": out.get("reply")}
            if out.get("kind") == "error" or data.get("ok") is False:
                return {"ok": False, "error": data.get("error") or out.get("reply")}
            return {"ok": True, "reply": out.get("reply")}

        def propose(b):
            text = str(b.get("text") or "").strip().lower()
            args = parse_request(text if re.match(VERB, text) else f"protect {text}")
            if not args:
                return {"ok": False, "error": "Say it like “2 hours tomorrow for the Q3 deck”."}
            return ask("protect_time", args)
        return {"propose": propose, "plan": lambda b: ask("calendar_plan_blocks", {})}

    def _on_event(self, ev: dict) -> None:
        if ev.get("type") == "planner":  # a re-plan: the "put these on my calendar" offer follows it
            try:
                self.publish()
            except Exception:
                pass


def _dur(m: int) -> str:
    return f"{m // 60}h{f' {m % 60}m' if m % 60 else ''}" if m >= 60 else f"{m}m"
