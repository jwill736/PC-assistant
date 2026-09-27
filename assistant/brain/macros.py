"""Trigger phrases: one spoken phrase → a sequence of actions ("brb", "raid mode").

Defined in config.yaml under ``macros``::

    macros:
      brb:
        say: ["brb", "be right back", "taking a break"]   # trigger phrases
        steps:
          - obs_switch_scene: {scene: BRB}
          - obs_set_mute: {source: mic, muted: true}
          - media_control: {action: play_pause}
          - say: "Be right back — scene's up."

Step kinds: ``<tool name>: {args}`` (any assistant tool), ``say: text``,
``wait: seconds`` (max 30) and ``command: "open discord"`` (runs like a spoken
command). A macro containing a risky tool asks for one "yes" up front.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from .router import clean

STEP_KINDS = {"say", "wait", "command"}


@dataclass
class Macro:
    name: str
    triggers: list[str]
    steps: list[dict] = field(default_factory=list)
    reply: str = ""

    def tool_steps(self) -> list[tuple[str, dict]]:
        out = []
        for step in self.steps:
            (kind, value), = step.items()
            if kind not in STEP_KINDS:
                out.append((kind, value or {}))
        return out


def load_macros(cfg: dict) -> dict[str, Macro]:
    macros = {}
    for name, spec in (cfg.get("macros") or {}).items():
        spec = spec or {}
        triggers = spec.get("say") or [name]
        if isinstance(triggers, str):
            triggers = [triggers]
        steps = []
        for step in spec.get("steps") or []:
            if isinstance(step, dict) and len(step) == 1:
                steps.append(step)
        macros[name.lower()] = Macro(name, [clean(t) for t in triggers if clean(t)], steps, spec.get("reply", ""))
    return macros


def validate(macros: dict[str, Macro], tool_names: set[str]) -> list[str]:
    """Human-readable problems (unknown tools) for the Setup tab."""
    problems = []
    for m in macros.values():
        for tool, _args in m.tool_steps():
            if tool not in tool_names:
                problems.append(f"macro '{m.name}': unknown step '{tool}'")
    return problems


def match(text: str, macros: dict[str, Macro], cutoff: float = 0.88) -> str | None:
    """Whole-utterance match against trigger phrases (exact, 'run X', or near-exact)."""
    t = clean(text)
    for prefix in ("run ", "do ", "start ", "trigger "):
        if t.startswith(prefix) and t[len(prefix):] in {n for n in macros}:
            return t[len(prefix):]
    phrases = {p: name for name, m in macros.items() for p in m.triggers}
    if t in phrases:
        return phrases[t]
    close = difflib.get_close_matches(t, list(phrases), n=1, cutoff=cutoff)
    return phrases[close[0]] if close else None
