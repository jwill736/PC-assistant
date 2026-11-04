"""What Vesper costs a gaming/streaming PC when nobody is looking: the system poller doesn't spawn nvidia-smi or
walk every disk every few seconds with no HUD open, and the caches that grow with use have a ceiling."""

import pytest

from assistant.integrations import system


@pytest.fixture
def rt(cfg, svc, monkeypatch):
    from assistant import runtime as rmod

    monkeypatch.setattr(rmod, "register_hotkey", lambda *a, **k: None)
    r = rmod.Runtime(cfg, services=svc)
    yield r
    r._commands.shutdown()


class Watcher:
    """A HUD connected to the event bus."""


def test_with_no_hud_open_the_system_poller_samples_every_30_seconds_not_every_2(rt, monkeypatch):
    taken = []
    monkeypatch.setattr(rt.svc.system, "snapshot",
                        lambda include_processes=True: taken.append(include_processes) or {"cpu": {"percent": 5}})
    for _ in range(30):  # a minute of 2 s ticks with nobody looking
        rt._poll_system()
    assert len(taken) == 2 and "system" in rt.bus.latest  # the first tick, then 30 s later
    rt.bus._clients.add(Watcher())  # the HUD opens: live again from the next tick
    for _ in range(3):
        rt._poll_system()
    assert len(taken) == 5


def test_disks_are_walked_every_30_seconds_and_the_gpu_queried_every_4(monkeypatch):
    mon = system.SystemMonitor()
    t = [1000.0]
    mon.clock = lambda: t[0]
    walks, smi = [], []
    real = system.psutil.disk_partitions
    monkeypatch.setattr(system.psutil, "disk_partitions", lambda all=False: walks.append(1) or real(all=all))
    mon._nvidia_smi = "nvidia-smi"

    class Done:
        stdout = "RTX 5090, 12, 20000, 32607, 55, 300, 0\n"
    monkeypatch.setattr(system.subprocess, "run", lambda *a, **k: smi.append(1) or Done())
    first = mon.snapshot(include_processes=False)
    t[0] += 2
    again = mon.snapshot(include_processes=False)
    assert len(walks) == 1 and again["disks"] == first["disks"] and len(smi) == 1
    t[0] += 3  # 5 s: a fresh GPU reading, the same disks
    assert mon.snapshot(include_processes=False)["gpus"][0]["util"] == 12.0
    assert len(walks) == 1 and len(smi) == 2
    t[0] += 30
    mon.snapshot(include_processes=False)
    assert len(walks) == 2


# ---- caches that grow with use (audit F20) ---------------------------------------------------------------------

def test_the_inbox_thread_cache_keeps_recent_threads_only(svc):
    from assistant.skills.inbox_zero import THREAD_CACHE, InboxZero

    t = [1000.0]
    iz = InboxZero(svc, clock=lambda: t[0])

    class Gmail:
        def thread(self, tid, account=None):
            return {"ok": True, "id": tid, "messages": [{"body": "x" * 1000}]}
    gm = Gmail()
    iz._thread(gm, "old", "me@x.com", "m0")
    t[0] += 7 * 3600  # older than the 6 h it is good for: gone at the next fetch
    for i in range(THREAD_CACHE + 50):
        iz._thread(gm, f"t{i}", "me@x.com", f"m{i}")
        t[0] += 1
    assert len(iz._threads) == THREAD_CACHE and "old" not in iz._threads
    assert f"t{THREAD_CACHE + 49}" in iz._threads and "t0" not in iz._threads  # the newest are kept


def test_twitch_chatter_names_have_a_ceiling_and_keep_the_latest():
    from assistant.integrations.eventsub import MAX_NAMES, TwitchFeed

    feed = TwitchFeed(clock=lambda: 1.0)
    feed.add({"kind": "chat", "user": "Dana_Streams", "text": "hi"})
    for i in range(MAX_NAMES):
        feed.add({"kind": "chat", "user": f"viewer{i}", "text": "hi"})
        if i % 100 == 0:
            feed.add({"kind": "chat", "user": "Dana_Streams", "text": "still here"})  # a regular keeps talking
    assert len(feed.names) <= MAX_NAMES
    last = f"viewer{MAX_NAMES - 1}"
    assert feed.resolve("dana streams") == "dana_streams" and feed.resolve(last) == last
    assert "viewer0" not in feed.names.values()  # an old one-off chatter made room


def test_jobs_waiting_for_an_ok_have_a_ceiling(svc):
    from assistant.skills.tasks_agent import MAX_WAITING, TasksAgent

    t = [1000.0]
    ta = TasksAgent(svc, has_model=lambda: False, autorun=False, clock=lambda: t[0])
    for i in range(MAX_WAITING + 3):
        t[0] += 1
        ta.jobs[f"w{i}"] = {"id": f"w{i}", "title": f"Job {i}", "state": "waiting", "steps": [], "updated": t[0],
                            "finished": t[0], "notes": [], "error": "",
                            "approvals": [{"id": "a", "step": "p", "tool": "discord_announce", "state": "waiting"}]}
    ta._prune()
    waiting = [k for k, j in ta.jobs.items() if j["state"] == "waiting"]
    assert len(waiting) == MAX_WAITING and "w0" not in waiting and f"w{MAX_WAITING + 2}" in waiting
    oldest = ta.jobs["w0"]
    assert oldest["state"] == "cancelled" and oldest["approvals"][0]["state"] == "rejected" and oldest["error"]
