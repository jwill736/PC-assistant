"""The eval's world: a real Assistant (router, tools, memory, planner, the confirmation and untrusted-text rules)
over a fake Gmail and fake calendars, answering with the model under test.

Nothing leaves the PC except the calls to the model server: the email and calendars are built from golden.yaml,
storage is in memory, and every tool that would touch the PC or the internet (volume, apps, Spotify, the weather,
your phone, Discord…) is replaced by a stub that only records the call and returns the canned result.
"""

from __future__ import annotations

import base64
import dataclasses
import re
import ssl
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from assistant.brain.assistant import Assistant
from assistant.bus import EventBus
from assistant.config import DEFAULTS, Config, deep_merge
from assistant.integrations.gmail import Gmail, body_text, summarize
from assistant.services import build_services
from assistant.storage import Storage

WORK, HOME = "jon@talent-sdk.com", "jwill736@gmail.com"
# Tools that really run, against the fakes and the in-memory storage. Everything else is a stub.
REAL_TOOLS = frozenset({"add_task", "list_tasks", "complete_task", "remember", "recall", "what_you_know", "remind",
                        "calendar", "search_email", "check_email", "read_email", "plan_day"})
CALENDARS = {"work": ("Work", WORK, "#4f8cff"), "personal": ("Personal", HOME, "#3fb950"),
             "stream": ("Stream", HOME, "#a371f7")}
STOP = frozenset("the and for from with about any anything did got back get me my mail email emails inbox new "
                 "subject has re fw fwd".split())


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def raw_message(m: dict, now: float) -> dict:
    """A golden.yaml email as Gmail's API returns it."""
    labels = ["INBOX"] + (["UNREAD"] if m.get("unread", True) else []) + \
        (["CATEGORY_PROMOTIONS"] if m.get("bulk") else ["CATEGORY_PERSONAL"])
    sent = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(now - float(m.get("hours_ago", 2)) * 3600))
    headers = [{"name": "From", "value": m["from"]}, {"name": "Subject", "value": m.get("subject", "")},
               {"name": "Date", "value": sent}, {"name": "To", "value": WORK if m.get("account") == "work" else HOME}]
    body = m.get("body") or m.get("snippet") or ""
    return {"id": m["id"], "threadId": "t" + m["id"], "labelIds": labels,
            "snippet": m.get("snippet") or body[:140],
            "payload": {"mimeType": "multipart/alternative", "headers": headers,
                        "parts": [{"mimeType": "text/plain", "body": {"data": _b64(body)}}]}}


def gmail_terms(query: str) -> list[str]:
    """The words of a Gmail query that can match a message: operators like is:unread or newer_than:7d dropped."""
    q = re.sub(r"(?<!\S)-?(?:is|in|label|newer_than|older_than|after|before|has|category|to|cc|larger|smaller):\S+",
               " ", query or "", flags=re.I)
    q = re.sub(r"\b(?:from|subject):", " ", q, flags=re.I)
    q = re.sub(r"\b(?:OR|AND)\b", " ", q)
    return [w for w in re.findall(r"[\w@.'-]+", q.lower()) if len(w) > 2 and w.strip(".'-") not in STOP]


class FakeGmail(Gmail):
    """The real Gmail class (its search, needs-you and account picking) over messages from golden.yaml.

    Search is lenient on purpose: a message matches when any word of the query is in its sender, subject or text,
    so a small model's odd query still finds the email and the case tests what it does with it. ``shown``: every
    message id the model was given (in a list or read in full)."""

    def __init__(self, emails: list[dict], now: float | None = None):
        super().__init__(google=None, cache_s=0)
        now = now or time.time()
        self.box = {WORK: [], HOME: []}
        for m in emails:
            self.box[WORK if m.get("account") == "work" else HOME].append(raw_message(m, now))
        self.shown: set[str] = set()

    def accounts(self) -> list[str]:
        return [WORK, HOME]

    def missing(self) -> list[str]:
        return []

    def _list(self, email: str, query: str, limit: int) -> list[dict]:
        terms = gmail_terms(query)
        unread_only = "is:unread" in (query or "")
        hits = []
        for raw in self.box.get(email, []):
            meta = summarize(email, raw)
            if unread_only and not meta["unread"]:
                continue
            hay = " ".join([meta["from"], meta["from_email"], meta["subject"], meta["snippet"],
                            body_text(raw["payload"])]).lower()
            if not terms or any(t in hay for t in terms):
                hits.append(meta)
        hits = hits[:limit]
        self.shown.update(m["id"] for m in hits)
        return hits

    def read(self, msg_id: str, account: str = "") -> dict:
        for email in self._pick(account) or []:
            for raw in self.box.get(email, []):
                if raw["id"] == msg_id:
                    self.shown.add(msg_id)
                    body = body_text(raw["payload"])
                    return {"ok": True, **summarize(email, raw), "body": body, "truncated": False}
        return {"ok": False, "error": "I can't find that email."}

    def thread(self, thread_id: str, account: str = "") -> dict:
        got = self.read(thread_id[1:] if thread_id.startswith("t") else thread_id, account)
        if not got.get("ok"):
            return {"ok": False, "error": "I can't find that conversation."}
        return {"ok": True, "account": got["account"], "thread_id": thread_id,
                "messages": [{**got, "to": "", "from_me": False}]}

    def sent(self, days: int = 14, limit: int = 30, account: str = "") -> list[dict]:
        return []


class FakeCalendars:
    """CalendarHub's reading side over events from golden.yaml ({title, day, start, end, profile, attendees})."""

    def __init__(self, events: list[dict], tz, today: date):
        self.tz = tz
        self.sources: list[dict] = []
        self._events = []
        for e in events:
            d = today + timedelta(days=int(e.get("day", 0)))
            profile = e.get("profile", "work")
            name, account, color = CALENDARS.get(profile, CALENDARS["work"])
            all_day = not e.get("start")
            s = datetime.combine(d, dtime.fromisoformat(e["start"]) if not all_day else dtime.min, tzinfo=tz)
            end = datetime.combine(d, dtime.fromisoformat(e["end"]), tzinfo=tz) if e.get("end") else \
                s + (timedelta(days=1) if all_day else timedelta(hours=1))
            self._events.append({"calendar": name, "profile": profile, "color": color, "title": e["title"],
                                 "start": s.isoformat(), "end": end.isoformat(), "all_day": all_day,
                                 "location": e.get("location", ""), "description": e.get("description", ""),
                                 "attendees": e.get("attendees") or [], "link": "", "account": account})

    def refresh(self, force: bool = False) -> None:
        pass

    def google_sources(self) -> list[dict]:
        return []

    def events(self, start: datetime, end: datetime, profile: str | None = None) -> list[dict]:
        out = [e for e in self._events if (not profile or e["profile"] == profile)
               and datetime.fromisoformat(e["start"]) < end and datetime.fromisoformat(e["end"]) > start]
        return sorted(out, key=lambda x: (x["start"], not x["all_day"]))

    def agenda(self, days: int = 1, profile: str | None = None, start: datetime | None = None) -> list[dict]:
        base = start or datetime.now(self.tz)
        day0 = datetime.combine(base.date(), dtime.min, tzinfo=self.tz)
        return self.events(day0, day0 + timedelta(days=days), profile)

    def status(self) -> list[dict]:
        return [{"name": name, "profile": p, "color": color, "kind": "google", "account": account, "ok": True,
                 "error": None, "missing": None} for p, (name, account, color) in CALENDARS.items()]


class Recorder:
    """Stands in front of the model: passes every call through and keeps what the model proposed and how long it
    took. Everything else (label, can_act, model…) is the model's own."""

    def __init__(self, llm):
        self.llm = llm
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, **kw) -> dict:
        t0 = time.perf_counter()
        out = self.llm.chat(messages, tools=tools, **kw)
        self.calls.append({"ms": round((time.perf_counter() - t0) * 1000), "tools_offered": len(tools or []),
                           "tool_calls": [{"name": c["name"], "arguments": c["arguments"]} for c in out["tool_calls"]],
                           "content": (out.get("content") or "")[:600]})
        return out

    def __getattr__(self, name):
        return getattr(self.llm, name)


_TLS: dict = {}


@contextmanager
def _shared_tls():
    """Building Vesper's services makes ~17 HTTP clients, and each one loads the certificate bundle (about 0.1 s
    apiece). The fake world never opens an HTTPS connection, so while it is built they share one TLS context: a run
    starts in milliseconds instead of seconds. (The model's own client is made elsewhere, untouched.)"""
    real = ssl.create_default_context

    def shared(purpose=ssl.Purpose.SERVER_AUTH, *, cafile=None, capath=None, cadata=None):
        key = (purpose, cafile, capath, cadata)
        if key not in _TLS:
            _TLS[key] = real(purpose, cafile=cafile, capath=capath, cadata=cadata)
        return _TLS[key]
    ssl.create_default_context = shared
    try:
        yield
    finally:
        ssl.create_default_context = real


def _stub(name: str, canned: dict | None):
    def run(**args) -> dict:
        return dict(canned) if canned else {"ok": True, "done": name.replace("_", " ")}
    return run


class World:
    """One fresh Assistant for one run of one case: its own data folder, storage, memory and conversation."""

    def __init__(self, llm, spec: dict, toolset: list[str], stubs: dict | None = None, temperature: float = 0.3,
                 tmp: Path | None = None):
        self._tmp = None if tmp else tempfile.TemporaryDirectory(prefix="vesper-eval-")
        root = Path(tmp or self._tmp.name)
        cfg = Config(deep_merge(DEFAULTS, {
            "data_dir": str(root / "data"),
            "assistant": {"name": "Vesper", "user_name": "J", "timezone": spec.get("timezone", "America/New_York")},
            "voice": {"enabled": False, "speak_typed": False}, "tracking": {"enabled": False},
            "obs": {"enabled": False}, "claude": {"enabled": False},
            "pc_control": {"toast_confirm": False},
            "brain": {"provider": "local", "local": {"enabled": True, "auto_test": False, "model": llm.model,
                                                     "temperature": temperature, "tools": list(toolset)}},
            "goals": spec.get("goals") or {"north_star": "Grow the Twitch channel to Partner", "this_week": []},
            "news": {"feeds": []},
            "skills": {"memory": {"embed_model": ""}},  # words only: no embedding calls
        }))
        cfg.root = root
        with _shared_tls():
            self.svc = svc = build_services(cfg, EventBus(), Storage(":memory:"))
            self.a = a = Assistant(svc)
        nud = svc.skills.get("nudges") if svc.skills is not None else None
        if nud is not None:  # never this machine's own desktop
            nud.fullscreen, nud.idle = (lambda: ""), (lambda: 0)
        self.now = datetime.now(svc.tz)
        self.today = self.now.date()
        svc.gmail = self.gmail = FakeGmail(spec.get("emails") or [])
        svc.calendars = FakeCalendars(spec.get("events") or [], svc.tz, self.today)
        self.task_ids = [self._task(t)["id"] for t in spec.get("tasks") or []]
        self.fact_ids: list[int | None] = []
        for f in spec.get("facts") or []:  # one by one: a fact may replace an earlier one
            self.fact_ids.append(self._fact(f))

        self.recorder = Recorder(llm)
        # the time as usual, but never this PC's real active window: the same prompt on every PC, nothing private
        a._context_line = lambda: f"[{datetime.now(svc.tz):%A %B %d %Y, %I:%M %p} | active window: none | profile: none]"
        a.local.llm = self.recorder
        a.local.found = {"kind": "ollama", "url": getattr(llm, "url", ""), "models": [{"name": llm.model}]}
        for name, tool in list(a.tools.tools.items()):
            if name not in REAL_TOOLS:
                a.tools.tools[name] = dataclasses.replace(tool, handler=_stub(name, (stubs or {}).get(name)))
        self.executed: list[dict] = []
        svc.bus.on(self._on_event)
        self.thought: list = []  # what svc.think returned (plans, memory extraction)
        think = svc.think

        def remember_thought(*args, **kw):
            out = think(*args, **kw)
            self.thought.append(out)
            return out
        svc.think = remember_thought

    def _on_event(self, event: dict) -> None:
        if event["type"] == "tool":
            d = event.get("data") or {}
            self.executed.append({"name": d.get("name"), "args": d.get("args") or {}})

    def _day(self, rel) -> str | None:
        if rel in (None, ""):
            return None
        offset = {"today": 0, "tomorrow": 1, "yesterday": -1}.get(str(rel))
        return (self.today + timedelta(days=offset if offset is not None else int(rel))).isoformat()

    def _task(self, t: dict) -> dict:
        return self.svc.storage.add_task(t["title"], profile=t.get("profile", "work"), priority=int(t.get("priority", 2)),
                                         due=self._day(t.get("due")))

    def _fact(self, f: dict) -> int | None:
        mem = self.svc.memory
        replaces = f.get("supersedes")
        fact = mem.add(f["text"], kind=f.get("kind", "fact"), importance=int(f.get("importance", 3)),
                       contradicts=[self.fact_ids[replaces]] if replaces is not None else None)
        if not fact:
            return None
        when = time.time() - float(f.get("days_ago", 0)) * 86400
        self.svc.storage.execute("UPDATE facts SET learned_at = ? WHERE id = ?", (when, fact["id"]))
        return fact["id"]

    def at(self, hm: str) -> float:
        """A time today, as a timestamp (the planner's clock)."""
        return datetime.combine(self.today, dtime.fromisoformat(hm), tzinfo=self.svc.tz).timestamp()

    def close(self) -> None:
        if self._tmp is not None:
            try:
                self._tmp.cleanup()
            except OSError:  # Windows: a log file still open; the temp folder gets cleaned later
                pass
