"""Dictation into any app: hold Ctrl+Alt+D, talk, let go, and clean text appears where your cursor is.

Also by voice: "Vesper, dictate" (the next thing you say is typed) or "Vesper, dictate: see you at eight".
What you say is cleaned up (spoken "comma", "period", "new line"; "um" and "uh" dropped; capitals), and longer
dictations get a light edit from the model when one is running. It's typed with Unicode key presses (or pasted, for
long text, with your clipboard put back afterwards). Never into a password manager, a sign-in prompt or a password
field. The HUD shows a small chip while you dictate and keeps the last few dictations with a Copy button (in memory
only: dictations aren't logged).

Settings (config.yaml, skills.dictation): hotkey (default ctrl+alt+d; "" turns it off), polish (true),
end_period (true), type_max_chars (400: longer is pasted), max_seconds (120).
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from ..integrations import textio
from ..voice import dictate
from . import Skill

log = logging.getLogger(__name__)

KEEP = 8
AWAIT_S = 12.0  # "Vesper, dictate": how long the next utterance counts as dictation
CANCEL = re.compile(r"(?i)^\W*(?:cancel|stop|never ?mind|forget it|stop dictation|no)\W*$")
INLINE = re.compile(r"(?is)^\s*(?:please\s+)?dictate\s*[:,]?\s+(.+)$")


class Dictation(Skill):
    name, title = "dictation", "Dictation"

    def __init__(self, svc, keys=None, board=None, window=None, password=None, clock=time.time, capture=None,
                 transcribe=None, threads: bool = True):
        super().__init__(svc)
        self.keys = keys if keys is not None else textio.default_keys()
        if board is None:
            from .clipboard import default_clipboard

            board = default_clipboard()
        self.board = board
        if window is None:
            from ..integrations import desktop

            window = desktop.active_window
        self.window, self.password = window, password or textio.focused_is_password
        self.clock, self.threads = clock, threads
        self._capture_factory = capture  # () -> Capture-like (tests)
        self._transcribe = transcribe  # samples -> text (tests); else the listener's engine
        self.listener = self.speaker = self.runtime = None
        self.hold = None
        self.hotkey_mode = "off"
        self.state = "idle"  # idle | recording | transcribing | typing
        self.session: dict | None = None
        self.awaiting: dict | None = None  # {"until": t, "target": window}
        self.recent: list[dict] = []
        self.note = ""  # the last thing that went wrong, for the HUD
        self._lock = threading.RLock()
        self._stt = None

    # ---- wiring ------------------------------------------------------------------------------------------------------
    def start(self, runtime) -> None:
        self.runtime = runtime
        self.listener = getattr(runtime, "listener", None)
        self.speaker = getattr(runtime, "speaker", None)
        combo = str(self.setting("hotkey", "ctrl+alt+d") or "").strip()
        if combo:
            from ..voice.holdkey import HoldKey

            try:
                self.hold = HoldKey(combo, self.key_down, self.key_up)
                self.hotkey_mode = self.hold.start()
            except ValueError as exc:
                self.note = f"Dictation hotkey: {exc}"
        taps = getattr(runtime, "voice_taps", None)
        if taps is not None:
            taps.append(self.tap)
        self.publish()

    def _set(self, state: str) -> None:
        self.state = state
        self.publish()

    def _run(self, fn, *args) -> None:
        if self.threads:
            threading.Thread(target=fn, args=args, name="dictation", daemon=True).start()
        else:
            fn(*args)

    # ---- where the text goes ------------------------------------------------------------------------------------------
    def _front(self):
        try:
            return self.window()
        except Exception:
            return None

    def _refused(self, win) -> str | None:
        """Why we won't type here (or None)."""
        if win is None:
            return None
        title = getattr(win, "title", "") or ""
        name = str(((self.svc.cfg.get("assistant") or {}).get("name")) or "Vesper")
        if title.endswith(f"{name} HUD") or title.startswith(f"{name} HUD"):
            return "that's my own HUD: click into the app you want to type in first"
        try:
            pw = self.password()
        except Exception:
            pw = None
        why = textio.guard(win, pw, {a.lower() for a in self.setting("deny_apps", []) or []})
        return f"{why}, so I don't type there" if why else None

    # ---- hold the hotkey ----------------------------------------------------------------------------------------------
    def key_down(self) -> None:
        with self._lock:
            if self.state != "idle":
                return
            win = self._front()
            why = self._refused(win)
            if why:
                self.note = f"Dictation is off here: {why}."
                self.notify(self.note, level="warn", key="dictation:refused")
                self.publish()
                return
            if self.speaker is not None and self.speaker.speaking.is_set():
                self.speaker.interrupt()  # you're talking now: Vesper stops
            cap = self._capture_factory() if self._capture_factory else dictate.Capture(
                self.listener, device=(self.svc.cfg.get("voice") or {}).get("input_device"))
            try:
                cap.start()
            except Exception as exc:
                self.note = f"The microphone didn't start: {exc}"
                self.notify(self.note, level="warn", key="dictation:mic")
                self.publish()
                return
            sid = uuid.uuid4().hex[:8]
            self.session = {"id": sid, "capture": cap, "target": win, "at": self.clock()}
            self.note = ""
            self._set("recording")
        limit = float(self.setting("max_seconds", 120))
        if self.threads:
            threading.Timer(limit, lambda: self._timeout(sid)).start()

    def _timeout(self, sid: str) -> None:
        if self.session and self.session["id"] == sid and self.state == "recording":
            self.key_up()

    def key_up(self) -> None:
        with self._lock:
            sess = self.session
            if self.state != "recording" or sess is None:
                return
            samples = sess["capture"].stop()
            self._set("transcribing")
        try:
            text = self._to_text(samples)
        except Exception as exc:
            log.exception("dictation: speech-to-text failed")
            text, self.note = "", f"Speech-to-text failed: {type(exc).__name__}"
        if not text.strip():
            if not self.note:
                self.note = "I didn't catch anything." if len(samples) >= 0.4 * dictate.SAMPLE_RATE else \
                    "That was too short: hold the keys while you talk."
            self.session = None
            self._set("idle")
            return
        self.insert(text, sess["target"], via="hotkey")

    def _to_text(self, samples) -> str:
        if len(samples) < 0.4 * dictate.SAMPLE_RATE:
            return ""
        parts = [self._stt_text(c) for c in dictate.chunks(samples)]
        return " ".join(p.strip() for p in parts if p and p.strip())

    def _stt_text(self, samples) -> str:
        if self._transcribe is not None:
            return self._transcribe(samples)
        if self.listener is not None and hasattr(self.listener, "transcribe"):
            return self.listener.transcribe(samples)
        if self._stt is None:  # voice listening is off: load the engine ourselves
            from ..voice import stt

            self._stt = stt.load(self.svc.cfg.get("voice") or {}, self.svc.cfg.data_dir / "models")
        return self._stt.transcribe(samples)

    # ---- tidy, then type --------------------------------------------------------------------------------------------
    def prepare(self, raw: str) -> tuple[str, bool]:
        """(text to type, whether the model edited it)."""
        text = dictate.clean(raw, end_period=bool(self.setting("end_period", True)))
        if self.setting("polish", True) and len(text.split()) > 12:
            better = dictate.polish(text, self.svc.think, timeout=float(self.setting("polish_timeout_s", 4)))
            if better:
                return better, better != text
        return text, False

    def insert(self, raw: str, target, via: str) -> dict:
        """Clean ``raw`` up and type it into ``target`` (if it's still in front and safe to type into)."""
        with self._lock:
            self._set("typing")
            text, polished = self.prepare(raw)
            front = self._front()
            why = self._refused(front)
            how, note = "typed", ""
            if why:
                how, note = "kept", f"Not typed: {why}."
            elif target is not None and not textio.same_window(front, target):
                how, note = "kept", "Not typed: the window changed while you talked. Copy it from the HUD."
            elif self.keys is None:
                how, note = "kept", "I can't type on this PC; copy it from the HUD."
            else:
                if not textio.wait_modifiers_released(self.keys):
                    how, note = "kept", "Not typed: let go of Ctrl and Alt first."
                else:
                    prev = self.recent[0] if self.recent else None
                    lead = " " if (prev and prev["how"] in ("typed", "pasted") and textio.same_window(prev.get("_win"), front)
                                   and self.clock() - prev["at"] < 120 and not prev["text"].endswith("\n")
                                   and not text.startswith("\n")) else ""
                    ok, how = self._put(lead + text)
                    if not ok:
                        how, note = "kept", "Windows blocked the typing (an app running as administrator?). Copy it from the HUD."
            item = {"id": uuid.uuid4().hex[:8], "text": text, "at": self.clock(), "app": getattr(front, "app", "") or "",
                    "how": how, "via": via, "polished": polished, "chars": len(text), "_win": front}
            self.recent = [item, *self.recent][:KEEP]
            self.note = note
            self.session = None
            if note:
                self.notify(note, level="warn", key="dictation:kept")
            self._set("idle")
            return {"ok": how != "kept", "how": how, "text": text, "note": note}

    def _put(self, text: str) -> tuple[bool, str]:
        if len(text) > int(self.setting("type_max_chars", 400)) and self.board is not None:
            return textio.paste(self.board, self.keys, text), "pasted"
        return bool(self.keys.type_text(text)), "typed"

    # ---- "Vesper, dictate" --------------------------------------------------------------------------------------------
    def dictate_next(self, text: str = "") -> dict:
        win = self._front()
        why = self._refused(win)
        if why:
            return {"ok": False, "error": f"I won't dictate there: {why}."}
        if text.strip():
            return self.insert(text, win, via="voice")
        self.awaiting = {"until": self.clock() + AWAIT_S, "target": win}
        if self.listener is not None and hasattr(self.listener, "arm"):
            self.listener.arm(AWAIT_S, source="dictate")  # the next thing you say needs no wake word
        self.publish()
        return {"ok": True, "waiting": True, "app": getattr(win, "app", "") or ""}

    def tap(self, text: str, owner: str = "unknown"):
        """Voice commands on their way to the assistant: after "dictate", the next one is the dictation;
        "Vesper, dictate: <words>" is typed as said (with its capitals)."""
        aw = self.awaiting
        if aw is not None and self.clock() <= aw["until"]:
            self.awaiting = None
            if owner == "mismatch":
                self.svc.bus.publish("heard", {"text": text, "ignored": "not your voice (dictation)"})
                self.publish()
                return True
            if CANCEL.match(text):
                self.publish()
                if self.speaker is not None:
                    self.speaker.say("Okay.", expects_reply=False)
                return True
            self._run(self.insert, text, aw["target"], "voice")
            return True
        if aw is not None:
            self.awaiting = None
        m = INLINE.match(text or "")
        if m and owner != "mismatch":
            self._run(self.dictate_next, m.group(1))
            return True
        return None

    # ---- skill surface ------------------------------------------------------------------------------------------------
    def tools(self):
        return [Tool("dictate", "Type what the user says next into the app they're in (or `text` right away). "
                                "Use for 'dictate', 'take dictation', 'type what I say'.",
                     obj({"text": {"type": "string"}}), self.dictate_next, tier=1)]

    def route(self, t: str):
        if re.fullmatch(r"(?:start |begin )?(?:dictate|dictation|dictating)(?: (?:this|something|a message|mode))?"
                        r"|take (?:a )?dictation|type (?:what i say|this for me|for me)|(?:can you )?type what i'?m saying", t):
            return Intent("tool", "dictate", {})
        return None

    def say(self, tool, args, result):
        if tool != "dictate":
            return None
        if result.get("waiting"):
            return "Go ahead."
        return None if result.get("ok") is False else ("Typed." if result.get("how") == "typed" else "Done.")

    def status(self) -> dict:
        combo = str(self.setting("hotkey", "ctrl+alt+d") or "")
        return {"state": self.state, "hotkey": combo, "mode": self.hotkey_mode, "available": self.keys is not None,
                "awaiting": bool(self.awaiting and self.clock() <= self.awaiting["until"]), "note": self.note,
                "polish": bool(self.setting("polish", True)),
                "recent": [{k: v for k, v in i.items() if not k.startswith("_")} for i in self.recent]}

    def actions(self):
        return {"copy": self._copy, "clear": self._clear}

    def _copy(self, body: dict) -> dict:
        item = next((i for i in self.recent if i["id"] == body.get("id")), self.recent[0] if self.recent else None)
        if item is None:
            return {"ok": False, "error": "Nothing dictated yet."}
        if self.board is None:
            return {"ok": False, "error": "I can't reach the clipboard on this PC."}
        if not self.board.write(item["text"]):
            return {"ok": False, "error": "Another app is holding the clipboard; try again in a moment."}
        return {"ok": True, "chars": item["chars"]}

    def _clear(self, _body: dict) -> dict:
        self.recent, self.note = [], ""
        return {"ok": True}
