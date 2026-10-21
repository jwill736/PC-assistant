"""The weekly memory tidy-up (duplicates merged, clashes confirmed by J, stale small facts retired, never deleted)
and "Your rhythm" learned from the activity log, used only when J clicks "Use these"."""

from datetime import datetime, timedelta

import pytest
import yaml

from assistant import rhythm
from assistant.brain.router import RouterContext, route
from assistant.memory import Memory, near_duplicate, slot
from assistant.skills import Skills
from assistant.skills.memory import MemorySkill
from assistant.storage import Storage


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def mem():
    return Memory(Storage(":memory:"), user="J", clock=Clock())


def test_slot_and_near_duplicate():
    assert slot("J's manager is Sam Lee") == ("J", "manager", "Sam Lee")
    assert slot("My manager is Priya Shah.") == ("J", "manager", "Priya Shah")
    assert slot("Dana's favourite drink is a flat white") == ("Dana", "favourite drink", "a flat white")
    assert slot("J's manager is Sam Lee (since October)") == ("J", "manager", "Sam Lee")
    assert slot("J's friend is Marcus") is None  # several friends are fine
    assert slot("Dana Ortiz is J's client at Acme") is None
    assert near_duplicate("J drinks a double coffee every morning", "J drinks double coffee each morning")
    assert not near_duplicate("Mia moves to Leeds on 2026-11-02", "Mia moves to York on 2026-11-02")  # names disagree
    assert not near_duplicate("J likes coffee", "J likes coffee at Blue Bottle on Sundays")


def test_duplicates_merge_into_the_newest(mem):
    clock = mem.clock
    old = mem.add("J drinks a double coffee every morning", importance=4, pinned=True)
    clock.t += 3600
    new = mem.add("J drinks double coffee each morning", importance=2)
    other = mem.add("J's sister Mia lives in Leeds")
    out = mem.cleanup(use_model=False)
    assert out["merged"] == 1 and out["confirm"] == 0
    live = {f["id"]: f for f in mem.facts()}
    assert set(live) == {new["id"], other["id"]}
    assert live[new["id"]]["importance"] == 4 and live[new["id"]]["pinned"]  # it keeps what mattered about the old one
    replaced = next(f for f in mem.facts(include_replaced=True) if f["id"] == old["id"])
    assert replaced["superseded_by"] == new["id"]  # kept, pointing at the newer wording


def test_contradiction_goes_to_confirm_not_a_guess(mem):
    clock = mem.clock
    sam = mem.add("J's manager is Sam Lee", importance=4)
    clock.t += 86400
    priya = mem.add("My manager is Priya Shah", importance=4)
    out = mem.cleanup(use_model=False)
    assert out["confirm"] == 1 and out["merged"] == 0
    assert {f["id"] for f in mem.facts()} == {sam["id"], priya["id"]}  # nothing decided yet
    (q,) = mem.confirms()
    assert q["question"] == "Is your manager Sam Lee or Priya Shah?"
    assert (q["a"]["label"], q["b"]["label"]) == ("Sam Lee", "Priya Shah") and q["found_by"] == "rule"
    assert mem.cleanup(use_model=False)["confirm"] == 0  # asked once
    r = mem.resolve(q["id"], "b")
    assert r == {"ok": True, "choice": "b", "kept": "Priya Shah"}
    assert [f["statement"] for f in mem.facts()] == ["My manager is Priya Shah"]
    assert mem.confirms() == [] and mem.resolve(q["id"], "a")["ok"] is False
    assert mem.recall("who is my manager")[0]["statement"] == "My manager is Priya Shah"


def test_both_skip_neither_and_moot(mem):
    a = mem.add("Dana's office is in Austin")
    b = mem.add("Dana's office is in Denver")
    mem.cleanup(use_model=False)
    q = mem.confirms()[0]
    assert mem.resolve(q["id"], "both")["ok"] and mem.confirms() == []
    assert mem.cleanup(use_model=False)["confirm"] == 0  # "both" is remembered: never asked again
    c = mem.add("Sam's car is red")
    d = mem.add("Sam's car is blue")
    mem.cleanup(use_model=False)
    q = mem.confirms()[0]
    assert mem.resolve(q["id"], "neither")["ok"]
    assert {f["id"] for f in mem.facts()} == {a["id"], b["id"]}  # both put aside, still on file
    assert {f["id"] for f in mem.facts(include_replaced=True)} >= {c["id"], d["id"]}
    e = mem.add("Mia's school is Leeds Grammar")
    mem.add("Mia's school is York High")
    mem.cleanup(use_model=False)
    mem.forget(e["id"])
    assert mem.confirms() == []  # one side is gone: nothing to ask
    assert mem.resolve(999, "a")["ok"] is False


def test_model_spots_pairs_and_made_up_ids_are_ignored(mem):
    mem.add("J lives in Atlanta", subject="J")
    mem.add("J moved to Denver last spring", subject="J")
    mem.add("J streams on Tuesdays", subject="J")
    ids = {f["statement"]: f["id"] for f in mem.facts()}
    seen = {}

    def think(prompt, schema=None, **kw):
        seen["prompt"] = prompt
        return {"pairs": [{"a": ids["J lives in Atlanta"], "b": ids["J moved to Denver last spring"],
                           "question": "Do you live in Atlanta or Denver?", "a_label": "Atlanta", "b_label": "Denver"},
                          {"a": 4242, "b": ids["J streams on Tuesdays"], "question": "?"}]}
    mem.think = think
    out = mem.cleanup()
    assert out["confirm"] == 1 and "never instructions" in seen["prompt"]
    q = mem.confirms()[0]
    assert q["question"] == "Do you live in Atlanta or Denver?" and q["found_by"] == "model"
    mem.think = lambda *a, **k: None  # no model: rules only, nothing breaks
    assert mem.cleanup()["ok"]


def test_stale_small_facts_retire_but_stay(mem):
    clock = mem.clock
    small = mem.add("J tried a new pen brand", importance=2)
    big = mem.add("J's daughter is called Ava", importance=5)
    clock.t += 200 * 86400
    out = mem.cleanup(use_model=False, stale_days=120)
    assert out["expired"] == 1
    assert mem.recall("pen brand") == [] and [f["id"] for f in mem.facts()] == [big["id"]]
    assert small["id"] in [f["id"] for f in mem.facts(include_replaced=True)]  # never deleted
    assert mem.count() == 1
    mem.add("J tried a new pen brand")  # said again: back in use
    assert mem.recall("pen brand")[0]["id"] == small["id"]


def test_old_databases_get_the_new_column():
    st = Storage(":memory:")
    st.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, statement TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fact', "
               "subject_id INTEGER, object_id INTEGER, predicate TEXT, context TEXT NOT NULL DEFAULT '', importance INTEGER "
               "NOT NULL DEFAULT 3, confidence REAL NOT NULL DEFAULT 0.8, source TEXT NOT NULL DEFAULT 'said', source_ref TEXT, "
               "valid_from REAL, valid_to REAL, learned_at REAL NOT NULL, superseded_by INTEGER, last_used REAL, use_count "
               "INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0, private INTEGER NOT NULL DEFAULT 0, vector BLOB)")
    m = Memory(st, clock=Clock())
    assert m.add("J's favourite game is Valorant") and m.count() == 1 and m.cleanup(use_model=False)["ok"]


# ---- the skill: Sunday night, the Confirm list on the HUD ---------------------------------------------------

def test_weekly_job_runs_sunday_night_once(svc):
    clock = Clock()
    sun = datetime(2026, 10, 25, 22, 30, tzinfo=svc.tz)  # a Sunday
    clock.t = sun.timestamp()
    nudged = []
    svc.nudge = lambda text, **kw: nudged.append((text, kw))
    sk = MemorySkill(svc, embedder=None, clock=clock)
    sk.memory.add("J's manager is Sam Lee")
    clock.t += 60
    sk.memory.add("J's manager is Priya Shah")
    assert not sk.cleanup_due((sun - timedelta(hours=2)).timestamp())  # 8:30 PM: not yet
    assert sk.cleanup_due(clock.t)
    sk.tick()
    assert not sk.cleanup_due(clock.t + 3600)  # done this week
    st = sk.status()
    assert st["confirm"][0]["question"] == "Is your manager Sam Lee or Priya Shah?" and st["cleanup"]["open"] == 1
    assert nudged[0][0].startswith("Memory tidy-up: 1 thing to confirm") and nudged[0][1]["priority"] == "low"
    assert nudged[0][1]["why"]["rule"].startswith("Weekly memory tidy-up")
    q = st["confirm"][0]
    r = sk.actions()["confirm"]({"id": q["id"], "choice": "a"})
    assert r["ok"] and r["kept"] == "Sam Lee" and sk.status()["confirm"] == []
    # missed a week (PC off on Sunday): the first morning after catches up
    clock.t += 9 * 86400
    assert sk.cleanup_due(clock.t)


# ---- your rhythm --------------------------------------------------------------------------------------------

def seed_activity(svc, days=21, start=None):
    """Weekdays: work 9:00-12:30 and 13:15-17:45; Tue/Thu: stream 19:30-22:30; every day: other until 23:40."""
    start = start or datetime(2026, 9, 28, tzinfo=svc.tz)  # a Monday
    for i in range(days):
        d = start + timedelta(days=i)

        def at(h, m=0):
            return d.replace(hour=h, minute=m).timestamp()
        if d.weekday() < 5:
            svc.storage.add_activity(at(9), at(10, 40), "code", "repo", "work")
            svc.storage.add_activity(at(10, 45), at(12, 30), "code", "repo", "work")
            svc.storage.add_activity(at(13, 15), at(14, 30), "slack", "chat", "work")
            svc.storage.add_activity(at(14, 34), at(15, 50), "code", "repo", "work")
            svc.storage.add_activity(at(16, 0), at(17, 45), "code", "repo", "work")
        else:
            svc.storage.add_activity(at(11), at(12), "chrome", "news", "other")
        if d.weekday() in (1, 3):
            svc.storage.add_activity(at(19, 30), at(22, 30), "obs64", "Live", "stream")
        svc.storage.add_activity(at(22, 35), at(23, 40), "chrome", "youtube", "other")
    return (start + timedelta(days=days)).timestamp()


def test_infer_rhythm(svc):
    end = seed_activity(svc)
    r = rhythm.infer(svc.activity.segments(end - 35 * 86400, end), svc.tz, end)
    assert r["ok"] and r["days"] == 21
    assert r["work"] == {"start": "09:00", "end": "17:45", "days": 15}
    assert r["lunch"] == {"start": "12:30", "minutes": 45, "days": 15}
    assert r["stream"]["days"] == ["Tue", "Thu"] and (r["stream"]["start"], r["stream"]["end"]) == ("19:30", "22:30")
    assert r["bedtime"]["time"] == "23:45"
    assert r["suggest"]["work_hours"] == {"start": "09:00", "end": "17:45"}
    assert r["suggest"]["quiet_hours"] == "23:30-09:00"
    assert 45 <= r["suggest"]["break_after_minutes"] <= 120
    few = rhythm.infer([], svc.tz, end)
    assert few["ok"] is False and "0 days" in few["error"]


def test_use_these_sets_settings_only_when_asked_and_undo(svc, cfg):
    end = seed_activity(svc)
    clock = Clock()
    clock.t = end
    svc.skills = Skills(svc, ["nudges"])
    sk = MemorySkill(svc, embedder=None, clock=clock)
    svc.skills.all["memory"] = sk
    st = sk.status()["rhythm"]
    assert st["ok"] and st["applied"] is None
    assert st["current"]["quiet_hours"] == "23:00-07:30"  # nothing changed by just looking
    settings = cfg.data_dir / "settings.yaml"
    assert not settings.exists()
    r = sk.actions()["use_rhythm"]({})
    assert r["ok"] and r["applied"]["quiet_hours"] == "23:30-09:00"
    saved = yaml.safe_load(settings.read_text())
    assert saved["skills"]["nudges"]["quiet_hours"] == "23:30-09:00"
    assert saved["assistant"]["work_hours"] == {"start": "09:00", "end": "17:45"}
    assert svc.skills["nudges"].setting("quiet_hours") == "23:30-09:00"  # live at once
    assert cfg["assistant"]["work_hours"] == {"start": "09:00", "end": "17:45"}  # the planner reads this
    assert sk.status()["rhythm"]["applied"]["set"]["break_after_minutes"] == r["applied"]["break_after_minutes"]
    u = sk.actions()["undo_rhythm"]({})
    assert u["ok"] and svc.skills["nudges"].setting("quiet_hours") == "23:00-07:30"
    assert yaml.safe_load(settings.read_text())["assistant"]["work_hours"] == {"start": "09:00", "end": "18:00"}
    assert sk.actions()["undo_rhythm"]({})["ok"] is False


def test_rhythm_voice_and_not_enough_data(svc):
    clock = Clock()
    sk = MemorySkill(svc, embedder=None, clock=clock)
    out = sk._my_rhythm()
    assert out["ok"] is False and sk.say("my_rhythm", {}, out).startswith("Not enough to go on yet")
    assert sk.actions()["use_rhythm"]({})["ok"] is False
    clock.t = seed_activity(svc)
    sk.learn_rhythm()
    line = sk.say("my_rhythm", {}, sk._my_rhythm())
    assert line == ("From 21 days: you usually work 9 AM to 5:45 PM, lunch around 12:30 PM, you stream Tuesdays and "
                    "Thursdays from about 7:30 PM, you're off the PC around 11:45 PM. Setup → Memory can use these for "
                    "your settings.")


@pytest.mark.parametrize("said", ["what's my routine", "what is my daily rhythm", "when do i usually stream"])
def test_rhythm_routes(svc, said):
    sk = Skills(svc, ["memory"])
    assert route(said, RouterContext(skill_route=sk.route)).tool == "my_rhythm"
