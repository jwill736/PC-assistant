""""Vesper, undo that": puts back the last thing Vesper changed, or the last few ("undo the last 3"), and the HUD lists
this session's actions with an Undo button on each.

The history is kept by the one door every action goes through (``ToolBox.run``; see brain/undo.py), and each tool says
next to its own code how it's put back: volume, mute and brightness, an app's volume, a window minimised or brought to
the front, a virtual desktop, the OBS scene, a source shown or hidden, an OBS mute, Spotify's volume, a task added or
ticked off, a reminder, timer or routine, a focus block, a Downloads tidy, a switch or text box in another app. What
can't be taken back (a sent message, a raid, a shutdown, a click) is listed as such and never pretended. Undoing goes
through the tiers like anything else (a focus block or another app's switch needs a yes again), and never fights J:
if he changed it himself since, Vesper says what it is now and leaves it.
"""

from __future__ import annotations

import re

from ..brain.router import Intent, number
from ..brain.tools import Tool, obj
from . import Skill

NOT_READY = "The action history isn't running yet."


class UndoSkill(Skill):
    name, title = "undo", "Undo"

    def __init__(self, svc):
        super().__init__(svc)
        self.history = None  # the ToolBox's ActionHistory, once Vesper is running
        self.runtime = None

    def start(self, runtime) -> None:
        self.runtime = runtime
        self.attach(runtime.assistant.tools.history)

    def attach(self, history) -> None:
        self.history = history
        history.on_change = self.publish
        self.publish()

    # ---- the tools ---------------------------------------------------------------------------------------------
    def _pin(self, a: dict) -> list[int]:
        """The actions this undo is for, worked out once (when J is asked), so a yes undoes exactly those."""
        if "ids" not in a:
            a["ids"] = self.history.pick(int(a.get("count") or 1)) if self.history is not None else []
        return [int(i) for i in a["ids"] or []]

    def _tier(self, a: dict) -> int:
        ids = self._pin(a)
        return self.history.tier(ids) if self.history is not None else 0

    def _describe(self, a: dict) -> str:
        ids = self._pin(a)
        return self.history.describe(ids) if self.history is not None else "undo"

    def undo(self, count: int = 1, ids: list | None = None) -> dict:
        if self.history is None:
            return {"ok": False, "error": NOT_READY}
        return self.history.undo(ids if ids is not None else self.history.pick(count))

    def listing(self, limit: int = 10) -> dict:
        if self.history is None:
            return {"ok": False, "error": NOT_READY}
        return {"ok": True, "actions": self.history.view()[:max(1, min(int(limit or 10), 30))]}

    def tools(self):
        return [
            Tool("undo", "Undo what Vesper itself just changed on the PC ('undo that', 'undo the last 3'): count = how "
                         "many of its latest actions. Puts back the exact earlier value; says so when something can't "
                         "be undone or the user has changed it since.",
                 obj({"count": {"type": "integer"}, "ids": {"type": "array", "items": {"type": "integer"}}}),
                 self.undo, tier=self._tier, confirm_text=self._describe, undo=False),
            Tool("undo_history", "Vesper's own recent actions this session, newest first, and which can be undone.",
                 obj({"limit": {"type": "integer"}}), self.listing),
        ]

    def route(self, t: str):
        if re.fullmatch(r"undo(?: (?:that|it|this|the last (?:one|thing|action|change)|what you (?:just )?did|"
                        r"your last (?:action|change)))?|take (?:that|it) back", t):
            return Intent("tool", "undo", {"count": 1})
        m = re.fullmatch(r"undo (?:the )?(?:last|past) (\d+|[a-z]+(?:-[a-z]+)?)(?: (?:things|actions|changes|ones|steps))?",
                         t)
        if m and (n := number(m.group(1))):
            return Intent("tool", "undo", {"count": max(1, min(n, 10))})
        if re.fullmatch(r"what can (?:you|i) undo|what have you (?:changed|done)(?: today)?|"
                        r"(?:show|list) (?:me )?(?:your|the) (?:action history|recent actions)", t):
            return Intent("tool", "undo_history", {})
        return None

    def say(self, tool, args, result):
        if tool == "undo":
            return result.get("message")
        if tool != "undo_history":
            return None
        acts = result.get("actions") or []
        can = [a for a in acts if a["can_undo"]]
        if not acts:
            return "I haven't changed anything yet this session."
        if not can:
            return f"Nothing I can undo: the last was {acts[0]['label']}, and that can't be taken back."
        return f"I can undo {len(can)} of my last {len(acts)} actions; the latest is {can[0]['label']}. They're on the HUD."

    def status(self) -> dict:
        return {"ready": self.history is not None, "actions": self.history.view() if self.history is not None else []}

    def actions(self):
        def undo_one(body: dict) -> dict:
            """The HUD's Undo button: through the tiers like a spoken undo (one that needs a yes asks for it)."""
            if self.runtime is None:
                return {"ok": False, "error": NOT_READY}
            out = self.runtime.assistant.run_tool("undo", {"ids": [int(body.get("id") or 0)]})
            data = out.get("data") or {}
            if out.get("kind") == "pending":
                return {"ok": True, "pending": True, "reply": out.get("reply")}
            return {"ok": data.get("ok") is not False, "reply": out.get("reply"), "error": data.get("error")}
        return {"undo": undo_one}
