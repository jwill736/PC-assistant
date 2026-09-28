"""Always-on voice input: mic -> Silero VAD -> speech-to-text -> wake word -> command.

The wake word is matched on the transcript, so it can be any name you choose
(no custom model training). After the assistant asks something, a short
follow-up window accepts the next sentence without the name. Push-to-talk
(hotkey or the dashboard mic button) skips the wake word for one utterance.
Speech-to-text is pluggable (stt.py); Parakeet 110M on CPU is the default.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
import math
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from ..bus import EventBus
from . import stt as stt_mod
from .tts import Speaker

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000

# Whisper's favourite things to "hear" in silence or fan noise.
HALLUCINATIONS = {"you", "thank you", "thanks for watching", "thank you for watching", "bye", "so", "okay", ""}
# Said over a reply, these stop it (and are not sent on as commands, except "cancel").
STOP_PHRASES = {"stop", "stop it", "stop talking", "ok stop", "okay stop", "cancel", "quiet", "be quiet", "shut up",
                "enough", "that's enough", "thats enough", "never mind", "nevermind", "hold on", "wait"}
# The kill switch by voice: distinctive enough to accept without the name (a false hit only pauses
# PC control; a missed one is the dangerous case).
KILL_PHRASES = {"stop everything", "kill switch", "emergency stop", "stop all actions"}
WAKE_ANCHOR_S = 2.0  # an acoustic wake-word hit counts only this soon after the utterance starts


def _norm(token: str) -> str:
    return re.sub(r"[^\w']", "", token.lower())


def _norm_sentence(text: str) -> str:
    return " ".join(_norm(t) for t in text.split() if _norm(t))


def strip_leading_name(transcript: str, name: str) -> str:
    """Drop a mangled name at the start ("Desperate, open Discord" -> "open Discord") once the
    acoustic model has already confirmed the name was said there."""
    tokens = re.findall(r"\S+", transcript)
    i = 1 if tokens and _norm(tokens[0]) in ("hey", "hi", "ok", "okay") else 0
    best, cut = 0.5, 0  # the name may come out as one word ("Desperate") or two ("Best per")
    for n in (1, 2):
        if i + n <= len(tokens):
            joined = "".join(_norm(t) for t in tokens[i:i + n]).removesuffix("'s")
            ratio = difflib.SequenceMatcher(None, joined, name.lower()).ratio()
            if ratio > best:
                best, cut = ratio, i + n
    return " ".join(tokens[cut:]).strip(" ,.!?;:-") if cut else transcript.strip()


def split_wake(transcript: str, wake_words: list[str], search_words: int = 4, cutoff: float = 0.8) -> tuple[bool, str]:
    """Find a wake word near the start of ``transcript``.

    Returns (matched, command_after_wake_word). Fuzzy so "Vesper," / "vesper's" /
    "Hey, Vesper" all match, and the command keeps its original casing. A fuzzy
    match must be nearly as long as the wake word, so a short everyday word that
    happens to share letters ("jars" for "Jarvis") never counts.
    """
    tokens = re.findall(r"\S+", transcript)
    normed = [_norm(t) for t in tokens]
    for wake in sorted(wake_words, key=lambda w: -len(w.split())):
        wake_tokens = [_norm(w) for w in wake.split()]
        n = len(wake_tokens)
        target = " ".join(wake_tokens)
        for i in range(0, max(0, min(search_words, len(tokens) - n + 1))):
            cand = " ".join(normed[i:i + n])
            if cand.endswith("'s"):
                cand = cand[:-2]
            close = (len(cand) >= 0.75 * len(target)
                     and difflib.SequenceMatcher(None, cand, target).ratio() >= cutoff)
            if cand == target or close:
                rest = " ".join(tokens[i + n:]).strip(" ,.!?;:-")
                return True, rest
    return False, transcript.strip()


class EnergySegmenter:
    """Adaptive-threshold voice activity detection over 30 ms int16 frames."""

    def __init__(self, min_rms: float = 350, silence_ms: int = 800, max_utterance_s: float = 15,
                 start_frames: int = 3, preroll_frames: int = 10, min_voiced_ms: int = 250):
        self.min_rms = min_rms
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.max_frames = int(max_utterance_s * 1000 / FRAME_MS)
        self.start_frames = start_frames
        self.min_voiced_frames = max(1, min_voiced_ms // FRAME_MS)
        self.noise = min_rms / 2
        self.preroll: deque = deque(maxlen=preroll_frames)
        self.reset()

    def reset(self) -> None:
        self.in_speech = False
        self.voiced = 0
        self.silent = 0
        self.voiced_total = 0
        self.buffer: list = []
        self.preroll.clear()

    @staticmethod
    def rms(frame) -> float:
        try:
            import numpy as np

            return float(np.sqrt(np.mean(np.asarray(frame, dtype=np.float32) ** 2)))
        except ImportError:  # tests without numpy
            return math.sqrt(sum(float(x) * float(x) for x in frame) / max(len(frame), 1))

    @property
    def threshold(self) -> float:
        return max(self.min_rms, self.noise * 2.5)

    def feed(self, frame) -> list | None:
        """Feed one frame; returns the finished utterance's frames when speech ends."""
        level = self.rms(frame)
        if not self.in_speech:
            self.preroll.append(frame)
            if level > self.threshold:
                self.voiced += 1
                if self.voiced >= self.start_frames:
                    self.in_speech = True
                    self.buffer = list(self.preroll)
                    self.silent = 0
                    self.voiced_total = self.voiced
            else:
                self.voiced = 0
                self.noise = 0.97 * self.noise + 0.03 * level
            return None
        self.buffer.append(frame)
        if level > self.threshold * 0.7:
            self.silent = 0
            self.voiced_total += 1
        else:
            self.silent += 1
        if self.silent >= self.silence_frames or len(self.buffer) >= self.max_frames:
            frames, voiced = self.buffer, self.voiced_total
            self.reset()
            # Clicks, coughs and key clacks are loud but short: require real voiced time.
            return frames if voiced >= self.min_voiced_frames else None
        return None


class VoiceListener:
    def __init__(self, bus: EventBus, speaker: Speaker, wake_words: list[str], on_command: Callable[[str], None],
                 cfg: dict, hint_words: Callable[[], list[str]] | None = None, verifier=None,
                 models_dir: Path | None = None):
        self.bus, self.speaker = bus, speaker
        self.wake_words = wake_words
        self.on_command = on_command
        self.cfg = cfg
        self.hint_words = hint_words or (lambda: [])
        self.state = "off"
        self.error = ""
        self.muted = False
        self.paused = False  # calibration borrows the mic
        self.verifier = verifier  # SpeakerVerifier or None
        self.armed_until = 0.0
        self.armed_by = ""
        self.last_command = 0.0
        self.last_owner = "unknown"  # match | mismatch | pressed | unknown, for the last command
        self.speech_times: deque = deque(maxlen=60)  # every utterance heard, addressed to us or not
        self._audio: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._gen = 0
        self.models_dir = Path(models_dir) if models_dir else Path("data/models")
        self.stt = None                  # speech-to-text engine (stt.py), loaded by the loop
        self.vad_kind = ""
        self.latencies: deque = deque(maxlen=50)   # ms from end of speech to transcript
        self._stt_lock = threading.Lock()
        self.wake = None                 # acoustic wake words / hard triggers (wakeword.py), if trained
        self._name_hit_at = 0.0
        self._speech_started_at = 0.0
        self.heartbeat: Callable[[], None] = lambda: None  # set by the watchdog

    # ---- control ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self.restart()

    def restart(self) -> threading.Thread:
        """Start a fresh loop; a previous (dead or stuck) one retires when it notices."""
        self._gen += 1
        self._thread = threading.Thread(target=self._run, args=(self._gen,), name="voice", daemon=True)
        self._thread.start()
        return self._thread

    @contextlib.contextmanager
    def _keepalive(self, every: float = 10.0):
        """Keep beating while legitimately busy (model download, a long Claude call)."""
        done = threading.Event()

        def tick():
            while not done.wait(every):
                self.heartbeat()

        threading.Thread(target=tick, name="voice-keepalive", daemon=True).start()
        try:
            yield
        finally:
            done.set()
            self.heartbeat()

    def stop(self) -> None:
        self._stop.set()

    def arm(self, seconds: float = 8, source: str = "hotkey") -> None:
        """Accept the next utterance without the wake word.

        ``source`` hotkey/button = a physical push-to-talk (trusted: skips the
        voice check); wake = the name said alone ("Vesper?") — still checked."""
        if source in ("hotkey", "button") and self.speaker.speaking.is_set():
            self.speaker.interrupt()  # pressing talk while it's talking means "stop, listen to me"
        self.armed_until = time.time() + seconds
        self.armed_by = source
        self.speaker.chime()
        self._set_state("armed")

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        self._set_state("muted" if muted else "listening")

    def _set_state(self, state: str, **extra) -> None:
        self.state = state
        self.bus.publish("voice_state", {"state": state, "error": self.error, **extra}, sticky=True)

    def status(self) -> dict:
        lat = sorted(self.latencies)
        return {"state": self.state, "error": self.error, "muted": self.muted, "wake_words": self.wake_words,
                "engine": stt_mod.LABELS.get(getattr(self.stt, "name", ""), ""), "vad": self.vad_kind,
                "latency_ms": lat[len(lat) // 2] if lat else None, "wake_mode": self.wake_mode(),
                "wake": self.wake.status() if self.wake else None,
                "triggers": {n: self.trigger_kind(n) for n in (self.wake.names if self.wake else [])}}

    # ---- acoustic wake words / hard triggers ------------------------------
    def load_wake_words(self) -> None:
        from . import wakeword

        try:
            self.wake = wakeword.load(self.cfg, self.models_dir)
        except Exception:
            log.exception("wake word models failed to load; using the transcript only")
            self.wake = None
        if self.wake:
            log.info("wake words: %s (%s mode)", ", ".join(self.wake.names), self.wake_mode())

    def trigger_kind(self, model: str) -> str:
        """name (wakes it) | stop (interrupts a reply) | command (runs the phrase at once)."""
        spoken = model.replace("_", " ").replace("-", " ").strip().lower()
        names = {w.lower() for w in self.wake_words[:2]}  # the name and "hey <name>"
        if spoken in names:
            return "name"
        if spoken in ("stop", "stop it", "okay stop", "ok stop"):
            return "stop"
        return "command"

    def wake_mode(self) -> str:
        """acoustic: speech-to-text only after the name model fires; hybrid: either wakes it;
        transcript: the name is matched on the text (no model needed)."""
        mode = self.cfg.get("wake_mode", "auto")
        has_name = bool(self.wake) and any(self.trigger_kind(n) == "name" for n in self.wake.names)
        if mode == "auto":
            return "acoustic" if has_name else "transcript"
        if mode in ("acoustic", "hybrid") and not has_name:
            return "transcript"
        return mode if mode in ("acoustic", "hybrid", "transcript") else "transcript"

    def _on_hits(self, hits: list, speaking: bool) -> None:
        for name, score in hits:
            kind = self.trigger_kind(name)
            self.bus.publish("wake_word", {"name": name, "score": score, "kind": kind})
            if kind == "name":
                self._name_hit_at = time.time()
                if speaking:
                    self.speaker.interrupt()  # talking over the reply to give a new command
                else:
                    self.bus.publish("wake", {})
            elif kind == "stop":
                if speaking:
                    self.speaker.interrupt()
            elif not speaking and not self.muted:  # "clip that": act now, no speech-to-text
                command = (self.cfg.get("hard_triggers") or {}).get(name) or name.replace("_", " ")
                self.last_command = time.time()
                self.bus.publish("heard", {"text": command, "trigger": name, "score": score})
                self.on_command(command)

    def _acoustic_wake(self, started_at: float | None) -> bool:
        """Did the name model fire at the *start* of this utterance? (Saying the name mid-sentence
        to chat fires the model too, but not in the first couple of seconds of what you said.)"""
        hit = self._name_hit_at
        if not hit or started_at is None or self.wake_mode() == "transcript":
            return False
        if started_at - 1.0 <= hit <= started_at + WAKE_ANCHOR_S:
            self._name_hit_at = 0.0
            return True
        return False

    # ---- speech-to-text ---------------------------------------------------
    def _progress(self, what: str):
        last = [-1]

        def report(fraction: float) -> None:
            pct = int(fraction * 100)
            if pct >= last[0] + 5 or pct == 100:
                last[0] = pct
                self._set_state("loading", detail=f"Downloading {what}… {pct}%")
                self.heartbeat()
        return report

    def ensure_stt(self):
        """The loaded engine (downloading its model the first time)."""
        with self._stt_lock:
            if self.stt is None:
                self.stt = stt_mod.load(self.cfg, self.models_dir, self._progress("speech model"))
        return self.stt

    def transcribe(self, samples) -> str:
        """float32 16 kHz samples -> text (also used by voice calibration)."""
        return self.ensure_stt().transcribe(samples, self._hints())

    def _hints(self) -> list[str]:
        return [self.wake_words[0].title(), *self.hint_words()][:40] if self.wake_words else list(self.hint_words())[:40]

    def correct(self, text: str) -> str:
        """Apply ``voice.corrections`` ({"vrb": "BRB"}) as whole-word, case-insensitive replacements."""
        for wrong, right in (self.cfg.get("corrections") or {}).items():
            text = re.sub(rf"(?<![\w']){re.escape(str(wrong))}(?![\w'])", str(right), text, flags=re.IGNORECASE)
        return text

    # ---- main loop --------------------------------------------------------
    def _run(self, gen: int = 0) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            self.error = "Voice extras not installed: pip install -r requirements-voice.txt"
            self._set_state("unavailable")
            return
        from .vad import make_segmenter

        self._set_state("loading")
        try:
            with self._keepalive():  # first run downloads the models
                self.ensure_stt()
                segmenter = make_segmenter(self.cfg, self.models_dir, self._progress("voice detector"))
        except Exception as exc:
            self.error = f"Speech model failed to load: {exc}"
            log.exception("speech model load failed")
            self._set_state("error")
            return
        self.vad_kind = getattr(segmenter, "kind", "energy")
        self.load_wake_words()
        log.info("voice: %s speech-to-text, %s voice detection", stt_mod.LABELS.get(self.stt.name, self.stt.name), self.vad_kind)

        def callback(indata, _frames, _time, status):
            if status:
                log.debug("audio status: %s", status)
            self._audio.put(indata[:, 0].copy())

        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME_SAMPLES,
                                    device=self.cfg.get("input_device"), callback=callback)
        except Exception as exc:
            self.error = f"Microphone unavailable: {exc}"
            self._set_state("error")
            return
        with stream:
            self.error = ""
            self._set_state("muted" if self.muted else "listening")
            last_beat = 0.0
            blocked = False
            while not self._stop.is_set() and self._gen == gen:
                now = time.time()
                if now - last_beat >= 5:
                    self.heartbeat()
                    last_beat = now
                try:
                    frame = self._audio.get(timeout=0.5)
                except queue.Empty:
                    continue
                speaking = self.speaker.speaking.is_set()
                barge_in = self.cfg.get("barge_in", True)
                if self.muted or self.paused or (speaking and not barge_in):
                    if not blocked:  # start fresh once, not on every frame
                        segmenter.reset()
                        if self.wake:
                            self.wake.reset()
                        blocked = True
                    continue
                blocked = False
                if self.wake:
                    self._on_hits(self.wake.feed(frame), speaking)
                was_speech = segmenter.in_speech
                frames = segmenter.feed(frame)
                if segmenter.in_speech and not was_speech:
                    self._speech_started_at = time.time()
                if frames is None:
                    if segmenter.in_speech and not speaking and self.state != "hearing":
                        self._set_state("hearing")
                    continue
                during = speaking or time.time() - self.speaker.last_end < 0.3
                if not during:
                    self._set_state("transcribing")
                try:
                    with self._keepalive():
                        self._handle_audio(np.concatenate(frames), self._speech_started_at, during_speech=during)
                except Exception:
                    log.exception("voice pipeline error")
                self._drain()
                if not self.muted:
                    self._set_state("armed" if self._armed() else "listening")

    def heard_speech(self, window_s: float = 60) -> int:
        """How many times you spoke in the last ``window_s`` seconds, wake word or not."""
        cutoff = time.time() - window_s
        return sum(1 for t in self.speech_times if t >= cutoff)

    def _drain(self) -> None:
        """Drop audio captured while we were busy (it's mostly our own voice)."""
        while True:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                return

    def _arm_source(self) -> str | None:
        """Why the next utterance needs no wake word: hotkey/button/wake/followup, or None."""
        now = time.time()
        if now < self.armed_until:
            return self.armed_by or "hotkey"
        follow = self.cfg.get("follow_up_seconds", 8)
        # Only after a question (or a "say yes" confirmation): an answer that asks nothing
        # shouldn't leave the mic open, or a false wake turns into a conversation loop.
        if (getattr(self.speaker, "expects_reply", True) and self.speaker.last_end > self.last_command
                and now - self.speaker.last_end < follow):
            return "followup"
        return None

    def _armed(self) -> bool:
        return self._arm_source() is not None

    def is_own_echo(self, text: str, window_s: float = 4.0) -> bool:
        """Did the mic just pick up our own reply? (speakers, no headset, no echo cancellation)"""
        said = getattr(self.speaker, "last_text", "")
        recent = self.speaker.speaking.is_set() or time.time() - self.speaker.last_end < window_s
        if not said or not recent:
            return False
        a, b = _norm_sentence(text), _norm_sentence(said)
        if not a:
            return False
        if len(a) < 10:  # "yes", "stop", "cancel": too short to judge, and exactly what you'd say back
            return a == b
        if a in b:
            return True
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6

    def _handle_audio(self, audio, started_at: float | None = None, during_speech: bool = False) -> None:
        import numpy as np

        samples = audio.astype(np.float32) / 32768.0
        if not during_speech:  # you talking (not us): the stream's mic check cross-checks against this
            self.speech_times.append(time.time())
        source = self._arm_source()
        acoustic = self._acoustic_wake(started_at)
        if self.wake_mode() == "acoustic" and not (acoustic or source or during_speech):
            return  # the name model didn't fire: nothing addressed to us, skip speech-to-text
        t0 = time.perf_counter()
        text = self.correct(self.stt.transcribe(samples, self._hints()))
        stt_ms = round((time.perf_counter() - t0) * 1000)
        self.latencies.append(stt_ms)
        if _norm(text.replace(" ", "")) in {h.replace(" ", "") for h in HALLUCINATIONS}:
            return
        if self.is_own_echo(text):
            self.bus.publish("heard", {"text": text, "ignored": "own voice", "stt_ms": stt_ms})
            return
        matched, command = split_wake(text, self.wake_words)
        if acoustic and not matched:  # the model heard the name even if the transcript mangled it
            matched, command = True, strip_leading_name(text, self.wake_words[0])
        said_all = _norm_sentence(command if matched else text)
        if said_all in KILL_PHRASES and not matched:
            matched, command = True, said_all  # no name needed for the kill switch
        if during_speech:
            said = _norm_sentence(command if matched else text)
            if said in STOP_PHRASES:
                if self._owner_ok(samples, source) and not self._stop_word_is_echo(said):
                    self.speaker.interrupt()
                    self.bus.publish("heard", {"text": text, "interrupted": True, "stt_ms": stt_ms})
                    if said == "cancel":
                        self.last_command = time.time()
                        self.on_command("cancel")
                return
            if not matched:  # talking over a reply needs the name (or push-to-talk)
                if source in ("hotkey", "button"):
                    matched, command = True, text
                else:
                    self.bus.publish("heard", {"text": text, "ignored": "while speaking", "stt_ms": stt_ms})
                    return
        if not (matched or source):
            self.bus.publish("heard", {"text": text, "wake": False, "armed": False, "stt_ms": stt_ms})
            return
        # Only the owner may command it — unless they physically pressed push-to-talk.
        owner = "pressed" if source in ("hotkey", "button") else "unknown"
        if self.verifier is not None and source not in ("hotkey", "button"):
            accepted, score = self.verifier.check(samples)
            if not accepted:
                self.bus.publish("heard", {"text": text, "ignored": "voice not recognised", "score": score, "stt_ms": stt_ms})
                return
            profile = getattr(self.verifier, "profile", None)
            if score is not None and profile is not None:  # LOG mode lets a non-match through: remember it did
                owner = "match" if score >= profile.threshold else "mismatch"
        if during_speech:
            self.speaker.interrupt()
        self.bus.publish("heard", {"text": text, "wake": matched, "armed": bool(source), "stt_ms": stt_ms,
                                   **({"acoustic": True} if acoustic else {})})
        if matched and not command:
            self.arm(source="wake")  # "Vesper?" -> listen for the actual command
            return
        self.armed_until = 0.0
        self.last_command = time.time()
        self.last_owner = owner  # read by the runtime: a T3 "yes" in someone else's voice doesn't count
        self.on_command(command if matched else text)

    def _owner_ok(self, samples, source) -> bool:
        if self.verifier is None or source in ("hotkey", "button"):
            return True
        return self.verifier.check(samples)[0]

    def _stop_word_is_echo(self, said: str) -> bool:
        """Without a voice profile, a lone "stop" that the reply itself contains might be the reply."""
        if self.verifier is not None and getattr(self.verifier, "active", False):
            return False  # the speaker check already rejected the TTS voice
        return said in _norm_sentence(getattr(self.speaker, "last_text", "")).split() and " " not in said
