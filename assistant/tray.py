"""System tray icon: the assistant's always-visible handle when no window is open.

The icon's colour mirrors the HUD orb (cyan listening, amber needs attention,
red error, grey muted) and the menu covers the everyday controls. pystray and
Pillow are imported lazily so the rest of the app (and the tests) never need
them.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

# Same mark as the HUD: graphite disc, warm-white ring, orange attention arc, core = state.
COLORS = {
    "listening": (242, 240, 234),
    "hearing": (255, 106, 43),
    "armed": (255, 106, 43),
    "speaking": (255, 106, 43),
    "thinking": (255, 106, 43),
    "attention": (242, 181, 58),
    "error": (239, 75, 75),
    "muted": (125, 122, 115),
    "off": (125, 122, 115),
}
ACCENT = (255, 106, 43)
GROUND = (11, 11, 12)
RING = (242, 240, 234)
ACTIVE = {"listening", "hearing", "armed", "speaking", "thinking"}


def tray_state(voice_state: str, speaking: bool, thinking: bool, unhealthy: int) -> str:
    """Collapse everything into one icon state; problems outrank activity."""
    if voice_state in ("error",):
        return "error"
    if unhealthy:
        return "attention"
    if speaking:
        return "speaking"
    if thinking:
        return "thinking"
    if voice_state in ("hearing", "armed", "transcribing"):
        return "hearing"
    if voice_state in ("muted",):
        return "muted"
    if voice_state in ("listening",):
        return "listening"
    return "off"  # voice disabled / unavailable: still running, just not listening


@dataclass
class MenuItem:
    label: str
    action: Callable[[], None] | None = None
    checked: Callable[[], bool] | None = None
    default: bool = False
    separator: bool = False


def pystray_menu(pystray, items: list[MenuItem]):
    """Our menu as a pystray.Menu. pystray accepts callbacks of 0-2 parameters and counts default
    arguments too, so each callback is a plain two-parameter closure (a ``fn=...`` default made it
    three, and pystray refused to start on Windows)."""
    def call(fn):
        return lambda _icon, _item: fn()

    def check(fn):
        return lambda _item: fn()

    def wrap(item: MenuItem):
        if item.separator:
            return pystray.Menu.SEPARATOR
        return pystray.MenuItem(item.label, call(item.action) if item.action else None,
                                checked=check(item.checked) if item.checked else None, default=item.default)

    return pystray.Menu(*(wrap(i) for i in items))


class Tray:
    def __init__(self, runtime, hud_url: str, on_quit: Callable[[], None]):
        self.rt = runtime
        self.url = hud_url
        self.on_quit = on_quit
        self.state = "off"
        self._icon = None
        self._thinking = False
        self._speaking = False
        self._last_refresh = 0.0
        runtime.bus.on(self._on_event)

    # ---- state ----------------------------------------------------------
    def compute_state(self) -> str:
        voice = self.rt.listener.state if self.rt.listener else "disabled"
        unhealthy = sum(1 for p in self.rt.supervisor.snapshot() if p["state"] in ("error", "stalled", "restarting"))
        return tray_state(voice, self._speaking, self._thinking, unhealthy)

    def tooltip(self) -> str:
        name = self.rt.cfg["assistant"]["name"]
        words = {"listening": f"listening for “{self.rt.cfg['assistant']['wake_words'][0]}”", "hearing": "hearing you",
                 "speaking": "speaking", "thinking": "thinking", "muted": "mic muted", "attention": "needs attention — see Setup",
                 "error": "voice error — see Setup", "off": "running (voice off)"}
        return f"{name}: {words.get(self.state, self.state)}"[:127]  # Windows tooltip limit

    def _on_event(self, event: dict) -> None:
        kind = event["type"]
        if kind == "thinking":
            self._thinking = bool((event.get("data") or {}).get("active"))
        elif kind == "speaking":
            self._speaking = bool((event.get("data") or {}).get("active"))
        elif kind not in ("voice_state", "health", "wake"):
            return
        self.refresh()

    def refresh(self) -> None:
        new = self.compute_state()
        if new == self.state and time.time() - self._last_refresh < 30:
            return
        self.state, self._last_refresh = new, time.time()
        if self._icon is not None:
            try:
                self._icon.icon = self.image(new)
                self._icon.title = self.tooltip()
            except Exception:
                log.debug("tray refresh failed", exc_info=True)

    # ---- menu -----------------------------------------------------------
    def menu_items(self) -> list[MenuItem]:
        rt = self.rt
        items = [MenuItem("Open HUD", self.open_hud, default=True)]
        if rt.listener:
            items += [
                MenuItem("Push to talk", rt.listener.arm),
                MenuItem("Mute microphone", lambda: rt.listener.set_muted(not rt.listener.muted),
                         checked=lambda: rt.listener.muted),
            ]
        items += [
            MenuItem("Stop everything (kill switch)", self.toggle_kill_switch,
                     checked=lambda: rt.assistant.tools.guard.hands_off),
            MenuItem("Pause activity tracking", self.toggle_tracking, checked=lambda: rt.svc.activity.paused),
            MenuItem("", separator=True),
            MenuItem("Setup && health", lambda: self.open_hud("#setup")),
            MenuItem("Rescan PC", lambda: threading.Thread(target=rt.rescan, daemon=True).start()),
            MenuItem("Open log folder", self.open_logs),
            MenuItem("", separator=True),
            MenuItem(f"Quit {rt.cfg['assistant']['name']}", self.quit),
        ]
        return items

    def toggle_kill_switch(self) -> None:
        """Ticked = PC control paused. Clicking it again resumes."""
        if self.rt.assistant.tools.guard.hands_off:
            self.rt.resume_control("tray")
        else:
            self.rt.kill_switch("tray")

    def toggle_tracking(self) -> None:
        act = self.rt.svc.activity
        act.paused = not act.paused
        self.rt.bus.publish("tracking", {"paused": act.paused}, sticky=True)

    def open_hud(self, fragment: str = "") -> None:
        from .integrations.browser import Browser

        url = self.url + fragment
        if not Browser().app_window(url):
            webbrowser.open(url)

    def open_logs(self) -> None:
        path = self.rt.cfg.data_dir / "logs"
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]  # pragma: no cover
        else:
            subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(path)])

    def quit(self) -> None:
        log.info("quit requested from tray")
        self.stop()
        self.on_quit()

    # ---- pystray plumbing ----------------------------------------------
    @staticmethod
    def image(state: str):
        from PIL import Image, ImageDraw

        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        color = COLORS.get(state, COLORS["off"])
        d.ellipse((1, 1, size - 1, size - 1), fill=GROUND + (255,))                        # readable on light or dark taskbars
        ring = RING if state in ACTIVE or state == "attention" else COLORS["off"]
        d.ellipse((9, 9, size - 9, size - 9), outline=ring + (255,), width=3)             # ring
        if state in ACTIVE:
            d.arc((7, 7, size - 7, size - 7), start=-90, end=0, fill=ACCENT + (255,), width=7)  # attention arc
        if state == "error":
            d.ellipse((9, 9, size - 9, size - 9), outline=color + (255,), width=3)
        d.ellipse((24, 24, size - 24, size - 24), fill=color + (255,))                    # core
        if state == "muted":
            d.line((16, size - 16, size - 16, 16), fill=RING + (255,), width=4)           # slash
        return img

    def start(self) -> bool:
        try:
            import pystray
        except Exception as exc:  # not installed, or no desktop session (CI, SSH)
            log.warning("tray icon unavailable: %s", exc)
            return False

        try:
            self.state = self.compute_state()
            self._icon = pystray.Icon("pc-assistant", self.image(self.state), self.tooltip(),
                                      menu=pystray_menu(pystray, self.menu_items()))
            try:
                self._icon.run_detached()
            except (NotImplementedError, AttributeError):
                threading.Thread(target=self._icon.run, name="tray", daemon=True).start()
        except Exception:  # the tray is a convenience: never let it take the app down
            log.exception("tray icon failed to start; running without it")
            self._icon = None
            return False
        return True

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
