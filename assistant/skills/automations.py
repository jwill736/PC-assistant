"""Automations: reminders, timers, routines on a schedule, and "when X happens, do Y".

Said out loud, understood without a model:
- "remind me in 20 minutes to call Dana", "remind me at 3 pm to send the invoice", "remind me tomorrow at 9 to ..."
- "remind me after the stream to post the VOD", "remind me when I'm back at my PC to ...", "... when Dana emails me"
- "set a timer for 10 minutes", "a 25 minute timer called pasta", "how long is left on the timer", "cancel the timer"
- "every weekday at 9 am check my email", "every friday at 4 pm recap my day": a routine runs that command as if you
  said it (anything risky still waits for your yes)
- "what are my reminders", "cancel the reminder about Dana"

Saved in data/automations.json; reminders fire through the nudges queue so they never go out on air.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

from ..brain.router import Intent, number
from ..brain.tools import Tool, obj
from . import Skill

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
TRIGGERS = {"after_stream": "after the stream", "back": "when you're back at the PC", "job_done": "when a job finishes"}


# ---- understanding times -------------------------------------------------------------------------------------

def _num(w: str) -> float | None:
    w = w.strip()
    if w in ("a", "an", "one"):
        return 1
    if w in ("half an", "half a"):
        return 0.5
    try:
        return float(w)
    except ValueError:
        return number(w)


def parse_duration(text: str) -> float | None:
    """'20 minutes', 'an hour and a half', 'two hours', '90 seconds', '1h 30m' -> seconds."""
    t = text.lower().strip()
    if re.fullmatch(r"(?:an? )?hour and a half", t):
        return 5400
    if re.fullmatch(r"half an? hour", t):
        return 1800
    total, found = 0.0, False
    unit = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)"
    pattern = (r"(\d+(?:\.\d+)?)\s*(" + unit[1:-1] + r"|s|m|h|d)\b"  # 20 minutes, 20m
               r"|\b(an?|one|[a-z]+(?:[ -][a-z]+)?)\s+" + unit + r"\b")  # twenty minutes, forty-five minutes
    for m in re.finditer(pattern, t):
        n = _num(m.group(1) or m.group(3))
        if n is None and m.group(3):  # "and twenty minutes": the number is the last word or two
            words = m.group(3).split()
            n = _num(" ".join(words[-2:])) if len(words) > 1 else None
            n = n if n is not None else _num(words[-1])
        if n is None:
            continue
        u = (m.group(2) or m.group(4))[0]
        total += n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]
        found = True
    return total if found else None


def parse_clock(text: str) -> tuple[int, int] | None:
    t = text.lower().strip().replace(".", "")
    if t in ("noon", "midday"):
        return 12, 0
    if t == "midnight":
        return 0, 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a m|p m)?|([a-z]+(?:[ -][a-z]+)?)\s*(am|pm|o'?clock)?", t)
    if not m:
        return None
    if m.group(1):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(" ", "")
    else:
        h = number(m.group(4) or "")
        if h is None:
            return None
        mi, ap = 0, (m.group(5) or "").replace("o'clock", "").replace("oclock", "")
    if h > 23 or mi > 59:
        return None
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 6:  # "at 3" in the afternoon, not 3 am
        h += 12
    return h, mi


def parse_when(text: str, now: datetime) -> datetime | None:
    """'in 20 minutes', 'at 3 pm', 'tomorrow at 9', 'tonight', 'friday at 10', 'tomorrow' -> a time."""
    t = text.lower().strip()
    m = re.fullmatch(r"in (.+)", t)
    if m:
        secs = parse_duration(m.group(1))
        return now + timedelta(seconds=secs) if secs else None
    day = now.date()
    m = re.match(r"(today|tonight|tomorrow(?: morning| afternoon| evening| night)?|(?:on |next )?(?:" + "|".join(DAYS) + r"))\s*(.*)$", t)
    rest = t
    default = None
    if m:
        word, rest = m.group(1), m.group(2)
        if word.startswith("tomorrow"):
            day = day + timedelta(days=1)
            default = {"morning": (9, 0), "afternoon": (14, 0), "evening": (19, 0), "night": (21, 0)}.get(word.split()[-1], (9, 0))
        elif word == "tonight":
            default = (20, 0)
        elif word != "today":
            target = DAYS.index(word.split()[-1])
            ahead = (target - now.weekday()) % 7 or 7
            day = day + timedelta(days=ahead)
            default = (9, 0)
    m2 = re.fullmatch(r"(?:at |by |around )?(.+)", rest.strip()) if rest.strip() else None
    hm = parse_clock(m2.group(1)) if m2 else default
    if hm is None:
        return None
    out = datetime.combine(day, datetime.min.time()).replace(hour=hm[0], minute=hm[1], tzinfo=now.tzinfo)
    if out <= now and not m:  # "at 9" when it's already 10: tomorrow
        out += timedelta(days=1)
    return out


def parse_every(text: str) -> dict | None:
    """'every weekday at 9 am', 'every day at 8', 'every monday and thursday at 4 pm', 'weekdays at 9' -> schedule."""
    m = re.fullmatch(r"(?:every |each |on )?(day|weekday|weekdays|weekend|weekends|morning|evening|night|(?:(?:"
                     + "|".join(DAYS) + r")s?(?:(?:,| and|, and) )?)+)\s*(?:at (.+))?", text.lower().strip())
    if not m:
        return None
    what, at = m.group(1), m.group(2)
    days = {"day": list(range(7)), "morning": list(range(7)), "evening": list(range(7)), "night": list(range(7)),
            "weekday": list(range(5)), "weekdays": list(range(5)), "weekend": [5, 6], "weekends": [5, 6]}.get(what)
    if days is None:
        days = sorted({DAYS.index(d) for d in re.findall("|".join(DAYS), what)})
    hm = parse_clock(at) if at else {"morning": (8, 0), "evening": (18, 0), "night": (21, 0)}.get(what, (9, 0))
    if hm is None:
        return None
    return {"days": days, "time": f"{hm[0]:02d}:{hm[1]:02d}"}


def next_run(schedule: dict, after: datetime) -> datetime:
    h, mi = (int(x) for x in schedule["time"].split(":"))
    for i in range(0, 8):
        d = after.date() + timedelta(days=i)
        cand = datetime.combine(d, datetime.min.time()).replace(hour=h, minute=mi, tzinfo=after.tzinfo)
        if d.weekday() in schedule["days"] and cand > after:
            return cand
    return after + timedelta(days=7)


def describe_schedule(s: dict) -> str:
    days = s["days"]
    label = ("every day" if len(days) == 7 else "weekdays" if days == list(range(5)) else
             "weekends" if days == [5, 6] else ", ".join(DAYS[d].title() + "s" for d in days))
    h, mi = (int(x) for x in s["time"].split(":"))
    return f"{label} at {datetime(2000, 1, 1, h, mi).strftime('%I:%M %p').lstrip('0').replace(':00', '')}"


# ---- the skill ----------------------------------------------------------------------------------------------

class Automations(Skill):
    name, title = "automations", "Automations"
    poll_s = 5
    delay_s = 5

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        self._lock = threading.RLock()
        self.items: list[dict] = self._load()
        self.run_command = None  # set at start: runtime.command (routines run as if said)
        self._was_live = False
        self._was_away = False
        svc.bus.on(self._on_event)

    def _path(self):
        try:
            return self.svc.cfg.data_dir / "automations.json"
        except (AttributeError, KeyError):
            return None

    def _load(self) -> list[dict]:
        p = self._path()
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p and p.exists() else []
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        p = self._path()
        if p:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.items, indent=1), encoding="utf-8")
            tmp.replace(p)

    def start(self, runtime) -> None:
        self.run_command = lambda text: runtime.command(text, source="automation")

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.svc.tz)

    # ---- making them ------------------------------------------------------------------------------------
    def add(self, kind: str, text: str, *, at: float | None = None, schedule: dict | None = None,
            trigger: str | None = None, command: str | None = None, label: str = "") -> dict:
        item = {"id": uuid.uuid4().hex[:8], "kind": kind, "text": text.strip().rstrip("."), "at": at, "schedule": schedule,
                "trigger": trigger, "command": command, "label": label, "created": self.clock(), "last_run": None,
                "enabled": True}
        if schedule:
            item["at"] = next_run(schedule, self.now()).timestamp()
        with self._lock:
            self.items.append(item)
            self._save()
        self.publish()
        return {"ok": True, "item": self._view(item)}

    def remind(self, text: str, when: str = "", trigger: str = "") -> dict:
        if trigger:
            trig = trigger if trigger in TRIGGERS or trigger.startswith("email_from:") else None
            if not trig:
                return {"ok": False, "error": f"I don't know the moment “{trigger}”."}
            return self.add("reminder", text, trigger=trig)
        at = parse_when(when, self.now()) if when else None
        if at is None:
            return {"ok": False, "error": f"I didn't catch when{': ' + when if when else ''}."}
        return self.add("reminder", text, at=at.timestamp())

    def timer(self, seconds: float, label: str = "") -> dict:
        if not seconds or seconds <= 0:
            return {"ok": False, "error": "How long?"}
        return self.add("timer", label or "Timer", at=self.clock() + seconds, label=label)

    def routine(self, every: str, command: str) -> dict:
        sched = parse_every(every)
        if not sched:
            return {"ok": False, "error": f"I didn't understand “{every}”. Try “every weekday at 9 am”."}
        return self.add("routine", command, schedule=sched, command=command)

    def cancel(self, what: str = "", id: str = "") -> dict:
        with self._lock:
            live = [i for i in self.items if i["enabled"]]
            if id:
                hit = [i for i in live if i["id"] == id]
            elif what in ("timer", "the timer", "timers"):
                hit = [i for i in live if i["kind"] == "timer"]
            else:
                w = set(re.findall(r"[a-z0-9]+", what.lower())) - {"the", "a", "my", "reminder", "routine", "timer", "about", "to"}
                hit = [i for i in live if w and w & set(re.findall(r"[a-z0-9]+", (i["text"] + " " + i["label"]).lower()))]
                if not hit and len(live) == 1 and what.lower() in ("reminder", "the reminder", "it", "that"):
                    hit = live
            for i in hit:
                i["enabled"] = False
            self.items = [i for i in self.items if i["enabled"]]
            self._save()
        self.publish()
        return {"ok": bool(hit), "cancelled": [self._view(i) for i in hit],
                **({} if hit else {"error": f"I can't find {what or 'that'} to cancel."})}

    def listing(self) -> dict:
        with self._lock:
            items = sorted((i for i in self.items if i["enabled"]), key=lambda i: (i.get("at") or 9e12))
        return {"ok": True, "items": [self._view(i) for i in items]}

    def _view(self, i: dict) -> dict:
        v = {k: i[k] for k in ("id", "kind", "text", "label", "trigger", "command")}
        if i.get("at"):
            v["at"] = i["at"]
            v["in_s"] = max(0, round(i["at"] - self.clock()))
            v["when"] = datetime.fromtimestamp(i["at"], self.svc.tz).strftime("%a %I:%M %p").replace(" 0", " ")
        if i.get("schedule"):
            v["every"] = describe_schedule(i["schedule"])
        if i.get("trigger"):
            v["moment"] = TRIGGERS.get(i["trigger"]) or "when " + i["trigger"].split(":", 1)[1] + " emails you"
        if i["kind"] == "timer":
            v["total_s"] = round(i["at"] - i["created"])
        return v

    # ---- firing -------------------------------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        due = []
        with self._lock:
            for i in self.items:
                if i["enabled"] and i.get("at") and i["at"] <= now and not i.get("trigger"):
                    due.append(i)
        for i in due:
            self._fire(i)
        self._watch_moments()
        if due or any(i["kind"] == "timer" for i in self.items):
            self.publish()

    def _fire(self, i: dict) -> None:
        now = self.clock()
        with self._lock:
            i["last_run"] = now
            if i.get("schedule"):
                i["at"] = next_run(i["schedule"], datetime.fromtimestamp(now + 1, self.svc.tz)).timestamp()
            else:
                i["enabled"] = False
            self.items = [x for x in self.items if x["enabled"]]
            self._save()
        if i["kind"] == "timer":
            self.nudge(f"{i['label'] or 'Your'} timer is done." if i["label"] else "Time's up.", priority="urgent",
                       key=f"timer:{i['id']}")
        elif i["kind"] == "reminder":
            self.nudge(f"Reminder: {i['text']}.", priority="urgent", key=f"reminder:{i['id']}")
        elif i["kind"] == "routine" and self.run_command:
            threading.Thread(target=self._run_routine, args=(i,), daemon=True).start()

    def _run_routine(self, i: dict) -> None:
        try:
            out = self.run_command(i["command"]) or {}
            reply = out.get("reply") if isinstance(out, dict) else None
            if reply:  # said at a good moment (never on air), like any other update
                self.nudge(reply, priority="urgent", key=f"routine:{i['id']}:{int(self.clock() // 60)}")
        except Exception as exc:
            self.notify(f"Routine “{i['command']}” failed: {type(exc).__name__}", level="warning")

    def _fire_trigger(self, name: str, match=None) -> None:
        with self._lock:
            hits = [i for i in self.items if i["enabled"] and i.get("trigger") == name and (match is None or match(i))]
        for i in hits:
            i.pop("at", None)
            self._fire({**i, "trigger": None})
            with self._lock:
                i["enabled"] = False
                self.items = [x for x in self.items if x["enabled"]]
                self._save()
        if hits:
            self.publish()

    def _watch_moments(self) -> None:
        live = bool(self.svc.live and self.svc.live())
        if self._was_live and not live:
            self._fire_trigger("after_stream")
        self._was_live = live
        try:
            from ..integrations import desktop

            idle = desktop.idle_seconds()
        except Exception:
            idle = 0
        if idle > 300:
            self._was_away = True
        elif self._was_away and idle < 30:
            self._was_away = False
            self._fire_trigger("back")
        if any(i.get("trigger", "") and i["trigger"].startswith("email_from:") for i in self.items if i["enabled"]):
            gm = self.svc.gmail
            if gm is not None and gm.accounts():
                people = [m for a in gm.needs_you().get("accounts", []) for m in a.get("people", [])]
                for m in people:
                    who = (m.get("from", "") + " " + m.get("from_email", "")).lower()
                    self._fire_trigger_email(who, m)

    def _fire_trigger_email(self, who: str, msg: dict) -> None:
        with self._lock:
            hits = [i for i in self.items if i["enabled"] and (i.get("trigger") or "").startswith("email_from:")
                    and i["trigger"].split(":", 1)[1].lower() in who]
        for i in hits:
            self.nudge(f"{msg.get('from')} emailed: {msg.get('subject')}. Reminder: {i['text']}.", priority="urgent",
                       key=f"reminder:{i['id']}")
            with self._lock:
                i["enabled"] = False
                self.items = [x for x in self.items if x["enabled"]]
                self._save()

    def _on_event(self, ev: dict) -> None:
        if ev.get("type") == "job" and (ev.get("data") or {}).get("status") in ("done", "failed"):
            self._fire_trigger("job_done")

    # ---- skill surface -------------------------------------------------------------------------------------
    def status(self) -> dict:
        return self.listing()

    def actions(self):
        return {"cancel": lambda b: self.cancel(id=str(b.get("id") or "")),
                "timer": lambda b: self.timer(float(b.get("seconds") or 0), str(b.get("label") or "")),
                "add": lambda b: self._from_text(str(b.get("text") or ""))}

    def _from_text(self, text: str) -> dict:
        from ..brain.router import clean

        intent = self.route(clean(text))
        if intent is None:
            return {"ok": False, "error": "Try “remind me in 20 minutes to …”, “10 minute timer” or “every weekday at 9 am check my email”."}
        return {"remind": self.remind, "set_timer": self.timer, "add_routine": self.routine, "cancel_automation": self.cancel,
                "list_automations": lambda: self.listing()}[intent.tool](**intent.args)

    def briefing(self):
        today = self.now().date()
        items = [self._view(i) for i in self.items if i["enabled"] and i.get("at")
                 and datetime.fromtimestamp(i["at"], self.svc.tz).date() == today]
        return [f"{v['when']}: {v['text']}" for v in items if v["kind"] != "timer"] or None

    def tools(self):
        return [
            Tool("remind", "Remind the user of something at a time ('in 20 minutes', 'at 3 pm', 'tomorrow at 9', "
                           "'friday at 10') or at a moment (trigger: after_stream, back, job_done, or email_from:<name>).",
                 obj({"text": {"type": "string"}, "when": {"type": "string"}, "trigger": {"type": "string"}}, ["text"]),
                 self.remind, tier=1),
            Tool("set_timer", "Start a countdown timer.", obj({"seconds": {"type": "number"}, "label": {"type": "string"}},
                                                             ["seconds"]), self.timer, tier=1),
            Tool("add_routine", "Run a command on a schedule, as if the user said it: every='every weekday at 9 am', "
                                "command='check my email'.",
                 obj({"every": {"type": "string"}, "command": {"type": "string"}}, ["every", "command"]), self.routine, tier=1),
            Tool("list_automations", "The user's reminders, timers and routines.", obj(), self.listing),
            Tool("cancel_automation", "Cancel a reminder, timer or routine by what it's about.",
                 obj({"what": {"type": "string"}}, ["what"]), self.cancel, tier=1),
        ]

    def route(self, t: str):
        m = re.fullmatch(r"remind me (?:to )?(.+?) (after (?:the |my )?stream|when i'?m back(?: at (?:my|the) (?:pc|computer|desk))?"
                         r"|when (?:the |my )?job (?:is )?(?:done|finishes)|when (.+?) (?:emails|messages|writes|replies to) me)$", t) \
            or re.fullmatch(r"(after (?:the |my )?stream|when i'?m back(?: at (?:my|the) (?:pc|computer|desk))?|when (?:the |my )?"
                            r"job (?:is )?(?:done|finishes)|when (.+?) (?:emails|messages|writes|replies to) me),? remind me (?:to )?(.+)$", t)
        if m:
            g = m.groups()
            text, moment, who = (g[0], g[1], g[2]) if t.startswith("remind") else (g[2], g[0], g[1])
            trig = ("after_stream" if "stream" in moment else "back" if "back" in moment else "job_done" if "job" in moment
                    else f"email_from:{who}")
            return Intent("tool", "remind", {"text": text, "trigger": trig})
        m = re.fullmatch(r"remind me ((?:in|at|tomorrow|tonight|today|on|next) .+?|tomorrow|tonight|(?:" + "|".join(DAYS)
                         + r")(?: at .+?)?) (?:to |that )(.+)$", t)
        if m:
            return Intent("tool", "remind", {"text": m.group(2), "when": m.group(1)})
        m = re.fullmatch(r"remind me (?:to )?(.+?) ((?:in|at|by|on|tomorrow|tonight|today|next|this) .+|"
                         r"(?:" + "|".join(DAYS) + r").*)$", t)
        if m:
            return Intent("tool", "remind", {"text": m.group(1), "when": m.group(2)})
        m = re.fullmatch(r"(?:set |start )?(?:a |an )?(?:timer (?:for )?(.+?)|(.+?) timer)(?: (?:called|for|named) (.+))?", t)
        if m and (m.group(1) or m.group(2)):
            secs = parse_duration(m.group(1) or m.group(2))
            if secs:
                return Intent("tool", "set_timer", {"seconds": secs, **({"label": m.group(3)} if m.group(3) else {})})
        m = re.fullmatch(r"(?:every|each) (.+?) (?:at (\S+(?: [ap]\.?m\.?)?) )?(?:please )?((?:check|read|give|recap|open|start|run|"
                         r"brief|tell|what|show|play|switch|turn|summari[sz]e|remind).+)$", t)
        if m:
            every = m.group(1) + (f" at {m.group(2)}" if m.group(2) else "")
            if parse_every(every):
                return Intent("tool", "add_routine", {"every": every, "command": m.group(3)})
        if re.fullmatch(r"(?:what are |list |show )?(?:my |the )?(?:reminders|timers|routines|automations)(?: today)?"
                        r"|what (?:reminders|timers) do i have|how (?:long|much time) is left(?: on (?:the|my) timer)?", t):
            return Intent("tool", "list_automations")
        m = re.fullmatch(r"(?:cancel|stop|delete|remove|clear) (?:the |my )?(timer|timers|(?:reminder|routine)(?: (?:about|to|for) .+)?|.+ (?:timer|reminder|routine))", t)
        if m:
            return Intent("tool", "cancel_automation", {"what": m.group(1)})
        return None

    def say(self, tool, args, result):
        if tool == "set_timer":
            secs = result["item"]["in_s"]
            return f"Timer set for {_human(secs)}" + (f": {args['label']}." if args.get("label") else ".")
        if tool == "remind":
            v = result["item"]
            when = v.get("moment") or v.get("when")
            return f"I'll remind you {when if v.get('moment') else 'on ' + when}: {v['text']}."
        if tool == "add_routine":
            v = result["item"]
            return f"Done: {v['every']}, I'll {v['command']}."
        if tool == "cancel_automation":
            c = result["cancelled"]
            return f"Cancelled {c[0]['label'] or c[0]['text']}." if len(c) == 1 else f"Cancelled {len(c)}."
        if tool == "list_automations":
            items = result["items"]
            if not items:
                return "No reminders, timers or routines."
            timers = [i for i in items if i["kind"] == "timer"]
            if timers and len(timers) == len(items):
                return " ".join(f"{t['label'] or 'Timer'}: {_human(t['in_s'])} left." for t in timers)
            parts = [f"{i['label'] or 'timer'} in {_human(i['in_s'])}" if i["kind"] == "timer" else
                     f"{i['text']}, {i.get('every') or i.get('moment') or i.get('when')}" for i in items[:4]]
            return "; ".join(parts) + (f"; and {len(items) - 4} more." if len(items) > 4 else ".")
        return None


def _human(secs: float) -> str:
    secs = int(round(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h} hour{'s' if h != 1 else ''}"] if h else []
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s and not h and m < 5:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(parts) or "0 seconds"
