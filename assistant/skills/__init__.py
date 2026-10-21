"""Skills: one file per feature, plugged into every part of Vesper at once.

A skill can give the brain tools, catch spoken phrases on the fast path, say a tool's result out loud, keep a panel on
the HUD fresh (its ``status`` is published on the bus under the skill's name), answer HUD buttons (``actions``),
add a line to the morning briefing, and run something every ``poll_s`` seconds. Its HUD side is
``assistant/web/skills/<name>.js``, loaded by the page when that file exists.

Turn one off with ``skills: {<name>: {enabled: false}}`` in config.yaml. A skill that fails to start is left out and
logged; it never takes Vesper down.
"""

from __future__ import annotations

import importlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover
    from ..brain.router import Intent
    from ..brain.tools import Tool
    from ..services import Services

log = logging.getLogger(__name__)

# Load order is the order on the HUD and in the briefing.
MODULES = ["nudges", "memory", "weather", "automations", "inbox_zero", "meetings", "people", "planner", "focus", "habits",
           "journal", "bills", "travel", "pulse", "screen", "selection", "dictation", "clipboard", "rewind", "janitor", "mcp",
           "phone", "spotify", "home", "youtube", "discord", "stream_copilot"]


class Skill:
    name: str = ""
    title: str = ""
    poll_s: float = 0  # call tick() this often (0: never)
    delay_s: float = 15  # wait this long after start before the first tick

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
        """Once, when Vesper starts (after the services and the brain exist)."""

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


class Skills:
    """Every skill that started, by name, plus which skill owns which tool."""

    def __init__(self, svc: "Services", modules: list[str] | None = None):
        self.svc = svc
        self.all: dict[str, Skill] = {}
        self.errors: dict[str, str] = {}
        conf = svc.cfg.get("skills") or {}
        for mod in modules if modules is not None else MODULES:
            if (conf.get(mod) or {}).get("enabled", True) is False:
                continue
            try:
                module = importlib.import_module(f"{__name__}.{mod}")
            except ModuleNotFoundError as exc:
                if exc.name and exc.name.endswith(f"skills.{mod}"):
                    continue  # not written yet
                self.errors[mod] = f"{type(exc).__name__}: {exc}"
                log.warning("skill %s not loaded: %s", mod, exc)
                continue
            for cls in _skill_classes(module):
                try:
                    skill = cls(svc)
                    self.all[skill.name] = skill
                except Exception as exc:  # one broken skill never stops the rest
                    self.errors[cls.name or mod] = f"{type(exc).__name__}: {exc}"
                    log.exception("skill %s failed to start", cls.name or mod)
        self._tool_owner: dict[str, Skill] = {}

    def __iter__(self):
        return iter(self.all.values())

    def __getitem__(self, name: str) -> Skill:
        return self.all[name]

    def get(self, name: str) -> Skill | None:
        return self.all.get(name)

    def tools(self) -> list["Tool"]:
        out = []
        for skill in self:
            try:
                for tool in skill.tools():
                    self._tool_owner[tool.name] = skill
                    out.append(tool)
            except Exception:
                log.exception("skill %s: tools failed", skill.name)
        return out

    def route(self, t: str) -> "Intent | None":
        for skill in self:
            try:
                intent = skill.route(t)
            except Exception:
                log.exception("skill %s: route failed", skill.name)
                continue
            if intent is not None:
                return intent
        return None

    def say(self, tool: str, args: dict, result: dict) -> str | None:
        if not self._tool_owner:
            self.tools()
        skill = self._tool_owner.get(tool)
        if skill is None:
            return None
        try:
            return skill.say(tool, args, result)
        except Exception:
            log.exception("skill %s: say failed", skill.name)
            return None

    def briefing(self) -> dict:
        out = {}
        for skill in self:
            try:
                b = skill.briefing()
            except Exception as exc:
                log.warning("skill %s: briefing failed: %s", skill.name, exc)
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
        fn = skill.actions().get(action)
        if fn is None:
            return {"ok": False, "error": f"{skill.title or name} has no action {action}."}
        out = fn(body or {})
        if isinstance(out, dict) and out.get("ok", True):
            skill.publish()
        return out


def _skill_classes(module) -> list[type[Skill]]:
    return [v for v in vars(module).values()
            if isinstance(v, type) and issubclass(v, Skill) and v is not Skill and v.__module__ == module.__name__]


# ---- small shared helpers for skills ----------------------------------------------------------------------

def words(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", t.lower()))
