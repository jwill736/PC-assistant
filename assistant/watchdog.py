"""Watchdog: every background part reports in, and anything that dies or hangs
gets restarted — with backoff, a reason in the log, and a row on the HUD.

Two kinds of parts:

* **Pollers** (system stats, OBS, calendars, news…): a function run on an
  interval. The supervisor owns the loop, so it knows when a run fails or hangs.
  A hung run can't be killed (Python threads can't be), so a fresh loop is
  started and the stuck one retires itself when it finally returns.
* **Services** (voice listener, activity tracker, speech output): long-lived
  threads with their own loop. They call ``beat(name)`` while healthy; the
  watchdog restarts them if their thread dies or they stop beating.

**Low-power mode.** While a fullscreen game runs or you're live (``low_power()`` names the reason), every poller
registered with a ``game_interval`` backs off to it (0: paused until the game ends); the rest (``game_interval``
None: the essential ones, like nudges, timers, OBS and the GPU watch) keep their pace. Each run's time is kept on
its Part (``last_ms``, ``avg_ms``, ``max_ms``), so the HUD's Running parts table shows what every part costs.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

OK, ERROR, STALLED, RESTARTING, STARTING, STOPPED, DISABLED = (
    "ok", "error", "stalled", "restarting", "starting", "stopped", "disabled")
MAX_BACKOFF_S = 300


@dataclass
class Part:
    name: str
    kind: str  # poller | service
    interval: float
    stall_after: float
    state: str = STARTING
    started: float = field(default_factory=time.time)
    last_beat: float = 0.0
    last_ok: float = 0.0
    runs: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    restarts: int = 0
    last_error: str = ""
    next_restart: float = 0.0
    generation: int = 0
    thread: threading.Thread | None = None
    # pollers
    fn: Callable[[], None] | None = None
    delay: float = 0.0
    game_interval: float | None = None  # in low-power mode: wait this long instead (0: paused); None: essential
    throttle: str = ""  # "" | "slowed" | "paused": what low-power mode is doing to it right now
    last_ms: float = 0.0  # how long the last run took
    avg_ms: float = 0.0  # moving average of a run
    max_ms: float = 0.0
    # services
    start_fn: Callable[[], threading.Thread | None] | None = None
    is_disabled: Callable[[], bool] | None = None

    def view(self) -> dict:
        return {
            "name": self.name, "kind": self.kind, "state": self.state, "interval": self.interval,
            "last_beat": self.last_beat or None, "last_ok": self.last_ok or None, "runs": self.runs,
            "errors": self.errors, "restarts": self.restarts, "last_error": self.last_error,
            "essential": self.kind == "service" or self.game_interval is None, "game_interval": self.game_interval,
            "throttle": self.throttle, "last_ms": round(self.last_ms, 1), "avg_ms": round(self.avg_ms, 1),
            "max_ms": round(self.max_ms, 1),
        }

    def timed(self, ms: float) -> None:
        self.last_ms, self.max_ms = ms, max(self.max_ms, ms)
        self.avg_ms = ms if self.runs == 0 else self.avg_ms * 0.8 + ms * 0.2


LOW_POWER_REASONS = {"game": "in a game", "live": "live on stream"}


class Supervisor:
    def __init__(self, bus=None, check_every: float = 5.0, clock: Callable[[], float] = time.time,
                 low_power: Callable[[], str] | None = None, power_check_s: float = 5.0, nap_s: float = 15.0):
        self.bus = bus
        self.check_every = check_every
        self.now = clock
        self.parts: dict[str, Part] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._last_published: list | None = None
        # low-power mode: "" or why ("game", "live"); asked at most every power_check_s, slowed pollers wake every nap_s
        self.low_power = low_power or (lambda: "")
        self.power_check_s, self.nap_s = power_check_s, nap_s
        self._power: tuple[float, str] | None = None

    # ---- registration ---------------------------------------------------
    def poller(self, name: str, interval: float, fn: Callable[[], None], *, delay: float = 0.0,
               stall_after: float | None = None, game_interval: float | None = None) -> Part:
        """``game_interval``: how often to run in low-power mode (0: not at all); None keeps ``interval`` (essential)."""
        part = Part(name, "poller", interval, stall_after or max(interval * 3, 60) + 60, fn=fn, delay=delay,
                    game_interval=game_interval)
        with self._lock:
            self.parts[name] = part
        self._start_poller(part)
        return part

    def service(self, name: str, start_fn: Callable[[], threading.Thread | None], *, heartbeat_s: float | None = None,
                is_disabled: Callable[[], bool] | None = None) -> Part:
        """``heartbeat_s=None`` means the part doesn't beat; only a dead thread counts as failure."""
        part = Part(name, "service", heartbeat_s or 0, (heartbeat_s * 6) if heartbeat_s else 0,
                    start_fn=start_fn, is_disabled=is_disabled)
        with self._lock:
            self.parts[name] = part
        self._start_service(part)
        return part

    def beat(self, name: str) -> None:
        part = self.parts.get(name)
        if part is None:
            return
        now = self.now()
        part.last_beat = part.last_ok = now
        if part.state in (STARTING, STALLED, RESTARTING, ERROR):
            part.state = OK
            part.consecutive_errors = 0

    def report_error(self, name: str, exc: BaseException | str) -> None:
        part = self.parts.get(name)
        if part is not None:
            self._record_error(part, exc)

    # ---- pollers ----------------------------------------------------------
    def _start_poller(self, part: Part) -> None:
        part.generation += 1
        gen = part.generation
        t = threading.Thread(target=self._poll_loop, args=(part, gen), name=f"poll-{part.name}", daemon=True)
        part.thread = t
        part.last_beat = self.now()  # the stall clock starts now
        t.start()

    def _poll_loop(self, part: Part, gen: int) -> None:
        if part.delay and self._stop.wait(part.delay):
            return
        while not self._stop.is_set() and part.generation == gen:
            if self.wait_for(part) is None:  # paused while you play: check again soon, run nothing
                part.last_beat = self.now()
                if self._stop.wait(min(self.power_check_s, self.nap_s)):
                    return
                continue
            part.last_beat = self.now()
            t0 = time.perf_counter()
            try:
                part.fn()
            except Exception as exc:
                if part.generation != gen:
                    return  # a replacement took over while we were stuck
                part.timed((time.perf_counter() - t0) * 1000)
                self._record_error(part, exc)
                wait = min(part.interval * 2 ** min(part.consecutive_errors, 8), max(part.interval, MAX_BACKOFF_S))
                if self._stop.wait(wait):
                    return
                continue
            if part.generation != gen:
                return
            part.timed((time.perf_counter() - t0) * 1000)
            part.runs += 1
            part.last_ok = part.last_beat = self.now()
            part.consecutive_errors = 0
            if part.state != OK:
                part.state = OK
            if self._nap(part, gen):
                return

    # ---- low-power mode ---------------------------------------------------
    def power_reason(self) -> str:
        """"" or why Vesper is holding back ("game", "live"); asked at most every ``power_check_s``."""
        now = self.now()
        if self._power is None or now - self._power[0] >= self.power_check_s or now < self._power[0]:
            try:
                reason = str(self.low_power() or "")
            except Exception:
                reason = ""
            self._power = (now, reason)
        return self._power[1]

    def wait_for(self, part: Part) -> float | None:
        """How long this poller waits between runs right now; None while it's paused."""
        if part.game_interval is None or not self.power_reason():
            part.throttle = ""
            return part.interval
        part.throttle = "paused" if not part.game_interval else "slowed" if part.game_interval > part.interval else ""
        return part.game_interval or None

    def _nap(self, part: Part, gen: int) -> bool:
        """Wait out the interval (True: stopping). A part low-power mode can slow wakes every ``nap_s`` to see
        whether the game started or ended, so it settles into the new pace without waiting out the old one."""
        if part.game_interval is None:
            return self._stop.wait(part.interval)
        began = time.monotonic()
        while part.generation == gen:
            target = self.wait_for(part)
            left = (target or 0) - (time.monotonic() - began)
            if target is None or left <= 0:
                return False
            if self._stop.wait(min(left, self.nap_s)):
                return True
            part.last_beat = self.now()  # asleep on purpose, not stuck
        return False

    def power(self) -> dict:
        reason = self.power_reason()
        with self._lock:
            parts = [p for p in self.parts.values() if p.kind == "poller"]
        return {"low_power": bool(reason), "reason": LOW_POWER_REASONS.get(reason, reason),
                "paused": sorted(p.name for p in parts if reason and p.game_interval == 0),
                "slowed": sorted(p.name for p in parts if reason and p.game_interval and p.game_interval > p.interval)}

    # ---- services -------------------------------------------------------
    def _start_service(self, part: Part) -> None:
        if part.is_disabled and part.is_disabled():
            part.state = DISABLED
            return
        try:
            part.thread = part.start_fn()
        except Exception as exc:
            self._record_error(part, exc)
            return
        part.last_beat = self.now()
        part.state = OK if not part.stall_after else STARTING

    # ---- bookkeeping ----------------------------------------------------
    def _record_error(self, part: Part, exc: BaseException | str) -> None:
        part.errors += 1
        part.consecutive_errors += 1
        part.state = ERROR
        if isinstance(exc, BaseException):
            part.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.error("%s failed (%d in a row): %s\n%s", part.name, part.consecutive_errors, part.last_error,
                      "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1500:])
        else:
            part.last_error = str(exc)[:300]
            log.error("%s: %s", part.name, part.last_error)

    def _backoff(self, part: Part) -> float:
        return min(5 * 2 ** min(part.restarts, 6), MAX_BACKOFF_S)

    def check(self) -> None:
        """One watchdog pass; public so tests can drive it deterministically."""
        now = self.now()
        with self._lock:
            parts = list(self.parts.values())
        for part in parts:
            if part.state in (STOPPED,):
                continue
            if part.kind == "service" and part.is_disabled and part.is_disabled():
                part.state = DISABLED
                continue
            alive = part.thread is not None and part.thread.is_alive()
            if part.kind == "poller":
                stalled = now - part.last_beat > part.stall_after
                if (stalled or not alive) and now >= part.next_restart:
                    part.state = STALLED if alive else RESTARTING
                    part.last_error = (f"no progress for {int(now - part.last_beat)}s — started a fresh loop"
                                       if alive else "loop thread died — restarted")
                    log.warning("watchdog: %s %s", part.name, part.last_error)
                    part.restarts += 1
                    part.next_restart = now + self._backoff(part)
                    self._start_poller(part)
                continue
            if part.state == DISABLED:
                if not (part.is_disabled and part.is_disabled()):
                    self._start_service(part)  # e.g. voice re-enabled
                continue
            stalled = bool(part.stall_after) and now - part.last_beat > part.stall_after
            if (not alive or stalled) and now >= part.next_restart:
                reason = "thread died" if not alive else f"no heartbeat for {int(now - part.last_beat)}s"
                log.warning("watchdog: restarting %s (%s)", part.name, reason)
                part.state = RESTARTING
                part.last_error = reason
                part.restarts += 1
                part.next_restart = now + self._backoff(part)
                self._start_service(part)
        self._publish()

    def _publish(self, force: bool = False) -> None:
        if self.bus is None:
            return
        snap = self.snapshot()
        compact = [(p["name"], p["state"], p["restarts"], p["errors"], p["throttle"], p["low_power"]) for p in snap]
        if force or compact != self._last_published or int(self.now()) % 30 < self.check_every:
            self._last_published = compact
            self.bus.publish("health", snap, sticky=True)

    def snapshot(self) -> list[dict]:
        """Every part's row; each also carries why low-power mode is on ("" when it isn't), for the HUD's header."""
        reason = self.power_reason()
        reason = LOW_POWER_REASONS.get(reason, reason)
        with self._lock:
            parts = list(self.parts.values())
        for p in parts:
            if p.kind == "poller":
                self.wait_for(p)  # throttle as of now, even for a part that's asleep
        return [{**p.view(), "low_power": reason} for p in parts]

    def healthy(self) -> bool:
        return all(p["state"] in (OK, STARTING, DISABLED) for p in self.snapshot())

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if self._watch_thread is None:
            self._watch_thread = threading.Thread(target=self._watch, name="watchdog", daemon=True)
            self._watch_thread.start()

    def _watch(self) -> None:
        while not self._stop.wait(self.check_every):
            try:
                self.check()
            except Exception:
                log.exception("watchdog pass failed")

    def stop(self) -> None:
        self._stop.set()
        for part in self.parts.values():
            part.state = STOPPED


# ---------------------------------------------------------------------------
# Logging: a rotating file, plus crash capture for every thread.
# ---------------------------------------------------------------------------

def setup_logging(data_dir: Path, debug: bool = False, console: bool = True) -> Path:
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "assistant.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console and sys.stderr is not None:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        root.addHandler(ch)
    for noisy in ("httpx", "httpcore", "faster_whisper", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        logging.getLogger("crash").error("uncaught exception in thread %s", args.thread.name if args.thread else "?",
                                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_hook
    return path
