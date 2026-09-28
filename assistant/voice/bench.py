"""``python -m assistant --bench-voice``: which speech engine hears *you* best, on *this* PC.

Records ten short commands in your voice, then runs every installed engine
over the same recordings and reports word errors, how often the wake word was
caught, and time from end of speech to text. ``--apply`` saves the winner as
``voice.stt_engine`` in data/settings.yaml. Recordings stay in memory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from ..config import save_setting
from . import calibrate as calib
from . import stt
from .listener import split_wake

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
