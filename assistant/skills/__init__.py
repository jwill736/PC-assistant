"""Skills: one file per feature, plugged into every part of Vesper at once.

A skill can give the brain tools, catch spoken phrases on the fast path, say a tool's result out loud, keep a panel on
the HUD fresh (its ``status`` is published on the bus under the skill's name), answer HUD buttons (``actions``),
add a line to the morning briefing, run something every ``poll_s`` seconds, and check its own outside connections
(``selftest``, for the health check and the diagnostic report). Its HUD side is ``assistant/web/skills/<name>.js``,
loaded by the page when that file exists.

Turn one off with ``skills: {<name>: {enabled: false}}`` in config.yaml, or Setup → Skills on the HUD (saved to
data/settings.yaml). A skill that fails to start is left out and logged; it never takes Vesper down. A skill that keeps
failing (its tick, route or spoken reply raising ``crash_limit`` times in a row) is switched off for this run, with a
notice saying why and how to turn it back on.
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover
    from ..brain.router import Intent
    from ..brain.tools import Tool
    from ..services import Services

log = logging.getLogger(__name__)

# Load order is the order on the HUD and in the briefing.
MODULES = ["conversation", "nudges", "ambient", "memory", "weather", "automations", "tasks_agent", "inbox_zero", "drafts",
           "meetings", "debrief", "people", "planner", "focusblocks", "focus", "habits", "journal", "bills",
           "travel", "review", "pulse", "screen", "selection", "dictation", "clipboard", "rewind", "janitor", "mcp",
           "phone", "spotify", "home", "youtube", "discord", "stream_copilot", "stream_moments", "stream_raid",
           "stream_privacy", "gtasks", "diagnostics"]

CRASH_LIMIT = 5  # this many failures in a row (of one kind: tick, route, say…) switches a skill off for the run
ON, OFF, CRASHED, FAILED = "on", "off", "crashed", "failed"
GAME_FACTOR = 4  # how much slower a non-essential skill ticks in low-power mode, unless it sets game_poll_s


class Skill:
    name: str = ""
    title: str = ""
    poll_s: float = 0  # call tick() this often (0: never)
    delay_s: float = 15  # wait this long after start before the first tick
    essential: bool = False  # never switched off (Setup → Skills itself)
    # Low-power mode (a fullscreen game, or live on stream): a game-essential skill keeps poll_s; any other ticks every
    # game_poll_s instead (0: not at all until the game ends; None: GAME_FACTOR × poll_s).
    game_essential: bool = False
    game_poll_s: float | None = None

    def low_power_poll(self) -> float | None:
        """The tick interval in low-power mode: None = unchanged (game-essential), 0 = paused."""
        if self.game_essential or self.essential:
            return None
        return self.game_poll_s if self.game_poll_s is not None else self.poll_s * GAME_FACTOR

    def __init__(self, svc: "Services"):
        self.svc = svc
        self.cfg: dict = ((svc.cfg.get("skills") or {}).get(self.name) or {})
        self.last_status: dict | None = None

    # ---- what a skill can offer (all optional) -------------------------------------------------------
    def tools(self) -> list["Tool"]:
        return []

    def route(self, t: str) -> "Intent | None":
        """``t`` is the cleaned utterance (lower case, no filler). Anchor patterns: this runs before the generic ones."""
        return None

    def say(self, tool: str, args: dict, result: dict) -> str | None:
        """What to say for one of this skill's tools on the fast path (None: the generic summary)."""
        return None

    def status(self) -> dict | None:
        """The HUD panel's data."""
        return None

    def actions(self) -> dict[str, Callable[[dict], dict]]:
        """HUD buttons: POST /api/skill/<name>/<action> with a JSON body."""
        return {}

    def briefing(self) -> Any:
        """A small JSON-able bit for the morning briefing (None: nothing today)."""
        return None

    def tick(self) -> None:
        """Every ``poll_s`` seconds: by default refresh the HUD panel."""
        self.publish()

    def start(self, runtime: Any) -> None:
        """Once, when Vesper starts (after the services and the brain exist). Runs in the background: the HUD is
        already answering."""

    def selftest(self) -> list[dict]:
        """Does this skill's outside connection work right now? A list of ``check(name, status, detail, fix)``
        (status pass / warn / fail / skip; skip = not set up). For the health check and the diagnostic report:
        quick (a few seconds at most), changes nothing, never puts a secret or a message's text in ``detail``.
        A skill with nothing outside to check leaves it empty."""
        return []

    # ---- helpers -------------------------------------------------------------------------------------
    def publish(self, data: dict | None = None) -> dict | None:
        data = data if data is not None else self.status()
        if data is not None:
            self.last_status = data
            self.svc.bus.publish(self.name, data, sticky=True)
        return data

    def notify(self, text: str, *, level: str = "info", speak: bool = False, key: str | None = None, **extra) -> None:
        """A card in the HUD's notification feed (and a spoken line, if asked and nobody's live or in a game)."""
        self.svc.bus.publish("notice", {"source": self.name, "text": text, "level": level, "ts": time.time(),
                                        "key": key or f"{self.name}:{text[:40]}", **extra})
        if speak and not (self.svc.live and self.svc.live()):
            self.svc.speak(text)

    def nudge(self, text: str, **kw) -> Any:
        """Speak up at the next good moment (skills/nudges.py); just a HUD card if nudges are off."""
        if self.svc.nudge is not None:
            return self.svc.nudge(text, source=kw.pop("source", self.name), **kw)
        self.notify(text, level=kw.get("priority", "info"))
        return None

    def setting(self, key: str, default: Any = None) -> Any:
        return self.cfg.get(key, default)


class Health:
    """One skill's failures: how many in a row of each kind, how many in all, and the last one."""

    __slots__ = ("in_row", "errors", "last_error", "where", "at")

    def __init__(self):
        self.in_row: dict[str, int] = {}
        self.errors = 0
        self.last_error = ""
        self.where = ""
        self.at = 0.0


class Skills:
    """Every skill that started, by name, plus which skill owns which tool, and which are switched off."""

    crash_limit = CRASH_LIMIT

    def __init__(self, svc: "Services", modules: list[str] | None = None):
        self.svc = svc
        self.modules = list(modules if modules is not None else MODULES)
        self.all: dict[str, Skill] = {}
        self.errors: dict[str, str] = {}
        self.off: dict[str, str] = {}  # name -> OFF (turned off: settings) | CRASHED (switched off after errors)
        self.titles: dict[str, str] = {}
        self.module_of: dict[str, str] = {}  # skill name -> its module (the same, for every shipped skill)
        self.health: dict[str, Health] = {}
        self.timings: dict[str, float] = {}  # ms to construct each skill (startup speed)
        self.started: dict[str, float] = {}  # ms in each skill's start()
        self.listeners: list[Callable[[], None]] = []  # told when a skill is switched on or off
        self.on_change: Callable[[], None] | None = None  # the runtime: ask the skills for their tools again
        self.starter: Callable[[Skill], None] | None = None  # the runtime: start a skill turned on after startup
        self._lock = threading.RLock()
        self._tool_owner: dict[str, Skill] = {}
        conf = svc.cfg.get("skills") or {}
        for mod in self.modules:
            if (conf.get(mod) or {}).get("enabled", True) is False:
                self.off[mod] = OFF
                continue
            self._load(mod)

    def _load(self, mod: str, only: str | None = None) -> Skill | None:
        """Import a skill module and make its skills (``only``: just that one, when one is turned back on)."""
        try:
            module = importlib.import_module(f"{__name__}.{mod}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.endswith(f"skills.{mod}"):
                return None  # not written yet
            self.errors[mod] = f"{type(exc).__name__}: {exc}"
            log.warning("skill %s not loaded: %s", mod, exc)
            return None
        except Exception as exc:  # a syntax or import-time error in one skill never stops the rest
            self.errors[mod] = f"{type(exc).__name__}: {exc}"
            log.exception("skill %s not loaded", mod)
            return None
        made = None
        conf = self.svc.cfg.get("skills") or {}
        for cls in _skill_classes(module):
            if only is not None and cls.name != only or cls.name in self.all:
                continue
            self.module_of[cls.name or mod] = mod
            if cls.name and cls.name != mod and (conf.get(cls.name) or {}).get("enabled", True) is False:
                self.off[cls.name] = OFF
                self.titles.setdefault(cls.name, cls.title or "")
                continue
            t0 = time.perf_counter()
            try:
                skill = cls(self.svc)
            except Exception as exc:  # one broken skill never stops the rest
                self.errors[cls.name or mod] = f"{type(exc).__name__}: {exc}"
                self.titles.setdefault(cls.name or mod, cls.title or "")
                log.exception("skill %s failed to start", cls.name or mod)
                continue
            self.timings[skill.name] = round((time.perf_counter() - t0) * 1000, 1)
            self.errors.pop(skill.name, None)
            self.titles[skill.name] = skill.title
            self.health.setdefault(skill.name, Health())
            self.all = {**self.all, skill.name: skill}  # copy on write: other threads iterate it
            made = made or skill
        return made

    # ---- which skills are on --------------------------------------------------------------------------
    def __iter__(self):
        return iter([s for s in self.all.values() if s.name not in self.off])

    def __getitem__(self, name: str) -> Skill:
        return self.all[name]

    def get(self, name: str) -> Skill | None:
        return self.all.get(name)

    def _known(self, name: str) -> bool:
        return name in self.all or name in self.modules or name in self.module_of or name in self.off

    def active(self, name: str) -> bool:
        return name in self.all and name not in self.off

    def title_of(self, name: str) -> str:
        skill = self.all.get(name)
        if skill is not None:
            return skill.title or name
        if name not in self.titles:
            try:
                module = importlib.import_module(f"{__name__}.{name}")
                self.titles[name] = next((c.title for c in _skill_classes(module) if c.title), "")
            except Exception:
                self.titles[name] = ""
        return self.titles.get(name) or name.replace("_", " ").capitalize()

    # ---- the crash guard ---------------------------------------------------------------------------------
    def _ok(self, skill: Skill, where: str) -> None:
        h = self.health.get(skill.name)
        if h is not None and h.in_row.get(where):
            h.in_row[where] = 0

    def _failed(self, skill: Skill, where: str, exc: BaseException) -> None:
        with self._lock:
            h = self.health.setdefault(skill.name, Health())
            h.in_row[where] = h.in_row.get(where, 0) + 1
            h.errors += 1
            h.last_error = f"{type(exc).__name__}: {exc}"[:300]
            h.where, h.at = where, time.time()
            n = h.in_row[where]
            trip = n >= self.crash_limit and not skill.essential and skill.name not in self.off
        if trip:
            self.switch_off(skill.name, reason=CRASHED)

    def guard(self, skill: Skill, where: str, fn: Callable, *a, reraise: bool = False, default: Any = None) -> Any:
        """Run one of a skill's methods, counting its failures. ``reraise``: the watchdog sees the error too."""
        try:
            out = fn(*a)
        except Exception as exc:
            log.exception("skill %s: %s failed", skill.name, where)
            self._failed(skill, where, exc)
            if reraise:
                raise
            return default
        self._ok(skill, where)
        return out

    def tick(self, skill: Skill) -> None:
        """The watchdog's poller for one skill: nothing while it's switched off."""
        if skill.name in self.off:
            return
        try:
            skill.tick()
        except Exception as exc:
            self._failed(skill, "tick", exc)
            raise  # the watchdog logs it and backs off
        self._ok(skill, "tick")

    def switch_off(self, name: str, *, reason: str = OFF, persist: bool = False) -> dict:
        skill = self.all.get(name)
        if skill is not None and skill.essential:
            return {"ok": False, "error": f"{skill.title or name} can't be turned off."}
        if not self._known(name):
            return {"ok": False, "error": f"No skill called {name}."}
        title = self.title_of(name)
        with self._lock:
            already = name in self.off
            self.off[name] = reason
        if persist:
            self._persist(name, False)
        if skill is not None and not already:
            for hook in ("stop", "close"):  # its own threads (the phone's long poll, MCP servers)
                fn = getattr(skill, hook, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        log.exception("skill %s: %s failed", name, hook)
        if reason == CRASHED and not already:
            h = self.health.get(name) or Health()
            n = max(h.in_row.values() or [self.crash_limit])
            text = (f"{title} was switched off after {n} errors: {h.last_error or 'unknown error'}. "
                    f"It's back after a restart or Setup → Skills → Turn on.")
            log.error("skill %s switched off after %d errors in %s: %s", name, n, h.where, h.last_error)
            self.svc.bus.publish("notice", {"source": name, "text": text, "level": "error", "ts": time.time(),
                                            "key": f"skill-off:{name}"})
        self._changed()
        return {"ok": True, "name": name, "state": self.off[name]}

    def switch_on(self, name: str, *, persist: bool = True) -> dict:
        """Back on. A skill that wasn't loaded (turned off in config, or it failed) is loaded now; the HUD reloads to
        show its panel."""
        if not self._known(name):
            return {"ok": False, "error": f"No skill called {name}."}
        if persist:
            self._persist(name, True)
        reload = False
        skill = self.all.get(name)
        was = self.off.pop(name, None)
        if skill is None:
            mod = self.module_of.get(name, name)
            skill = self._load(mod, only=name if mod != name else None)
            if skill is None:
                self._changed()
                return {"ok": False, "error": f"{self.title_of(name)} didn't start: {self.errors.get(name, 'not found')}"}
            reload = True
        h = self.health.get(name)
        if h is not None:
            h.in_row.clear()
        if (reload or was is not None) and self.starter is not None:
            # a new skill, or one whose threads were stopped: start it (again), with its poller
            try:
                self.starter(skill)
            except Exception as exc:
                log.exception("skill %s: start failed", name)
                self._failed(skill, "start", exc)
        self._changed()
        return {"ok": True, "name": name, "state": ON, "reload": reload}

    def set_enabled(self, name: str, on: bool) -> dict:
        """Setup → Skills: the toggle (saved to data/settings.yaml skills.<name>.enabled)."""
        return self.switch_on(name) if on else self.switch_off(name, reason=OFF, persist=True)

    def _persist(self, name: str, on: bool) -> None:
        from ..config import save_setting

        try:
            save_setting(self.svc.cfg, f"skills.{name}.enabled", on)
        except Exception:  # a read-only data folder: kept for this run
            log.warning("couldn't save skills.%s.enabled", name)

    def _changed(self) -> None:
        self._tool_owner = {}
        for fn in [self.on_change, *self.listeners]:
            if fn is None:
                continue
            try:
                fn()
            except Exception:
                log.exception("skills: change listener failed")

    def overview(self) -> list[dict]:
        """Every skill, on or not, for Setup → Skills and the diagnostic report."""
        names = list(dict.fromkeys([*self.modules, *self.all, *self.off]))
        out = []
        for name in names:
            skill = self.all.get(name)
            if skill is None and name not in self.off and name not in self.errors:
                continue  # not written yet
            h = self.health.get(name) or Health()
            state = self.off.get(name) or (ON if skill is not None else FAILED)
            out.append({"name": name, "title": self.title_of(name), "state": state,
                        "essential": bool(skill is not None and skill.essential),
                        "last_error": h.last_error or self.errors.get(name, ""), "where": h.where if h.last_error else
                        ("load" if name in self.errors else ""), "at": h.at or None, "errors": h.errors,
                        "load_ms": self.timings.get(name), "start_ms": self.started.get(name)})
        return out

    # ---- every skill at once ---------------------------------------------------------------------------
    def tools(self) -> list["Tool"]:
        out = []
        owners = {}
        for skill in self:
            tools = self.guard(skill, "tools", skill.tools, default=[]) or []
            for tool in tools:
                owners[tool.name] = skill
                out.append(tool)
        self._tool_owner = owners
        return out

    def route(self, t: str) -> "Intent | None":
        for skill in self:
            try:
                intent = skill.route(t)
            except Exception as exc:
                log.exception("skill %s: route failed", skill.name)
                self._failed(skill, "route", exc)
                continue
            self._ok(skill, "route")
            if intent is not None:
                return intent
        return None

    def say(self, tool: str, args: dict, result: dict) -> str | None:
        if not self._tool_owner:
            self.tools()
        skill = self._tool_owner.get(tool)
        if skill is None or skill.name in self.off:
            return None
        return self.guard(skill, "say", skill.say, tool, args, result)

    def briefing(self) -> dict:
        out = {}
        for skill in self:
            try:
                b = skill.briefing()
            except Exception as exc:
                log.warning("skill %s: briefing failed: %s", skill.name, exc)
                self._failed(skill, "briefing", exc)
                continue
            if b:
                out[skill.name] = b
        return out

    def statuses(self) -> dict:
        return {s.name: s.last_status for s in self if s.last_status is not None}

    def action(self, name: str, action: str, body: dict) -> dict:
        skill = self.get(name)
        if skill is None:
            return {"ok": False, "error": f"No skill called {name}."}
        if name in self.off:
            return {"ok": False, "error": f"{skill.title or name} is switched off: Setup → Skills → Turn on."}
        fn = skill.actions().get(action)
        if fn is None:
            return {"ok": False, "error": f"{skill.title or name} has no action {action}."}
        try:
            out = fn(body or {})
        except ValueError as exc:  # e.g. a pasted key with a line break in it (firstrun.set_env_value refuses it)
            return {"ok": False, "error": str(exc)}
        except TypeError:  # a body missing what the button sends (no stack trace for a bad request)
            return {"ok": False, "error": f"{skill.title or name}: {action} needs more than that."}
        except Exception as exc:
            log.exception("skill %s: action %s failed", name, action)
            self._failed(skill, "action", exc)
            return {"ok": False, "error": f"{skill.title or name} failed: {type(exc).__name__}: {exc}"[:300]}
        self._ok(skill, "action")
        if isinstance(out, dict) and out.get("ok", True):
            self.guard(skill, "status", skill.publish)
        return out

    def selftests(self, timeout: float = 8.0, names: list[str] | None = None) -> dict[str, list[dict]]:
        """Every on skill's self-test, side by side, each given ``timeout`` seconds: {name: [check, ...]}."""
        from ..doctor import TIMEOUT, run_parallel

        skills = [s for s in self if names is None or s.name in names]
        results = run_parallel([(s.name, s.selftest) for s in skills], timeout)
        out: dict[str, list[dict]] = {}
        for s in skills:
            kind, value = results[s.name]
            if kind == TIMEOUT:
                out[s.name] = [check(s.title or s.name, "warn", f"didn't answer within {timeout:.0f}s",
                                     "Something it talks to is slow or stuck; run the check again in a minute.")]
            elif isinstance(value, BaseException):
                out[s.name] = [check(s.title or s.name, "fail", f"the self-test crashed: {type(value).__name__}: {value}"[:300])]
            else:
                out[s.name] = [c if isinstance(c, dict) else dict(vars(c)) for c in (value or [])]
        return out


def _skill_classes(module) -> list[type[Skill]]:
    return [v for v in vars(module).values()
            if isinstance(v, type) and issubclass(v, Skill) and v is not Skill and v.__module__ == module.__name__]


# ---- small shared helpers for skills ----------------------------------------------------------------------

def words(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", t.lower()))


def check(name: str, status: str, detail: str = "", fix: str = "") -> dict:
    """One self-test result: status pass, warn, fail or skip (not set up)."""
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def gmail_checks(svc: Any, per_account: bool = True) -> list[dict]:
    """The self-test of a skill that reads Gmail: may each signed-in Google account read mail?"""
    gm, google = getattr(svc, "gmail", None), getattr(svc, "google", None)
    if gm is None or google is None or not getattr(google, "configured", False):
        return [check("Gmail", "skip", "Google isn't set up", "Optional: Setup → Google accounts.")]
    accounts = google.accounts()
    if not accounts:
        return [check("Gmail", "skip", "no Google account connected", "Setup → Google accounts → Connect.")]
    out = []
    for a in accounts:
        email = a.get("email", "")
        if a.get("error"):
            out.append(check(f"Gmail · {email}", "fail", a["error"], "Setup → Google accounts → Reconnect."))
        elif "Gmail" in (a.get("missing") or []):
            out.append(check(f"Gmail · {email}", "warn", "connected, but Gmail wasn't allowed",
                             "Setup → Google accounts → Reconnect, and tick the Gmail box on Google's page."))
        else:
            out.append(check(f"Gmail · {email}", "pass", "can read mail"))
    if per_account:
        return out
    good = sum(c["status"] == "pass" for c in out)
    worst = "pass" if good == len(out) else "warn" if good else "fail"
    return [check("Gmail", worst, f"{good} of {len(out)} account{'s' if len(out) != 1 else ''} can read mail",
                  "" if worst == "pass" else "Setup → Google accounts → Reconnect, and tick the Gmail box.")]


class LazyClient:
    """An ``httpx.Client`` made on its first use. Making one loads the TLS certificates (about 40 ms), and two dozen
    skills doing that while Vesper starts kept the HUD waiting. ``lazy_client(timeout=10)`` where a skill would say
    ``httpx.Client(timeout=10)``."""

    def __init__(self, **kw):
        self._kw = kw
        self._client = None
        self._made = threading.Lock()

    def _get(self):
        if self._client is None:
            with self._made:
                if self._client is None:
                    import httpx

                    self._client = httpx.Client(**self._kw)
        return self._client

    def __getattr__(self, name: str):
        if name in ("_kw", "_client", "_made"):  # half-made (copy, pickle): never recurse
            raise AttributeError(name)
        return getattr(self._get(), name)


def lazy_client(**kw) -> LazyClient:
    return LazyClient(**kw)
