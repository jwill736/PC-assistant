""""Undo that": what Vesper changed this session, and how to put each change back.

``ToolBox.run`` (the one door every action goes through) adds each action of tier 1 or more to the ``ActionHistory``.
A tool says how to undo itself next to its own code, with ``Tool(..., undo=...)``:

* ``undo(args)`` runs just before the tool does (so it can read the value it is about to change) and returns
  ``after(result) -> Undo | None``, called once the tool succeeded;
* ``undo=None`` (the default): the action is listed as one Vesper can't undo (a sent message, a raid, a shutdown);
* ``undo=False``: not an action to list at all ("undo" itself).

Undoing runs through the tiers like any action (the ``undo`` tool's tier is the highest of what it puts back), and
never fights J: an ``Undo`` with ``now``/``left`` first checks the value is still what the action left, and if J
changed it since, it says so and leaves it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

KEEP = 30  # actions remembered (this session only)
OPEN, UNDONE, CHANGED = "open", "undone", "changed"
# Done on Vesper's own (a raid's auto-marker, a chat spike's, a routine): listed, but never "that" in "undo that".
BACKGROUND = frozenset({"highlight", "stream_moments", "automation"})


@dataclass
class Undo:
    """How to put back what one action changed."""

    text: str  # what undoing does, said and shown: "volume back to 35%"
    run: Callable[[], dict]  # puts it back: {"ok": False, "error": ...} when it can't
    tier: int = 1  # the undo's own tier (a switch in another app: 2, so it needs a yes like the action did)
    now: Callable[[], Any] | None = None  # the value as it is now...
    left: Any = None  # ...and as the action left it: if they differ, J changed it since
    what: str = ""  # the thing, for "the volume is 60% now, not 30%"
    show: Callable[[Any], str] = str  # a value as said: (60, False) -> "60%"


def restore(read: Callable[[dict], Any], put: Callable[[dict, Any], dict], what: str | Callable[[dict], str],
            show: Callable[[Any], str] = str, tier: int = 1,
            left: Callable[[dict], Any] | None = None) -> Callable[[dict], Callable[[dict], Undo | None] | None]:
    """The undo for a setting that reads back (a level, a scene, a mute): put back the value it had just before the
    action, unless J changed it since. ``read(args)`` gives the value (raising when it can't be read: then there's no
    undo rather than a guess); ``put(args, value)`` sets it and returns a tool-style result. ``what`` names the
    setting ("the volume"), or works it out from the args ("discord's volume"). ``left(result)``: what the action
    left, from its own result, for a service that reports the old value for a moment after a change (Spotify)."""
    def before(args: dict):
        try:
            old = read(args)
        except Exception:
            return None

        def after(result: dict) -> Undo | None:
            try:
                now = left(result) if left else read(args)
            except Exception:
                return None
            name = what(args) if callable(what) else what
            return Undo(f"{name} back to {show(old)}", lambda: put(args, old), tier=tier, now=lambda: read(args),
                        left=now, what=name, show=show)
        return after
    return before


def checked(result: dict) -> dict:
    """A tool-style result, or the reason it failed raised (for ``read``)."""
    if not isinstance(result, dict) or result.get("ok") is False:
        raise RuntimeError((result or {}).get("error") or "can't read it")
    return result


class ActionHistory:
    """This session's actions, newest last. Thread-safe; ``on_change`` is told after every change (the HUD panel)."""

    def __init__(self, keep: int = KEEP, clock: Callable[[], float] = time.time):
        self.keep, self.clock = keep, clock
        self.items: list[dict] = []
        self.on_change: Callable[[], None] | None = None
        self._next = 1
        self._lock = threading.RLock()

    def add(self, tool: str, label: str, tier: int, undo: Undo | None, source: str | None = None) -> dict:
        """``source``: who asked (the command's source); one of BACKGROUND is never picked for "undo that"."""
        with self._lock:
            item = {"id": self._next, "tool": tool, "label": label, "tier": tier, "at": self.clock(), "undo": undo,
                    "state": OPEN, "note": "", "source": source}
            self._next += 1
            self.items = (self.items + [item])[-self.keep:]
        self._changed()
        return item

    def view(self) -> list[dict]:
        """Newest first, for the HUD and "what can you undo"."""
        with self._lock:
            return [{"id": i["id"], "tool": i["tool"], "label": i["label"], "tier": i["tier"], "at": i["at"],
                     "state": i["state"], "note": i["note"], "can_undo": i["undo"] is not None and i["state"] == OPEN,
                     "undo": i["undo"].text if i["undo"] else None} for i in reversed(self.items)]

    def pick(self, count: int = 1) -> list[int]:
        """The newest ``count`` actions not undone yet (one Vesper can't undo counts too: "that" is the last thing).
        Never one Vesper did on its own (BACKGROUND): "that" is J's last change."""
        with self._lock:
            live = [i["id"] for i in reversed(self.items) if i["state"] == OPEN and i.get("source") not in BACKGROUND]
        return live[:max(1, int(count or 1))]

    def _get(self, ids) -> list[dict]:
        with self._lock:
            by_id = {i["id"]: i for i in self.items}
        return [by_id[int(n)] for n in ids or [] if int(n) in by_id]

    def tier(self, ids) -> int:
        return max([i["undo"].tier for i in self._get(ids) if i["undo"] and i["state"] == OPEN] or [0])

    def describe(self, ids) -> str:
        parts = [i["undo"].text for i in self._get(ids) if i["undo"] and i["state"] == OPEN]
        return "undo: " + "; ".join(parts) if parts else "undo"

    def undo(self, ids) -> dict:
        """Put back each of ``ids`` (newest first). Never one J has changed since; never one with no way back."""
        undone, changed, cant, failed = [], [], [], []
        items = sorted(self._get(ids), key=lambda i: -i["id"])  # newest first, whatever order they came in
        for i in items:
            u: Undo | None = i["undo"]
            if i["state"] != OPEN:
                continue
            if u is None:
                cant.append(i["label"])
                continue
            if u.now is not None:
                try:
                    now = u.now()
                except Exception as exc:  # it can't be read (OBS closed, the window gone): don't guess
                    failed.append(f"I can't check {u.what or i['label']} right now ({exc})")
                    continue
                if now != u.left:
                    what = u.what or i["label"]
                    with self._lock:
                        i["state"], i["note"] = CHANGED, f"changed since: {u.show(now)}"
                    changed.append(f"{what[:1].upper()}{what[1:]} is {u.show(now)} now, not {u.show(u.left)}, "
                                   "so I left it")
                    continue
            try:
                out = u.run() or {}
            except Exception as exc:
                log.exception("undo of %s failed", i["tool"])
                out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if out.get("ok") is False:
                failed.append(out.get("error") or f"undoing {i['label']} failed")
                continue
            with self._lock:
                i["state"] = UNDONE
            undone.append(u.text)
        if items:
            self._changed()
        return _outcome(undone, changed, cant, failed, bool(items))

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.exception("action history: change listener failed")


def _outcome(undone: list, changed: list, cant: list, failed: list, any_items: bool) -> dict:
    parts = []
    if undone:
        parts.append(f"Undone: {', '.join(undone)}.")
    parts += [f"{c}." for c in changed]
    if cant:
        parts.append(f"I can't undo {', '.join(cant)}; that can't be taken back.")
    parts += [f"{f}." for f in failed]
    message = " ".join(parts) or ("That's already been undone." if any_items else "There's nothing to undo yet.")
    out = {"ok": bool(undone), "undone": undone, "changed": changed, "cant": cant, "failed": failed, "message": message}
    if not undone:
        out["error"] = message  # nothing was put back: the spoken reply is the reason
    return out
