"""Speech output.

Engines:
* ``pyttsx3`` — offline Windows SAPI voices, speaks even with the dashboard closed.
* ``browser`` — the dashboard speaks via the Web Speech API; Edge/Chrome ship far
  more natural voices (e.g. "Microsoft Ryan Online (Natural)").
* ``none``    — silent; replies still show on the dashboard.
"""

from __future__ import annotations

import logging
import queue
import re
import sys
import threading
import time

from ..bus import EventBus

log = logging.getLogger(__name__)


class Speaker:
    def __init__(self, bus: EventBus, engine: str = "pyttsx3", rate: int = 190, voice_hint: str = ""):
        self.bus = bus
        self.engine_name = engine
        self.rate, self.voice_hint = rate, voice_hint
        self.speaking = threading.Event()
        self.last_end = 0.0
        self.last_text = ""  # what we said last, so the mic can ignore hearing itself
        self.expects_reply = False  # did the last reply ask something? (opens the follow-up window)
        self._q: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._browser_done = threading.Event()
        if engine == "pyttsx3" and not _offline_tts_available():
            log.warning("No offline speech engine (pyttsx3 / pywin32); falling back to browser speech")
            self.engine_name = "browser"

    def start(self) -> None:
        if self._thread is None and self.engine_name != "none":
            self.restart()

    def restart(self) -> threading.Thread | None:
        if self.engine_name == "none":
            return None
        self.speaking.clear()
        self._thread = threading.Thread(target=self._worker, name="tts", daemon=True)
        self._thread.start()
        return self._thread

    def say(self, text: str, expects_reply: bool | None = None) -> None:
        text = clean_for_speech(text)
        if text and self.engine_name != "none":
            # Flag immediately so the mic ignores us before the worker even starts talking.
            self.speaking.set()
            self.last_text = text
            self.expects_reply = asks_something(text) if expects_reply is None else expects_reply
            self._q.put(text)

    def browser_finished(self) -> None:
        self._browser_done.set()

    def chime(self) -> None:
        """Short acknowledgement tone when the wake word is heard."""
        self.bus.publish("wake", {})
        if sys.platform == "win32":
            try:
                import winsound

                winsound.Beep(988, 90)
            except Exception:
                pass

    def stop(self) -> None:
        self._q.put(None)

    # ------------------------------------------------------------------
    def _worker(self) -> None:
        engine = self._init_pyttsx3() if self.engine_name == "pyttsx3" else None
        while True:
            text = self._q.get()
            if text is None:
                return
            self.speaking.set()
            self.bus.publish("speaking", {"active": True})
            try:
                if engine is not None:
                    try:
                        engine.say(text)
                        engine.runAndWait()
                    except Exception:
                        log.exception("pyttsx3 failed; re-initialising")
                        engine = self._init_pyttsx3()
                else:
                    self._speak_in_browser(text)
            finally:
                self.last_end = time.time()
                if self._q.empty():
                    self.speaking.clear()
                    self.bus.publish("speaking", {"active": False})

    def _speak_in_browser(self, text: str) -> None:
        if self.bus.client_count == 0:
            return
        self._browser_done.clear()
        self.bus.publish("speak", {"text": text, "voice_hint": self.voice_hint, "rate": self.rate / 190})
        # Wait for the dashboard to report it finished; estimate if it never does.
        self._browser_done.wait(timeout=2 + len(text.split()) / 2.4)

    def _init_pyttsx3(self):
        if sys.platform == "win32":
            sapi = _SapiVoice.create(self.rate, self.voice_hint)
            if sapi is not None:
                return sapi
            try:  # SAPI is COM; this thread needs its own apartment.
                import pythoncom

                pythoncom.CoInitialize()
            except ImportError:
                try:
                    import comtypes

                    comtypes.CoInitialize()
                except Exception:
                    pass
        try:
            import pyttsx3

            engine = pyttsx3.init()
            engine.setProperty("rate", self.rate)
            if self.voice_hint:
                for v in engine.getProperty("voices"):
                    if self.voice_hint.lower() in (v.name or "").lower():
                        engine.setProperty("voice", v.id)
                        break
            return engine
        except Exception:
            log.exception("pyttsx3 unavailable; using browser speech")
            self.engine_name = "browser"
            return None


_MD = [(re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),     # [text](url) -> text
       (re.compile(r"https?://\S+"), "the link"),
       (re.compile(r"[*_`#>]+"), ""),                           # markdown emphasis, code, headings, quotes
       (re.compile(r"^\s*[-•]\s+", re.M), ""),                  # list bullets
       (re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]"), ""),  # emoji and symbols
       (re.compile(r"\s+"), " ")]


def clean_for_speech(text: str) -> str:
    """Strip what reads fine on screen but sounds wrong aloud (markdown, links, emoji)."""
    text = text or ""
    for pattern, repl in _MD:
        text = pattern.sub(repl, text)
    return text.strip()


def asks_something(text: str) -> bool:
    """Should the mic stay open for an answer without the wake word?"""
    tail = text.strip()[-160:].lower()
    return tail.endswith("?") or bool(re.search(r"\bsay yes\b|\bconfirm\b|\byes or no\b", tail))


def _offline_tts_available() -> bool:
    for module in ("win32com.client", "pyttsx3") if sys.platform == "win32" else ("pyttsx3",):
        try:
            __import__(module)
            return True
        except ImportError:
            continue
    return False


class _SapiVoice:
    """Windows SAPI called directly. pyttsx3's runAndWait() can hang on the second
    call from a worker thread; SpVoice.Speak() is synchronous and doesn't."""

    def __init__(self, voice):
        self.voice = voice

    @classmethod
    def create(cls, rate: int, hint: str):
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            voice = win32com.client.Dispatch("SAPI.SpVoice")
        except Exception:
            return None
        voice.Rate = max(-10, min(10, round((rate - 180) / 20)))  # SAPI rate is -10..10, 0 ~ 180 wpm
        if hint:
            tokens = voice.GetVoices()
            for i in range(tokens.Count):
                if hint.lower() in tokens.Item(i).GetDescription().lower():
                    voice.Voice = tokens.Item(i)
                    break
        return cls(voice)

    def say(self, text: str) -> None:
        self.voice.Speak(text)

    def runAndWait(self) -> None:  # matches the pyttsx3 call sequence in the worker
        pass
