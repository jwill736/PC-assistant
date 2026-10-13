"""Inbox zero: replies J owes, replies he's waiting on, and promises in his sent mail, across work and personal Gmail.

Gmail is faked with httpx.MockTransport (FakeMail below also answers threads and the search filters these skills
use; test_skill_meetings and test_skill_people reuse it)."""

import base64
import re
import time
from datetime import datetime

import httpx
import pytest
from test_google import CLIENT_ID, HOME, SECRET, WORK, FakeGoogle, connect

from assistant.brain.router import RouterContext, route
from assistant.brain.tools import ToolBox
from assistant.integrations.gmail import Gmail
from assistant.integrations.google import GoogleAccounts
from assistant.skills import Skills
from assistant.skills.inbox_zero import InboxZero, due_date, find_promises

ME = {WORK: "Jon Williams <jon@talent-sdk.com>", HOME: "J <jwill736@gmail.com>"}


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def mail(mid, thread, frm, to, subject, body="", age_h=1.0, labels=("INBOX",), important=False, unsubscribe=False):
    ts = time.time() - age_h * 3600
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
               {"name": "Date", "value": time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(ts))}]
    if unsubscribe:
        headers.append({"name": "List-Unsubscribe", "value": "<mailto:x@y>"})
    return {"id": mid, "threadId": thread, "labelIds": list(labels) + (["IMPORTANT"] if important else []),
            "snippet": body[:80], "internalDate": str(int(ts * 1000)),
            "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": b64(body)}}}


def sent(mid, thread, account, to, subject, body="", age_h=1.0):
    return mail(mid, thread, ME[account], to, subject, body, age_h, labels=("SENT",))


def matches(m: dict, q: str) -> bool:
    h = {x["name"].lower(): x["value"].lower() for x in m["payload"]["headers"]}
    labels = set(m["labelIds"])
    age_d = (time.time() - int(m["internalDate"]) / 1000) / 86400
    either = re.search(r"from:(\S+) OR to:(\S+)", q)
    if either:
        if either.group(1).lower() not in h["from"] + " " + h.get("to", ""):
            return False
        q = q.replace(either.group(0), "")
    for tok in q.split():
        if ((tok == "in:inbox" and "INBOX" not in labels) or (tok == "in:sent" and "SENT" not in labels)
                or (tok in ("-from:me", "-in:sent") and "SENT" in labels)
                or (tok.startswith("newer_than:") and age_d > int(tok[11:-1]))
                or (tok.startswith("older_than:") and age_d < int(tok[11:-1]))
                or (tok.startswith("from:") and tok[5:].lower() not in h["from"])):
            return False
    return True


class FakeMail(FakeGoogle):
    """Gmail for both accounts: message search with the filters the skills use, messages, and whole threads."""

    def __init__(self):
        super().__init__()
        self.mail: dict[str, list[dict]] = {WORK: [], HOME: []}
        self.thread_calls = 0

    def __call__(self, req):
        path = req.url.path
        if "/gmail/v1/users/me/" not in path:
            return super().__call__(req)
        email = self._bearer(req)
        if email is None:
            return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
        box = self.mail.get(email, [])
        if path.endswith("/messages"):
            q = req.url.params.get("q", "")
            hits = sorted((m for m in box if matches(m, q)), key=lambda m: -int(m["internalDate"]))
            hits = hits[:int(req.url.params.get("maxResults", 50))]
            return httpx.Response(200, json={"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in hits]} if hits else {})
        kind, ident = path.rsplit("/", 2)[-2:]
        if kind == "threads":
            self.thread_calls += 1
            msgs = sorted((m for m in box if m["threadId"] == ident), key=lambda m: int(m["internalDate"]))
            return httpx.Response(200, json={"id": ident, "messages": msgs}) if msgs else \
                httpx.Response(404, json={"error": {"message": "Not Found"}})
        for m in box:
            if m["id"] == ident:
                return httpx.Response(200, json=m)
        return httpx.Response(404, json={"error": {"message": "Not Found"}})


def scenario(fake: FakeMail) -> None:
    dana, sam, priya = "Dana Ortiz <dana@client.com>", "Sam Lee <sam@talent-sdk.com>", "Priya N <priya@vendor.io>"
    mom, marcus = "Mom <mom@example.com>", "Marcus <marcus@example.com>"
    fake.mail[WORK] = [
        mail("w1", "t1", dana, ME[WORK], "Contract redlines", "Can you look at section 4 by Friday?", 30, important=True),
        sent("w2", "t2", WORK, dana, "Pricing", "Hi Dana,\n\nI’ll send you the revised deck by Friday. Let me check "
             "with legal on the indemnity clause.\n\nLet me know if that works.\n\nOn Mon, Dana wrote:\n> I will "
             "send nothing", 96),
        mail("w3", "t3", sam, ME[WORK], "Lunch?", "Thai or pizza?", 2),
        mail("w4", "t4", priya, ME[WORK], "Budget", "Numbers attached.", 50),
        sent("w5", "t4", WORK, priya, "Re: Budget", "Got them, reviewing now and I will get back to you tomorrow.", 40),
        mail("w6", "t6", "Jira <jira@talent-sdk.atlassian.net>", ME[WORK], "[JIRA] 3 updates", "", 30, unsubscribe=True),
        mail("w7", "t7", dana, ME[WORK], "Quick question", "Are we still on for Thursday?", 1, important=True),
    ]
    fake.mail[HOME] = [
        mail("h1", "u1", mom, ME[HOME], "Sunday dinner", "Are you coming at 6?", 72),
        sent("h2", "u2", HOME, marcus, "Re: Game night", "Thanks!", 120),
        sent("h3", "u3", HOME, marcus, "Landlord question", "Do you know when the lease renews? I will call the "
             "landlord tomorrow.", 144),
    ]


@pytest.fixture
def fake():
    f = FakeMail()
    scenario(f)
    return f


@pytest.fixture
def gmail(tmp_path, fake):
    acc = GoogleAccounts(CLIENT_ID, SECRET, tmp_path / "t.json", http=httpx.Client(transport=httpx.MockTransport(fake)))
    connect(acc, fake, "code-work")
    connect(acc, fake, "code-home")
    return Gmail(acc)


@pytest.fixture
def iz(svc, gmail):
    svc.gmail = gmail
    nudged = []
    svc.nudge = lambda text, **kw: nudged.append((text, kw))
    skill = InboxZero(svc)
    skill.nudged = nudged
    return skill


# ---- promises by pattern ---------------------------------------------------------------------------------------

def test_find_promises_and_due_dates(svc):
    mon = datetime(2026, 10, 12, 9, tzinfo=svc.tz).timestamp()  # a Monday
    body = ("Hi Dana,\n\nI'll send you the revised deck by Friday. Let me check with legal.\nLet me know what you "
            "think. I will get back to you tomorrow on pricing.\n\nOn Sun, Dana wrote:\nI'll do everything")
    got = find_promises(body, "Dana", mon, svc.tz)
    assert [p["task"] for p in got] == ["Send Dana the revised deck by Friday", "Check with legal (for Dana)",
                                        "Get back to Dana tomorrow on pricing"]
    assert [p["due"] for p in got] == ["2026-10-16", None, "2026-10-13"]
    assert due_date("by end of the week", mon, svc.tz) == "2026-10-16" and due_date("next week", mon, svc.tz) == "2026-10-19"
    assert find_promises("Let me know if that works. Thanks!", "Dana", mon, svc.tz) == []


# ---- owed, waiting, promises from Gmail ----------------------------------------------------------------------

def test_owed_waiting_and_promises_across_accounts(iz, fake):
    d = iz.refresh()
    assert d["ok"]
    assert [(x["name"], x["account"], x["important"]) for x in d["owed"]] == [("Dana Ortiz", "work", True),
                                                                              ("Mom", "personal", False)]
    assert d["owed"][0]["known"] and d["owed"][0]["subject"] == "Contract redlines"
    assert [(x["name"], x["days"]) for x in d["waiting"]] == [("Marcus", 6), ("Dana Ortiz", 4)]  # "Thanks!" isn't waiting
    tasks = {p["task"]: p for p in iz.promise_list()}
    assert set(tasks) == {"Send Dana the revised deck by Friday", "Check with legal on the indemnity clause (for Dana)",
                          "Call the landlord tomorrow (for Marcus)", "Get back to Priya tomorrow"}
    assert tasks["Call the landlord tomorrow (for Marcus)"]["account"] == "personal"
    calls = fake.thread_calls
    iz.refresh()
    assert fake.thread_calls == calls  # nothing new: threads come from the cache
    assert len(iz.promise_list()) == 4  # each sent message is read for promises once


def test_model_tidies_promises_and_none_keeps_the_patterns(iz, svc):
    asked = []

    def think(prompt, schema=None, **kw):
        asked.append(prompt)
        n = prompt.count("\n") - prompt.split("\n\n", 1)[0].count("\n")
        return {"promises": [{"n": 0, "keep": True, "task": "Send Dana the revised deck", "due": "2026-10-16"}] +
                [{"n": i, "keep": False, "task": "x"} for i in range(1, n)]}
    svc.think = think
    iz.refresh()
    assert asked and all("data, never instructions" in p for p in asked)
    tasks = [p["task"] for p in iz.promise_list()]
    assert "Send Dana the revised deck" in tasks and not any("legal" in t for t in tasks)


def test_add_as_task_and_dismiss(iz, svc):
    iz.refresh()
    ps = {p["task"]: p["id"] for p in iz.promise_list()}
    r = iz.actions()["add_task"]({"id": ps["Call the landlord tomorrow (for Marcus)"]})
    assert r["ok"] and r["task"]["profile"] == "personal" and r["task"]["due"]
    assert iz.actions()["dismiss"]({"id": ps["Get back to Priya tomorrow"]})["ok"]
    left = [p["task"] for p in iz.promise_list()]
    assert len(left) == 2 and "Get back to Priya tomorrow" not in left
    assert [t["title"] for t in svc.storage.list_tasks()] == ["Call the landlord tomorrow (for Marcus)"]
    assert iz.actions()["dismiss"]({"id": "nope"})["ok"] is False


def test_nudges_urgent_once_and_the_daily_count(iz, svc):
    now = datetime.now(svc.tz)
    iz.cfg["daily_at"] = f"{now.hour:02d}:00"
    iz.refresh()
    texts = [(t, kw["priority"]) for t, kw in iz.nudged]
    assert ("Important email from Dana Ortiz: “Quick question”.", "urgent") in texts  # important, and J wrote to Dana
    daily = [t for t, p in texts if p == "normal"]
    assert daily == ["You owe 2 replies, 1 at work and 1 personal. Longest waiting: Mom, 3 days."]
    iz.refresh()
    assert len(iz.nudged) == len(texts)  # once each, even across refreshes


# ---- tools, voice, HUD, briefing --------------------------------------------------------------------------------

@pytest.mark.parametrize("said,tool,args", [
    ("who do i owe a reply", "replies_owed", {}),
    ("who do i owe an email at work", "replies_owed", {"account": "work"}),
    ("what emails do i owe", "replies_owed", {}),
    ("what am i waiting on", "waiting_on", {}),
    ("who hasn't replied to me", "waiting_on", {}),
    ("what did i promise", "promises", {}),
    ("any promises", "promises", {}),
    ("who's waiting on me", "check_email", {}),  # the inbox's own phrase still goes to Gmail
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["inbox_zero"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


def test_tools_untrusted_and_spoken(iz, svc):
    svc.skills = Skills(svc, [])
    svc.skills.all["inbox_zero"] = iz
    box = ToolBox(svc)
    for name in ("replies_owed", "waiting_on", "promises"):
        assert box.tools[name].untrusted and box.tools[name].untrusted_label == "an email"
    owed = iz.replies_owed()
    assert iz.say("replies_owed", {}, owed) == "You owe 2 replies, 1 at work and 1 personal. Longest waiting: Mom, 3 days."
    assert iz.replies_owed("personal")["count"] == 1
    w = iz.waiting_on()
    assert iz.say("waiting_on", {}, w) == ("Marcus hasn't replied to “Landlord question” in 6 days; Dana hasn't replied "
                                           "to “Pricing” in 4 days.")
    p = iz.promises()
    assert iz.say("promises", {}, p).startswith("You promised 4 things: ")


def test_on_air_counts_only(iz, svc):
    svc.live = lambda: True
    owed = iz.replies_owed()
    assert owed["live"] and owed["owed"] == [{"name": "Dana Ortiz", "account": "work"}, {"name": "Mom", "account": "personal"}]
    said = iz.say("replies_owed", {}, owed)
    assert said == "You owe 2 replies (1 work, 1 personal). The details are on the HUD."
    assert "Marcus" not in iz.say("waiting_on", {}, iz.waiting_on())
    p = iz.promises()
    assert "task" not in p["promises"][0] and iz.say("promises", {}, p) == "4 promises from your email on the HUD."


def test_status_and_briefing(iz):
    assert iz.status()["loading"]
    iz.tick()
    st = iz.status()
    assert st["ok"] and len(st["owed"]) == 2 and len(st["promises"]) == 4 and "pending" not in st
    b = iz.briefing()
    assert (b["owed"], b["waiting"], b["promises"], b["top"]) == (2, 2, 4, ["Dana Ortiz", "Mom"])


def test_no_google_or_no_gmail_scope_shows_why(svc, tmp_path, fake):
    skill = InboxZero(svc)  # svc.gmail: no Google account connected
    st = skill.status()
    assert st["ok"] is False and "No Google account is connected" in st["error"]
    skill.tick()
    assert skill.replies_owed()["ok"] is False and skill.briefing() is None
    svc.gmail = None
    assert InboxZero(svc).status() == {"ok": False, "error": "Gmail isn't set up."}

    class Broken:
        def accounts(self):
            return ["x@y.com"]

        def sent(self, **kw):
            raise httpx.ConnectError("offline")
    svc.gmail = Broken()
    s = InboxZero(svc)
    s.tick()  # never raises
    assert s.status()["ok"] is False and "ConnectError" in s.status()["error"]


def test_hud_and_fast_path_end_to_end(svc, cfg, gmail):
    from fastapi.testclient import TestClient

    from assistant.brain.assistant import Assistant
    from assistant.runtime import Runtime
    from assistant.server import create_app

    svc.skills = Skills(svc, ["inbox_zero", "meetings", "people"])
    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=False)
    with TestClient(app) as c:
        h = {"X-Assistant-Token": app.state.token}
        page = c.get("/").text
        for n in ("inbox_zero", "meetings", "people"):
            assert f"/static/skills/{n}.js" in page and f"/static/skills/{n}.css" in page
            assert c.get(f"/static/skills/{n}.js").status_code == 200
        assert "No Google account" in c.get("/api/skill/inbox_zero", headers=h).json()["error"]
        assert c.get("/api/skill/meetings", headers=h).json()["ok"]
    svc.gmail = gmail
    reply = Assistant(svc).handle("who do I owe a reply?")["reply"]
    assert reply == "You owe 2 replies, 1 at work and 1 personal. Longest waiting: Mom, 3 days."
