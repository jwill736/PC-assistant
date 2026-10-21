"""A hotkey you hold down: ``on_down`` when the whole combination is held, ``on_up`` when any key of it comes up.

``hotkey.py``'s GlobalHotKeys only reports presses, so dictation (hold Ctrl+Alt+D, talk, let go) watches raw key
events with a ``pynput`` listener. ``Chord`` is the pure part (which keys are down), so it's tested without a keyboard.
Without pynput (or without a desktop) the combination still works as a toggle through ``hotkey.register_hotkey``:
press once to start, again to stop.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from .hotkey import _ALIASES, register_hotkey

log = logging.getLogger(__name__)

_SIDES = {"ctrl_l": "ctrl", "ctrl_r": "ctrl", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt", "shift_l": "shift",
          "shift_r": "shift", "cmd_l": "cmd", "cmd_r": "cmd", "win": "cmd", "super": "cmd"}


def parse(combo: str) -> frozenset[str]:
    """'ctrl+alt+d' -> {'ctrl', 'alt', 'd'} (sides folded: left and right Ctrl are both 'ctrl')."""
    keys = set()
    for raw in (combo or "").lower().replace(" ", "").split("+"):
        if not raw:
            raise ValueError(f"bad hotkey: {combo!r}")
        k = _ALIASES.get(raw, raw)
        keys.add(_SIDES.get(k, k))
    return frozenset(keys)


def key_name(key) -> str | None:
    """A pynput key -> 'ctrl' / 'alt' / 'd' / 'f9'. Letters come from the virtual key code: with Ctrl held,
    Windows reports 'd' as the control character '\\x04'."""
    name = getattr(key, "name", None)
    if name:
        return _SIDES.get(name, name)
    vk = getattr(key, "vk", None)
    if vk is not None and (0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39):
        return chr(vk).lower()
    char = getattr(key, "char", None)
    return char.lower() if char else None


class Chord:
    """Which keys of the combination are down; says when it's fully held and when it lets go."""

    def __init__(self, combo: str):
        self.keys = parse(combo)
        self.down: set[str] = set()
        self.active = False

    def press(self, name: str | None) -> bool:
        """True when this press completes the combination (key repeat while held does not count again)."""
        if name is None:
            return False
        self.down.add(name)
        if not self.active and self.keys <= self.down:
            self.active = True
            return True
        return False

    def release(self, name: str | None) -> bool:
        """True when this release ends a held combination."""
        if name is None:
            return False
        self.down.discard(name)
        if self.active and name in self.keys:
            self.active = False
            return True
        return False


class HoldKey:
    def __init__(self, combo: str, on_down: Callable[[], None], on_up: Callable[[], None]):
        self.combo, self.on_down, self.on_up = combo, on_down, on_up
        self.chord = Chord(combo)
        self.mode = "off"  # hold | toggle | off
        self._listener = None
        self._toggled = False

    def start(self) -> str:
        try:
            from pynput import keyboard

            self._listener = keyboard.Listener(on_press=lambda k: self._press(key_name(k)),
                                               on_release=lambda k: self._release(key_name(k)))
            self._listener.daemon = True
            self._listener.start()
            self.mode = "hold"
        except Exception as exc:  # no pynput, or no desktop to hook: press once to start, again to stop
            log.info("hold-to-talk for %s unavailable (%s); it toggles instead", self.combo, exc)
            self.mode = "toggle" if register_hotkey(self.combo, self.toggle) else "off"
        return self.mode

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _press(self, name: str | None) -> None:
        if self.chord.press(name):
            self._fire(self.on_down)

    def _release(self, name: str | None) -> None:
        if self.chord.release(name):
            self._fire(self.on_up)

    def toggle(self) -> None:
        self._toggled = not self._toggled
        self._fire(self.on_down if self._toggled else self.on_up)

    @staticmethod
    def _fire(fn: Callable[[], None]) -> None:
        # Never on the keyboard hook's thread: Windows drops a low-level hook that takes too long to return.
        threading.Thread(target=fn, name="holdkey", daemon=True).start()
