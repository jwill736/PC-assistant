"""What Vesper is using: Setup → Diagnostics → "What Vesper is using", and the same table in the diagnostics report.

``measure()`` watches Vesper for 30 seconds when J asks, and only then:

- CPU: the whole process, and per part, from each thread's own CPU time (the voice listener, speech output, OBS's
  event feed, every poller, the web server, and the native threads the speech models and the audio run on);
- RAM: the process, and what each speech model added when it loaded (``loading()`` notes it at load time);
- video memory: each model Ollama holds, how much of the card it takes and when it unloads (flagged mid-game);
  the card's own used/total; Vesper itself uses none (its speech runs on the CPU);
- wakeups a second (the process's voluntary context switches), and every poller's period, pace and run time;
- programs launched in the last hour, by name (nvidia-smi, git, powercfg, ffmpeg…);
- model calls in the last hour, by who asked (a skill, J, the briefing), which model and whether it had to load.

When nobody is looking it costs nothing: no thread, no sampling. What is kept all the time is a timestamp appended
where something expensive already happens (a program launched, a model asked, a speech model loaded).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import Counter, deque
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

HOUR = 3600.0
MEASURE_S = 30.0
_LOCK = threading.Lock()
_LAUNCHES: deque = deque(maxlen=20000)  # (time, program)
_CALLS: deque = deque(maxlen=5000)  # (time, who, model, loaded: bool | None, keep_alive)
_MODELS: dict[str, dict] = {}  # speech model -> {"rss_mb", "load_s", "at"}
_STARTED = time.time()
_HOOKED = [False]
LAST: list = [None]  # the latest measurement (the HUD card and the diagnostics report show it)


# ---- the counters (always on, a few microseconds each, only where something expensive happens anyway) ----------

def launched(program: str) -> None:
    with _LOCK:
        _LAUNCHES.append((time.time(), program))


def hook_launches() -> None:
    """Count every program this process starts (subprocess.Popen), by name. Once, at startup."""
    import subprocess

    if _HOOKED[0]:
        return
    _HOOKED[0] = True
    real = subprocess.Popen.__init__

    def init(self, args, *a, **kw):
        try:
            launched(program(args, kw.get("executable")))
        except Exception:
            pass
        return real(self, args, *a, **kw)
    subprocess.Popen.__init__ = init


def program(args, executable=None) -> str:
    """The program a Popen starts: "nvidia-smi", "git", "powercfg"…"""
    if executable:
        first = os.fsdecode(executable)
    elif isinstance(args, (str, bytes)):
        line = os.fsdecode(args).strip()
        first = line[1:].split('"', 1)[0] if line.startswith('"') else (line.split() or [""])[0]
    elif isinstance(args, os.PathLike):
        first = os.fsdecode(args)
    else:
        first = os.fsdecode(args[0]) if args else ""
    return Path(first.replace("\\", "/")).name.lower().removesuffix(".exe") or "?"


def _who() -> str:
    """The skill (or part of Vesper) whose code is asking: the first caller outside the brain."""
    f = sys._getframe(2)
    for _ in range(12):
        if f is None:
            break
        mod = f.f_globals.get("__name__", "")
        if mod.startswith("assistant.skills."):
            name = mod.rsplit(".", 1)[-1]
            if name != "tasks_agent" or f.f_code.co_name not in ("think", "_gated"):
                return name
        elif mod.startswith("assistant.") and not mod.startswith(("assistant.brain.local_llm", "assistant.brain.assistant",
                                                                  "assistant.resources")):
            return mod.removeprefix("assistant.").replace("brain.", "")
        elif mod == "assistant.brain.assistant" and f.f_code.co_name in ("_ask_local", "_local_briefing"):
            return "you (a question)" if f.f_code.co_name == "_ask_local" else "briefing"
        f = f.f_back
    return "other"


def model_call(model: str, keep_alive=None, load_s: float | None = None, who: str | None = None) -> None:
    """A model was asked (Ollama tells how long it spent loading it: over half a second means it had to)."""
    loaded = None if load_s is None else load_s > 0.5
    who = who or _who()
    try:
        from .brain.local_llm import asked

        if asked() and not who.startswith("you"):
            who += " (you asked)"
    except Exception:
        pass
    with _LOCK:
        _CALLS.append((time.time(), who, model, loaded, keep_alive))


@contextmanager
def loading(name: str):
    """Note what loading a model added to Vesper's memory and how long it took (the speech models)."""
    before, t0 = rss_mb(), time.perf_counter()
    try:
        yield
    finally:
        loaded(name, before, t0)


def loaded(name: str, before_mb: float | None, t0: float) -> None:
    """A model finished loading: what it added since ``before_mb`` (rss_mb()) and ``t0`` (time.perf_counter())."""
    after = rss_mb()
    if before_mb is not None and after is not None:
        with _LOCK:
            _MODELS[name] = {"rss_mb": round(max(after - before_mb, 0.0)), "load_s": round(time.perf_counter() - t0, 1),
                             "at": time.time()}


def rss_mb() -> float | None:
    try:
        import psutil

        return psutil.Process().memory_info().rss / 1048576
    except Exception:
        return None


# ---- the measurement (on demand) --------------------------------------------------------------------------------

def _thread_names() -> dict[int, str]:
    return {t.native_id: t.name for t in threading.enumerate() if t.native_id is not None}


def _part(name: str | None) -> str:
    """A thread's group in the table."""
    if name is None:
        return "native threads (speech models, audio, web server workers)"
    if name == "MainThread":
        return "web server (HUD, phone)"
    if name.startswith("poll-"):
        return f"poller: {name[5:]}"
    if name in ("voice", "voice listener"):
        return "voice listener"
    if name in ("tts", "speech output"):
        return "speech output"
    if "trigger" in name or "recv_events" in name:
        return "OBS events"
    if name.startswith(("AnyIO worker", "ThreadPoolExecutor", "asyncio")):
        return "web requests and commands"
    return name


def _wakeups(proc) -> int | None:
    """Times any of Vesper's threads went to sleep and was woken (voluntary context switches), all threads. Windows
    counts them per process; Linux's per-process number is the main thread's only, so its threads are added up."""
    if sys.platform.startswith("linux"):
        total = 0
        try:
            for tid in os.listdir("/proc/self/task"):
                with open(f"/proc/self/task/{tid}/status") as f:
                    for line in f:
                        if line.startswith("voluntary_ctxt_switches:"):
                            total += int(line.split()[1])
                            break
            return total
        except (OSError, ValueError):
            pass
    try:
        return proc.num_ctx_switches().voluntary
    except Exception:
        return None


def _threads(proc) -> dict[int, float]:
    try:
        return {t.id: t.user_time + t.system_time for t in proc.threads()}
    except Exception:
        return {}


def _proc_numbers(proc) -> dict:
    out: dict = {}
    try:
        mem = proc.memory_info()
        out["rss_mb"] = round(mem.rss / 1048576)
        private = getattr(mem, "private", None)  # Windows: what Task Manager calls the process's memory
        out["private_mb"] = round(private / 1048576) if private else None
    except Exception:
        pass
    for key, fn in (("handles", "num_handles"), ("files", "num_fds"), ("threads", "num_threads")):
        try:
            out[key] = getattr(proc, fn)()
        except Exception:
            pass
    return out


def _pollers(runtime) -> list[dict]:
    sup = getattr(runtime, "supervisor", None)
    if sup is None:
        return []
    up_h = max((time.time() - getattr(runtime, "started", _STARTED)) / HOUR, 1 / 60)
    rows = []
    for p in sup.snapshot():
        if p.get("kind") != "poller":
            continue
        runs_h = p.get("runs", 0) / up_h
        rows.append({"name": p["name"], "every_s": p.get("interval"), "in_game_s": p.get("game_interval"),
                     "pace": p.get("throttle") or "normal", "runs_h": round(runs_h, 1), "avg_ms": p.get("avg_ms"),
                     "max_ms": p.get("max_ms"), "ms_h": round(runs_h * (p.get("avg_ms") or 0))})
    return sorted(rows, key=lambda r: -r["ms_h"])


def _ollama(runtime, now: float) -> list[dict] | None:
    """What Ollama holds now (None: no Ollama found)."""
    local = getattr(getattr(runtime, "assistant", None), "local", None)
    if local is None or (getattr(local, "found", None) or {}).get("kind") != "ollama":
        return None
    try:
        rows = local.loaded()
    except Exception:
        return None
    return [{"name": m["name"], "vram_gb": m.get("vram_gb"), "ram_gb": m.get("ram_gb"),
             "unloads_in_s": round(m["expires_at"] - now) if m.get("expires_at") else None} for m in rows]


def _gpu(runtime) -> dict | None:
    system = getattr(getattr(runtime, "svc", None), "system", None)
    try:
        gpus = system.gpus() if system is not None else []
    except Exception:
        gpus = []
    if not gpus:
        return None
    g = gpus[0]
    used, total = g.get("mem_used_mb"), g.get("mem_total_mb")
    return {"name": g.get("name"), "used_gb": round(used / 1024, 1) if used is not None else None,
            "total_gb": round(total / 1024, 1) if total else None, "util": g.get("util")}


def _last_hour(now: float):
    since = max(now - HOUR, _STARTED)
    with _LOCK:
        launches = [p for t, p in _LAUNCHES if t >= since]
        calls = [c for c in _CALLS if c[0] >= since]
    return since, launches, calls


def _calls_table(calls: list) -> list[dict]:
    by: dict[str, dict] = {}
    for _t, who, model, loaded, keep in calls:
        row = by.setdefault(who, {"who": who, "calls": 0, "models": Counter(), "loads": 0, "keep_alive": set()})
        row["calls"] += 1
        row["models"][model] += 1
        row["loads"] += 1 if loaded else 0
        if keep is not None:
            row["keep_alive"].add(str(keep))
    return [{**r, "models": dict(r["models"]), "keep_alive": sorted(r["keep_alive"])}
            for r in sorted(by.values(), key=lambda r: -r["calls"])]


def measure(runtime=None, seconds: float = MEASURE_S, sleep=time.sleep, on_progress=None) -> dict:
    """Watch Vesper for ``seconds`` and report what it used (see the module's docstring)."""
    snap = begin()
    left = seconds
    while left > 0:
        step = min(1.0, left)
        sleep(step)
        left -= step
        if on_progress:
            on_progress(round(1 - left / seconds, 2) if seconds else 1.0)
    return end(snap, runtime)


def begin() -> dict:
    """Start a measurement: the process's and each thread's CPU time and wakeups so far."""
    import psutil

    proc = psutil.Process()
    ctx0 = _wakeups(proc)
    return {"proc": proc, "t": time.time(), "cpu": proc.cpu_times(), "threads": _threads(proc), "ctx": ctx0}


def end(snap: dict, runtime=None) -> dict:
    """What was used since ``begin()``."""
    import psutil

    proc, t0, cpu0, th0, ctx0 = snap["proc"], snap["t"], snap["cpu"], snap["threads"], snap["ctx"]
    cores = psutil.cpu_count() or 1
    t1, cpu1, th1 = time.time(), proc.cpu_times(), _threads(proc)
    dur = max(t1 - t0, 1e-6)
    ctx1 = _wakeups(proc)
    wake = round((ctx1 - ctx0) / dur, 1) if ctx0 is not None and ctx1 is not None else None
    names = _thread_names()
    parts: Counter = Counter()
    for tid, cpu in th1.items():
        parts[_part(names.get(tid))] += cpu - th0.get(tid, 0.0)
    total = (cpu1.user + cpu1.system) - (cpu0.user + cpu0.system)
    by_part = [{"part": k, "cpu_pct": round(100 * v / dur, 2)} for k, v in parts.most_common() if v > 0]
    since, launches, calls = _last_hour(t1)
    window_h = max((t1 - since) / HOUR, 1 / 60)
    gaming = False
    try:
        gaming = bool(runtime.assistant.gaming()) if runtime is not None else False
    except Exception:
        pass
    with _LOCK:
        models = [{"name": k, **v} for k, v in sorted(_MODELS.items(), key=lambda kv: -kv[1]["rss_mb"])]
    report = {
        "at": t1, "seconds": round(dur, 1), "cores": cores,
        "cpu_pct": round(100 * total / dur, 2), "cpu_pct_all": round(100 * total / dur / cores, 3),
        "wakeups_s": wake, **_proc_numbers(proc),
        "by_part": by_part,
        "speech_models": models,
        "ollama": _ollama(runtime, t1), "gpu": _gpu(runtime), "in_game": gaming,
        "vesper_vram_gb": 0,
        "pollers": _pollers(runtime),
        "launches_window_h": round(window_h, 2),
        "launches": [{"program": p, "count": n, "per_h": round(n / window_h)} for p, n in Counter(launches).most_common()],
        "nvml_reads": _nvml_reads(),
        "model_calls": _calls_table(calls),
    }
    report["headline"] = headline(report)
    LAST[0] = report
    return report


def recent(snap: dict | None, runtime=None, max_age_s: float = HOUR) -> dict | None:
    """For the diagnostics report: the last 30 s measurement if it's under an hour old, else what was used while the
    report was being made (``snap`` from begin() when it started: no extra wait)."""
    last = LAST[0]
    if last is not None and time.time() - last["at"] < max_age_s:
        return last
    if snap is None:
        return None
    out = end(snap, runtime)
    out["while"] = "making this report"
    return out


def _nvml_reads() -> int | None:
    try:
        from .integrations import nvml

        return nvml.LAUNCHES_SAVED[0] if nvml.available() else None
    except Exception:
        return None


def headline(r: dict) -> str:
    """The first line, one J can act on: "Vesper: 0.6% CPU, 840 MB RAM, no video memory (Ollama: …)"."""
    held = [m for m in r.get("ollama") or [] if m.get("vram_gb")]
    if held:
        def when(m):
            s = m.get("unloads_in_s")
            return "" if s is None else ", unloads in under a minute" if s < 60 else \
                f", unloads in {round(s / 60)} min" if s < 3 * HOUR else ", stays loaded"
        ollama = "; ".join(f"{m['name']} {m['vram_gb']:g} GB{when(m)}" for m in held)
        ollama = f" (Ollama: {ollama}{' — mid-game' if r.get('in_game') else ''})"
    else:
        ollama = " (Ollama: nothing loaded)" if r.get("ollama") is not None else ""
    return (f"Vesper: {r['cpu_pct']:.1f}% of one CPU core ({r['cpu_pct_all']:.2f}% of your CPU), "
            f"{r.get('rss_mb', '?')} MB RAM, no video memory of its own{ollama}.")


def markdown(r: dict) -> list[str]:
    """The diagnostics report's section."""
    how = (" while this report was made (Setup → Diagnostics → What Vesper is using → Measure 30 s gives a steadier "
           "one)" if r.get("while") else "")
    out = [f"Measured over {r['seconds']:g} s{how}, {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['at']))}.", "",
           f"**{r['headline']}**", "",
           f"- Wakeups: {r.get('wakeups_s')} a second; threads: {r.get('threads')}; "
           f"{'handles' if r.get('handles') is not None else 'open files'}: {r.get('handles', r.get('files'))}"
           + (f"; private memory: {r['private_mb']} MB" if r.get("private_mb") else ""), "",
           "| part | CPU (% of one core) |", "|---|---|"]
    out += [f"| {p['part']} | {p['cpu_pct']:.2f} |" for p in r["by_part"][:15]] or ["| (nothing measurable) | 0 |"]
    out += [""]
    if r["speech_models"]:
        out += ["| speech model | RAM | load |", "|---|---|---|"]
        out += [f"| {m['name']} | {m['rss_mb']} MB | {m['load_s']} s |" for m in r["speech_models"]] + [""]
    if r.get("ollama"):
        out += ["| Ollama model | video memory | in RAM | unloads in |", "|---|---|---|---|"]
        out += [f"| {m['name']} | {m['vram_gb']} GB | {m['ram_gb']} GB | "
                f"{'?' if m['unloads_in_s'] is None else str(round(m['unloads_in_s'] / 60, 1)) + ' min'} |"
                for m in r["ollama"]] + [""]
    if r.get("gpu"):
        g = r["gpu"]
        out += [f"Graphics card: {g['name']}, {g['used_gb']} of {g['total_gb']} GB used"
                + (" (a fullscreen game is running)" if r.get("in_game") else "") + "."]
        if r.get("nvml_reads") is not None:
            out += ["GPU readings come through NVML: no nvidia-smi launches."]
        out += [""]
    out += [f"Programs launched (last {r['launches_window_h']:g} h): "
            + (", ".join(f"{x['program']} {x['count']} ({x['per_h']}/h)" for x in r["launches"]) or "none"), ""]
    out += ["| model calls (last hour) | calls | models | had to load | keep-alive |", "|---|---|---|---|---|"]
    out += [f"| {c['who']} | {c['calls']} | {', '.join(f'{k} ×{v}' for k, v in c['models'].items())} | {c['loads']} | "
            f"{', '.join(c['keep_alive'])} |" for c in r["model_calls"]] or ["| none | | | | |"]
    out += ["", "| poller | every | in a game | pace now | runs/h (since start) | avg ms | ms/h |",
            "|---|---|---|---|---|---|---|"]
    out += [f"| {p['name']} | {p['every_s']:g} s | "
            f"{'same' if p['in_game_s'] is None else 'paused' if not p['in_game_s'] else str(round(p['in_game_s'])) + ' s'} | "
            f"{p['pace']} | {p['runs_h']} | {p['avg_ms']} | {p['ms_h']} |" for p in r["pollers"][:25]]
    return out
