"""Typing into the app you're in, and reading what you've selected there.

Used by dictation (skills/dictation.py) and the selected-text actions (skills/selection.py).

* ``Keys.type_text`` types Unicode straight into the focused control with ``SendInput`` (no clipboard, any language);
  a new line is Shift+Enter, so a "new line" dictated into Discord or Slack doesn't send the message.
* ``paste`` puts text in through the clipboard (long text) and puts back what was on the clipboard before.
* ``read_selection`` presses Ctrl+C, reads the clipboard, then puts back what was there.
* ``guard`` says no when the focused window is a password manager, a sign-in prompt or a password field.

Windows is the real target (pure ctypes); elsewhere ``pynput`` is used when it can reach a display, and the DLLs and
clipboard are injectable so tests never touch the real desktop.
"""

from __future__ import annotations

import ctypes
import logging
import re
import sys
import time
import uuid
from typing import Callable

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# Never type into, or read from, these (the clipboard skill's list plus the Windows prompts that take a password).
PASSWORD_APPS = {"1password", "keepass", "keepassxc", "bitwarden", "lastpass", "dashlane", "keeper", "enpass",
                 "nordpass", "roboform", "protonpass", "authy", "credentialuibroker", "consent", "logonui",
                 "lockapp", "credwiz"}
PASSWORD_TITLES = re.compile(r"(?i)\b(?:passwords?|passcode|passphrase|enter (?:your )?pin|(?:sign|log)[ -]?in(?!\.\w)|"
                             r"windows security|credentials?|two[- ]factor|one[- ]time code|verification code)\b")

VK = {"shift": 0x10, "ctrl": 0x11, "alt": 0x12, "enter": 0x0D, "tab": 0x09, "backspace": 0x08, "lwin": 0x5B,
      "rwin": 0x5C, **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"}}
MODIFIERS = (0x10, 0x11, 0x12, 0x5B, 0x5C)
INPUT_KEYBOARD, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 1, 0x0002, 0x0004
CHUNK = 120  # inputs per SendInput call (some apps drop keys from one giant batch)


# ---- SendInput structures (plain ctypes: they build on any OS, only the call needs Windows) ------------------------

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)))


def text_inputs(text: str) -> list[INPUT]:
    """Key events for ``text``: each UTF-16 unit as a Unicode key press; a line break as Shift+Enter."""
    out: list[INPUT] = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            out += [_key(VK["shift"]), _key(VK["enter"]), _key(VK["enter"], flags=KEYEVENTF_KEYUP),
                    _key(VK["shift"], flags=KEYEVENTF_KEYUP)]
            continue
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):  # an emoji is two units (a surrogate pair)
            unit = int.from_bytes(data[i:i + 2], "little")
            out += [_key(scan=unit, flags=KEYEVENTF_UNICODE), _key(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    return out


def chord_inputs(*names: str) -> list[INPUT]:
    vks = [VK[n] for n in names]
    return [_key(v) for v in vks] + [_key(v, flags=KEYEVENTF_KEYUP) for v in reversed(vks)]


class WinKeys:
    """Keyboard output through user32 (injectable for tests)."""

    def __init__(self, user32=None, sleep=time.sleep):
        if user32 is None:  # pragma: no cover - Windows only
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SendInput.restype, user32.SendInput.argtypes = wintypes.UINT, [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
            user32.GetAsyncKeyState.restype, user32.GetAsyncKeyState.argtypes = ctypes.c_short, [ctypes.c_int]
        self.u, self.sleep = user32, sleep

    def _send(self, inputs: list[INPUT]) -> bool:
        for i in range(0, len(inputs), CHUNK):
            part = inputs[i:i + CHUNK]
            arr = (INPUT * len(part))(*part)
            if self.u.SendInput(len(part), arr, ctypes.sizeof(INPUT)) != len(part):
                return False  # blocked: an elevated window (UIPI) or the secure desktop
            if i + CHUNK < len(inputs):
                self.sleep(0.005)
        return True

    def type_text(self, text: str) -> bool:
        return self._send(text_inputs(text)) if text else True

    def chord(self, *names: str) -> bool:
        return self._send(chord_inputs(*names))

    def modifiers_down(self) -> bool:
        return any(self.u.GetAsyncKeyState(vk) & 0x8000 for vk in MODIFIERS)


class PynputKeys:
    """Anywhere else with a display: pynput's keyboard controller."""

    def __init__(self, controller, keymod):
        self.c, self.k = controller, keymod

    def type_text(self, text: str) -> bool:
        for i, line in enumerate(text.split("\n")):
            if i:
                with self.c.pressed(self.k.Key.shift):
                    self.c.tap(self.k.Key.enter)
            if line:
                self.c.type(line)
        return True

    def chord(self, *names: str) -> bool:
        keys = [getattr(self.k.Key, n) if hasattr(self.k.Key, n) else n for n in names]
        for k in keys:
            self.c.press(k)
        for k in reversed(keys):
            self.c.release(k)
        return True

    def modifiers_down(self) -> bool:
        return False


def default_keys():
    if IS_WINDOWS:
        try:  # pragma: no cover - Windows only
            return WinKeys()
        except (OSError, AttributeError):  # pragma: no cover
            return None
    try:
        from pynput import keyboard

        return PynputKeys(keyboard.Controller(), keyboard)
    except Exception:  # not installed, or no display (a headless box, CI)
        return None


def wait_modifiers_released(keys, timeout: float = 1.5, sleep=time.sleep, clock=time.monotonic) -> bool:
    """A hotkey's Ctrl/Alt still held would turn typed letters into shortcuts: wait for them to come up."""
    end = clock() + timeout
    while keys.modifiers_down():
        if clock() >= end:
            return False
        sleep(0.02)
    return True


# ---- what has focus ---------------------------------------------------------------------------------------------

_uia = None


def _focused_is_password_uia() -> bool | None:  # pragma: no cover - Windows only
    """UI Automation's IsPassword on the focused element: browsers and modern apps, where Win32 can't tell."""
    global _uia
    try:
        import comtypes.client

        if _uia is None:
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation

            _uia = comtypes.client.CreateObject(CUIAutomation, interface=IUIAutomation)
        el = _uia.GetFocusedElement()
        return bool(el.CurrentIsPassword) if el else None
    except Exception:
        return None


def _focused_is_password_win32() -> bool | None:  # pragma: no cover - Windows only
    """A classic Edit control with the ES_PASSWORD style (dialogs, older apps)."""
    try:
        from ctypes import wintypes

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                        ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]
        u = ctypes.WinDLL("user32", use_last_error=True)
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        if not u.GetGUIThreadInfo(0, ctypes.byref(info)) or not info.hwndFocus:
            return None
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(info.hwndFocus, cls, 64)
        if "edit" not in cls.value.lower():
            return False
        u.GetWindowLongW.restype = ctypes.c_long
        return bool(u.GetWindowLongW(info.hwndFocus, -16) & 0x20)  # GWL_STYLE & ES_PASSWORD
    except Exception:
        return None


def focused_is_password() -> bool | None:
    """True for a password field, False for an ordinary one, None when it can't be told."""
    if not IS_WINDOWS:
        return None
    win32 = _focused_is_password_win32()  # pragma: no cover - Windows only
    if win32:  # pragma: no cover
        return True
    uia = _focused_is_password_uia()  # pragma: no cover
    return uia if uia is not None else win32  # pragma: no cover


def guard(window, is_password: bool | None = None, extra_apps: set[str] | None = None) -> str | None:
    """Why typing or reading here is refused (a password manager, a sign-in prompt, a password field), or None."""
    app = (getattr(window, "app", "") or "").lower()
    title = getattr(window, "title", "") or ""
    if app in PASSWORD_APPS | (extra_apps or set()):
        return f"{app} is a password app"
    if is_password:
        return "that's a password field"
    if PASSWORD_TITLES.search(title):
        return "that window asks for a password or a sign-in"
    return None


def same_window(a, b) -> bool:
    if a is None or b is None:
        return a is b
    ha, hb = getattr(a, "handle", 0), getattr(b, "handle", 0)
    if ha and hb:
        return ha == hb
    return (getattr(a, "app", ""), getattr(a, "pid", 0)) == (getattr(b, "app", ""), getattr(b, "pid", 0))


# ---- through the clipboard ----------------------------------------------------------------------------------------

def _wait_change(board, before_seq, sentinel, timeout: float, sleep, clock) -> str | None:
    end = clock() + timeout
    while True:
        seq = board.sequence()
        if before_seq is not None and seq is not None:
            if seq != before_seq:
                sleep(0.03)  # the copying app may still be writing other formats
                return board.read()
        else:
            text = board.read()
            if text != sentinel:
                return text
        if clock() >= end:
            return None
        sleep(0.02)


def read_selection(board, keys, *, timeout: float = 0.6, sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic) -> str | None:
    """The selected text in the focused app (Ctrl+C), with the clipboard put back as it was. None: nothing selected."""
    old = board.read()
    seq = board.sequence()
    sentinel = None
    if seq is None:  # no change counter (pyperclip): mark the clipboard so a copy shows
        sentinel = f"vesper-{uuid.uuid4().hex}"
        board.write(sentinel)
    try:
        if not keys.chord("ctrl", "c"):
            return None
        text = _wait_change(board, seq, sentinel, timeout, sleep, clock)
    finally:
        if old is not None:
            board.write(old)
        elif sentinel is not None:
            board.write("")
    return text if text and text.strip() and text != sentinel else None


def paste(board, keys, text: str, *, settle: float = 0.35, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Put ``text`` in at the cursor (replacing any selection) through the clipboard, then restore the clipboard."""
    old = board.read()
    if not board.write(text):
        return False
    try:
        sleep(0.03)
        ok = keys.chord("ctrl", "v")
        sleep(settle)  # the app reads the clipboard after the key arrives: don't swap it back too soon
    finally:
        if old is not None:
            board.write(old)
    return ok


def focus(window) -> bool:
    """Bring a window back to the front (after a HUD click took focus)."""
    handle = getattr(window, "handle", 0)
    if not handle:
        return False
    try:
        from . import desktop

        fn = getattr(desktop, "_focus", None)
        return bool(fn(handle)) if fn else False
    except Exception:
        log.debug("focus failed", exc_info=True)
        return False
