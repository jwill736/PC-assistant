import threading
import time

from assistant.bus import EventBus
from assistant.integrations.activity import ActivityTracker
from assistant.storage import Storage
from assistant.watchdog import Supervisor, setup_logging


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_poller_runs_and_reports_ok():
    sup = Supervisor(EventBus())
    calls = []
    sup.poller("p", 0.05, lambda: calls.append(1))
    assert wait_for(lambda: len(calls) >= 3)
    view = sup.snapshot()[0]
    assert view["state"] == "ok" and view["runs"] >= 3 and view["errors"] == 0
    sup.stop()


def test_failing_poller_backs_off_and_recovers():
    sup = Supervisor()
    attempts = []

    def flaky():
        attempts.append(time.time())
        if len(attempts) < 3:
            raise RuntimeError("feed down")

    sup.poller("news", 0.05, flaky)
    assert wait_for(lambda: len(attempts) >= 3)
    part = sup.parts["news"]
    assert part.errors == 2 and "feed down" in part.last_error
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    assert gaps[1] > gaps[0]  # exponential backoff between failures
    assert wait_for(lambda: part.state == "ok")
    sup.stop()


def test_hung_poller_is_replaced_and_old_loop_retires():
    clock = [1000.0]
    sup = Supervisor(clock=lambda: clock[0])
    release = threading.Event()
    runs = []

    def job():
        runs.append(threading.current_thread())
        if len(runs) == 1:
            release.wait(5)  # first run hangs

    sup.poller("calendars", 0.01, job, stall_after=30)
    assert wait_for(lambda: len(runs) == 1)
    first = runs[0]
    clock[0] += 31
    sup.check()
    part = sup.parts["calendars"]
    assert part.restarts == 1 and "no progress" in part.last_error
    assert wait_for(lambda: len(runs) >= 2)  # the fresh loop is running
    assert wait_for(lambda: part.state == "ok")  # ...and healthy again
    release.set()
    first.join(2)
    assert not first.is_alive()  # the stuck loop exited instead of running twice
    sup.stop()


def test_dead_service_is_restarted_with_backoff():
    clock = [0.0]
    sup = Supervisor(clock=lambda: clock[0])
    starts = []

    def start():
        t = threading.Thread(target=lambda: None)  # dies immediately
        t.start()
        t.join()
        starts.append(t)
        return t

    sup.service("tts", start)
    assert len(starts) == 1
    clock[0] = 1
    sup.check()
    assert len(starts) == 2 and sup.parts["tts"].restarts == 1
    clock[0] = 2
    sup.check()
    assert len(starts) == 2  # inside the backoff window
    clock[0] = 20
    sup.check()
    assert len(starts) == 3


def test_silent_service_counts_as_stalled():
    clock = [0.0]
    sup = Supervisor(clock=lambda: clock[0])
    stop = threading.Event()
    threads = []

    def start():
        t = threading.Thread(target=stop.wait, daemon=True)
        t.start()
        threads.append(t)
        return t

    sup.service("voice", start, heartbeat_s=10)
    clock[0] = 30
    sup.beat("voice")
    clock[0] = 59
    sup.check()
    assert len(threads) == 1 and sup.parts["voice"].state == "ok"
    clock[0] = 100  # 70 s without a beat > 6 x 10 s
    sup.check()
    assert len(threads) == 2 and "no heartbeat" in sup.parts["voice"].last_error
    stop.set()


def test_disabled_service_is_not_restarted():
    sup = Supervisor()
    starts = []
    sup.service("voice", lambda: starts.append(1), is_disabled=lambda: True)
    sup.check()
    assert starts == [] and sup.parts["voice"].state == "disabled"


def test_health_is_published_to_bus():
    bus = EventBus()
    sup = Supervisor(bus)
    sup.poller("x", 10, lambda: None)
    sup.check()
    assert bus.latest["health"]["data"][0]["name"] == "x"
    sup.stop()


def test_activity_tracker_restart_and_pause(monkeypatch):
    from assistant.integrations import desktop

    monkeypatch.setattr(desktop, "idle_seconds", lambda: 0.0)
    monkeypatch.setattr(desktop, "active_window", lambda: desktop.WindowInfo("main.py", "code", 1))
    tr = ActivityTracker(Storage(":memory:"), {}, sample_seconds=0.02)
    beats = []
    tr.heartbeat = lambda: beats.append(1)
    first = tr.restart()
    assert wait_for(lambda: len(beats) >= 2)
    second = tr.restart()
    assert wait_for(lambda: not first.is_alive())  # old generation retired
    tr.paused = True
    assert wait_for(lambda: tr.current is None)
    tr.stop()
    second.join(1)


def test_setup_logging_writes_file(tmp_path):
    import logging

    path = setup_logging(tmp_path, console=False)
    logging.getLogger("assistant.test").info("hello log")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "hello log" in path.read_text(encoding="utf-8")
    t = threading.Thread(target=lambda: 1 / 0, name="boom")
    t.start()
    t.join()
    for h in logging.getLogger().handlers:
        h.flush()
    assert "uncaught exception in thread boom" in path.read_text(encoding="utf-8")
    logging.getLogger().handlers.clear()
    threading.excepthook = threading.__excepthook__
