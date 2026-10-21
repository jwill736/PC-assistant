"""Low-power mode: while a fullscreen game runs (or J is live), the non-essential pollers back off or pause and the
essential ones keep their pace; every run is timed on its watchdog Part; the HUD's Running parts table says so."""

import threading
import time

import pytest

from assistant.bus import EventBus
from assistant.skills import GAME_FACTOR, MODULES
from assistant.watchdog import Supervisor


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class Screen:
    """A fake Windows "what's in front" signal and clock."""

    def __init__(self):
        self.state, self.t, self.asked = "", 1000.0, 0

    def reason(self):
        self.asked += 1
        return "game" if self.state == "game" else ""

    def clock(self):
        return self.t


def test_the_pace_of_each_kind_of_part_with_a_fake_clock_and_fullscreen():
    scr = Screen()
    sup = Supervisor(clock=scr.clock, low_power=scr.reason, power_check_s=5)
    sup.poller = lambda *a, **kw: None  # registration only: no threads in this test
    from assistant.watchdog import Part

    essential = Part("obs", "poller", 3, 60)
    slowed = Part("weather", "poller", 900, 60, game_interval=3600)
    paused = Part("clipboard", "poller", 1.5, 60, game_interval=0)
    assert [sup.wait_for(p) for p in (essential, slowed, paused)] == [3, 900, 1.5]
    assert (slowed.throttle, paused.throttle) == ("", "")
    scr.state = "game"
    assert sup.wait_for(slowed) == 900  # the answer is kept for power_check_s: no Windows call on every loop
    scr.t += 5
    assert [sup.wait_for(p) for p in (essential, slowed, paused)] == [3, 3600, None]
    assert (essential.throttle, slowed.throttle, paused.throttle) == ("", "slowed", "paused")
    asked = scr.asked
    for _ in range(50):
        sup.wait_for(slowed)
    assert scr.asked == asked  # cached
    scr.state, scr.t = "", scr.t + 5
    assert [sup.wait_for(p) for p in (essential, slowed, paused)] == [3, 900, 1.5]
    assert slowed.throttle == paused.throttle == ""


def test_a_broken_signal_never_stops_vesper():
    def boom():
        raise OSError("no desktop")

    sup = Supervisor(low_power=boom)
    assert sup.power_reason() == "" and sup.power()["low_power"] is False


def test_in_a_game_non_essential_pollers_pause_or_slow_and_essential_ones_keep_going():
    scr = Screen()
    sup = Supervisor(EventBus(), low_power=scr.reason, power_check_s=0.01, nap_s=0.01)
    runs = {"nudges": [], "clipboard": [], "weather": []}
    sup.poller("nudges", 0.02, lambda: runs["nudges"].append(time.monotonic()))
    sup.poller("clipboard", 0.02, lambda: runs["clipboard"].append(time.monotonic()), game_interval=0)
    sup.poller("weather", 0.02, lambda: runs["weather"].append(time.monotonic()), game_interval=0.3)
    assert wait_for(lambda: all(len(r) >= 3 for r in runs.values()))
    scr.state = "game"
    time.sleep(0.1)  # let every loop see it
    before = {k: len(v) for k, v in runs.items()}
    time.sleep(0.6)
    during = {k: len(v) - before[k] for k, v in runs.items()}
    assert during["nudges"] >= 10  # essential: full pace
    assert during["clipboard"] == 0  # paused
    assert 1 <= during["weather"] <= 3  # every 0.3 s instead of every 0.02 s
    view = {p["name"]: p for p in sup.snapshot()}
    assert view["clipboard"]["throttle"] == "paused" and view["weather"]["throttle"] == "slowed"
    assert view["nudges"]["throttle"] == "" and view["nudges"]["essential"] is True
    assert {p["low_power"] for p in view.values()} == {"in a game"}
    assert view["clipboard"]["state"] == "ok" and sup.healthy()  # paused is not broken
    sup.check()  # and the watchdog doesn't restart a paused part as "stalled"
    assert sup.parts["clipboard"].restarts == 0
    scr.state = ""  # the game ends: everything is back at its normal pace soon (not after a full slow interval)
    n = len(runs["clipboard"])
    assert wait_for(lambda: len(runs["clipboard"]) >= n + 3, timeout=1.0)
    assert wait_for(lambda: sup.snapshot()[1]["throttle"] == "" and sup.snapshot()[1]["low_power"] == "")
    sup.stop()


def test_live_on_stream_is_low_power_too_and_the_hud_row_says_why():
    bus = EventBus()
    sup = Supervisor(bus, low_power=lambda: "live", power_check_s=0)
    sup.poller("news", 60, lambda: None, delay=60, game_interval=3600)
    sup.check()
    row = bus.latest["health"]["data"][0]
    assert (row["low_power"], row["throttle"], row["game_interval"], row["essential"]) == (
        "live on stream", "slowed", 3600, False)
    assert sup.power() == {"low_power": True, "reason": "live on stream", "paused": [], "slowed": ["news"]}
    sup.stop()


def test_every_run_is_timed():
    sup = Supervisor(EventBus())
    sup.poller("slow", 0.01, lambda: time.sleep(0.03))
    assert wait_for(lambda: sup.parts["slow"].runs >= 3)
    row = sup.snapshot()[0]
    assert row["last_ms"] >= 25 and row["avg_ms"] >= 25 and row["max_ms"] >= row["last_ms"] - 0.1
    sup.stop()


def test_a_failing_run_is_timed_and_still_backs_off():
    sup = Supervisor()
    sup.poller("bad", 0.01, lambda: 1 / 0)
    assert wait_for(lambda: sup.parts["bad"].errors >= 2)
    assert sup.parts["bad"].last_ms >= 0 and sup.parts["bad"].runs == 0
    sup.stop()


# ---- the skills' declarations ----------------------------------------------------------------------------------

ESSENTIAL = {"nudges", "memory", "automations", "focus", "janitor", "screen", "spotify", "home", "discord",
             "stream_copilot"}


def test_every_polling_skill_declares_its_low_power_pace(svc):
    seen = set()
    for skill in svc.skills:
        if not skill.poll_s:
            continue
        seen.add(skill.name)
        declared = "game_essential" in vars(type(skill)) or "game_poll_s" in vars(type(skill))
        assert declared, f"{skill.name} must say game_essential = True or set game_poll_s"
        pace = skill.low_power_poll()
        if skill.name in ESSENTIAL:
            assert skill.game_essential and pace is None, skill.name
        else:
            assert pace == 0 or pace > skill.poll_s, (skill.name, pace)  # slower, or paused
    assert seen >= ESSENTIAL and seen <= set(MODULES)
    sk = svc.skills
    assert sk["clipboard"].low_power_poll() == 0  # 1.5 s clipboard checks: paused
    for name in ("rewind", "pulse", "people", "bills", "travel", "youtube", "weather"):
        assert sk[name].low_power_poll() >= GAME_FACTOR * sk[name].poll_s or sk[name].low_power_poll() >= 3600, name


def test_a_new_skill_is_non_essential_by_default(svc):
    from assistant.skills import Skill

    class Thing(Skill):
        name, poll_s = "thing", 30

    assert Thing(svc).low_power_poll() == 30 * GAME_FACTOR


# ---- wiring in the runtime ---------------------------------------------------------------------------------------

@pytest.fixture
def rt(cfg, svc, monkeypatch):
    from assistant import runtime as rmod

    monkeypatch.setattr(rmod, "register_hotkey", lambda *a, **k: None)
    r = rmod.Runtime(cfg, services=svc)
    r.registered = {}
    r.supervisor.poller = lambda name, interval, fn, **kw: r.registered.__setitem__(name, (interval, kw.get("game_interval")))
    r.supervisor.service = lambda *a, **k: None
    r.supervisor.start = lambda: None
    for skill in svc.skills:
        skill.start = lambda runtime: None  # no phone/MCP threads in a test
    monkeypatch.setattr(threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())
    return r


def test_the_runtime_registers_every_poller_with_its_game_pace(rt):
    rt.start()
    rt._start_skills()  # on its own thread in the app
    reg, sk = rt.registered, rt.svc.skills

    def skill(name):
        return f"skill · {sk[name].title}"

    for name in ("obs", "twitch", "pre-stream check", "morning briefing", skill("nudges"), skill("automations"),
                 skill("stream_copilot"), skill("janitor")):
        assert reg[name][1] is None, name  # essential
    for name in ("model test", "pc scan", skill("clipboard")):
        assert reg[name][1] == 0, name  # paused in a game
    for name in ("system stats", "news", "gmail", "calendars", "projects", skill("weather"), skill("rewind")):
        assert reg[name][1] > reg[name][0], name  # slower


def test_the_runtime_knows_when_to_hold_back(rt, svc):
    nud = svc.skills["nudges"]
    live = [False]
    svc.live = lambda: live[0]
    assert rt._low_power() == ""
    nud.fullscreen = lambda: "game"
    assert rt._low_power() == "game"
    nud.fullscreen = lambda: "presentation"  # a slideshow isn't a game: Vesper keeps its pace
    assert rt._low_power() == ""
    live[0] = True
    assert rt._low_power() == "live"
    rt.cfg["low_power"] = {"enabled": False}
    assert rt._low_power() == ""


def test_the_hud_shows_low_power_in_running_parts():
    from pathlib import Path

    js = (Path(__file__).parent.parent / "assistant" / "web" / "app.js").read_text(encoding="utf-8")
    assert "low-power: ${low}" in js and "PART_THROTTLE" in js and "avg_ms" in js
