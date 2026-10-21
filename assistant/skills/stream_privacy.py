"""On-air safety net: while you're live, the HUD's private panels blur themselves.

Inbox, Replies, People, Money, Journal, Memory, Clipboard, Rewind and the text of notifications get a "Hidden while live
· click to peek" cover (a peek shows one panel for 10 s). It turns on when you're live, and also when OBS is recording
with a display capture (or a capture of the HUD's own window) in the program scene, since a recording gets posted too.
Vesper's own Windows notifications (the Yes/No toasts) are held back while it's on; the HUD and voice still ask.

The Stream tab has the switch: "Privacy while live" (on by default, ``skills.stream_privacy.when_live``). The HUD sets the
``live-private`` class on <body> from the OBS/Twitch live state at once (app.js), and from ``private`` here for the
recording case; styles.css lists the panels.
"""

from __future__ import annotations

import logging
import re
import time

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill
from ._kit import save_setting

log = logging.getLogger(__name__)

PANELS = ["inbox", "inbox_zero", "people", "bills", "journal", "memory", "clipboard", "rewind"]
BROWSERS = re.compile(r"(?:chrome|msedge|firefox|brave|opera|vivaldi|arc)\.exe$", re.I)


def exposes_hud(capture: dict, hud_title: str) -> bool:
    """A display capture shows whatever is on that screen; a window capture only matters if it's the HUD's window."""
    if capture.get("display"):
        return True
    win = (capture.get("window") or "").lower()
    title = win.split(":", 1)[0]
    return bool(hud_title) and hud_title.lower() in title


class StreamPrivacy(Skill):
    name, title = "stream_privacy", "Privacy while live"
    poll_s = 5
    game_essential = True  # hides private panels the moment you go live
    delay_s = 3

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        self.captures: list[dict] = []
        self._captures_at = 0.0
        self.error: str | None = None
        self._was: bool | None = None

    @property
    def on(self) -> bool:
        return bool(self.setting("when_live", True))

    def start(self, runtime) -> None:
        """Hold Vesper's own Windows toasts (Yes/No confirmations) while the screen may be on air."""
        assistant = getattr(runtime, "assistant", None)
        toast = getattr(assistant, "on_pending", None)
        if assistant is not None and toast is not None and not getattr(toast, "_privacy", False):
            def guarded(pending, _toast=toast):
                if pending and self.private():
                    return None
                return _toast(pending)
            guarded._privacy = True
            assistant.on_pending = guarded

    def _obs(self) -> dict:
        return (self.svc.bus.latest.get("obs") or {}).get("data") or {}

    def live(self) -> bool:
        return bool(self.svc.live and self.svc.live())

    def _refresh_captures(self) -> None:
        obs = self._obs()
        if not obs.get("connected") or not self.setting("capture_check", True):
            self.captures = []
            return
        if self.clock() - self._captures_at < float(self.setting("capture_every_s", 30)):
            return
        self._captures_at = self.clock()
        try:
            self.captures = self.svc.obs.screen_captures()
        except Exception as exc:  # OBS busy or an older obs-websocket: keep the last answer
            log.debug("OBS screen captures: %s", exc)

    def exposed(self) -> list[dict]:
        hud = f"{self.svc.name} HUD"
        return [c for c in self.captures if exposes_hud(c, hud)]

    def reason(self) -> str:
        if not self.on:
            return ""
        if self.live():
            return "live"
        if (self._obs().get("recording") or {}).get("active") and self.exposed():
            return "recording your screen"
        return ""

    def private(self) -> bool:
        return bool(self.reason())

    def tick(self) -> None:
        try:
            self._refresh_captures()
            self.error = None
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        now = self.private()
        if now != self._was or self.clock() - (self.last_status or {}).get("ts", 0) >= 60:
            self._was = now
            self.publish()

    def status(self) -> dict:
        why = self.reason()
        return {"on": self.on, "private": bool(why), "why": why, "live": self.live(), "panels": PANELS,
                "captures": [{"name": c["name"], "kind": c["kind"], "shows_hud": exposes_hud(c, f"{self.svc.name} HUD")}
                             for c in self.captures],
                "error": self.error, "ts": self.clock()}

    def toggle(self, on: bool) -> dict:
        save_setting(self, "when_live", bool(on))
        return {"ok": True, "on": bool(on)}

    def actions(self):
        return {"toggle": lambda b: self.toggle(bool(b.get("on", not self.on)))}

    def tools(self):
        return [Tool("privacy_while_live", "Turn the HUD's on-air privacy on or off: while live, private panels (email, "
                                           "people, money, journal, memory, clipboard, rewind, notification text) blur.",
                     obj({"on": {"type": "boolean"}}, ["on"]), lambda on=True: self.toggle(bool(on)), tier=1)]

    def route(self, t: str):
        m = re.fullmatch(r"(?:turn |switch )?(on|off) (?:the )?privacy(?: while live| on stream| mode)?"
                         r"|(?:turn |switch )?(?:the )?privacy(?: while live| on stream| mode)? (on|off)", t)
        if m:
            return Intent("tool", "privacy_while_live", {"on": (m.group(1) or m.group(2)) == "on"})
        return None

    def say(self, tool, args, result):
        if result.get("ok") is False:
            return result.get("error")
        return ("Privacy while live is on: private panels blur when you go live." if result.get("on")
                else "Privacy while live is off: panels stay visible on stream.")
