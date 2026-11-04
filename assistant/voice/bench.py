"""Voice benchmarks on *your* voice and *this* PC.

``python -m assistant --bench-voice``: which speech engine hears you best. Records ten short commands, runs every
installed engine over the same recordings and reports word errors, how often the wake word was caught, and time from
end of speech to text. ``--apply`` saves the winner as ``voice.stt_engine``. Recordings stay in memory.

The voice test (Setup → Voice test, ``RealBench``): you read 30–40 of your own commands (taken from what you've said
to Vesper lately), then leave a few minutes of only background playing (the game, Discord, music, Vesper talking).
Both are replayed offline through the chain the live mic uses (voice detector, end of turn, speech-to-text with your
word fixes and names, the name model, the transcript wake check, the voice check). It reports word errors, missed
wakes, false wakes per hour of background, your voice scores against everyone else's, and end of speech to text, and
suggests settings from those numbers; nothing changes until you click Apply. Recordings and results stay in
data/bench/ (delete them from the panel), so it can re-run on the same audio after a change.
"""

from __future__ import annotations

import json
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import save_setting
from . import calibrate as calib
from . import stt
from .listener import HALLUCINATIONS, WAKE_ANCHOR_S, _norm, split_wake

COMMANDS = [
    "{name}, open Discord",
    "{name}, switch to the BRB scene",
    "{name}, good morning",
    "Mute the mic",
    "{name}, what's on my calendar today",
    "Start stream mode",
    "{name}, add a task to send the invoice",
    "Clip that",
    "{name}, open Spotify",
    "End the stream",
]


def words(text: str) -> list[str]:
    import re

    return re.sub(r"[^a-z' ]+", " ", text.lower()).split()


def wer(ref: str, hyp: str) -> tuple[int, int]:
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)], len(r)


def record_commands(name: str, record: Callable, say: Callable[[str], None] = print, seconds: float = 4.0,
                    pause: float = 0.6) -> list[tuple[str, object]]:
    say("Stay quiet for 2 seconds…")
    noise = record(2.0, lambda _lvl: None)
    import numpy as np

    floor = max(float(np.percentile(calib.frame_rms(noise), 90)) * 2, 150.0)
    clips = []
    for i, template in enumerate(COMMANDS, 1):
        text = template.format(name=name)
        say(f"[{i}/{len(COMMANDS)}] Say: “{text}”")
        time.sleep(pause)
        audio = calib.trim(record(seconds, lambda _lvl: None), floor)
        if len(audio) >= 8000:
            clips.append((text, audio))
        else:
            say("  (didn't catch that — skipping)")
    return clips


def score(engine, clips, wake_words: list[str]) -> dict:
    engine.transcribe(clips[0][1])  # warm-up
    errs = n = woke = with_name = 0
    ms, samples = [], []
    for text, audio in clips:
        t0 = time.perf_counter()
        hyp = engine.transcribe(audio)
        ms.append((time.perf_counter() - t0) * 1000)
        e, k = wer(text, hyp)
        errs, n = errs + e, n + k
        if text.lower().startswith(wake_words[0]):
            with_name += 1
            woke += split_wake(hyp, wake_words)[0]
        samples.append({"said": text, "heard": hyp})
    ms.sort()
    return {"wer": round(errs / max(n, 1) * 100, 1), "woke": woke, "with_name": with_name,
            "p50_ms": round(ms[len(ms) // 2]), "samples": samples}


def run(cfg, record: Callable, loaders: dict[str, Callable] | None = None, say: Callable[[str], None] = print,
        apply: bool = False) -> dict:
    voice = cfg["voice"]
    models_dir = Path(cfg.data_dir) / "models"
    if loaders is None:
        have = stt.available()
        loaders = {e: (lambda e=e: stt.load({**voice, "stt_engine": e}, models_dir))
                   for e in ("parakeet", "moonshine", "whisper") if have.get(e)}
    name = cfg["assistant"]["name"]
    wake_words = cfg["assistant"]["wake_words"]
    say(f"Voice benchmark: read {len(COMMANDS)} commands the way you'd normally say them.")
    clips = record_commands(name, record, say)
    if len(clips) < 3:
        raise RuntimeError("too few usable recordings — check the mic (Setup tab → Health check)")
    results = {}
    for engine, load in loaders.items():
        say(f"Testing {stt.LABELS.get(engine, engine)}…")
        try:
            results[engine] = score(load(), clips, wake_words)
        except Exception as exc:
            results[engine] = {"error": f"{type(exc).__name__}: {exc}"}
    ok = {e: r for e, r in results.items() if "error" not in r}
    best = min(ok, key=lambda e: (-ok[e]["woke"], ok[e]["wer"], ok[e]["p50_ms"])) if ok else None
    say("")
    say(f"{'engine':16s} {'wake':>7s} {'word errors':>12s} {'latency':>9s}")
    for e, r in results.items():
        if "error" in r:
            say(f"{stt.LABELS.get(e, e):16s} failed: {r['error']}")
        else:
            say(f"{stt.LABELS.get(e, e):16s} {r['woke']:>3d}/{r['with_name']:<3d} {r['wer']:>11.1f}% {r['p50_ms']:>7d} ms"
                + ("   ← best" if e == best else ""))
    report = {"when": time.time(), "best": best, "results": results}
    out = Path(cfg.data_dir) / "bench"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"voice-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    if best and apply:
        save_setting(cfg, "voice.stt_engine", best)
        say(f"Saved voice.stt_engine: {best} (data/settings.yaml). Restart the assistant to use it.")
    elif best:
        say(f"Run again with --apply to switch to {stt.LABELS.get(best, best)}, or set voice.stt_engine: {best}.")
    return report


# ---------------------------------------------------------------------------
# The voice test: your real commands and your real background
# ---------------------------------------------------------------------------

SR = calib.SAMPLE_RATE
PAUSE = "…"  # in a phrase: pause there, the way you do when you're thinking
EVERYDAY = [
    "{name}, open Discord", "{name}, switch to the BRB scene", "{name}, what's on my calendar today", "Mute the mic",
    "{name}, add a task to send the invoice", "Clip that", "{name}, turn the music down a bit", "Start stream mode",
    "{name}, how many viewers do we have", "{name}, what's the weather like this afternoon", "Stop everything",
    "{name}, read me my last email",
]
WITH_PAUSES = [
    "{name}, remind me to … call the dentist tomorrow", "{name}, switch the scene to … just chatting",
    "{name}, set a timer for … ten minutes", "{name}, what's … the weather like this weekend",
]
# Said by Vesper during the background recording (never its name: hearing that would be a false wake)
VESPER_LINES = [
    "This is the background part of the voice test. Keep playing, I'm only listening.",
    "Your next meeting is at three. Chat is quiet. Nothing needs you right now.",
    "The stream has been live for two hours and forty minutes, with sixty two viewers.",
    "I'll stay quiet now. Carry on with the game, the music or the call.",
]
ENDPOINTS = (300, 400, 500, 600, 800, 1000)  # ms of silence that end a sentence, tried on your recordings
VAD_THRESHOLDS = (0.5, 0.6, 0.7)


def reference(phrase: str) -> str:
    return " ".join(phrase.replace(PAUSE, " ").split())


def phrases(name: str, history: list[str], count: int = 36) -> list[str]:
    """What to read: your own recent voice commands first (the conversation log, newest first), then everyday ones,
    and four with a pause in the middle (end of turn is judged on those). Every other one of yours gets the name."""
    def key(text):  # "Friday, open Discord" and "open discord" are the same command
        w = words(text)
        return " ".join(w[1:] if w[:1] == [name.lower()] else w)

    seen, own = set(), []
    for text in reversed(history):
        key_ = key(text)
        if 2 <= len(key_.split()) <= 12 and key_ not in seen and not any(ch.isdigit() for ch in text) and len(text) < 90:
            seen.add(key_)
            own.append(text.strip().rstrip(".?!"))
    own = [f"{name}, {t[0].lower() + t[1:]}" if i % 2 == 0 else t for i, t in enumerate(own[:count - 10])]
    fill = [p.format(name=name) for p in EVERYDAY if key(p.format(name=name)) not in seen]
    pauses = [p.format(name=name) for p in WITH_PAUSES]
    body = (own + fill)[:count - len(pauses)]
    step = max(1, len(body) // len(pauses))  # spread the pauses through the list
    for i, p in enumerate(pauses):
        body.insert(min(len(body), (i + 1) * step + i), p)
    return body


def record_seconds(phrase: str) -> float:
    return round(min(10.0, 2.2 + 0.42 * len(reference(phrase).split()) + 1.6 * phrase.count(PAUSE)), 1)


class Recorder(calib.Calibrator):
    """The guided recording, through calibration's plumbing (prompts, the level meter, cancel)."""

    TOPIC = "voice_bench"
    STEPS = ("commands", "background")

    def __init__(self, cfg, bus, record: Callable, lines: list[str], background_s: float = 180,
                 speak: Callable[[str], None] | None = None, chunk_s: float = 30, prompt_pause: float = 0.8):
        super().__init__(cfg, bus, record, transcribe=None, prompt_pause=prompt_pause)
        self.lines, self.background_s, self.speak, self.chunk_s = lines, background_s, speak, chunk_s

    def run(self) -> dict:
        """{"commands": [(reference, int16)], "background": int16}, or {"cancelled": True}."""
        import numpy as np

        try:
            commands = []
            for i, line in enumerate(self.lines):
                tip = " (pause where the … is)" if PAUSE in line else ""
                audio = self._rec("commands", f"Say: “{line}”{tip}", record_seconds(line), i + 1, len(self.lines))
                commands.append((reference(line), to_pcm(audio)))
            pieces, left, n = [], float(self.background_s), 0
            total = max(1, int(-(-self.background_s // self.chunk_s)))
            while left > 0:
                if self.speak is not None and n < len(VESPER_LINES):
                    self.speak(VESPER_LINES[n])  # Vesper's own voice belongs in the background too
                seconds = min(self.chunk_s, left)
                pieces.append(to_pcm(self._rec("background", "Don't talk to me now: play, chat on Discord, let the "
                                                "music run. I'll say a few things too.", seconds, n + 1, total)))
                left, n = left - seconds, n + 1
            return {"commands": commands, "background": np.concatenate(pieces) if pieces else np.zeros(0, np.int16)}
        except calib.CalibrationCancelled:
            self.emit(step="cancelled")
            return {"cancelled": True}


def to_pcm(samples):
    import numpy as np

    return (np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype(np.int16)


def save_recordings(folder: Path, commands: list, background) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.wav"):
        old.unlink()
    for i, (_ref, pcm) in enumerate(commands):
        _write_wav(folder / f"cmd-{i:02d}.wav", pcm)
    _write_wav(folder / "background.wav", background)
    (folder / "commands.json").write_text(json.dumps([ref for ref, _ in commands], indent=1), encoding="utf-8")


def load_recordings(folder: Path) -> tuple[list, object] | None:
    try:
        refs = json.loads((folder / "commands.json").read_text(encoding="utf-8"))
        commands = [(ref, _read_wav(folder / f"cmd-{i:02d}.wav")) for i, ref in enumerate(refs)]
        return commands, _read_wav(folder / "background.wav")
    except (OSError, ValueError, wave.Error):
        return None


def _write_wav(path: Path, pcm) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def _read_wav(path: Path):
    import numpy as np

    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy()


@dataclass
class Chain:
    """The live capture chain, offline: everything the listener does to a sentence, short of acting on it."""

    cfg: dict                                     # voice settings (copies with other values are tried)
    wake_words: list[str]
    transcribe: Callable                          # (float32 samples, hints) -> text
    segmenter: Callable[[dict], object]           # voice settings -> a fresh segmenter (vad.make_segmenter)
    hints: list[str] = field(default_factory=list)
    correct: Callable[[str], str] = lambda t: t   # voice.corrections
    wake: object | None = None                    # WakeWords, or None (no trained name)
    wake_name: str | None = None                  # its model that means the name
    wake_mode: str = "transcript"
    score: Callable | None = None                 # voice part -> owner score (the voice profile), or None
    threshold: float | None = None                # the profile's threshold now
    turn_model: Callable | None = None            # Smart Turn, to compare end-of-turn modes
    shortest_s: float = 0.8                       # speaker_id.SHORTEST_CLIP_S

    def name_threshold(self) -> float:
        if self.wake is None or not self.wake_name:
            return 1.0
        return float((getattr(self.wake, "thresholds", None) or {}).get(self.wake_name, getattr(self.wake, "threshold", 0.5)))


def replay(seg, pcm, wake=None, wake_name: str | None = None) -> list[dict]:
    """Feed a recording through a segmenter (and the name model) 30 ms at a time, as the listener does. Each
    utterance: its clip, where the speech sits in it, when it started (s), how long after the speech ended it was
    handed over (ms, on the recording's own clock) and the name model's best score around its start."""
    import numpy as np

    frame = 480
    out, scores, started = [], [], None
    if wake is not None:
        wake.reset()
    for i in range(0, len(pcm) - frame + 1, frame):
        f = pcm[i:i + frame]
        if wake is not None and wake_name:
            wake.feed(f)
            scores.append((i / SR, float(wake.last_scores.get(wake_name, 0.0))))
        was = seg.in_speech
        frames = seg.feed(f)
        if seg.in_speech and not was:
            started = i / SR
        if frames is None:
            continue
        clip = np.concatenate(frames)
        a, b, ago = seg.last_span or (0, len(clip), 0)
        at = started if started is not None else (i + frame - len(clip)) / SR
        out.append({"clip": clip, "span": (a, b), "start": at, "delay_ms": round((len(clip) - b + ago) / SR * 1000)})
        started = None
    for u in out:  # the name model scores a phrase just after it's said
        near = [s for t, s in scores if u["start"] - 1.0 <= t <= u["start"] + WAKE_ANCHOR_S + 1.5]
        u["wake_peak"] = round(max(near), 3) if near else None
    return out


def hear(chain: Chain, u: dict, plain: bool = False) -> dict:
    """What the listener makes of one utterance: the text, whether it woke, the voice score."""
    samples = u["clip"].astype("float32") / 32768.0
    t0 = time.perf_counter()
    text = chain.correct(chain.transcribe(samples, chain.hints))
    out = {"text": text, "stt_ms": round((time.perf_counter() - t0) * 1000), "start": u["start"],
           "delay_ms": u["delay_ms"], "wake_peak": u.get("wake_peak")}
    if plain and chain.hints:
        out["plain"] = chain.correct(chain.transcribe(samples, ()))
    out["hallucination"] = _norm(text.replace(" ", "")) in {h.replace(" ", "") for h in HALLUCINATIONS}
    named = split_wake(text, chain.wake_words)[0]
    acoustic = u.get("wake_peak") is not None and u["wake_peak"] >= chain.name_threshold()
    mode = chain.wake_mode
    out["woke"] = not out["hallucination"] and bool(
        acoustic if mode == "acoustic" else (named or acoustic) if mode == "hybrid" else named)
    a, b = u["span"]
    voice = samples[a:b]
    out["score"] = round(float(chain.score(voice)), 3) if chain.score and len(voice) >= chain.shortest_s * SR else None
    return out


def _pad(pcm, after_s: float):
    import numpy as np

    return np.concatenate([np.zeros(SR // 2, np.int16), pcm, np.zeros(int(after_s * SR), np.int16)])


def _after_s(cfg: dict) -> float:
    return max(int(cfg.get("turn_max_silence_ms", 1500)), int(cfg.get("endpoint_ms", 400)),
               int(cfg.get("silence_ms", 800))) / 1000 + 0.6


def splits(chain: Chain, commands: list, cfg: dict) -> dict:
    """Voice detection alone, at other settings: how many commands came out in more than one piece (cut off) or
    not at all, and how long after you stopped each one was handed over."""
    seg, cut, missed, delays = chain.segmenter(cfg), 0, 0, []
    for _ref, pcm in commands:
        seg.reset()
        utts = replay(seg, _pad(pcm, _after_s(cfg)))
        missed += not utts
        cut += len(utts) > 1
        delays += [utts[-1]["delay_ms"]] if utts else []
    return {"cut": cut, "missed": missed, "delay_ms": _pct(delays, 50)}


def _pct(values, p):
    v = sorted(x for x in values if x is not None)
    return v[min(len(v) - 1, int(len(v) * p / 100))] if v else None


def wer_of(pairs) -> float | None:
    errs = n = 0
    for ref, hyp in pairs:
        e, k = wer(ref, hyp)
        errs, n = errs + e, n + k
    return round(errs / n * 100, 1) if n else None


class RealBench:
    """Analyse one set of recordings: ``run(commands, background) -> report``."""

    def __init__(self, chain: Chain, progress: Callable[[str], None] = lambda _t: None, clock=time.time):
        self.chain, self.progress, self.clock = chain, progress, clock

    def run(self, commands: list, background) -> dict:
        ch, cfg = self.chain, dict(self.chain.cfg)
        self.progress(f"Listening to your {len(commands)} commands…")
        seg = ch.segmenter(cfg)
        rows = []
        for ref, pcm in commands:
            seg.reset()
            heard = [hear(ch, u, plain=True) for u in replay(seg, _pad(pcm, _after_s(cfg)), ch.wake, ch.wake_name)]
            first = heard[0] if heard else None
            name_only = len(heard) > 1 and split_wake(first["text"], ch.wake_words) == (True, "")
            rows.append({"said": ref, "heard": " ".join(h["text"] for h in heard),
                         "plain": " ".join(h.get("plain", h["text"]) for h in heard), "pieces": len(heard),
                         "cut": len(heard) > 1 and not name_only,  # "Vesper," then a pause: it waits for the rest
                         "named": bool(ch.wake_words) and split_wake(ref, ch.wake_words[:1])[0],
                         "woke": bool(first and first["woke"]), "score": first["score"] if first else None,
                         "wake_peak": first["wake_peak"] if first else None,
                         "endpoint_ms": heard[-1]["delay_ms"] if heard else None,
                         "stt_ms": heard[-1]["stt_ms"] if heard else None,
                         "latency_ms": heard[-1]["delay_ms"] + heard[-1]["stt_ms"] if heard else None})
        bg_s = len(background) / SR if background is not None else 0
        bg = []
        if bg_s:
            self.progress(f"Listening to {round(bg_s / 60, 1)} minutes of background…")
            seg.reset()
            bg = [hear(ch, u) for u in replay(seg, background, ch.wake, ch.wake_name)[:400]]
        self.progress("Trying other settings on the same recordings…")
        report = self._report(rows, bg, bg_s, self._try_settings(commands, background, cfg))
        report["recommendations"] = recommend(report, cfg, ch)
        return report

    def _try_settings(self, commands, background, cfg) -> dict:
        ch = self.chain
        energy = getattr(ch.segmenter({**cfg, "end_of_turn": "silence"}), "kind", "energy") == "energy"
        key = "silence_ms" if energy else "endpoint_ms"
        base = {**cfg, "end_of_turn": "silence"}
        out: dict = {"endpoint_key": key, "endpoint": {ms: splits(ch, commands, {**base, key: ms}) for ms in ENDPOINTS},
                     "vad": {}, "turn": None}
        if ch.turn_model is not None:
            now = int(cfg.get(key, 800 if energy else 400))
            out["turn"] = {"smart": splits(ch, commands, {**cfg, "end_of_turn": "smart"}),
                           "silence": out["endpoint"].get(now) or splits(ch, commands, base)}
        if not energy and background is not None and len(background):
            for t in VAD_THRESHOLDS:
                c = {**base, "vad_threshold": t}
                seg = ch.segmenter(c)
                out["vad"][t] = {"background_segments": len(replay(seg, background)), **splits(ch, commands, c)}
        return out

    def _report(self, rows, bg, bg_s, tried) -> dict:
        ch = self.chain
        named = [r for r in rows if r["named"]]
        owner = [r["score"] for r in rows if r["score"] is not None]
        others = [b["score"] for b in bg if b["score"] is not None and not b["hallucination"]]
        false = [b for b in bg if b["woke"]]
        would_run = [b for b in false if ch.threshold is None or b["score"] is None or b["score"] >= ch.threshold]
        per_hour = (lambda n: round(n / (bg_s / 3600), 1)) if bg_s else (lambda n: None)
        return {
            "when": self.clock(), "commands": len(rows), "background_s": round(bg_s), "hints": len(ch.hints),
            "wer": wer_of((r["said"], r["heard"]) for r in rows),
            "wer_without_names": wer_of((r["said"], r["plain"]) for r in rows) if ch.hints else None,
            "named": len(named), "missed_wakes": sum(not r["woke"] for r in named),
            "cut_off": sum(r["cut"] for r in rows),
            "false_wakes": len(false), "false_wakes_per_hour": per_hour(len(false)),
            "would_run": len(would_run), "would_run_per_hour": per_hour(len(would_run)),
            "false_heard": [b["text"] for b in false][:10],
            "threshold": ch.threshold, "owner_scores": owner, "other_scores": others,
            "latency_ms": {"p50": _pct([r["latency_ms"] for r in rows], 50),
                           "p90": _pct([r["latency_ms"] for r in rows], 90),
                           "endpoint_p50": _pct([r["endpoint_ms"] for r in rows], 50),
                           "stt_p50": _pct([r["stt_ms"] for r in rows], 50)},
            "wake": {"mode": ch.wake_mode, "model": ch.wake_name,
                     "threshold": ch.name_threshold() if ch.wake_name else None,
                     "owner_peaks": [r["wake_peak"] for r in named if r["wake_peak"] is not None],
                     "background_peaks": [b["wake_peak"] for b in bg if b["wake_peak"] is not None]},
            "tried": tried, "rows": rows,
        }


def split_point(good: list[float], bad: list[float], lo: float, hi: float, false_cost: float = 3.0) -> float | None:
    """The threshold that best separates ``good`` (should pass) from ``bad`` (shouldn't): the middle of the gap when
    there is one, else the fewest mistakes, letting one in counting ``false_cost`` times turning one away. Without
    any ``bad`` it sits just under your weakest. None: too few of yours to say."""
    if len(good) < 5:
        return None
    weak = sorted(good)[int(len(good) * 0.05)]  # ignore the odd one-off (a cough, a mumble)
    if not bad:
        return round(min(hi, max(lo, weak - 0.03)), 2)
    if max(bad) < weak:
        return round(min(hi, max(lo, (max(bad) + weak) / 2)), 2)
    grid = [round(lo + i * 0.01, 2) for i in range(int(round((hi - lo) / 0.01)) + 1)]
    return min(grid, key=lambda t: (sum(g < t for g in good) + false_cost * sum(b >= t for b in bad), -t))


CUT_COST_S = 4.0  # a command cut off costs about this much: saying it again


def _ending(tried: dict, cfg: dict, n: int) -> list[dict]:
    """How a sentence should end, weighing cut-offs (each costs CUT_COST_S of saying it again) against the wait after
    every command: silence of each length tried, or natural end of turn. One choice, as one or two settings."""
    key = tried.get("endpoint_key", "endpoint_ms")
    ends = {("silence", int(ms)): v for ms, v in (tried.get("endpoint") or {}).items()}
    if (tried.get("turn") or {}).get("smart"):
        ends[("smart", 0)] = tried["turn"]["smart"]
    if not ends or not n:
        return []
    cost = {o: (v["cut"] + v["missed"]) * CUT_COST_S * 1000 + (v["delay_ms"] or 0) * n for o, v in ends.items()}
    smart_now = cfg.get("end_of_turn", "silence") == "smart"
    now_ms = int(cfg.get(key, 400 if key == "endpoint_ms" else 800))
    now = ("smart", 0) if smart_now else ("silence", now_ms)
    best = min(ends, key=lambda o: (cost[o], o[1]))
    if best == now or (now in cost and cost[best] > cost[now] * 0.95):
        return []

    def say(o):
        v = ends[o]
        what = "Natural end of turn" if o[0] == "smart" else f"{o[1]} ms of silence"
        return f"{what}: {v['cut']} of {n} commands cut off, answers {v['delay_ms']} ms after you stop"

    why = f"{say(best)}. Now, {say(now)[0].lower() + say(now)[1:]}." if now in ends else f"{say(best)}."
    out = []
    if (best[0] == "smart") != smart_now:
        out.append({"key": "voice.end_of_turn", "label": "End of turn", "current": "smart" if smart_now else "silence",
                    "value": best[0], "why": why, "group": "ending"})
    if best[0] == "silence" and best[1] != now_ms:
        out.append({"key": f"voice.{key}", "label": "Silence that ends a sentence", "current": now_ms,
                    "value": best[1], "unit": "ms", "why": why, "group": "ending"})
    return out


def recommend(report: dict, cfg: dict, chain: Chain) -> list[dict]:
    """The settings your numbers point to, each with why: only ones that differ from what you have now."""
    from .speaker_id import MAX_THRESHOLD, MIN_THRESHOLD

    out = []
    tried = report.get("tried") or {}
    out += _ending(tried, cfg, report.get("commands") or 0)
    vad = tried.get("vad") or {}
    now_t = float(cfg.get("vad_threshold", 0.6))
    cur = vad.get(now_t)
    if cur:
        ok = [t for t, v in vad.items() if v["missed"] == 0 and v["cut"] <= cur["cut"]]
        pick = max(ok) if ok else None
        if pick is not None and pick > now_t and vad[pick]["background_segments"] <= cur["background_segments"] * 0.75:
            out.append({"key": "voice.vad_threshold", "label": "Voice detector", "current": now_t, "value": pick,
                        "why": f"Hears every command at {pick}, and takes {vad[pick]['background_segments']} background "
                               f"sounds for speech instead of {cur['background_segments']}."})
        elif cur["missed"] and ok and min(ok) < now_t:
            out.append({"key": "voice.vad_threshold", "label": "Voice detector", "current": now_t, "value": min(ok),
                        "why": f"{cur['missed']} commands weren't heard at all at {now_t}; none at {min(ok)}."})
    if chain.threshold is not None:
        owner, others = report["owner_scores"], report["other_scores"]
        t = split_point(owner, others, MIN_THRESHOLD, MAX_THRESHOLD)
        if t is not None and abs(t - chain.threshold) >= 0.02:
            now = chain.threshold
            out.append({"key": "speaker_threshold", "label": "Voice match", "current": now, "value": t,
                        "why": f"At {now}: {sum(s < now for s in owner)} of your {len(owner)} commands turned away, "
                               f"{sum(s >= now for s in others)} of {len(others)} other voices let in. At {t}: "
                               f"{sum(s < t for s in owner)} and {sum(s >= t for s in others)}."})
    w = report.get("wake") or {}
    if w.get("model") and w.get("threshold") is not None:
        t = split_point(w["owner_peaks"], w["background_peaks"], 0.2, 0.95)
        if t is not None and abs(t - w["threshold"]) >= 0.03:
            out.append({"key": f"voice.wake_thresholds.{w['model']}", "label": "Name model", "current": w["threshold"],
                        "value": t, "why": f"Your name scores {_pct(w['owner_peaks'], 10)} or more 9 times in 10; "
                                           f"the background peaks at {max(w['background_peaks'], default=0)}."})
    with_names, without = report.get("wer"), report.get("wer_without_names")
    if with_names is not None and without is not None and float(cfg.get("hotwords_score", 2.0) or 0) > 0 \
            and with_names > without + 2:
        out.append({"key": "voice.hotwords_score", "label": "Expect names", "current": cfg.get("hotwords_score", 2.0),
                    "value": 0, "why": f"Word errors: {with_names}% expecting names, {without}% without."})
    return out


def apply(cfg, recommendations: list[dict], keys: list[str], verifier=None) -> list[str]:
    """Save the chosen recommendations: data/settings.yaml, and the voice match into the voice profile."""
    done = []
    for rec in recommendations:
        if rec["key"] not in keys:
            continue
        if rec["key"] == "speaker_threshold":
            if verifier is not None and verifier.set_threshold(rec["value"]):
                done.append(rec["key"])
            continue
        save_setting(cfg, rec["key"], rec["value"])
        done.append(rec["key"])
    return done
