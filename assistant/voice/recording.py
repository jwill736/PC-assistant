"""The recorder's audio side: mic (and optionally the PC's own sound) to chunked audio files and a live transcript.

* ``Session`` takes 30 ms int16 frames from any thread (``push``), writes them to WAV chunks of ``chunk_s`` seconds
  (one file per source, so nothing hours-long is ever held in memory), cuts them into utterances at pauses, and
  transcribes each one in a background thread with the same speech-to-text engine the listener uses. Every line goes
  to ``transcript.jsonl`` as it's made. Speech while Vesper is talking is marked ``vesper`` (it's her voice coming
  back through the mic or the speakers), and anything that a ``is_command`` check says was said *to* Vesper is
  dropped. When speech-to-text falls behind (a slow engine), waiting utterances spill to disk instead of memory.
* ``SystemAudio`` is the optional second source: what the PC plays (the other people on a call). WASAPI loopback
  through ``pyaudiowpatch`` when it's installed, else a "Stereo Mix"-style input device through ``sounddevice``, else
  it says why not and the recording is the mic alone. mic = J, system = the others: a free "who spoke".
* ``decode_file`` turns an audio/video file into 16 kHz mono frames: WAV with the standard library, anything else
  through ffmpeg when it's on PATH.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import re
import shutil
import subprocess
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME = 480  # 30 ms
CHUNK_S = 300  # one audio file per 5 minutes per source
MAX_UTTERANCE_S = 25  # what the speech model gets at once
SOURCES = {"mic": "J", "system": "Others"}
# What speech models "hear" in silence, fan noise or music (also the listener's list).
HALLUCINATIONS = {"you", "thank you", "thanks for watching", "thank you for watching", "bye", "so", "okay", "",
                  "thanks", "uh", "um", "hmm", "mm"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".webm", ".mp4", ".mkv", ".mov", ".avi"}


def stamp(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w' ]", " ", (text or "").lower()).split())


# ---- writing audio ------------------------------------------------------------------------------------------------

class ChunkWriter:
    """int16 frames -> ``<prefix>-000.wav``, ``<prefix>-001.wav``… of ``chunk_s`` seconds each. ``on_chunk(path)`` runs
    after each file is closed (the skill compresses it there when ffmpeg is around)."""

    def __init__(self, folder: Path, prefix: str, chunk_s: float = CHUNK_S, on_chunk: Callable[[Path], None] | None = None):
        self.folder, self.prefix = Path(folder), prefix
        self.limit = int(chunk_s * SAMPLE_RATE)
        self.on_chunk = on_chunk
        self.samples = 0  # in all
        self.files: list[Path] = []
        self._wav: wave.Wave_write | None = None
        self._in_file = 0

    def write(self, frame) -> None:
        import numpy as np

        data = np.asarray(frame, dtype=np.int16).reshape(-1)
        while len(data):
            if self._wav is None:
                path = self.folder / f"{self.prefix}-{len(self.files):03d}.wav"
                self._wav = wave.open(str(path), "wb")
                self._wav.setnchannels(1)
                self._wav.setsampwidth(2)
                self._wav.setframerate(SAMPLE_RATE)
                self.files.append(path)
                self._in_file = 0
            room = self.limit - self._in_file
            part, data = data[:room], data[room:]
            self._wav.writeframes(part.tobytes())
            self._in_file += len(part)
            self.samples += len(part)
            if self._in_file >= self.limit:
                self._close()

    def _close(self) -> None:
        if self._wav is None:
            return
        self._wav.close()
        self._wav = None
        if self.on_chunk is not None:
            try:
                self.on_chunk(self.files[-1])
            except Exception:
                log.exception("recorder: chunk hook failed")

    def close(self) -> None:
        self._close()


# ---- cutting speech into utterances ----------------------------------------------------------------------------------

@dataclass
class Utterance:
    source: str
    start: float  # seconds into the recording
    end: float
    audio: object  # int16 samples, or a Path to them on disk (spilled)
    vesper: float = 0.0  # share of it said while Vesper was talking
    wall: float = 0.0  # when it ended (epoch)


class Track:
    """One source: its audio files, its position, and its speech detector."""

    def __init__(self, folder: Path, source: str, chunk_s: float, on_chunk=None):
        from .listener import EnergySegmenter

        self.source = source
        self.writer = ChunkWriter(folder, source, chunk_s, on_chunk)
        # quieter floor for the PC's own sound (a call at a low volume); a little more patient than commands
        self.seg = EnergySegmenter(min_rms=180 if source == "system" else 300, silence_ms=700,
                                   max_utterance_s=MAX_UTTERANCE_S, min_voiced_ms=300)
        self._pre: deque = deque(maxlen=self.seg.preroll.maxlen)
        self._flags: list[bool] = []

    @property
    def seconds(self) -> float:
        return self.writer.samples / SAMPLE_RATE

    def feed(self, frame, tts: bool, wall: float) -> Utterance | None:
        import numpy as np

        self.writer.write(frame)
        was = self.seg.in_speech
        frames = self.seg.feed(frame)
        if not was:
            self._pre.append(tts)
            if self.seg.in_speech:
                self._flags = list(self._pre)
        else:
            self._flags.append(tts)
        if frames is None:
            if was and not self.seg.in_speech:
                self._flags = []  # a click or a cough: dropped
            return None
        return self._utterance(np.concatenate([np.asarray(f, dtype=np.int16).reshape(-1) for f in frames]), wall)

    def _utterance(self, audio, wall: float) -> Utterance:
        flags, self._flags = self._flags or [False], []
        self._pre.clear()
        end = self.seconds
        return Utterance(self.source, max(0.0, end - len(audio) / SAMPLE_RATE), end, audio,
                         sum(flags) / len(flags), wall)

    def flush(self, wall: float) -> Utterance | None:
        """The speech still open when the recording stops."""
        import numpy as np

        frames = self.seg.buffer if self.seg.in_speech else []
        voiced = self.seg.voiced_total
        self.seg.reset()
        self.writer.close()
        if not frames or voiced < self.seg.min_voiced_frames:
            return None
        return self._utterance(np.concatenate([np.asarray(f, dtype=np.int16).reshape(-1) for f in frames]), wall)


# ---- a recording -----------------------------------------------------------------------------------------------------

class Session:
    """One recording: frames in, audio files and transcript lines out.

    ``transcribe(float32 samples) -> text``; ``is_tts() -> bool`` (Vesper is talking right now); ``is_command(text,
    wall) -> bool`` (that was said to Vesper: not part of the transcript); ``on_line(line)`` for every new line.
    ``threads=False`` does everything inside ``push`` (tests)."""

    def __init__(self, folder: Path, transcribe: Callable, *, is_tts: Callable[[], bool] = lambda: False,
                 is_command: Callable[[str, float], bool] = lambda text, wall: False,
                 on_line: Callable[[dict], None] | None = None, on_chunk: Callable[[Path], None] | None = None,
                 chunk_s: float = CHUNK_S, clock: Callable[[], float] = time.time, threads: bool = True,
                 max_backlog: int = 120, line_offset: int = 0, scrub: Callable[[str], str] = lambda t: t):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.transcribe_fn, self.is_tts, self.is_command = transcribe, is_tts, is_command
        self.on_line, self.on_chunk, self.scrub = on_line, on_chunk, scrub
        self.chunk_s, self.clock, self.threads = chunk_s, clock, threads
        self.max_backlog = max_backlog
        self.tracks: dict[str, Track] = {}
        self.paused = False
        self.stopped = False
        self.lines: deque = deque(maxlen=60)  # the latest, for the HUD (the whole transcript is on disk)
        self.count = line_offset  # lines written
        self.words = 0
        self.dropped = 0  # said to Vesper: left out
        self.errors = 0
        self.last_frame = 0.0  # wall time of the latest frame (a mic that went quiet shows on the HUD)
        self._frames: queue.Queue = queue.Queue()
        self._utts: queue.Queue = queue.Queue()
        self._spilled = 0
        self._file = open(self.folder / "transcript.jsonl", "a", encoding="utf-8")  # noqa: SIM115 - open for the session
        self._file_lock = threading.Lock()
        self._writer = self._reader = None
        if threads:
            self._writer = threading.Thread(target=self._write_loop, name="recorder-audio", daemon=True)
            self._reader = threading.Thread(target=self._transcribe_loop, name="recorder-stt", daemon=True)
            self._writer.start()
            self._reader.start()

    # ---- in ------------------------------------------------------------------------------------------------------
    def push(self, frame, source: str = "mic") -> None:
        """A 30 ms int16 frame from any thread (the mic callback, the listener's loop)."""
        if self.stopped or self.paused:
            return
        try:
            tts = bool(self.is_tts())
        except Exception:
            tts = False
        now = self.clock()
        self.last_frame = now
        if self.threads:
            self._frames.put((source, frame, tts, now))
        else:
            self._take(source, frame, tts, now)
            self._drain_utterances()

    @property
    def seconds(self) -> float:
        """Recorded so far (pauses don't count): the mic's length, or the longest source."""
        if "mic" in self.tracks:
            return self.tracks["mic"].seconds
        return max((t.seconds for t in self.tracks.values()), default=0.0)

    @property
    def backlog(self) -> int:
        return self._utts.qsize() + self._frames.qsize() // 34  # waiting utterances (+ ~1 per second of raw audio)

    def files(self) -> list[Path]:
        return [p for t in self.tracks.values() for p in t.writer.files]

    # ---- the audio thread ----------------------------------------------------------------------------------------
    def _track(self, source: str) -> Track:
        tr = self.tracks.get(source)
        if tr is None:
            tr = self.tracks[source] = Track(self.folder, source, self.chunk_s, self.on_chunk)
        return tr

    def _take(self, source: str, frame, tts: bool, wall: float) -> None:
        try:
            utt = self._track(source).feed(frame, tts, wall)
        except Exception:
            self.errors += 1
            log.exception("recorder: writing audio failed")
            return
        if utt is not None:
            mic = self.tracks.get("mic")
            if source != "mic" and mic is not None:
                # on the mic's clock: Windows sends no loopback audio while nothing plays, so the PC's own track is
                # shorter than the recording; it ended just now, which on the mic's track is its current position
                shift = mic.seconds - utt.end
                utt.start, utt.end = max(0.0, utt.start + shift), max(0.0, utt.end + shift)
            self._queue(utt)

    def _queue(self, utt: Utterance) -> None:
        if self._utts.qsize() >= self.max_backlog:  # speech-to-text is behind: keep the audio on disk, not in memory
            spill = self.folder / ".pending"
            spill.mkdir(exist_ok=True)
            path = spill / f"{self._spilled:06d}.pcm"
            self._spilled += 1
            path.write_bytes(utt.audio.tobytes())
            utt.audio = path
        self._utts.put(utt)

    def _write_loop(self) -> None:
        while True:
            try:
                item = self._frames.get(timeout=0.25)
            except queue.Empty:
                if self.stopped:
                    break
                continue
            if item is None:
                break
            self._take(*item)
        self._flush()
        self._utts.put(None)

    def _flush(self) -> None:
        for tr in self.tracks.values():
            utt = tr.flush(self.clock())
            if utt is not None:
                self._queue(utt)

    # ---- the speech-to-text thread -------------------------------------------------------------------------------
    def _transcribe_loop(self) -> None:
        while True:
            utt = self._utts.get()
            if utt is None:
                break
            self._transcribe(utt)

    def _drain_utterances(self) -> None:
        while True:
            try:
                utt = self._utts.get_nowait()
            except queue.Empty:
                return
            if utt is not None:
                self._transcribe(utt)

    def _transcribe(self, utt: Utterance) -> None:
        import numpy as np

        audio = utt.audio
        if isinstance(audio, Path):
            try:
                raw = audio.read_bytes()
                audio.unlink()
            except OSError:
                return
            audio = np.frombuffer(raw, dtype=np.int16)
        samples = np.asarray(audio, dtype=np.float32) / 32768.0
        try:
            from .dictate import chunks

            text = " ".join(p.strip() for p in (self.transcribe_fn(c) for c in chunks(samples, max_s=MAX_UTTERANCE_S))
                            if p and p.strip())
        except Exception:
            self.errors += 1
            log.exception("recorder: speech-to-text failed")
            return
        text = re.sub(r"\s+", " ", text or "").strip()
        if _norm(text) in HALLUCINATIONS:
            return
        try:
            text = self.scrub(text)  # a password said out loud is never written down
        except Exception:
            return
        vesper = utt.vesper >= 0.5
        if not vesper:
            try:
                if self.is_command(text, utt.wall):
                    self.dropped += 1
                    return
            except Exception:
                log.exception("recorder: command check failed")
        line = {"n": self.count, "t": round(utt.start, 2), "end": round(utt.end, 2), "src": utt.source,
                "who": "Vesper" if vesper else SOURCES.get(utt.source, utt.source), "text": text, "wall": round(utt.wall, 1)}
        if vesper:
            line["vesper"] = True
        self.count += 1
        if not vesper:
            self.words += len(text.split())
        self.lines.append(line)
        with self._file_lock:
            if not self._file.closed:
                self._file.write(json.dumps(line, ensure_ascii=False) + "\n")
                self._file.flush()
        if self.on_line is not None:
            try:
                self.on_line(line)
            except Exception:
                log.exception("recorder: line hook failed")

    # ---- control ---------------------------------------------------------------------------------------------------
    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def stop(self, timeout: float | None = None) -> None:
        """No more audio; finish the files and transcribe what's waiting (``timeout``: give up waiting after it)."""
        if self.stopped:
            return
        self.paused = False
        self.stopped = True
        if self.threads:
            self._frames.put(None)
            for t in (self._writer, self._reader):
                if t is not None:
                    t.join(timeout)
        else:
            self._flush()
            self._drain_utterances()
        with self._file_lock:
            self._file.close()
        with contextlib.suppress(OSError):
            spill = self.folder / ".pending"
            if spill.exists() and not any(spill.iterdir()):
                spill.rmdir()


def read_lines(folder: Path) -> Iterator[dict]:
    """Every transcript line of a recording, from disk (a broken last line from a crash is skipped)."""
    path = Path(folder) / "transcript.jsonl"
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                continue


# ---- the PC's own sound ---------------------------------------------------------------------------------------------

class Framer:
    """Any rate, any channels, float or int -> 16 kHz mono int16 frames of 480 samples."""

    def __init__(self, rate: int, channels: int = 1):
        self.rate, self.channels = int(rate), max(1, int(channels))
        self._carry = None
        self._pos = 0.0  # resampling phase carried between blocks
        self._out = None

    def feed(self, block) -> list:
        import numpy as np

        a = np.asarray(block)
        if a.dtype.kind in "iu":
            a = a.astype(np.float32) / 32768.0
        a = a.astype(np.float32)
        if a.ndim == 1 and self.channels > 1:
            a = a[: len(a) // self.channels * self.channels].reshape(-1, self.channels)
        if a.ndim == 2:
            a = a.mean(axis=1)
        if self.rate != SAMPLE_RATE and len(a):
            if self._carry is not None:
                a = np.concatenate([self._carry, a])
            step = self.rate / SAMPLE_RATE
            idx = np.arange(self._pos, len(a) - 1, step)
            res = np.interp(idx, np.arange(len(a)), a) if len(idx) else np.zeros(0, np.float32)
            nxt = (idx[-1] + step) if len(idx) else self._pos
            keep = int(nxt)
            self._carry, self._pos = a[keep:], nxt - keep
            a = res
        pcm = np.clip(a * 32767.0, -32768, 32767).astype(np.int16)
        self._out = pcm if self._out is None else np.concatenate([self._out, pcm])
        n = len(self._out) // FRAME
        frames = [self._out[i * FRAME:(i + 1) * FRAME] for i in range(n)]
        self._out = self._out[n * FRAME:]
        return frames


LOOPBACK_NAMES = re.compile(r"stereo mix|what u hear|wave out|loopback|cable output|voicemeeter out", re.I)


class SystemAudio:
    """What the PC plays, as 16 kHz frames: ``start(push)`` -> True, or False with ``why`` set."""

    def __init__(self, opener: Callable | None = None):
        self.opener = opener  # (push) -> (stream with stop()/close(), how): tests and other back ends
        self.why = ""
        self.how = ""
        self._stream = None

    def start(self, push: Callable) -> bool:
        tries = [self.opener] if self.opener is not None else [self._pyaudiowpatch, self._stereo_mix]
        reasons = []
        for opener in tries:
            try:
                got = opener(push)
            except Exception as exc:  # a device that vanished, a driver that refused
                reasons.append(f"{type(exc).__name__}: {exc}"[:120])
                continue
            if got:
                self._stream, self.how = got
                self.why = ""
                return True
        self.why = "; ".join(r for r in reasons if r) or (
            "no way to record the PC's sound here: turn on Stereo Mix (Sound settings → Recording) or install "
            "pyaudiowpatch")
        return False

    def stop(self) -> None:
        s, self._stream = self._stream, None
        if s is None:
            return
        for fn in ("stop_stream", "stop", "close"):
            with contextlib.suppress(Exception):
                getattr(s, fn, lambda: None)()

    @staticmethod
    def _pyaudiowpatch(push):  # pragma: no cover - Windows with the optional package
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            return None
        pa = pyaudio.PyAudio()
        try:
            info = pa.get_default_wasapi_loopback()
        except Exception:
            pa.terminate()
            return None
        rate, ch = int(info["defaultSampleRate"]), int(info["maxInputChannels"]) or 2
        framer = Framer(rate, ch)

        def callback(data, _n, _t, _s):
            import numpy as np

            for f in framer.feed(np.frombuffer(data, dtype=np.int16)):
                push(f)
            return (None, pyaudio.paContinue)
        stream = pa.open(format=pyaudio.paInt16, channels=ch, rate=rate, frames_per_buffer=int(rate * 0.03), input=True,
                         input_device_index=info["index"], stream_callback=callback)
        stream.start_stream()
        return stream, "loopback"

    @staticmethod
    def _stereo_mix(push):  # pragma: no cover - needs a sound card with a loopback input
        import sounddevice as sd

        dev = next((i for i, d in enumerate(sd.query_devices())
                    if d.get("max_input_channels", 0) > 0 and LOOPBACK_NAMES.search(d.get("name", ""))), None)
        if dev is None:
            return None
        info = sd.query_devices(dev)
        rate = int(info.get("default_samplerate") or 48000)
        ch = min(2, int(info.get("max_input_channels") or 1))
        framer = Framer(rate, ch)

        def callback(indata, _frames, _time, _status):
            for f in framer.feed(indata.copy()):
                push(f)
        stream = sd.InputStream(samplerate=rate, channels=ch, dtype="int16", device=dev, callback=callback,
                                blocksize=int(rate * 0.03))
        stream.start()
        return stream, f"input: {info.get('name', 'loopback')}"


# ---- a file J already has -------------------------------------------------------------------------------------------

class DecodeError(Exception):
    pass


def check_file(path: Path, ffmpeg: str | None = None) -> str:
    """Can ``decode_file`` read it? The ffmpeg to use ("" for a WAV read without it), or a DecodeError saying why not."""
    path = Path(path)
    if not path.is_file():
        raise DecodeError(f"There's no file at {path}.")
    ext = path.suffix.lower()
    if ext not in AUDIO_EXT:
        raise DecodeError(f"{path.name} isn't an audio or video file I know ({', '.join(sorted(AUDIO_EXT))}).")
    ff = ffmpeg if ffmpeg is not None else (shutil.which("ffmpeg") or "")
    if not ff and ext != ".wav":
        raise DecodeError(f"To read {ext} files I need ffmpeg: install it (winget install Gyan.FFmpeg), restart "
                          f"Vesper, and ask again. WAV files work without it.")
    return ff


def decode_file(path: Path, ffmpeg: str | None = None, runner=subprocess.Popen) -> Iterator:
    """16 kHz mono int16 frames from an audio or video file. WAV needs nothing; the rest needs ffmpeg on PATH."""
    path = Path(path)
    ff = check_file(path, ffmpeg)
    if not ff:
        yield from _wav_frames(path)
        return
    proc = runner([ff, "-nostdin", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    import numpy as np

    got = 0
    try:
        while True:
            raw = proc.stdout.read(FRAME * 2 * 100)
            if not raw:
                break
            pcm = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16)
            got += len(pcm)
            for i in range(0, len(pcm), FRAME):
                yield pcm[i:i + FRAME]
    finally:
        with contextlib.suppress(Exception):
            proc.stdout.close()
        code = proc.wait()
    if code != 0 and not got:
        err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace").strip().splitlines()
        raise DecodeError(f"ffmpeg couldn't read {path.name}: {(err[-1] if err else f'exit code {code}')[:200]}")


def _wav_frames(path: Path) -> Iterator:
    import numpy as np

    try:
        w = wave.open(str(path), "rb")
    except (wave.Error, EOFError) as exc:
        raise DecodeError(f"{path.name} isn't a WAV file I can read ({exc}).") from exc
    with w:
        if w.getsampwidth() != 2:
            raise DecodeError(f"{path.name} isn't 16-bit audio; install ffmpeg to read it.")
        framer = Framer(w.getframerate(), w.getnchannels())
        while True:
            raw = w.readframes(SAMPLE_RATE)
            if not raw:
                break
            yield from framer.feed(np.frombuffer(raw, dtype=np.int16))


def compress(path: Path, ffmpeg: str | None = None, run=subprocess.run) -> Path | None:
    """A finished WAV chunk -> Opus in an .ogg (about a tenth of the size) when ffmpeg is on PATH; the WAV goes."""
    ff = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
    if not ff:
        return None
    out = Path(path).with_suffix(".ogg")
    try:
        r = run([ff, "-nostdin", "-v", "error", "-y", "-i", str(path), "-c:a", "libopus", "-b:a", "24k", "-ac", "1",
                 str(out)], capture_output=True, timeout=300, **_no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(r, "returncode", 1) == 0 and out.exists() and out.stat().st_size > 0:
        with contextlib.suppress(OSError):
            Path(path).unlink()
        return out
    with contextlib.suppress(OSError):
        out.unlink()
    return None


def _no_window() -> dict:
    import sys

    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW
