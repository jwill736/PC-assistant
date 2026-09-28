"""Health check: does each connection actually work right now?

``python -m assistant --doctor`` runs the PC scan plus live checks and prints
PASS / WARN / FAIL with the fix for each. The HUD's Setup view runs the same
checks (minus the slow ones) against the live services.
"""

from __future__ import annotations

import shutil
import socket
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Callable

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def _guard(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as exc:  # a crashing check is itself a finding
        return Check(name, FAIL, f"{type(exc).__name__}: {exc}")


def check_python() -> Check:
    ok = sys.version_info >= (3, 11)
    return Check("Python", PASS if ok else FAIL, sys.version.split()[0],
                 "" if ok else "Install Python 3.11+ from python.org and re-run setup.bat.")


def check_files(cfg) -> list[Check]:
    root = cfg.root
    out = [Check("config.yaml", PASS if (root / "config.yaml").exists() else WARN,
                 "found" if (root / "config.yaml").exists() else "missing — running on defaults + PC scan",
                 "" if (root / "config.yaml").exists() else "copy config.example.yaml to config.yaml and set your goals.")]
    goal = (cfg.get("goals") or {}).get("north_star")
    out.append(Check("North-star goal", PASS if goal else WARN, goal or "not set",
                     "" if goal else "Set goals.north_star in config.yaml — briefings can't prioritise without it."))
    return out


def check_claude(cfg) -> Check:
    import anthropic

    key = cfg.secret(cfg["claude"].get("api_key_env"))
    if not key:
        return Check("Claude API", WARN, "no API key — offline command set only",
                     "Add ANTHROPIC_API_KEY to .env (console.anthropic.com → API keys).")
    client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=15)
    model = cfg["claude"]["model"]
    try:
        client.models.retrieve(model)  # free call: validates the key and the model in one go
    except anthropic.AuthenticationError:
        return Check("Claude API", FAIL, "key rejected", "Create a new key in the Anthropic console and update .env.")
    except anthropic.NotFoundError:
        return Check("Claude API", WARN, f"key valid, but {model} isn't available to it",
                     "Set claude.model in config.yaml to a model your account can use.")
    except anthropic.APIConnectionError:
        return Check("Claude API", WARN, "couldn't reach api.anthropic.com", "Check the internet connection / firewall.")
    return Check("Claude API", PASS, f"key valid, {model} available")


def check_brain(cfg, brain=None) -> Check:
    """What answers open questions: Claude, a model on this PC, or nothing (the router only)."""
    from .brain import local_llm

    has_key = bool(cfg.secret(cfg["claude"].get("api_key_env")))
    lcfg = (cfg.get("brain") or {}).get("local") or {}
    local = brain if brain is not None else local_llm.LocalBrain(lcfg)
    llm = local.refresh(force=True) if lcfg.get("enabled", True) else None
    provider = (cfg.get("brain") or {}).get("provider", "auto")
    if provider == "local":
        return (Check("Brain", PASS, f"local: {llm.label}") if llm else
                Check("Brain", FAIL, "set to local, but no local model server is running",
                      "Start Ollama or LM Studio (with a model downloaded)."))
    if has_key:
        return Check("Brain", PASS, "Claude" + (f"; {llm.label} also found" if llm else ""))
    if llm:
        return Check("Brain", PASS, f"local: {llm.label} (no Claude key)")
    return Check("Brain", WARN, "no model: only built-in commands work",
                 "Start Ollama/LM Studio with a Llama model, or add ANTHROPIC_API_KEY to .env.")


def check_obs(cfg, obs=None) -> Check:
    if not cfg["obs"].get("enabled", True):
        return Check("OBS", SKIP, "disabled in config")
    if obs is None:
        from .integrations.obs import OBSController
        from .services import obs_password

        o = cfg["obs"]
        obs = OBSController(o["host"], o["port"], obs_password(cfg), o.get("scene_aliases"))
    st = obs.status()
    if st.get("connected"):
        return Check("OBS", PASS, f"connected · scene {st.get('current_scene')} · {len(st.get('scenes', []))} scenes")
    err = st.get("error") or "not reachable"
    fix = ("Start OBS, then Tools → WebSocket Server Settings → Enable." if "running" in err or "off" in err
           else "Password mismatch: clear OBS_PASSWORD in .env so it's read from OBS, or copy it from OBS → "
                "Tools → WebSocket Server Settings → Show Connect Info." if "password" in err else "")
    return Check("OBS", WARN, err, fix)


def check_calendars(cfg, hub=None) -> list[Check]:
    if hub is None:
        from .integrations.calendars import CalendarHub

        hub = CalendarHub(cfg["calendars"], cfg["assistant"].get("timezone"))
    if not hub.sources:
        return [Check("Calendars", WARN, "none connected",
                      "Paste each calendar's private iCal link into .env and list it under calendars: in config.yaml.")]
    hub.refresh(force=True)
    out = []
    for c in hub.status():
        out.append(Check(f"Calendar · {c['name']}", PASS if c["ok"] else FAIL, "loads" if c["ok"] else c["error"] or "failed",
                         "" if c["ok"] else "Re-copy the Secret iCal address (Workspace admins can disable it — "
                                            "then use Google sign-in, issue #18)."))
    return out


def check_news(cfg, feed=None) -> Check:
    if feed is None:
        from .integrations.news import NewsFeed

        feed = NewsFeed(cfg["news"].get("feeds") or None)
    feed.refresh(force=True)
    st = feed.status()
    if st["items"] and not st["errors"]:
        return Check("News feeds", PASS, f"{st['items']} headlines from {st['feeds']} feeds")
    if st["items"]:
        return Check("News feeds", WARN, f"{st['items']} headlines; failing: {', '.join(st['errors'])}",
                     "Fix or remove the failing feed URLs under news.feeds.")
    return Check("News feeds", WARN, "no headlines", "Check internet access or the feed URLs under news.feeds.")


def check_browser() -> Check:
    from .integrations.browser import find_chrome

    chrome = find_chrome()
    return Check("Chrome", PASS if chrome else WARN, chrome or "not found — using the default browser",
                 "" if chrome else "Install Chrome for tabs and the app-window HUD.")


def check_claude_cli(cfg) -> Check:
    cli = shutil.which(cfg["jobs"].get("claude_code_cmd", "claude"))
    return Check("Claude Code CLI", PASS if cli else WARN, cli or "not on PATH",
                 "" if cli else "npm install -g @anthropic-ai/claude-code (enables background coding jobs).")


def check_voice(cfg, test_mic: bool, load_model: bool) -> list[Check]:
    if not cfg["voice"].get("enabled", True):
        return [Check("Voice", SKIP, "disabled in config")]
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError:
        return [Check("Voice packages", FAIL, "sounddevice / numpy not installed",
                      "pip install -r requirements-voice.txt")]
    out = []
    if test_mic:
        def mic() -> Check:
            rec = sd.rec(int(1.5 * 16000), samplerate=16000, channels=1, dtype="int16",
                         device=cfg["voice"].get("input_device"))
            sd.wait()
            level = float(np.sqrt(np.mean(rec.astype(np.float32) ** 2)))
            floor = cfg["voice"].get("min_rms", 350)
            if level < 5:
                return Check("Microphone", FAIL, "silent — muted or wrong device",
                             "Unmute the mic / pick it with voice.input_device (see the Setup view's mic list).")
            if level > floor:
                return Check("Microphone", WARN, f"room noise {level:.0f} is above the speech threshold {floor}",
                             f"Raise voice.min_rms to about {int(level * 3)} or use a noise gate.")
            return Check("Microphone", PASS, f"working · room noise {level:.0f} (threshold {floor})")
        out.append(_guard("Microphone", mic))
    from .voice import wakeword

    trained = wakeword.model_files(cfg.data_dir / "models")
    if trained and not wakeword.available():
        out.append(Check("Wake words", WARN, f"{', '.join(trained)} trained, but the runtime isn't installed",
                         "pip install -r requirements-voice.txt (adds livekit-wakeword)."))
    elif trained:
        out.append(Check("Wake words", PASS, f"trained: {', '.join(trained)}"))
    if load_model:
        def model() -> Check:
            from .voice import stt

            t0 = time.time()
            engine = stt.load(cfg["voice"], cfg.data_dir / "models")
            load_s = time.time() - t0
            t1 = time.perf_counter()
            engine.transcribe(np.zeros(16000, dtype=np.float32))  # one second of silence: a warm-up + timing
            ms = (time.perf_counter() - t1) * 1000
            label = stt.LABELS.get(engine.name, engine.name)
            if engine.name == "whisper" and ms > 600:
                return Check("Speech model", WARN, f"{label} loaded in {load_s:.1f}s · {ms:.0f} ms per second of audio",
                             "Set voice.stt_engine: parakeet (about 4x faster on CPU) or run --bench-voice.")
            return Check("Speech model", PASS, f"{label} loaded in {load_s:.1f}s · {ms:.0f} ms per second of audio")
        out.append(_guard("Speech model", model))
        out.append(_guard("Speaking voice", lambda: check_tts(cfg, sd)))
    return out


def check_tts(cfg, sd=None) -> Check:
    """Which voice replies, and how long you wait before it starts talking."""
    from .voice import neural
    from .voice.tts import resolve_engine

    tts = cfg["voice"]["tts"]
    engine = resolve_engine(tts.get("engine", "auto"))
    if engine not in neural.ENGINES:
        if engine == "none":
            return Check("Speaking voice", SKIP, "off (voice.tts.engine: none)")
        label = {"pyttsx3": "Windows SAPI voice", "browser": "browser voice (needs the HUD open)"}[engine]
        if not neural.available():
            return Check("Speaking voice", WARN, f"{label} — the natural local voices aren't installed",
                         "pip install -r requirements-voice.txt (sherpa-onnx), then pick a voice in Setup → Voice.")
        return Check("Speaking voice", PASS, label)
    if sd is not None:
        try:
            sd.query_devices(kind="output")
        except Exception:
            return Check("Speaking voice", FAIL, "no audio output device",
                         "Plug in speakers or headphones, or set voice.tts.output_device.")
    t0 = time.time()
    voice = neural.load(engine, cfg.data_dir / "models", tts.get("voice") or None, float(tts.get("speed", 1.0)),
                        int(tts.get("threads", 2)))
    voice.warm_up()
    load_s = time.time() - t0
    t1 = time.perf_counter()
    voice.synth("Switched to your gameplay scene.")
    ms = (time.perf_counter() - t1) * 1000
    detail = f"{neural.LABELS[engine]} · {voice.voice.label} · ready in {load_s:.1f}s · first words after {ms:.0f} ms"
    if ms > 900:
        return Check("Speaking voice", WARN, detail,
                     "Replies will start late. Pick Supertonic in Setup → Voice (about 5x faster on CPU).")
    return Check("Speaking voice", PASS, detail)


PC_CONTROL_LIBS = {"pycaw": "exact and per-app volume", "screen_brightness_control": "brightness",
                   "pyvda": "numbered virtual desktops", "win11toast": "Yes/No notification buttons"}


def check_pc_control(cfg, importer=None) -> Check:
    """The Windows libraries behind the system controls; each missing one only loses its feature."""
    if sys.platform != "win32" and importer is None:
        return Check("PC control", SKIP, "Windows only")
    import importlib

    importer = importer or importlib.import_module
    missing = []
    for mod, feature in PC_CONTROL_LIBS.items():
        try:
            importer(mod)
        except Exception:
            missing.append(feature)
    hotkey = (cfg.get("pc_control") or {}).get("kill_hotkey") or "none"
    if missing:
        return Check("PC control", WARN, f"missing: {', '.join(missing)} · kill switch {hotkey}",
                     "Run setup.bat again to install them.")
    return Check("PC control", PASS, f"volume, brightness, desktops, toast confirmations · kill switch {hotkey}")


def check_port(cfg) -> Check:
    host, port = cfg["server"]["host"], cfg["server"]["port"]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return Check("HUD port", WARN, f"{host}:{port} is in use",
                         "Another copy may be running, or use --port / server.port.")
    return Check("HUD port", PASS, f"{host}:{port} free")


def run_doctor(cfg, svc=None, *, test_mic: bool = True, load_model: bool = True, check_ports: bool = True,
               network: bool = True) -> dict:
    checks: list[Check] = [check_python(), *check_files(cfg)]
    if network:
        checks.append(_guard("Claude API", lambda: check_claude(cfg)))
    checks.append(_guard("Brain", lambda: check_brain(cfg)))
    checks.append(_guard("OBS", lambda: check_obs(cfg, svc.obs if svc else None)))
    if network:
        checks += _guard_list("Calendars", lambda: check_calendars(cfg, svc.calendars if svc else None))
        checks.append(_guard("News feeds", lambda: check_news(cfg, svc.news if svc else None)))
    checks.append(_guard("Chrome", check_browser))
    checks.append(_guard("Claude Code CLI", lambda: check_claude_cli(cfg)))
    checks += _guard_list("Voice", lambda: check_voice(cfg, test_mic, load_model))
    checks.append(_guard("PC control", lambda: check_pc_control(cfg)))
    if check_ports:
        checks.append(_guard("HUD port", lambda: check_port(cfg)))
    return {"ran_at": time.time(), "checks": [asdict(c) for c in checks],
            "summary": dict(Counter(c.status for c in checks))}


def _guard_list(name: str, fn: Callable[[], list[Check]]) -> list[Check]:
    try:
        return fn()
    except Exception as exc:
        return [Check(name, FAIL, f"{type(exc).__name__}: {exc}")]


LABEL = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP",
         "connected": " OK ", "found": "FOUND", "action": " TODO", "missing": " ---"}


def format_report(discovery: dict | None, doctor: dict | None) -> str:
    """Plain-text report for the console."""
    lines: list[str] = []
    if discovery:
        lines.append(f"PC scan ({discovery['duration_s']}s)")
        lines.append("-" * 60)
        for f in discovery["findings"]:
            lines.append(f"[{LABEL.get(f['status'], f['status'])}] {f['area']:<10} {f['name']}: {f['detail']}")
            if f.get("fix"):
                lines.append(f"{'':18}-> {f['fix']}")
        lines.append("")
    if doctor:
        lines.append("Health check")
        lines.append("-" * 60)
        for c in doctor["checks"]:
            lines.append(f"[{LABEL.get(c['status'], c['status'])}] {c['name']}: {c['detail']}")
            if c.get("fix"):
                lines.append(f"{'':7}-> {c['fix']}")
        s = doctor["summary"]
        lines.append("")
        lines.append(f"{s.get(PASS, 0)} passed, {s.get(WARN, 0)} warnings, {s.get(FAIL, 0)} failed")
    return "\n".join(lines)
