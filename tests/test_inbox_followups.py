"""Follow-ups close themselves: a reply ends "waiting on" (and any "when Dana emails me" reminder), J's own answer ends
"owed", and a promise that looks kept asks "Did you …?" (✓/✕) instead of ticking itself off.

Gmail is the FakeMail from test_skill_inbox_zero."""
# ruff: noqa: F811  (its fixtures are imported, then named as test arguments)

import pytest
from test_google import HOME, WORK
from test_skill_inbox_zero import ME, fake, gmail, iz, mail, sent  # noqa: F401  (fixtures)

from assistant.skills import Skills
from assistant.skills.inbox_zero import did_it, done_question, task_words

DANA = "Dana Ortiz <dana@client.com>"


@pytest.fixture
def notices(svc):
    got = []
    svc.bus.on(lambda ev: got.append(ev["data"]) if ev["type"] == "notice" else None)
    return got


def test_a_reply_closes_waiting_quietly(iz, fake, notices):
    d = iz.refresh()
    assert "Dana Ortiz" in [w["name"] for w in d["waiting"]]
    fake.mail[WORK].append(mail("w9", "t2", DANA, ME[WORK], "Re: Pricing", "Thanks, the deck looks good.", 0.5))
    d = iz.refresh()
    assert "Dana Ortiz" not in [w["name"] for w in d["waiting"]]
    n = next(x for x in notices if x.get("source") == "inbox_zero")
    assert n["text"] == "Dana replied about “Pricing”." and n["level"] == "quiet"
    assert n["why"]["rule"].startswith("Follow-up closed itself")
    assert any("Gmail: Dana Ortiz answered" in e for e in n["why"]["evidence"])
    st = iz.status()
    assert [(c["kind"], c["text"]) for c in st["closed"]] == [("replied", "Dana replied about “Pricing”.")]
    iz.refresh()
    assert len([x for x in notices if x.get("source") == "inbox_zero"]) == 1  # once


def test_a_reply_resolves_the_email_from_reminder(iz, fake, svc):
    svc.skills = Skills(svc, ["automations"])
    auto = svc.skills["automations"]
    auto.remind("ask her about the invoice", trigger="email_from:dana")
    auto.remind("call the landlord back", trigger="email_from:marcus")
    iz.refresh()
    fake.mail[WORK].append(mail("w9", "t2", DANA, ME[WORK], "Re: Pricing", "Works for me.", 0.2))
    iz.refresh()
    text, kw = iz.nudged[-1]
    assert text == "Dana replied about “Pricing”. Your reminder: ask her about the invoice."
    assert kw["priority"] == "normal" and "Reminder: “ask her about the invoice” (when Dana emails)" in kw["why"]["evidence"]
    assert [i["text"] for i in auto.listing()["items"]] == ["call the landlord back"]  # only Dana's is resolved


def test_an_owed_reply_goes_once_j_answers(iz, fake, notices):
    d = iz.refresh()
    assert "Mom" in [x["name"] for x in d["owed"]]
    fake.mail[HOME].append(sent("h9", "u1", HOME, "Mom <mom@example.com>", "Re: Sunday dinner", "Yes, see you at 6!", 0.1))
    d = iz.refresh()
    assert "Mom" not in [x["name"] for x in d["owed"]]
    assert [c["text"] for c in iz.status()["closed"]] == ["You answered Mom about “Sunday dinner”."]
    assert not [x for x in notices if x.get("source") == "inbox_zero"]  # J knows he answered: no notice


def test_aging_out_is_not_a_reply(iz, fake, notices):
    iz.refresh()
    fake.mail[HOME] = [m for m in fake.mail[HOME] if m["threadId"] != "u3"]  # Marcus's thread vanishes from the search
    d = iz.refresh()
    assert "Marcus" not in [w["name"] for w in d["waiting"]]
    assert iz.closed == [] and not notices


def test_kept_promise_asks_before_ticking_off(iz, fake, svc):
    iz.refresh()
    ps = {p["task"]: p["id"] for p in iz.promise_list()}
    pid = ps["Send Dana the revised deck by Friday"]
    task = iz.set_promise(pid, "added")["task"]
    assert pid not in [p["id"] for p in iz.promise_list()]  # a task now
    # another promise in the same conversation is not "done"
    fake.mail[WORK].append(sent("w10", "t2", WORK, DANA, "Re: Pricing", "Sorry, I'll send the revised deck on Monday.", 1.0))
    iz.refresh()
    assert pid not in [p["id"] for p in iz.promise_list()]
    fake.mail[WORK].append(sent("w11", "t2", WORK, DANA, "Re: Pricing", "Hi Dana,\nHere's the revised deck, attached.\nJ", 0.5))
    iz.refresh()
    p = next(p for p in iz.promise_list() if p["id"] == pid)
    assert p["confirm"] and p["question"] == "Did you send Dana the revised deck by Friday?"
    assert p["confirm_quote"] == "Here's the revised deck, attached."
    text, kw = next((t, k) for t, k in iz.nudged if "Did you send Dana" in t)
    assert kw["priority"] == "low" and kw["why"]["rule"] == "A promise looks kept"
    assert svc.storage.list_tasks()[0]["status"] == "open"  # never ticked off by itself
    # ✕ Not yet: stays open, and that email isn't asked about again
    assert iz.actions()["kept_no"]({"id": pid})["ok"]
    iz._threads.clear()
    iz.refresh()
    assert pid not in [p["id"] for p in iz.promise_list()]
    # a later email that looks like it: asked again; ✓ ticks the task off
    fake.mail[WORK].append(sent("w12", "t2", WORK, DANA, "Re: Pricing", "As promised, the final deck: https://x.co/d", 0.2))
    iz.refresh()
    assert next(p for p in iz.promise_list() if p["id"] == pid)["confirm_quote"].startswith("As promised")
    r = iz.actions()["kept_yes"]({"id": pid})
    assert r["ok"] and r["task"]["id"] == task["id"] and r["task"]["status"] == "done"
    assert pid not in [p["id"] for p in iz.promise_list()]


def test_open_promise_can_be_confirmed_too(iz, fake):
    iz.refresh()
    pid = next(p["id"] for p in iz.promise_list() if p["task"] == "Call the landlord tomorrow (for Marcus)")
    fake.mail[HOME].append(sent("h10", "u3", HOME, "Marcus <marcus@example.com>", "Re: Landlord question",
                                "Called the landlord, the lease renews in March.", 0.3))
    iz.refresh()
    p = next(p for p in iz.promise_list() if p["id"] == pid)
    assert p["confirm"] and p["question"] == "Did you call the landlord tomorrow?"
    assert iz.actions()["kept_yes"]({"id": pid}) == {"ok": True, "done": True, "task": None}
    assert iz.actions()["kept_yes"]({"id": "gone"})["ok"] is False


def test_helpers():
    assert task_words("Send Dana the revised deck by Friday", "Dana Ortiz") == {"revised", "deck"}
    assert task_words("Check with legal on the indemnity clause (for Dana)", "Dana") == {"legal", "indemnity", "clause"}
    w = {"revised", "deck"}
    assert did_it("Here's the revised deck.", w) == "Here's the revised deck."
    assert did_it("I'll send the revised deck tomorrow.", w) is None
    assert did_it("Thanks! Talk soon.", w) is None
    assert did_it("Attached, as promised.", set()) == "Attached, as promised."
    assert did_it("> quoted\nOn Mon, Dana wrote:\nhere's the revised deck", w) is None
    assert done_question("Get back to Priya tomorrow") == "Did you get back to Priya tomorrow?"


def test_did_it_knows_the_past_tense():
    assert did_it("Called the landlord just now.", {"landlord"}, "Call") == "Called the landlord just now."
    assert did_it("Sent Priya the numbers.", {"numbers"}, "Send") == "Sent Priya the numbers."
    assert did_it("The landlord is away.", {"landlord"}, "Call") is None
