"""Typing into the focused app and reading the selection: the Windows calls faked, the clipboard put back, and
never into a password field. Plus the hold-to-talk key combination."""

import ctypes
from types import SimpleNamespace

import pytest

from assistant.integrations import textio
from assistant.voice import holdkey


class User32:
    """SendInput and GetAsyncKeyState, recording what would have been typed."""

    def __init__(self, held=()):
        self.sent, self.calls, self.held, self.accept = [], [], set(held), True

    def SendInput(self, n, arr, size):
        assert size == ctypes.sizeof(textio.INPUT)
        self.calls.append(n)
        self.sent += [(arr[i].u.ki.wVk, arr[i].u.ki.wScan, arr[i].u.ki.dwFlags) for i in range(n)]
        return n if self.accept else 0

    def GetAsyncKeyState(self, vk):
        return -32768 if vk in self.held else 0


def typed(sent):
    """Reassemble the text from the key-down Unicode events (Shift+Enter as a new line)."""
    out, units = [], []
    for vk, scan, flags in sent:
        if flags & textio.KEYEVENTF_KEYUP:
            continue
        if flags & textio.KEYEVENTF_UNICODE:
            units.append(scan)
        elif vk == textio.VK["enter"]:
            out.append(b"".join(u.to_bytes(2, "little") for u in units).decode("utf-16-le"))
            units = []
    out.append(b"".join(u.to_bytes(2, "little") for u in units).decode("utf-16-le"))
    return "\n".join(out)


def test_unicode_text_is_typed_key_by_key_with_new_lines_as_shift_enter():
    u = User32()
    keys = textio.WinKeys(user32=u, sleep=lambda s: None)
    text = "Café at 9 🎉\nSee you"
    assert keys.type_text(text)
    assert typed(u.sent) == text
    downs = [s for s in u.sent if not s[2] & textio.KEYEVENTF_KEYUP]
    ups = [s for s in u.sent if s[2] & textio.KEYEVENTF_KEYUP]
    assert len(downs) == len(ups)  # nothing left held down
    enter = u.sent.index((textio.VK["shift"], 0, 0))
    assert u.sent[enter + 1][0] == textio.VK["enter"]  # Shift+Enter: Discord and Slack don't send the message


def test_long_text_goes_in_batches_and_a_blocked_send_is_reported():
    u = User32()
    keys = textio.WinKeys(user32=u, sleep=lambda s: None)
    assert keys.type_text("x" * 200)
    assert max(u.calls) <= textio.CHUNK and sum(u.calls) == 400
    u.accept = False  # an elevated window (UIPI) swallows synthetic input
    assert keys.type_text("hi") is False


def test_chord_and_held_modifiers():
    u = User32(held={0x11})
    keys = textio.WinKeys(user32=u, sleep=lambda s: None)
    keys.chord("ctrl", "v")
    assert u.sent == [(0x11, 0, 0), (0x56, 0, 0), (0x56, 0, 2), (0x11, 0, 2)]
    assert keys.modifiers_down()
    t = [0.0]

    def tick(s):
        t[0] += s
    assert textio.wait_modifiers_released(keys, timeout=0.1, sleep=tick, clock=lambda: t[0]) is False
    u.held.clear()
    assert textio.wait_modifiers_released(keys, sleep=tick, clock=lambda: t[0]) is True


class Board:
    def __init__(self, text="what was there", seq=True):
        self.text, self.n, self.has_seq, self.writes = text, 0, seq, []

    def sequence(self):
        return self.n if self.has_seq else None

    def read(self):
        return self.text

    def write(self, text):
        self.text, self.n = text, self.n + 1
        self.writes.append(text)
        return True


class Keys:
    def __init__(self, board=None, selection=None):
        self.board, self.selection, self.chords, self.typed = board, selection, [], []

    def chord(self, *names):
        self.chords.append(names)
        if names == ("ctrl", "c") and self.selection is not None:
            self.board.write(self.selection)  # the app copies what's selected
        if names == ("ctrl", "v"):
            self.typed.append(self.board.read())  # the app pastes the clipboard
        return True

    def type_text(self, text):
        self.typed.append(text)
        return True

    def modifiers_down(self):
        return False


@pytest.mark.parametrize("has_seq", [True, False])
def test_read_selection_copies_then_puts_the_clipboard_back(has_seq):
    board = Board(seq=has_seq)
    keys = Keys(board, selection="teh quick fox")
    assert textio.read_selection(board, keys, sleep=lambda s: None) == "teh quick fox"
    assert board.text == "what was there" and keys.chords == [("ctrl", "c")]


def test_nothing_selected_is_none_and_the_clipboard_is_untouched():
    board = Board()
    t = [0.0]
    out = textio.read_selection(board, Keys(board, selection=None), timeout=0.2,
                                sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert out is None and board.text == "what was there"


def test_paste_uses_the_clipboard_and_restores_it():
    board = Board()
    keys = Keys(board)
    assert textio.paste(board, keys, "new text", sleep=lambda s: None)
    assert keys.typed == ["new text"] and board.text == "what was there"


@pytest.mark.parametrize("win,pw,refused", [
    (SimpleNamespace(app="code", title="notes.md - Visual Studio Code"), False, False),
    (SimpleNamespace(app="code", title="login.ts - Visual Studio Code"), False, False),
    (SimpleNamespace(app="1password", title="1Password"), False, True),
    (SimpleNamespace(app="bitwarden", title="Bitwarden"), None, True),
    (SimpleNamespace(app="chrome", title="Sign in - Google Accounts - Google Chrome"), None, True),
    (SimpleNamespace(app="credentialuibroker", title="Windows Security"), None, True),
    (SimpleNamespace(app="chrome", title="Bank of Somewhere - Google Chrome"), True, True),
    (SimpleNamespace(app="discord", title="#general - Discord"), None, False),
])
def test_password_places_are_refused(win, pw, refused):
    assert bool(textio.guard(win, pw)) is refused


def test_same_window_compares_handles_then_process():
    a = SimpleNamespace(app="code", pid=1, handle=10)
    assert textio.same_window(a, SimpleNamespace(app="code", pid=1, handle=10))
    assert not textio.same_window(a, SimpleNamespace(app="code", pid=1, handle=11))
    assert textio.same_window(SimpleNamespace(app="x", pid=2, handle=0), SimpleNamespace(app="x", pid=2, handle=0))
    assert textio.same_window(None, None) and not textio.same_window(a, None)


def test_default_keys_never_crash_without_a_desktop():
    keys = textio.default_keys()
    assert keys is None or hasattr(keys, "type_text")


# ---- hold-to-talk -------------------------------------------------------------------------------------------------

def test_chord_fires_once_when_fully_held_and_once_when_let_go():
    c = holdkey.Chord("ctrl+alt+d")
    assert c.keys == {"ctrl", "alt", "d"}
    assert not c.press("ctrl") and not c.press("alt")
    assert c.press("d") is True
    assert not c.press("d")  # key repeat while held
    assert c.release("x") is False
    assert c.release("d") is True and not c.release("alt")
    assert c.press("d") is False  # Ctrl is still down but Alt came up


def test_key_names_fold_sides_and_read_letters_from_the_key_code():
    assert holdkey.key_name(SimpleNamespace(name="ctrl_l")) == "ctrl"
    assert holdkey.key_name(SimpleNamespace(name="alt_gr")) == "alt"
    assert holdkey.key_name(SimpleNamespace(name=None, vk=0x44, char="\x04")) == "d"  # Ctrl+D on Windows
    assert holdkey.key_name(SimpleNamespace(name=None, vk=None, char="D")) == "d"
    assert holdkey.key_name(SimpleNamespace(name="f9")) == "f9"
    with pytest.raises(ValueError):
        holdkey.parse("ctrl++")


def test_hold_key_without_a_desktop_falls_back_to_a_toggle(monkeypatch):
    calls = []
    hk = holdkey.HoldKey("ctrl+alt+d", lambda: calls.append("down"), lambda: calls.append("up"))
    monkeypatch.setattr(holdkey, "register_hotkey", lambda combo, cb: True)
    monkeypatch.setattr(holdkey.HoldKey, "_fire", staticmethod(lambda fn: fn()))
    import builtins

    real = builtins.__import__

    def no_pynput(name, *a, **kw):
        if name.startswith("pynput"):
            raise ImportError("no display")
        return real(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", no_pynput)
    assert hk.start() == "toggle"
    hk.toggle()
    hk.toggle()
    assert calls == ["down", "up"]
    hk._press("ctrl"), hk._press("alt"), hk._press("d"), hk._release("alt")
    assert calls == ["down", "up", "down", "up"]
