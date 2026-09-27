"""Global push-to-talk hotkey (Windows: works without admin via the `keyboard` package)."""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)


def register_hotkey(combo: str | None, callback: Callable[[], None]) -> bool:
    if not combo:
        return False
    try:
        import keyboard

        keyboard.add_hotkey(combo, callback, suppress=False)
        return True
    except Exception as exc:  # ImportError, or needs root on Linux
        log.warning("push-to-talk hotkey %s unavailable: %s", combo, exc)
        return False
