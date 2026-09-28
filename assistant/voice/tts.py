"""Speech output.

Engines:
* ``supertonic`` / ``kokoro`` - local neural voices (``neural.py``): natural,
  offline, stream sentence by sentence and stop mid-word when interrupted.
* ``pyttsx3`` - the legacy Windows SAPI voices; no download, speaks anywhere.
* ``browser`` - the dashboard speaks via the Web Speech API (needs the HUD open).
* ``none`` - silent; replies still show on the dashboard.
* ``auto`` (default) - Supertonic when sherpa-onnx is installed, else SAPI, else browser.

Every utterance belongs to a *turn*. A new command starts a new turn, so the
rest of a reply you've moved past is dropped instead of spoken late, and an
interrupt silences the turn it cut off, including sentences Claude is still
streaming.
"""

from __future__ import annotations

import logging
import queue
import re
import statistics
import sys
import threading
import time
from collections import deque
from pathlib import Path

from ..bus import EventBus
from . import neural

log = logging.getLogger(__name__)

PREVIEW_TEXT = "Good evening. Your stream starts in ten minutes, and the thumbnail still isn't done."


def resolve_engine(engine: str) -> str:
    engine = (engine or "auto").lower()
    if engine in ("none", "browser"):
        return engine
    if engine in neural.ENGINES or engine == "auto":
        if neural.available():
            return "supertonic" if engine == "auto" else engine
        if engine != "auto":
            log.warning("%s voice needs sherpa-onnx and sounddevice (pip install -r requirements-voice.txt)", engine)
    if _offline_tts_available():
        return "pyttsx3"
    if engine not in ("auto", "browser"):
        log.warning("No offline speech engine (pyttsx3 / pywin32); falling back to browser speech")
    return "browser"


class Speaker:
    def __init__(self, bus: EventBus, engine: str = "auto", rate: int = 190, voice_hint: str = "", *,
                 voice: str | None = None, speed: float = 1.0, threads: int = 2, models_dir: Path | None = None,
                 output_device=None, loader=None, audio_factory=None):
        self.bus = bus
        self.requested = engine
        self.engine_name = resolve_engine(engine)
        self.rate, self.voice_hint = rate, voice_hint
        self.voice_id, self.speed, self.threads = voice, speed, threads
        self.models_dir = models_dir
        self.output_device = output_device
        self._loader = loader or neural.load
        self._audio_factory = audio_factory or (lambda sr: neural.AudioOut(sr, device=self.output_device))
        self.neural: neural.NeuralVoice | None = None
        self.out: neural.AudioOut | None = None
        self.state, self.error = "idle", ""
        self.speaking = threading.Event()
        self.last_end = 0.0
        self.last_text = ""  # this turn's words, so the mic can ignore hearing itself
        self.expects_reply = False  # did the last reply ask something? (opens the follow-up window)
        self.turn = 0
        self._silenced = -1  # turns up to this one were interrupted: drop the rest of them
        self._text_turn = -1
        self._playing_turn = -1
        self._gen = 0  # bumped by configure(): a replaced worker must not install the model it was loading
        self.first_audio_ms: deque[int] = deque(maxlen=30)
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._browser_done = threading.Event()
        self._cancel = threading.Event()  # barge-in: stop the current reply now

    def start(self) -> None:
        if self._thread is None and self.engine_name != "none":
            self.restart()

    def restart(self) -> threading.Thread | None:
        """Start the worker, or return the live one. The watchdog calls this when the thread it
        last saw has ended, which a live voice switch (``configure``) also does on purpose."""
        if self.engine_name == "none":
            return None
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self.speaking.clear()
        self._thread = threading.Thread(target=self._worker, args=(self._gen,), name="tts", daemon=True)
        self._thread.start()
        return self._thread

    def configure(self, engine: str | None = None, voice: str | None = None, speed: float | None = None) -> dict:
        """Switch engine/voice/speed live (from the Setup tab)."""
        if engine is not None:
            self.requested = engine
        if voice is not None:
            self.voice_id = voice
        if speed is not None:
            self.speed = max(0.6, min(1.6, float(speed)))
        self.interrupt()
        wanted = resolve_engine(self.requested)
        if self.state == "loading" and wanted == self.engine_name:
            return self.status()  # still downloading this engine: the new voice/speed apply once it's loaded
        if self.neural is not None and wanted == self.neural.engine and self.engine_name == wanted:
            self.neural.voice = neural.voice(wanted, self.voice_id)  # same model: just another speaker id
            self.neural.speed = self.speed
            self._publish_status()
            return self.status()
        self.stop()
        self._gen += 1
        if self._thread is not None:
            self._thread.join(timeout=5)  # a download in progress finishes on its own and is discarded
        self._thread = None
        if self.out is not None:
            self.out.close()
        self.neural, self.out, self.error = None, None, ""
        self._q = queue.Queue()
        self.engine_name = wanted
        self.start()
        return self.status()

    def new_turn(self) -> int:
        """A new command: anything still queued from earlier turns is dropped."""
        self.turn += 1
        return self.turn

    def _stale(self, turn: int) -> bool:
        return turn <= self._silenced or turn < self.turn

    def say(self, text: str, expects_reply: bool | None = None, turn: int | None = None) -> None:
        text = clean_for_speech(text)
        if not text or self.engine_name == "none":
            return
        streamed = turn is not None  # one sentence of a reply that's still arriving
        if turn is None:
            if self.turn == self._silenced:  # the first thing said after an interrupt starts afresh
                self.new_turn()
            turn = self.turn
        if self._stale(turn):
            log.debug("dropped stale speech (turn %s < %s): %s", turn, self.turn, text[:60])
            return
        # Flag immediately so the mic ignores us before the worker even starts talking.
        self.speaking.set()
        self.last_text = f"{self.last_text} {text}"[-800:].strip() if streamed and turn == self._text_turn else text
        self._text_turn = turn
        self.expects_reply = asks_something(text) if expects_reply is None else expects_reply
        self._q.put((turn, text, time.perf_counter(), self.out is None or not self.out.playing))

    def preview(self, text: str | None = None) -> None:
        self.interrupt()
        self.say(text or PREVIEW_TEXT, expects_reply=False, turn=self.new_turn())

    def browser_finished(self) -> None:
        self._browser_done.set()

    def interrupt(self) -> bool:
        """Stop talking now and drop anything queued (you talked over it / said "stop").
        The rest of this turn stays silenced, including sentences Claude is still streaming."""
        self._silenced = self.turn
        if not self.speaking.is_set():
            return False
        self._cancel.set()
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is None:  # keep a pending shutdown request
                self._q.put(None)
                break
        if self.out is not None:
            self.out.abort()
        if self.engine_name == "browser":
            self.bus.publish("speak_stop", {})
            self._browser_done.set()
        self.bus.publish("interrupted", {"text": self.last_text})
        return True

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

    def status(self) -> dict:
        lat = list(self.first_audio_ms)
        engine = self.engine_name
        return {
            "engine": engine, "requested": self.requested, "state": self.state, "error": self.error,
            "voice": self.neural.voice.id if self.neural else (self.voice_hint if engine == "pyttsx3" else self.voice_id),
            "speed": self.speed,
            "first_audio_ms": round(statistics.median(lat)) if lat else None,
            "voices": {e: [{"id": v.id, "label": v.label} for v in vs] for e, vs in neural.VOICES.items()},
            "labels": {**neural.LABELS, "pyttsx3": "Windows voice (SAPI)", "browser": "Browser voice", "none": "Silent"},
            "neural_available": neural.available(),
        }

    def _publish_status(self) -> None:
        self.bus.publish("tts", self.status(), sticky=True)

    # ------------------------------------------------------------------
    def _worker(self, gen: int = 0) -> None:
        engine = None
        if self.engine_name in neural.ENGINES:
            self._init_neural(gen)
            if gen != self._gen:
                return
        if self.engine_name == "pyttsx3":
            engine = self._init_pyttsx3()
        if self.state in ("idle", "loading"):
            self.state = "ready"  # "fallback" stays visible in the HUD
        self._publish_status()
        q = self._q
        while True:
            try:  # poll while talking, to notice the end of playback or a newer turn
                item = q.get(timeout=0.03 if self.speaking.is_set() else 5)
            except queue.Empty:
                if self.speaking.is_set():
                    if self.out is not None and self.out.playing and self._stale(self._playing_turn):
                        self.out.abort()
                    self._finish_if_idle()
                elif self.out is not None:
                    self.out.maybe_close()
                continue
            if item is None:
                return
            turn, text, queued_at, waited = item
            if self._stale(turn):
                self._finish_if_idle()
                continue
            self._cancel.clear()
            self.speaking.set()
            self.bus.publish("speaking", {"active": True})
            try:
                if self.neural is not None:
                    self._speak_neural(turn, text, queued_at, waited)
                    continue
                if engine is not None:
                    try:
                        if isinstance(engine, _SapiVoice):
                            engine.say(text, cancel=self._cancel)
                        else:  # pyttsx3 can't be stopped mid-sentence from another thread
                            engine.say(text)
                            engine.runAndWait()
                    except Exception:
                        log.exception("pyttsx3 failed; re-initialising")
                        engine = self._init_pyttsx3()
                else:
                    self._speak_in_browser(text)
                self.last_end = time.time()
            finally:
                self._finish_if_idle()

    def _speak_neural(self, turn: int, text: str, queued_at: float, waited: bool) -> None:
        """Synthesise sentence by sentence; each plays while the next is made.
        Returns without waiting for playback, so the next queued text is synthesised meanwhile."""
        self._playing_turn = turn
        first = True
        for sentence in neural.split_sentences(text):
            if self._stale(turn) or self._cancel.is_set():
                break
            try:
                audio = self.neural.synth(sentence)
            except Exception:
                log.exception("speech synthesis failed for %r", sentence[:60])
                continue
            if self._stale(turn) or self._cancel.is_set():
                break
            if first and waited:  # someone was waiting for this: that's the latency that matters
                self.first_audio_ms.append(round((time.perf_counter() - queued_at) * 1000))
            first = False
            self.out.play(audio)

    def _finish_if_idle(self) -> None:
        if self.speaking.is_set() and self._q.empty() and (self.out is None or not self.out.playing):
            self.last_end = time.time()
            self.speaking.clear()
            self.bus.publish("speaking", {"active": False})

    def _init_neural(self, gen: int = 0) -> None:
        engine = self.engine_name
        self.state = "loading"
        self._publish_status()
        try:
            def progress(frac: float) -> None:
                self.bus.publish("tts", {**self.status(), "state": "loading", "progress": round(frac, 2)}, sticky=True)

            models_dir = self.models_dir or Path("data") / "models"
            loaded = self._loader(engine, models_dir, self.voice_id, self.speed, self.threads, on_progress=progress)
            took = loaded.warm_up()
            if gen != self._gen:  # switched to another engine while this one loaded
                return
            loaded.voice, loaded.speed = neural.voice(engine, self.voice_id), self.speed  # picked while loading
            self.neural, self.out = loaded, self._audio_factory(loaded.sample_rate)
            log.info("voice: %s (%s), warmed up in %.1fs", neural.LABELS[engine], loaded.voice.label, took)
        except Exception as exc:
            if gen != self._gen:
                return
            log.exception("%s voice failed to load", engine)
            self.neural, self.out = None, None
            self.error = f"{neural.LABELS.get(engine, engine)} failed to load: {exc}"
            self.engine_name = "pyttsx3" if _offline_tts_available() else "browser"
            self.state = "fallback"

    def _speak_in_browser(self, text: str) -> None:
        if self.bus.client_count == 0 or self._cancel.is_set():
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

    def say(self, text: str, cancel: threading.Event | None = None) -> None:
        if cancel is None:
            self.voice.Speak(text)
            return
        self.voice.Speak(text, 1)  # SVSFlagsAsync: return at once, poll so we can stop mid-sentence
        while not self.voice.WaitUntilDone(50):
            if cancel.is_set():
                self.voice.Speak("", 3)  # SVSFlagsAsync | SVSFPurgeBeforeSpeak: silence now
                break

    def runAndWait(self) -> None:  # matches the pyttsx3 call sequence in the worker
        pass
