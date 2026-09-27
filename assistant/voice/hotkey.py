"""Global push-to-talk hotkey.

Uses ``pynput`` (maintained; no admin needed on Windows). The old ``keyboard``
package (last release 2020) is still used as a fallback if it's the one
installed. Config keeps the friendly format: ``push_to_talk_hotkey: ctrl+alt+j``.
"""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

_MODIFIERS = {"ctrl", "alt", "shift", "cmd", "win", "super", "alt_gr", "ctrl_l", "ctrl_r", "alt_l", "alt_r",
              "shift_l", "shift_r"}
_NAMED = {"space", "enter", "tab", "esc", "backspace", "delete", "insert", "home", "end", "page_up", "page_down",
          "up", "down", "left", "right", "caps_lock", "pause", "scroll_lock", "print_screen", "menu"}
_ALIASES = {"control": "ctrl", "win": "cmd", "super": "cmd", "windows": "cmd", "escape": "esc", "return": "enter",
            "pgup": "page_up", "pgdn": "page_down", "del": "delete", "ins": "insert"}
_listener = None


def to_pynput(combo: str) -> str:
    """'ctrl+alt+j' -> '<ctrl>+<alt>+j'; 'ctrl+shift+f13' -> '<ctrl>+<shift>+<f13>'."""
    parts = []
    for raw in combo.lower().replace(" ", "").split("+"):
        if not raw:
            raise ValueError(f"bad hotkey: {combo!r}")
        key = _ALIASES.get(raw, raw)
        if key in _MODIFIERS or key in _NAMED or (key.startswith("f") and key[1:].isdigit()):
            parts.append(f"<{key}>")
        elif len(key) == 1:
            parts.append(key)
        else:
            raise ValueError(f"unknown key {raw!r} in hotkey {combo!r}")
    return "+".join(parts)


def register_hotkey(combo: str | None, callback: Callable[[], None]) -> bool:
    global _listener
    if not combo:
        return False
    try:
        from pynput import keyboard as pk

        if _listener is not None:
            _listener.stop()
        _listener = pk.GlobalHotKeys({to_pynput(combo): callback})
        _listener.daemon = True
        _listener.start()
        return True
    except ImportError:
        pass
    except Exception as exc:
        log.warning("push-to-talk hotkey %s unavailable: %s", combo, exc)
        return False
    try:  # older installs
        import keyboard

        keyboard.add_hotkey(combo, callback, suppress=False)
        return True
    except Exception as exc:  # ImportError, or needs root on Linux
        log.warning("push-to-talk hotkey %s unavailable: %s", combo, exc)
        return False
