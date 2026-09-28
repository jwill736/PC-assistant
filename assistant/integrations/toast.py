"""Confirm a waiting action from a Windows notification: Yes / No buttons.

Useful mid-game or with the HUD closed: the toast names the exact action, and
a click answers that one action only. If something else replaced it in the
meantime, the click does nothing (the assistant checks the pending id).
Uses ``win11toast`` (MIT); without it this quietly does nothing.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

TITLES = {2: "needs a yes", 3: "needs a yes · can't be undone"}


def available() -> bool:
    try:
        import win11toast  # noqa: F401
        return True
    except Exception:
        return False


def clicked(result) -> str | None:
    """'yes' / 'no' from what win11toast returns for a button click; None if dismissed or timed out."""
    if isinstance(result, dict):
        text = str(result.get("arguments", "")).lower()
        if text.endswith("yes") or text == "http:yes":
            return "yes"
        if text.endswith("no"):
            return "no"
    return None


class ToastConfirmer:
    def __init__(self, on_yes: Callable[[str], None], on_no: Callable[[str], None], app_name: str = "Vesper",
                 toast_fn=None):
        self.on_yes, self.on_no = on_yes, on_no
        self.app_name = app_name
        self._toast = toast_fn

    def __call__(self, pending: dict | None) -> threading.Thread | None:
        if not pending:
            return None
        t = threading.Thread(target=self._ask, args=(pending,), name="toast-confirm", daemon=True)
        t.start()
        return t

    def _ask(self, pending: dict) -> None:
        toast = self._toast
        if toast is None:
            try:
                from win11toast import toast
            except Exception:
                return
        try:  # blocks until a button is clicked or the toast times out
            result = toast(f"{self.app_name} {TITLES.get(pending.get('tier', 2), TITLES[2])}",
                           f"{pending['text'][:200]}?\nSay yes, or choose below.", buttons=["Yes", "No"])
        except Exception:
            log.debug("toast failed", exc_info=True)
            return
        choice = clicked(result)
        if choice == "yes":
            self.on_yes(pending["id"])
        elif choice == "no":
            self.on_no(pending["id"])
