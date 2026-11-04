"""Sweep regressions for background jobs (LIFE-7, LIFE-8, LIFE-13)."""

import threading
import time

import pytest
from test_skill_tasks_agent import act, make, read, recipe

from assistant.skills.tasks_agent import SUMMARY_SCHEMA


@pytest.fixture
def nudged(svc):
    out = []
    svc.nudge = lambda text, **kw: out.append((text, kw))
    return out


# ---- LIFE-7: "cancel that job" during the write-up --------------------------------------------------------------------

def test_cancel_during_the_write_up_stops_the_pre_approved_delivery(svc, nudged):
    sent = []
    a = make(svc, [read("calendar"), act("send_to_phone", sent)], model=True)
    a.extra_recipes = [recipe("r", [{"id": "c", "tool": "calendar"}])]
    jid = a.create(recipe="r", preapproved=True, deliver=["phone"])["job"]["id"]
    heard = []

    def think(prompt, schema=None, **kw):
        if schema is SUMMARY_SCHEMA:  # J cancels while the write-up is being made
            out = a.control("cancel", "that")
            heard.append(a.say("job_control", {}, out))
            return {"summary": "Found two.", "markdown": "# R\n\nFound two."}
        return None
    svc.think = think
    a.drain()
    job = a.jobs[jid]
    assert heard == ["Cancelled R."]
    assert sent == [], f"sent to the phone after J heard 'Cancelled': {sent}"
    assert job["state"] == "cancelled", job["state"]
    assert not any("is done" in t for t, _ in nudged)


def test_cancel_during_the_write_up_leaves_nothing_waiting_for_an_ok(svc, nudged):
    a = make(svc, [read("calendar"), act("email_me", [])], model=True)
    a.extra_recipes = [recipe("r", [{"id": "c", "tool": "calendar"}])]
    jid = a.create(recipe="r", deliver=["email"])["job"]["id"]

    def think(prompt, schema=None, **kw):
        a.control("cancel", "that")
        return {"summary": "Found two.", "markdown": "# R"}
    svc.think = think
    a.drain()
    job = a.jobs[jid]
    assert job["state"] == "cancelled" and not [x for x in job["approvals"] if x["state"] == "waiting"]
    assert not any("Waiting on your OK" in t for t, _ in nudged)


def test_cancel_mid_batch_of_pre_approved_actions_sends_no_more(svc, nudged):
    sent = []
    a = make(svc, [read("calendar", lambda **k: {"ok": True, "items": [{"t": "a"}, {"t": "b"}, {"t": "c"}]}),
                   act("add_task", sent)])

    def add(**k):
        sent.append(k)
        if len(sent) == 1:
            a.control("cancel", "that")
        return {"ok": True}
    a.toolbox.tools["add_task"].handler = add
    a.extra_recipes = [recipe("r", [{"id": "c", "tool": "calendar"},
                                    {"id": "t", "tool": "add_task", "args": {"title": "{item.t}"},
                                     "each": {"from": "c", "path": "items"}}])]
    jid = a.create(recipe="r", preapproved=True)["job"]["id"]
    a.drain()
    assert len(sent) == 1 and a.jobs[jid]["state"] == "cancelled"


# ---- LIFE-13: "add to that" during the write-up delivers once -----------------------------------------------------------

def test_add_to_that_during_the_write_up_delivers_once(svc, nudged):
    a = make(svc, [read("calendar"), read("web_lookup"), act("email_me", [])], model=True)
    a.extra_recipes = [recipe("r", [{"id": "c", "tool": "calendar"}])]
    jid = a.create(recipe="r", deliver=["email"])["job"]["id"]
    added, model = [], {"on": True}
    a._has_model = lambda: model["on"]

    def think(prompt, schema=None, **kw):
        if schema is SUMMARY_SCHEMA and not added:
            model["on"] = False  # add_to plans with the web_lookup fallback (no second model call in this thread)
            added.append(a.add_to("that", "also check Best Buy"))
            model["on"] = True
        return {"summary": "Found two.", "markdown": "# R\n\nFound two."}
    svc.think = think
    a.drain()
    job = a.jobs[jid]
    assert added and added[0]["ok"]
    emails = [x for x in job["approvals"] if x["tool"] == "email_me" and x["state"] == "waiting"]
    assert len(emails) == 1, f"{len(emails)} 'email you the write-up' items waiting: {[x['step'] for x in emails]}"
    assert job["steps"][1]["title"] == "Also: also check Best Buy" and job["steps"][1]["state"] == "done"


def test_add_to_that_during_a_pre_approved_write_up_sends_once(svc, nudged):
    sent = []
    a = make(svc, [read("calendar"), read("web_lookup"), act("email_me", sent)], model=True)
    a.extra_recipes = [recipe("r", [{"id": "c", "tool": "calendar"}])]
    jid = a.create(recipe="r", deliver=["email"], preapproved=True)["job"]["id"]
    added, model = [], {"on": True}
    a._has_model = lambda: model["on"]

    def think(prompt, schema=None, **kw):
        if schema is SUMMARY_SCHEMA and not added:
            model["on"] = False
            added.append(a.add_to("that", "also check Best Buy"))
            model["on"] = True
        return {"summary": "Found two.", "markdown": "# R\n\nFound two and Best Buy."}
    svc.think = think
    a.drain()
    assert len(sent) == 1 and a.jobs[jid]["state"] == "done"


# ---- LIFE-8: "add to that" answers at once ------------------------------------------------------------------------------

def test_add_to_that_answers_without_waiting_for_the_jobs_model_call(svc, nudged):
    a = make(svc, [read("calendar"), read("web_lookup", lambda **k: {"ok": True, "results": [k["query"]]})], model=True)
    a.extra_recipes = [recipe("r", [{"id": "c", "tool": "calendar"}])]
    jid = a.create(recipe="r")["job"]["id"]
    inside = threading.Event()

    def think(prompt, schema=None, **kw):
        if schema is SUMMARY_SCHEMA:
            inside.set()
            time.sleep(1.5)  # the write-up holds the model (60-120 s on the 35B)
            return {"summary": "Found two.", "markdown": "# R"}
        return {"title": "x", "steps": [{"tool": "web_lookup", "title": "Best Buy", "args": {"query": "best buy"}}]}
    svc.think = think
    t = threading.Thread(target=a.drain, daemon=True)
    t.start()
    assert inside.wait(5)
    t0 = time.monotonic()
    out = a.add_to("that", "also check Best Buy")
    waited = time.monotonic() - t0
    t.join(15)
    assert out["ok"] and a.say("job_add", {}, out) == "Adding that to R."
    assert waited < 1.0, f"J's command waited {waited:.1f} s for the job's own model call"
    job = a.jobs[jid]
    assert [s["title"] for s in job["steps"]] == ["calendar", "Best Buy"]  # planned by the model, on the job's turn
    assert job["steps"][1]["state"] == "done" and job["state"] == "done"


def test_add_to_that_by_voice_does_not_wait_for_itself(svc, nudged):
    """add_to ran inside Assistant.handle (thinking=active), and its model call first yielded to J: to itself."""
    a = make(svc, [read("calendar"), read("web_lookup")], model=True)
    a.cfg["yield_seconds"] = 2
    a.extra_recipes = [recipe("r", [{"id": "c", "tool": "calendar"}])]
    a.create(recipe="r")
    svc.think = lambda prompt, schema=None, **kw: {"title": "x", "steps": [{"tool": "web_lookup", "title": "Best Buy",
                                                                            "args": {"query": "best buy"}}]}
    svc.bus.publish("thinking", {"active": True})  # Assistant.handle, J's command
    t0 = time.monotonic()
    out = a.add_to("that", "also check Best Buy")
    waited = time.monotonic() - t0
    svc.bus.publish("thinking", {"active": False})
    assert out["ok"]
    assert waited < 1.0, f"J's own command waited {waited:.1f} s (the whole yield) for itself"


def test_an_addition_the_model_cant_plan_is_the_web_lookup(svc, nudged):
    looked = []
    a = make(svc, [read("calendar"), read("web_lookup", lambda **k: looked.append(k["query"]) or {"ok": True})],
             model=True)
    a.extra_recipes = [recipe("r", [{"id": "c", "tool": "calendar"}], inputs=[{"name": "topic", "label": "Topic"}])]
    jid = a.create(recipe="r", inputs={"topic": "capture cards"})["job"]["id"]
    svc.think = lambda prompt, schema=None, **kw: None
    a.drain()
    a.add_to("that", "also check Best Buy")
    a.drain()
    assert looked == ["capture cards Best Buy"] and a.jobs[jid]["state"] == "done"
