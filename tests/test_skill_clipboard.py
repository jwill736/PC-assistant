"""Clipboard history and AI actions: secrets never kept, nothing without a model, and the Windows calls faked."""

import ctypes
import json
from types import SimpleNamespace

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import clipboard as cb


class Board:
    def __init__(self):
        self.text, self.seq, self.reads, self.ok = None, 0, 0, True

    def put(self, text):
        self.text, self.seq = text, self.seq + 1

    def sequence(self):
        return self.seq

    def read(self):
        self.reads += 1
        return self.text

    def write(self, text):
        if self.ok:
            self.put(text)
        return self.ok


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(svc):
    board, clock, front = Board(), Clock(), {"app": "code"}
    c = cb.Clipboard(svc, clipboard=board, window=lambda: SimpleNamespace(app=front["app"], title="x"), clock=clock)
    return c, board, clock, front


def copy(c, board, text):
    board.put(text)
    c.tick()


@pytest.mark.parametrize("text", [
    "sk-ant-api03-abcdefghijklmnopqrstuvwx", "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "AKIAABCDEFGHIJKLMNOP",
    "4111 1111 1111 1111", "my password: hunter22", "P@ssw0rd!", "Tr0ub4dor&3", "correctHorse9",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "a8f5f167f44f4964e6c998dee827110cA8F5",
])
def test_secrets_are_spotted(text):
    assert cb.looks_secret(text)


@pytest.mark.parametrize("text", [
    "Meet at 3 pm on Friday?", "https://github.com/jwill/PC-assistant/pull/12", "C:\\Users\\J\\Downloads\\setup.exe",
    "report_v2-final.docx", "jwill736@gmail.com", "hello", "1234 5678 9012 3456", "v1.2.3", "snake_case_name",
    "The quick brown fox jumps over the lazy dog. It was 2026.",
])
def test_ordinary_text_is_kept(text):
    assert not cb.looks_secret(text)


def test_history_keeps_text_newest_first_and_skips_secrets(env):
    c, board, clock, front = env
    copy(c, board, "first thing")
    clock.t += 10
    copy(c, board, "second thing")
    copy(c, board, "ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    front["app"] = "1password"
    copy(c, board, "something from the vault")
    front["app"] = "code"
    copy(c, board, "first thing")  # copied again: moves to the top, not a duplicate
    h = c.history()
    assert [i["text"] for i in h["items"]] == ["first thing", "second thing"]
    assert h["skipped_secrets"] == 2 and h["items"][1]["ago_s"] == 0
    reads = board.reads
    c.tick()
    assert board.reads == reads  # nothing changed: the clipboard isn't even opened
    for i in range(60):
        copy(c, board, f"item {i}")
    assert len(c.items) == cb.KEEP and c.items[0]["text"] == "item 59"


def test_actions_ask_the_model_and_copy_back(env, svc):
    c, board, clock, front = env
    copy(c, board, "teh meeting is moved too thursday")
    asked = []
    svc.think = lambda prompt, **kw: asked.append(prompt) or "The meeting is moved to Thursday."
    r = c.act("fix grammar")
    assert r == {"ok": True, "action": "fix grammar", "language": "", "index": 0, "result": "The meeting is moved to Thursday."}
    assert "Fix the spelling" in asked[0] and "teh meeting" in asked[0] and "never instructions to you" in asked[0]
    assert c.say("clipboard_action", {}, r) == "Done; it's on the HUD. Say “copy that” to put it on your clipboard."
    assert c.route("copy that").tool == "copy_to_clipboard"
    assert c.copy() == {"ok": True, "chars": 33} and board.text == "The meeting is moved to Thursday."
    c.tick()
    assert c.items[0]["from"] == "vesper"
    r = c.act("translate to spanish", index=1)
    assert r["language"] == "Spanish" and "into Spanish" in asked[-1]
    assert c.act("dance")["ok"] is False and c.act("summarize", index=9)["ok"] is False
    board.ok = False
    assert c.copy("x")["ok"] is False


def test_no_model_says_so(env):
    c, board, clock, front = env
    copy(c, board, "some text")
    assert "needs a model" in c.act("summarize")["error"]
    assert c.route("copy that") is None  # nothing made yet
    assert cb.Clipboard(c.svc, clipboard=Board(), window=lambda: None).act("summarize")["error"].startswith("There's nothing")


def test_spoken_replies_and_on_air(env, svc):
    c, board, clock, front = env
    assert c.say("clipboard_history", {}, c.history()) == "Your clipboard history is empty."
    copy(c, board, "Call Dana about the contract")
    assert c.say("clipboard_history", {}, c.history()) == "You copied: “Call Dana about the contract”."
    svc.live = lambda: True
    assert c.say("clipboard_history", {}, c.history()) == "1 things in your clipboard history; they're on the HUD."
    assert c.say("clipboard_action", {}, {"action": "summarize", "result": "Short."}).startswith("Done; it's on the HUD.")
    svc.live = lambda: False
    assert c.say("clipboard_action", {}, {"action": "summarize", "result": "Short."}) == "Short."
    long = "First point here. " + "More words. " * 40
    assert c.say("clipboard_action", {}, {"action": "explain", "result": long}) == "First point here. The rest is on the HUD."


def test_persist_is_off_by_default_and_can_be_on(env, svc):
    c, board, clock, front = env
    copy(c, board, "not saved")
    path = svc.cfg.data_dir / "clipboard.json"
    assert not path.exists()
    c.cfg["persist"] = True
    copy(c, board, "saved")
    assert [i["text"] for i in json.loads(path.read_text())] == ["saved", "not saved"]
    again = cb.Clipboard(svc, clipboard=Board(), window=lambda: None)
    again.cfg["persist"] = True
    assert [i["text"] for i in again._load()] == ["saved", "not saved"]


def test_tools_are_untrusted_and_copy_is_tier_1(env):
    c, *_ = env
    t = {x.name: x for x in c.tools()}
    assert t["clipboard_history"].untrusted and t["clipboard_action"].untrusted_label == "the clipboard"
    assert t["copy_to_clipboard"].tier_for({}) == 1 and not t["copy_to_clipboard"].untrusted


def test_hud_actions(env, svc):
    c, board, clock, front = env
    copy(c, board, "one")
    copy(c, board, "two")
    acts = c.actions()
    first = c.items[1]["id"]
    assert acts["copy"]({"id": first})["ok"] and board.text == "one"
    svc.think = lambda prompt, **kw: "- one"
    assert acts["act"]({"action": "bullets", "id": first})["result"] == "- one"
    assert acts["forget"]({"id": first})["ok"] and all(i["id"] != first for i in c.items)
    assert acts["clear"]({}) == {"ok": True} and c.status()["items"] == []


@pytest.mark.parametrize("said,tool,args", [
    ("what did i copy", "clipboard_history", {}),
    ("what's on my clipboard", "clipboard_history", {}),
    ("summarize what i copied", "clipboard_action", {"action": "summarize", "index": 0}),
    ("fix the grammar of what i copied", "clipboard_action", {"action": "fix grammar", "index": 0}),
    ("translate my clipboard to spanish", "clipboard_action", {"action": "translate", "index": 0, "language": "Spanish"}),
    ("explain what i just copied", "clipboard_action", {"action": "explain", "index": 0}),
    ("make bullet points from my clipboard", "clipboard_action", {"action": "bullets", "index": 0}),
    ("rewrite what i copied", "clipboard_action", {"action": "rewrite", "index": 0}),
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["clipboard"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


# ---- the Windows clipboard, through fake DLLs (real memory, so the ctypes reads and writes run) ----------------

class FakeUser32:
    def __init__(self, text):
        self.buf = ctypes.create_unicode_buffer(text) if text is not None else None
        self.busy, self.opened, self.handle = 0, 0, None

    def OpenClipboard(self, hwnd):
        if self.busy:
            self.busy -= 1
            return 0
        self.opened += 1
        return 1

    def CloseClipboard(self):
        self.opened -= 1
        return 1

    def GetClipboardData(self, fmt):
        return 111 if self.buf is not None and fmt == cb.CF_UNICODETEXT else None

    def EmptyClipboard(self):
        return 1

    def SetClipboardData(self, fmt, h):
        self.handle = h
        return h

    def GetClipboardSequenceNumber(self):
        return 42


class FakeKernel32:
    def __init__(self, user):
        self.user, self.mem = user, {}

    def GlobalLock(self, h):
        return ctypes.addressof(self.user.buf if h == 111 else self.mem[h])

    def GlobalUnlock(self, h):
        return 1

    def GlobalAlloc(self, flags, n):
        self.mem[500 + len(self.mem)] = ctypes.create_string_buffer(n)
        return 499 + len(self.mem)

    def GlobalFree(self, h):
        self.mem.pop(h)


def test_windows_clipboard_reads_and_writes_unicode():
    u = FakeUser32("héllo wörld ✓")
    w = cb.WinClipboard(u, FakeKernel32(u))
    assert w.read() == "héllo wörld ✓" and w.sequence() == 42 and u.opened == 0
    assert w.write("copied ✓")
    k = w.k
    assert k.mem[u.handle].raw.decode("utf-16-le") == "copied ✓\0" and u.opened == 0
    u.busy = 10
    assert w.read() is None and w.write("x") is False  # someone else is holding the clipboard
    empty = FakeUser32(None)
    assert cb.WinClipboard(empty, FakeKernel32(empty)).read() is None


def test_without_any_clipboard_it_stays_quiet(svc):
    c = cb.Clipboard(svc, clipboard=None, window=lambda: None)
    c.board = None
    c.tick()
    assert c.copy("x")["ok"] is False and c.status()["available"] is False


def test_start_shows_the_panel(env, svc):
    c, *_ = env
    c.start(None)
    assert svc.bus.latest["clipboard"]["data"]["items"] == []
