"""Google Tasks two-way sync: a "Vesper · Work" list in the work account and "Vesper · Personal" in the personal one,
new / done / renamed / due dates both ways, newest change wins, never copied twice, and the missing box named."""

import threading
import time

import pytest
from fake_workspace import ALL_BUT, FakeWorkspace, accounts_for
from test_google import HOME, WORK

from assistant.brain.router import RouterContext, route
from assistant.brain.tools import ToolBox
from assistant.skills import Skills
from assistant.skills.gtasks import GTasks


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def fake():
    return FakeWorkspace()


@pytest.fixture
def gt(svc, fake, tmp_path):
    svc.google = accounts_for(tmp_path, fake)
    skill = svc.skills["gtasks"]  # the one instance listening on the bus
    assert isinstance(skill, GTasks)
    skill.clock = skill.clk = Clock()
    return skill


def titles(fake, email, title):
    return sorted(t["title"] for t in fake.tasks_in(email, title))


def test_makes_a_list_per_area_in_the_right_account(gt, svc, fake):
    svc.storage.add_task("Finish Q3 deck", "work", 1, "2026-10-23")
    svc.storage.add_task("Book dentist", "personal", 3)
    svc.storage.add_task("Cut promo from VOD", "stream", 2)
    r = gt.sync()
    assert r["ok"] and r["counts"]["sent"] == 3
    assert {li["title"] for li in fake.lists[WORK].values()} == {"Vesper · Work"}
    assert {li["title"] for li in fake.lists[HOME].values()} == {"Vesper · Stream", "Vesper · Personal"}
    [deck] = fake.tasks_in(WORK, "Vesper · Work")
    assert deck["title"] == "Finish Q3 deck" and deck["due"] == "2026-10-23T00:00:00.000Z"
    assert titles(fake, HOME, "Vesper · Personal") == ["Book dentist"]
    again = gt.sync()  # nothing new: nothing written, nothing copied twice
    assert again["counts"]["sent"] == 0 and len(fake.tasks_in(WORK, "Vesper · Work")) == 1
    assert {(x["profile"], x["account"], x["tasks"]) for x in gt.status()["lists"]} == {
        ("work", WORK, 1), ("stream", HOME, 1), ("personal", HOME, 1)}


def test_changes_both_ways(gt, svc, fake):
    t = svc.storage.add_task("Finish Q3 deck", "work", 1)
    gt.sync()
    # in Vesper: renamed and a due date
    svc.storage.update_task(t["id"], title="Finish Q3 deck v2", due="2026-10-30")
    fake.tick()
    gt.sync()
    [g] = fake.tasks_in(WORK, "Vesper · Work")
    assert g["title"] == "Finish Q3 deck v2" and g["due"].startswith("2026-10-30")
    # on the phone: ticked off
    g["status"], g["updated"] = "completed", (fake.tick(), fake.rfc())[1]
    r = gt.sync()
    assert r["counts"]["updated_here"] == 1 and svc.storage.list_tasks("done")[0]["title"] == "Finish Q3 deck v2"
    # added on the phone: comes in once
    fake.gtask(WORK, "Call the accountant", due="2026-11-02")
    gt.sync()
    gt.sync()
    new = [x for x in svc.storage.list_tasks() if x["title"] == "Call the accountant"]
    assert len(new) == 1 and new[0]["profile"] == "work" and new[0]["due"] == "2026-11-02"
    # done in Vesper: completed there
    svc.storage.update_task(new[0]["id"], status="done")
    gt.sync()
    assert next(x for x in fake.tasks_in(WORK, "Vesper · Work") if x["title"] == "Call the accountant")["status"] == "completed"


def test_newest_change_wins(gt, svc, fake):
    t = svc.storage.add_task("Draft blog post", "work", 2)
    gt.sync()
    [g] = fake.tasks_in(WORK, "Vesper · Work")
    # both sides renamed it; Google's edit is newer
    gt.clk.t = 1_800_000_100.0
    svc.storage.update_task(t["id"], title="Draft blog post (Vesper)")
    svc.bus.publish("tasks", svc.storage.list_tasks())  # Vesper notices its change at 100 s
    if gt._timer:
        gt._timer.cancel()
    fake.t = 1_800_000_200.0
    g["title"], g["updated"] = "Draft blog post (phone)", fake.rfc()
    r = gt.sync()
    assert r["counts"]["conflicts"] == 1 and svc.storage.list_tasks()[0]["title"] == "Draft blog post (phone)"
    # now Vesper's edit is the newer one
    gt.clk.t = 1_800_000_500.0
    svc.storage.update_task(t["id"], title="Draft blog post (final)")
    svc.bus.publish("tasks", svc.storage.list_tasks())
    if gt._timer:
        gt._timer.cancel()
    fake.t = 1_800_000_300.0
    g = fake.tasks_in(WORK, "Vesper · Work")[0]
    g["title"], g["updated"] = "Draft blog post (older phone edit)", fake.rfc()
    gt.sync()
    assert fake.tasks_in(WORK, "Vesper · Work")[0]["title"] == "Draft blog post (final)"
    assert svc.storage.list_tasks()[0]["title"] == "Draft blog post (final)"


def test_dedupes_by_google_id_and_title(gt, svc, fake, tmp_path):
    svc.storage.add_task("Renew passport", "personal", 2)
    gt.sync()
    gt.svc.storage.execute("DELETE FROM gtasks_map")  # the link table lost (a restored backup)
    gt.sync()
    assert titles(fake, HOME, "Vesper · Personal") == ["Renew passport"]
    assert [x["title"] for x in svc.storage.list_tasks()] == ["Renew passport"]


def test_deleted_in_google_stays_here_and_isnt_sent_back(gt, svc, fake):
    svc.storage.add_task("Old idea", "work", 3)
    gt.sync()
    li = next(iter(fake.lists[WORK].values()))
    li["tasks"].clear()
    r = gt.sync()
    assert r["counts"]["unlinked"] == 1 and fake.tasks_in(WORK, "Vesper · Work") == []
    assert gt.sync()["counts"]["sent"] == 0 and svc.storage.list_tasks()[0]["title"] == "Old idea"


def test_a_deleted_list_is_made_again(gt, svc, fake):
    svc.storage.add_task("Ship it", "work", 1)
    gt.sync()
    fake.lists[WORK].clear()
    gt.sync()
    assert titles(fake, WORK, "Vesper · Work") == ["Ship it"]


def test_says_which_box_is_missing(svc, fake, tmp_path):
    fake.grant[WORK] = ALL_BUT["Google Tasks"]
    svc.google = accounts_for(tmp_path, fake)
    svc.skills = Skills(svc, ["gtasks"])
    gt = svc.skills["gtasks"]
    svc.storage.add_task("Work thing", "work", 2)
    svc.storage.add_task("Home thing", "personal", 2)
    r = gt.sync()
    assert fake.lists[WORK] == {} and titles(fake, HOME, "Vesper · Personal") == ["Home thing"]
    assert any(WORK in e and "Create, edit, organize, and delete all your tasks" in e for e in r["errors"])
    assert "Reconnect" in gt.status()["missing"]


def test_nothing_connected(svc):
    svc.skills = Skills(svc, ["gtasks"])
    gt = svc.skills["gtasks"]
    st = gt.status()
    assert st["ok"] is False and "Google" in st["error"]
    r = gt.sync()
    assert r["lists"] == [] and "isn't" in svc.skills.say("sync_google_tasks", {}, r) or "No Google" in r["errors"][0]
    gt.tick()  # never throws


def test_syncs_soon_after_a_change(gt, svc, fake, monkeypatch):
    monkeypatch.setattr("assistant.skills.gtasks.DEBOUNCE_S", 0.05)
    svc.storage.add_task("Before start", "work", 2)
    svc.bus.publish("tasks", svc.storage.list_tasks())
    assert gt._timer is None  # not started yet: no background sync
    gt.start(None)
    done = threading.Event()
    real = gt.sync
    monkeypatch.setattr(gt, "sync", lambda: (real(), done.set())[0])
    svc.storage.add_task("From the HUD", "work", 2)
    svc.bus.publish("tasks", svc.storage.list_tasks())
    assert done.wait(5)
    assert titles(fake, WORK, "Vesper · Work") == ["Before start", "From the HUD"]


def test_route_tool_and_say(gt, svc, fake):
    svc.storage.add_task("Thing", "work", 2)
    skills = svc.skills
    intent = route("sync my tasks with google", RouterContext(skill_route=skills.route))
    assert intent.tool == "sync_google_tasks"
    box = ToolBox(svc)
    assert box.tools["sync_google_tasks"].tier_for({}) == 1
    r = box.run("sync_google_tasks", {})
    assert skills.say("sync_google_tasks", {}, r) == "Google Tasks is in step: 1 sent."
    assert skills.action("gtasks", "sync", {})["ok"]
    time.sleep(0)  # (the timer from the publish, if any, is harmless)
