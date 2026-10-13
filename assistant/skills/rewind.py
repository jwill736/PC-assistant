"""Rewind: where the time went, from what Vesper already tracks (the window in front, every few seconds).

"Where was I?" (what you were on before the break), "what was I doing at 3 pm yesterday", "what was I working on
Tuesday", "what did I do this morning", "how long was I on YouTube today". Window titles are grouped into the things
behind them: the repo in VS Code, the document in Word, the site in the browser. Titles can be private, so on air only
apps and times are said. The HUD gets a 24-hour scrubber: drag along it to see what was in front at any moment.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from ..integrations.activity import IDLE
from ..integrations.desktop import normalize_app
from . import Skill
from .automations import DAYS, parse_clock, parse_duration

PERIODS = {"morning": (5, 12), "lunch": (11, 14), "afternoon": (12, 17), "evening": (17, 22), "night": (19, 24)}
BROWSERS = {"chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "arc", "librewolf", "zen"}
EDITORS = {"code", "cursor", "windsurf", "devenv", "pycharm64", "idea64", "webstorm64", "rider64", "sublime_text", "zed"}
NAMES = {"code": "VS Code", "msedge": "Edge", "chrome": "Chrome", "firefox": "Firefox", "explorer": "File Explorer",
         "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint", "obs64": "OBS", "windowsterminal": "Terminal",
         "devenv": "Visual Studio", "idle": "away"}
SEP = re.compile(r"\s+[-—–|·]\s+")
TAIL = re.compile(r"(?i)^(?:google chrome|microsoft​?\s*edge|mozilla firefox|firefox|brave|opera|vivaldi|visual studio "
                  r"code|cursor|word|excel|powerpoint|notepad|notepad\+\+|sublime text|pycharm|obsidian|personal|work|"
                  r"profile \d+)$")
WHEN = (r"today|yesterday|this (?:morning|afternoon|evening|week)|last night|tonight|(?:on |last )?(?:" + "|".join(DAYS)
        + r")(?: (?:morning|afternoon|evening|night))?|yesterday (?:morning|afternoon|evening)")


# ---- when ------------------------------------------------------------------------------------------------------

def parse_span(text: str, now: datetime) -> dict | None:
    """'at 3 pm yesterday', 'this morning', 'tuesday afternoon', 'yesterday', 'an hour ago', 'this week' ->
    {start, end, point, label} (datetimes; ``point`` is set for a moment rather than a stretch)."""
    t = re.sub(r"\b(\d{1,2}) (\d{2})\b", r"\1:\2", (text or "").lower().strip())  # the router turns "3:30" into "3 30"
    t = re.sub(r"^(?:on|during|in the|for)\s+", "", t)
    midnight = datetime.combine(now.date(), datetime.min.time()).replace(tzinfo=now.tzinfo)
    if t in ("", "today", "so far today"):
        return {"start": midnight, "end": now, "point": None, "label": "today"}
    if t == "this week":
        return {"start": midnight - timedelta(days=now.weekday()), "end": now, "point": None, "label": "this week"}
    m = re.fullmatch(r"(.+?) ago", t)
    if m and parse_duration(m.group(1)):
        p = now - timedelta(seconds=parse_duration(m.group(1)))
        return {"start": p - timedelta(minutes=20), "end": min(now, p + timedelta(minutes=20)), "point": p,
                "label": f"{m.group(1)} ago"}
    back, day_label = 0, ""
    if "day before yesterday" in t:
        back, day_label = 2, "the day before yesterday"
    elif "yesterday" in t or "last night" in t:
        back, day_label = 1, "yesterday"
    elif m := re.search(r"\b(last )?(" + "|".join(DAYS) + r")\b", t):
        back = (now.weekday() - DAYS.index(m.group(2))) % 7 or (7 if m.group(1) else 0)
        day_label = "today" if back == 0 else m.group(2).title() if back < 7 else f"last {m.group(2).title()}"
    explicit = bool(day_label)
    day = midnight - timedelta(days=back)
    clock = re.search(r"\b(?:at|around|about|by|near)\s+((?:\d{1,2}(?::\d{2})?|[a-z]+)(?:\s*(?:am|pm|a m|p m|o'?clock))?)\b", t) \
        or re.search(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm|a m|p m)|noon|midnight)\b", t)
    hm = parse_clock(clock.group(1)) if clock else None
    if hm:
        p = day.replace(hour=hm[0], minute=hm[1])
        if p > now and not explicit:
            p, day_label = p - timedelta(days=1), "yesterday"
        when = p.strftime("%I:%M %p").lstrip("0").replace(":00", "")
        return {"start": p - timedelta(minutes=20), "end": min(now, p + timedelta(minutes=20)), "point": p,
                "label": f"{day_label or 'today'} around {when}"}
    per = "night" if "last night" in t or "tonight" in t else next((k for k in PERIODS if re.search(rf"\b{k}\b", t)), None)
    if per:
        a, b = PERIODS[per]
        start, end = day + timedelta(hours=a), min(now, day + timedelta(hours=b))
        if start >= now:
            start, end, day_label = start - timedelta(days=1), day + timedelta(hours=b - 24), "yesterday"
        label = {"today": f"this {per}", "yesterday": f"yesterday {per}" if per != "night" else "last night"}.get(
            day_label or "today", f"{day_label} {per}")
        return {"start": start, "end": end, "point": None, "label": label}
    if explicit:
        return {"start": day, "end": min(now, day + timedelta(days=1)), "point": None, "label": day_label}
    return None


# ---- what ------------------------------------------------------------------------------------------------------

def app_name(app: str) -> str:
    a = normalize_app(app)
    return NAMES.get(a, a[:1].upper() + a[1:])


def thing(app: str, title: str) -> tuple[str, str]:
    """The thing behind a window: ('repo', 'PC-assistant'), ('site', 'YouTube'), ('document', 'Budget.xlsx'), ('app', 'Discord')."""
    a = normalize_app(app)
    t = re.sub(r"^\(\d+\)\s*|^[●•*]\s*", "", (title or "").strip())
    t = re.sub(r"(?i)\s+and \d+ more pages?\b", "", t)
    parts = [p.strip() for p in SEP.split(t) if p.strip() and not TAIL.match(p.strip())]
    if not parts:
        return "app", app_name(app)
    if a in BROWSERS:
        return "site", parts[-1]
    if a in EDITORS:
        return "repo", parts[-1]
    if re.search(r"\.[A-Za-z0-9]{1,5}$", parts[0]):
        return "document", parts[0]
    return "app", app_name(app)


def group(segments: list[dict]) -> tuple[list[dict], list[dict]]:
    """(things, apps), most time first, idle left out."""
    things: dict[tuple, float] = defaultdict(float)
    apps: dict[str, float] = defaultdict(float)
    for s in segments:
        if s["category"] == IDLE:
            continue
        dur = s["end"] - s["start"]
        kind, name = thing(s["app"], s["title"])
        things[(name, kind, app_name(s["app"]))] += dur
        apps[app_name(s["app"])] += dur
    return ([{"name": n, "kind": k, "app": a, "seconds": round(v)} for (n, k, a), v in sorted(things.items(), key=lambda x: -x[1])],
            [{"app": a, "seconds": round(v)} for a, v in sorted(apps.items(), key=lambda x: -x[1])])


def human(secs: float) -> str:
    m = int(round(secs / 60))
    if m < 1:
        return "under a minute"
    h, m = divmod(m, 60)
    return " ".join(x for x in (f"{h} hour{'s' if h != 1 else ''}" if h else "", f"{m} minute{'s' if m != 1 else ''}" if m else "") if x)


def _on(x: dict) -> str:
    return x["name"] if x["kind"] == "app" else f"{x['name']} in {x['app']}"


def _join(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


# ---- the skill ----------------------------------------------------------------------------------------------------

class Rewind(Skill):
    name, title = "rewind", "Rewind"
    poll_s = 60
    delay_s = 20

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock

    def live(self) -> bool:
        return bool(self.svc.live and self.svc.live())

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.svc.tz)

    def segments(self, start: float, end: float) -> list[dict]:
        try:
            return sorted(self.svc.activity.segments(start, end), key=lambda s: s["start"])
        except Exception:
            return []

    def _work(self, segs: list[dict]) -> list[dict]:
        cats = set(self.setting("work_categories", ["work"]))
        return [s for s in segs if s["category"] in cats] or [s for s in segs if s["category"] != IDLE]

    # ---- tools ----------------------------------------------------------------------------------------------
    def where_was_i(self) -> dict:
        now = self.clock()
        work = self._work(self.segments(now - 12 * 3600, now))
        if not work:
            return {"ok": True, "found": False}
        end = work[-1]["end"]
        recent = [dict(s, start=max(s["start"], end - 1800)) for s in work if s["end"] > end - 1800]
        things, apps = group(recent)
        return {"ok": True, "found": True, "stopped_at": end, "away_s": round(now - end), "apps": apps[:3],
                **({"things_count": len(things)} if self.live() else {"things": things[:3]})}

    def rewind(self, when: str = "") -> dict:
        span = parse_span(when, self.now())
        if span is None:
            return {"ok": False, "error": f"I didn't catch when{': ' + when if when else ''}. Try “at 3 pm yesterday”, "
                                          "“this morning” or “Tuesday afternoon”."}
        segs = self.segments(span["start"].timestamp(), span["end"].timestamp())
        things, apps = group(segs)
        out = {"ok": True, "label": span["label"], "from": span["start"].timestamp(), "to": span["end"].timestamp(),
               "found": bool(apps), "active_s": sum(a["seconds"] for a in apps), "apps": apps[:5]}
        if span["point"] is not None:
            p = span["point"].timestamp()
            near = [s for s in segs if s["category"] != IDLE]
            hit = min(near, key=lambda s: 0 if s["start"] <= p <= s["end"] else min(abs(s["start"] - p), abs(s["end"] - p)),
                      default=None)
            if hit is not None:
                kind, name = thing(hit["app"], hit["title"])
                out["at"] = {"app": app_name(hit["app"])} if self.live() else {"app": app_name(hit["app"]), "name": name, "kind": kind}
        if self.live():
            out["things_count"] = len(things)
        else:
            out["things"] = things[:6]
        return out

    def time_spent(self, target: str, when: str = "today") -> dict:
        span = parse_span(when or "today", self.now())
        if span is None or span["point"] is not None:
            span = parse_span("today", self.now())
        tgt = (target or "").lower().strip()
        tgt = re.sub(r"^(?:the |my )", "", tgt)
        if not tgt:
            return {"ok": False, "error": "On what?"}
        segs = [s for s in self.segments(span["start"].timestamp(), span["end"].timestamp()) if s["category"] != IDLE and (
            tgt in s["app"].lower() or tgt in app_name(s["app"]).lower() or tgt in (s["title"] or "").lower())]
        return {"ok": True, "target": target, "label": span["label"], "seconds": round(sum(s["end"] - s["start"] for s in segs)),
                "first": segs[0]["start"] if segs else None, "last": segs[-1]["end"] if segs else None}

    def tools(self):
        return [
            Tool("where_was_i", "What the user was working on just before their current break (the last 30 minutes "
                                "of work): to pick up where they left off.", obj(), self.where_was_i),
            Tool("rewind", "What the user was doing at a past time or during a stretch: when='at 3 pm yesterday', "
                           "'this morning', 'tuesday afternoon', 'an hour ago', 'yesterday'.",
                 obj({"when": {"type": "string"}}, ["when"]), self.rewind),
            Tool("time_spent", "How long the user spent on an app, site or project (target='YouTube', 'Discord', "
                               "'PC-assistant') today or another day (when).",
                 obj({"target": {"type": "string"}, "when": {"type": "string"}}, ["target"]), self.time_spent),
        ]

    def route(self, t: str):
        if re.fullmatch(r"where was i|where did i leave off|what was i (?:doing|working on)(?: before (?:my |the |this )?break"
                        r"| before i left)?", t):
            return Intent("tool", "where_was_i", {})
        m = re.fullmatch(r"what (?:was i doing|was i working on|was i up to|did i do|did i work on|have i been doing|"
                         r"have i been working on) (.+)", t)
        if m and m.group(1) not in ("today", "today so far") and parse_span(m.group(1), self.now()) is not None:
            return Intent("tool", "rewind", {"when": m.group(1)})
        m = re.fullmatch(r"how (?:long|much time) (?:was i|have i been|did i spend|have i spent|did i waste)(?: on| in| using| "
                         r"watching| playing| in| at)? (.+?)(?: (" + WHEN + r"))?", t)
        if m and not re.search(r"\b(?:away|afk|gone|left)\b", m.group(1)):
            return Intent("tool", "time_spent", {"target": m.group(1), "when": m.group(2) or "today"})
        return None

    def say(self, tool, args, result):
        live = self.live()
        if tool == "where_was_i":
            if not result.get("found"):
                return "I don't see any work in the last twelve hours."
            mins = result["away_s"] // 60
            apps = _join([a["app"] for a in result["apps"]])
            if live:
                return f"Your last stretch of work, in {apps}, ended {human(result['away_s'])} ago. The details are on the HUD."
            on = _join([_on(x) for x in result["things"]])
            return f"Right now you're on {on}." if mins < 2 else f"Before your break, {human(result['away_s'])} ago, you were on {on}."
        if tool == "rewind":
            label = result["label"][:1].upper() + result["label"][1:]
            if not result["found"]:
                return f"{label} the PC was idle, or Vesper wasn't tracking."
            if "at" in result:
                at = result["at"]
                where = at["app"] if live or at.get("kind") == "app" else f"{at['name']} in {at['app']}"
                more = [x for x in result.get("things") or [] if x["name"] != at.get("name")][:2]
                return f"{label} you were on {where}." + (f" Around then also {_join([_on(x) for x in more])}." if more else "")
            if live:
                return f"{label}: {human(result['active_s'])} at the PC, mostly {_join([a['app'] for a in result['apps'][:3]])}."
            top = [f"{_on(x)}, {human(x['seconds'])}" for x in result["things"][:3]]
            return f"{label}: {human(result['active_s'])} at the PC. Mostly {_join(top)}."
        if tool == "time_spent":
            if not result["seconds"]:
                return f"I don't see any time on {result['target']} {result['label']}."
            return f"{human(result['seconds'])} on {result['target']} {result['label']}."
        return None

    # ---- the HUD ------------------------------------------------------------------------------------------
    def status(self) -> dict:
        now = self.clock()
        live, blocks = self.live(), []
        segs = self.segments(now - 86400, now)
        for s in segs:
            a = app_name(s["app"])
            t = "" if live or s["category"] == IDLE else (s["title"] or "")[:100]
            if blocks and blocks[-1]["a"] == a and blocks[-1]["t"] == t and s["start"] - blocks[-1]["e"] < 30:
                blocks[-1]["e"] = round(s["end"])
            elif s["end"] - s["start"] >= 10:
                blocks.append({"s": round(s["start"]), "e": round(s["end"]), "c": s["category"], "a": a, "t": t,
                               "n": "" if live else thing(s["app"], s["title"])[1]})
        midnight = datetime.combine(self.now().date(), datetime.min.time()).replace(tzinfo=self.svc.tz).timestamp()
        things, apps = group([s for s in segs if s["end"] > midnight])
        ww = self.where_was_i()
        return {"now": now, "from": now - 86400, "blocks": blocks[-1500:], "live": live,
                "today": {"active_s": sum(a["seconds"] for a in apps), "apps": apps[:5], "things": [] if live else things[:6]},
                "where": ww if ww.get("found") else None}
