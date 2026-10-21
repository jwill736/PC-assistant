"""Meeting prep: for each meeting with other people in the next day, a card with who's coming, the last emails with
each of them, related documents, notes and open tasks, and the meeting link.

- 10–15 minutes before: "Prep for Contract review is on the HUD: last thing from Dana was 'Redlines' on Tuesday."
- Up to half an hour after it ends: "Any follow-ups from Contract review? Say 'note: …'" (once).
- "prep me for my next meeting", "prep me for the budget review", "who's in my next meeting".

Attendees come from Google calendars (Setup → Google accounts); iCal links don't list them. Email subjects are other
people's words, so meeting_prep is marked untrusted, and nothing but times and names is said on air.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from ..brain.router import Intent
from ..brain.tools import EMAIL_NOTE, Tool, obj
from . import Skill
from .people import day_phrase, display_name, first_name, gmail_or_reason, on_air

URL = re.compile(r"https://(?:[\w-]+\.)?(?:zoom\.us|meet\.google\.com|teams\.microsoft\.com|teams\.live\.com|"
                 r"webex\.com|whereby\.com|around\.co)/[^\s<>\"')]+")
GENERIC = set("meeting meetings call sync weekly daily monthly catch up catchup chat check in checkin 1:1 1-1 one on "
              "with and the a an of for re to about intro x / - & + w".split())
NOT_MEETINGS = {"stream", "the stream", "streaming", "my stream", "tonight", "today", "tomorrow", "the day", "going live"}


def clock(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%I:%M %p").lstrip("0").replace(":00", "")


def title_words(title: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", title.lower()) if w not in GENERIC and len(w) > 2]


def names(people: list[dict], n: int = 3) -> str:
    got = [p["name"] for p in people[:n]]
    more = len(people) - len(got)
    if more:
        return ", ".join(got) + f" and {more} more"
    return got[0] if len(got) == 1 else ", ".join(got[:-1]) + " and " + got[-1] if got else "nobody else"


class Meetings(Skill):
    name, title = "meetings", "Meetings"
    poll_s = 60
    game_poll_s = 120  # still in time for the 10-minute heads-up
    delay_s = 45

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        self._cards: dict[str, tuple[float, dict]] = {}
        self._next: dict | None = None
        self.error: str | None = None
        svc.storage.execute("CREATE TABLE IF NOT EXISTS meetings_nudged (key TEXT PRIMARY KEY, ts REAL NOT NULL)")

    # ---- the calendar -----------------------------------------------------------------------------------------
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.svc.tz)

    def events(self, hours: float = 24, back_h: float = 1) -> list[dict]:
        now = self.now()
        try:
            evs = self.svc.calendars.events(now - timedelta(hours=back_h), now + timedelta(hours=hours))
        except Exception as exc:
            self.error = f"Couldn't read the calendar ({type(exc).__name__})."
            return []
        out = []
        for e in evs:
            if e.get("all_day"):
                continue
            others = [{"name": display_name(a.get("name", ""), a["email"]), "email": a["email"],
                       "response": a.get("response", ""), "organizer": a.get("organizer", False)}
                      for a in e.get("attendees") or [] if not a.get("self") and a.get("email")]
            out.append({**e, "others": others, "key": f"{e['title']}|{e['start']}"})
        return out

    def upcoming(self, hours: float = 24, people_only: bool = True) -> list[dict]:
        now = self.now()
        return [e for e in self.events(hours) if datetime.fromisoformat(e["start"]) > now and (e["others"] or not people_only)]

    def find(self, title: str) -> dict | None:
        want = set(title_words(title)) or set(re.findall(r"[a-z0-9']+", title.lower()))
        best, score = None, 0
        for e in self.upcoming(hours=24 * 7, people_only=False):
            have = set(re.findall(r"[a-z0-9']+", e["title"].lower())) | {w for p in e["others"] for w in p["name"].lower().split()}
            s = len(want & have)
            if s > score:
                best, score = e, s
        return best

    # ---- the prep card ----------------------------------------------------------------------------------------
    def prep(self, e: dict, fresh: bool = False) -> dict:
        hit = self._cards.get(e["key"])
        if hit and not fresh and self.clock() - hit[0] < 900:
            return hit[1]
        gm, why = gmail_or_reason(self.svc)
        mine = {a.lower() for a in gm.accounts()} if gm else set()
        ppl = self.svc.skills.get("people") if self.svc.skills is not None else None
        attendees = []
        for a in e["others"][:8]:
            mails = []
            if gm is not None:
                try:
                    r = gm.search(f"from:{a['email']} OR to:{a['email']} newer_than:60d", limit=3)
                    mails = [{"subject": m["subject"], "ts": m["ts"], "from": m["from"], "link": m["link"],
                              "from_me": m["from_email"].lower() in mine} for m in r.get("messages", [])][:3]
                except Exception:
                    mails = []
            about = ppl.about(a["email"]) if ppl is not None else {"notes": [], "ask": []}
            known = ppl.known(a["email"]) if ppl is not None and len(a["name"].split()) < 2 else None
            attendees.append({**a, "name": known["name"] if known else a["name"], "emails": mails, "notes": [n["text"] for n in about["notes"][:3]],
                              "ask": [n["text"] for n in about["ask"][:3]]})
        words = title_words(e["title"])
        docs = []
        if self.svc.library is not None and words:
            try:
                docs = [{"title": d["title"], "file": d["file"], "path": d["path"], "folder": d["folder"]}
                        for d in self.svc.library.search(" ".join(words), 3)]
            except Exception:
                docs = []
        notes = [r["text"] for r in self.svc.storage.search_memory(" ".join(words) or e["title"], 8) if r["kind"] == "note"][:3]
        keys = set(words) | {w.lower() for p in e["others"] for w in p["name"].split() if len(w) > 2}
        tasks = [t["title"] for t in self.svc.storage.list_tasks()
                 if keys & set(re.findall(r"[a-z0-9']+", t["title"].lower()))][:5]
        link = e.get("link") or next((m.group(0) for m in (URL.search(e.get("location") or ""),
                                                            URL.search(e.get("description") or "")) if m), "")
        card = {"key": e["key"], "title": e["title"], "start": e["start"], "end": e["end"], "location": e.get("location", ""),
                "link": link, "calendar": e.get("calendar", ""), "attendees": attendees, "docs": docs, "notes": notes,
                "tasks": tasks, "gmail_error": why or None, "at": self.clock()}
        self._cards[e["key"]] = (self.clock(), card)
        return card

    def last_mail(self, card: dict) -> str | None:
        """'last thing from Dana was “Redlines” on Tuesday' (theirs first), or None."""
        mails = sorted(((a, m) for a in card["attendees"] for m in a["emails"]), key=lambda x: -x[1]["ts"])
        theirs = next(((a, m) for a, m in mails if not m["from_me"]), None)
        if theirs or mails:
            a, m = theirs or mails[0]
            when = day_phrase(m["ts"], self.svc.tz, self.clock())
            return (f"last thing from {first_name(a['name'])} was “{m['subject']}” {when}" if theirs else
                    f"you last wrote to {first_name(a['name'])} about “{m['subject']}” {when}")
        return None

    def line(self, card: dict) -> str:
        """The prep nudge."""
        return f"Prep for {card['title']} is on the HUD: {self.last_mail(card) or 'with ' + names(card['attendees'])}."

    # ---- nudges -----------------------------------------------------------------------------------------------
    def _once(self, key: str) -> bool:
        return self.svc.storage.execute("INSERT OR IGNORE INTO meetings_nudged(key, ts) VALUES (?, ?)",
                                        (key, self.clock())).rowcount > 0

    def check(self) -> None:
        now = self.now()
        lo, hi = (float(x) for x in str(self.setting("lead_minutes", "10-15")).split("-"))
        nxt = None
        for e in self.events(hours=24, back_h=1):
            if not e["others"]:
                continue
            start, end = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
            mins = (start - now).total_seconds() / 60
            if start > now and nxt is None:
                nxt = e
            if lo <= mins <= hi and self._once(f"prep:{e['key']}"):
                self.nudge(self.line(self.prep(e, fresh=True)), priority="urgent", key=f"meetings:prep:{e['key']}",
                           expires_in=max(60.0, mins * 60))
            after = (now - end).total_seconds() / 60
            debrief = self.svc.skills is not None and self.svc.skills.get("debrief") is not None  # asks its own question
            if 0 <= after <= 30 and not debrief and self._once(f"followup:{e['key']}"):
                self.nudge(f"Any follow-ups from {e['title']}? Say “note: …”.", priority="normal",
                           key=f"meetings:followup:{e['key']}", expires_in=3600)
        self._next = self.prep(nxt) if nxt else None

    # ---- tools ------------------------------------------------------------------------------------------------
    def meeting_prep(self, title: str = "") -> dict:
        e = self.find(title) if title else next(iter(self.upcoming()), None)
        if e is None:
            return {"ok": False, "error": f"I can't find a meeting called {title} this week." if title else
                    "No meetings with other people in the next day." + self._hint()}
        card = self.prep(e)
        mins = int((datetime.fromisoformat(card["start"]) - self.now()).total_seconds() // 60)
        if on_air(self.svc):
            return {"ok": True, "live": True, "start": card["start"], "in_minutes": mins,
                    "attendees": [{"name": a["name"]} for a in card["attendees"]]}
        return {"ok": True, "in_minutes": mins, **card}

    def next_meeting(self) -> dict:
        e = next(iter(self.upcoming(people_only=False)), None)
        if e is None:
            return {"ok": True, "meeting": None}
        m = {"title": e["title"], "start": e["start"], "end": e["end"], "link": e.get("link") or "",
             "attendees": [{"name": a["name"], "response": a["response"]} for a in e["others"]],
             "in_minutes": int((datetime.fromisoformat(e["start"]) - self.now()).total_seconds() // 60)}
        if on_air(self.svc):
            m = {k: m[k] for k in ("start", "attendees", "in_minutes")}
        return {"ok": True, "meeting": m, **({"live": True} if on_air(self.svc) else {})}

    def _hint(self) -> str:
        if not any(s.get("kind") == "google" for s in self.svc.calendars.status()):
            return " (Who's invited only comes from Google calendars: Setup → Google accounts.)"
        return ""

    def tools(self):
        return [
            Tool("meeting_prep", "Prep for a meeting (the next one with other people if no title): who's coming, the "
                                 "last emails with each, related documents, notes, open tasks, the link.",
                 obj({"title": {"type": "string"}}), self.meeting_prep,
                 untrusted=True, untrusted_label="an email", untrusted_note=EMAIL_NOTE),
            Tool("next_meeting", "The next meeting today or tomorrow and who's in it.", obj(), self.next_meeting),
        ]

    def route(self, t: str):
        if re.fullmatch(r"(?:(?:prep|prepare|brief|get) me (?:ready )?for|prep for) (?:my |the )?next (?:meeting|call)|"
                        r"meeting prep|prep my next meeting", t):
            return Intent("tool", "meeting_prep", {})
        m = re.fullmatch(r"(?:prep|prepare) me for (?:my |the )?(.+?)(?: meeting| call)?", t)
        if m and m.group(1) not in NOT_MEETINGS and not re.search(r"\b(?:stream|live)\b", m.group(1)):
            return Intent("tool", "meeting_prep", {"title": m.group(1)})
        if re.fullmatch(r"who(?:'s| is| are)? (?:in|on|coming to|attending|joining) my next (?:meeting|call)|who am i "
                        r"meeting (?:with )?next|who(?:'s| is) my next (?:meeting|call) with", t):
            return Intent("tool", "next_meeting", {})
        return None

    def say(self, tool, args, result):
        if tool == "next_meeting":
            m = result.get("meeting")
            if not m:
                return "No more meetings today or tomorrow."
            who = names(m["attendees"]) if m["attendees"] else "just you"
            if result.get("live"):
                return f"At {clock(m['start'])}, with {len(m['attendees'])} {'person' if len(m['attendees']) == 1 else 'people'}."
            return f"{m['title']} at {clock(m['start'])}, with {who}."
        if tool != "meeting_prep":
            return None
        n = len(result["attendees"])
        if result.get("live"):
            return f"Your meeting at {clock(result['start'])} has {n} {'person' if n == 1 else 'people'}. The prep is on the HUD."
        soon = f" in {result['in_minutes']} minutes" if 0 <= result["in_minutes"] < 60 else ""
        out = f"{result['title']} at {clock(result['start'])}{soon}, with {names(result['attendees'])}. "
        last = self.last_mail(result)
        out += f"{last[0].upper()}{last[1:]}." if last else ""
        ask = next(((a, x) for a in result["attendees"] for x in a["ask"]), None)
        if ask:
            out += f" Remember to {ask[1][0].lower()}{ask[1][1:]} with {first_name(ask[0]['name'])}." \
                if ask[1].lower().startswith("ask ") else f" About {first_name(ask[0]['name'])}: {ask[1]}."
        extra = [f"{len(result[k])} {label}{'s' if len(result[k]) != 1 else ''}" for k, label in
                 (("docs", "related document"), ("tasks", "open task")) if result[k]]
        return out + (f" {' and '.join(extra)} on the HUD." if extra else "")

    # ---- HUD and briefing -----------------------------------------------------------------------------------
    def status(self) -> dict:
        up = [{"title": e["title"], "start": e["start"], "end": e["end"], "with": [a["name"] for a in e["others"]]}
              for e in self.upcoming()][:6]
        out = {"ok": True, "next": self._next, "upcoming": up}
        if self.error:
            out["warning"] = self.error
        elif not up:
            out["hint"] = self._hint().strip(" ()") or None
        return out

    def actions(self):
        return {"refresh": lambda b: {"ok": True, "next": self._refresh_next()}}

    def _refresh_next(self) -> dict | None:
        e = next(iter(self.upcoming()), None)
        self._next = self.prep(e, fresh=True) if e else None
        return self._next

    def briefing(self):
        now = self.now()
        today = [e for e in self.upcoming(hours=24) if datetime.fromisoformat(e["start"]).date() == now.date()]
        return {"meetings": [{"title": e["title"], "time": clock(e["start"]), "with": [a["name"] for a in e["others"][:4]]}
                             for e in today[:4]]} if today else None

    def tick(self) -> None:
        try:
            self.error = None
            self.check()
        except Exception as exc:
            self.error = f"Meeting prep failed ({type(exc).__name__})."
        self.publish()
