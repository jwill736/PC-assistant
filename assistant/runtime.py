"""Wires everything together and runs the background pollers."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import discovery
from .brain.assistant import Assistant
from .brain.router import KILL, clean
from .bus import EventBus
from .config import Config, load_config, save_setting
from .integrations import toast
from .integrations.activity import Categorizer
from .integrations.jobs import JobRunner
from .services import Services, build_services
from .voice.hotkey import register_hotkey
from .voice import calibrate as calib
from .voice import neural
from .voice.listener import VoiceListener
from .voice.speaker_id import SpeakerVerifier, delete_profile
from .voice.tts import Speaker
from .watchdog import Supervisor

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, cfg: Config, services: Services | None = None, assistant: Assistant | None = None):
        self.cfg = cfg
        self.bus = services.bus if services else EventBus()
        self.svc = services or build_services(cfg, self.bus)
        self.assistant = assistant or Assistant(self.svc)
        tts = cfg["voice"]["tts"]
        self.speaker = Speaker(self.bus, tts.get("engine", "auto"), tts.get("rate", 190), tts.get("voice_hint", ""),
                               voice=tts.get("voice") or None, speed=float(tts.get("speed", 1.0)),
                               threads=int(tts.get("threads", 2)), models_dir=cfg.data_dir / "models",
                               output_device=tts.get("output_device"))
        self.svc.speak = self.speaker.say
        # Voice commands run here, not on the mic thread, so "stop" is heard while Claude thinks or talks.
        self._commands = ThreadPoolExecutor(max_workers=1, thread_name_prefix="command")
        self.svc.jobs = JobRunner(
            self.svc.storage, self.bus, cfg["jobs"], repo_lookup=self.svc.projects.repos,
            research_fn=self.assistant.research if self.assistant.claude_ready else None,
            announce=self.announce,
        )
        self.listener: VoiceListener | None = None
        self.verifier: SpeakerVerifier | None = None
        self.calibrator = None
        if cfg["voice"].get("enabled", True):
            self.verifier = SpeakerVerifier(cfg.data_dir, cfg["voice"].get("speaker_check", "strict"))
            self.listener = VoiceListener(
                self.bus, self.speaker, cfg["assistant"]["wake_words"],
                on_command=self.voice_command,
                cfg=cfg["voice"], hint_words=self._hint_words, verifier=self.verifier,
                models_dir=cfg.data_dir / "models",
            )
        self.supervisor = Supervisor(self.bus)
        self.started = time.time()
        # PC control safety: the kill switch also silences speech and stops background jobs.
        self.assistant.on_kill = self._on_kill
        pc = cfg.get("pc_control") or {}
        if pc.get("toast_confirm", True) and toast.available():
            self.assistant.on_pending = toast.ToastConfirmer(
                lambda pid: self.confirm_pending(pid, "toast"), lambda pid: self.assistant.cancel(pid),
                cfg["assistant"]["name"])

    def _hint_words(self) -> list[str]:
        """Vocabulary that biases speech recognition toward your scene/app names."""
        words = list(self.svc.obs.scenes)
        words += list(self.cfg["apps"].keys()) + list(self.cfg["sites"].keys())
        return words

    def voice_command(self, text: str) -> None:
        """A new turn: whatever's left of the previous reply is dropped, not spoken late."""
        turn = self.speaker.new_turn()
        if KILL.match(clean(text)):  # never queue the kill switch behind the command it has to stop
            self.kill_switch("voice")
            return
        owner = getattr(self.listener, "last_owner", "unknown") if self.listener else "unknown"
        self._commands.submit(self._run_voice_command, text, turn, owner)

    def _run_voice_command(self, text: str, turn: int, owner: str = "unknown") -> None:
        try:
            self.assistant.handle(text, "voice", turn=turn, owner=owner)
        except Exception:
            log.exception("voice command failed: %s", text)

    # ---- kill switch and confirmations -------------------------------------
    def kill_switch(self, source: str = "hotkey") -> dict:
        """Ctrl+Alt+K / tray / HUD: stop talking, drop what's waiting, cancel background jobs, and
        refuse every action (reads still work) until you resume."""
        self.assistant.stop_everything(source)
        self.speaker.say("Stopped. Say resume control when you're ready.", expects_reply=False)
        return {"ok": True, **self.assistant.control_status()}

    def resume_control(self, source: str = "hud") -> dict:
        self.assistant.resume_control(source)
        return {"ok": True, **self.assistant.control_status()}

    def _on_kill(self) -> None:
        self.speaker.interrupt(silence_turn=False)  # stop the audio now, but let "Stopped" be said
        if self.svc.jobs:
            for job in self.svc.storage.list_jobs(30):
                if job.get("status") in ("queued", "running"):
                    self.svc.jobs.cancel(job["id"])

    def confirm_pending(self, pending_id: str | None, via: str) -> str:
        """A yes from outside a command (HUD button, toast): run it, show and say the result."""
        reply = self.assistant.confirm(pending_id, via=via)
        self.bus.publish("assistant_said", {"text": reply, "kind": "confirm", "source": via})
        if via == "toast":
            self.speaker.say(reply)
        return reply

    def announce(self, text: str) -> None:
        self.bus.publish("announce", {"text": text})
        self.speaker.say(text)

    # ------------------------------------------------------------------
    def start(self) -> None:
        sup = self.supervisor
        # Long-lived services: restarted if their thread dies or they stop beating.
        if self.cfg["tracking"].get("enabled", True):
            self.svc.activity.heartbeat = lambda: sup.beat("activity tracker")
            sup.service("activity tracker", self.svc.activity.restart,
                        heartbeat_s=max(self.svc.activity.sample, 5) * 2)
        sup.service("speech output", self.speaker.restart, is_disabled=lambda: self.speaker.engine_name == "none")
        if self.listener:
            listener = self.listener
            listener.heartbeat = lambda: sup.beat("voice listener")
            sup.service("voice listener", listener.restart, heartbeat_s=30,
                        is_disabled=lambda: listener.state == "unavailable")
            register_hotkey(self.cfg["voice"].get("push_to_talk_hotkey"), listener.arm)
        register_hotkey((self.cfg.get("pc_control") or {}).get("kill_hotkey"), self.kill_switch)
        # Pollers: the supervisor owns their loops.
        sup.poller("system stats", 2, self._poll_system, stall_after=60)
        sup.poller("obs", 3, self._poll_obs, stall_after=60)
        sup.poller("activity summary", 60, self._poll_activity)
        sup.poller("twitch", 90, self._poll_twitch)
        sup.poller("projects", 120, self._poll_projects)
        sup.poller("calendars", 300, self._poll_calendar)
        sup.poller("news", 900, self._poll_news)
        sup.poller("morning briefing", 60, self._morning_check)
        sup.poller("pc scan", 6 * 3600, self._rescan_if_stale, delay=6 * 3600)  # startup already scanned
        sup.start()

    def stop(self) -> None:
        self.supervisor.stop()
        self.svc.activity.stop()
        if self.listener:
            self.listener.stop()
        self.speaker.stop()
        self._commands.shutdown(wait=False, cancel_futures=True)
        if self.svc.jobs:
            self.svc.jobs.shutdown()

    # ---- pollers -----------------------------------------------------
    _sys_ticks = 0

    def _poll_system(self) -> None:
        self._sys_ticks += 1
        # Walking every process is the expensive part; do it every third tick.
        snap = self.svc.system.snapshot(include_processes=self._sys_ticks % 3 == 1)
        if "processes" not in snap and "system" in self.bus.latest:
            snap["processes"] = (self.bus.latest["system"]["data"] or {}).get("processes")
        snap["history"] = list(self.svc.system.history)[-90:]
        self.bus.publish("system", snap, sticky=True)

    def _poll_obs(self) -> None:
        self.bus.publish("obs", self.svc.obs.status(), sticky=True)

    def _poll_twitch(self) -> None:
        if self.svc.twitch.enabled:
            self.bus.publish("twitch", self.svc.twitch.status(), sticky=True)

    def _poll_activity(self) -> None:
        self.bus.publish("activity", self.svc.activity.summary_for_day(tz=self.svc.tz), sticky=True)

    def _poll_projects(self) -> None:
        self.bus.publish("projects", self.svc.projects.summary(), sticky=True)

    def _poll_calendar(self) -> None:
        self.svc.calendars.refresh(force=True)
        self.bus.publish("calendar", {"events": self.svc.calendars.agenda(7), "calendars": self.svc.calendars.status()},
                         sticky=True)

    def _poll_news(self) -> None:
        self.svc.news.refresh(force=True)
        self.bus.publish("news", {"headlines": self.svc.news.headlines(limit=30)}, sticky=True)

    _briefed_on: str | None = None

    def _morning_check(self) -> None:
        """Pre-build the morning briefing at first activity after 5am so 'good morning' is instant on screen."""
        now = datetime.now(self.svc.tz)
        today = now.date().isoformat()
        if self._briefed_on == today or now.hour < 5:
            return
        last = self.assistant.last_briefing
        if last and datetime.fromtimestamp(last["created"], self.svc.tz).date() == now.date():
            self._briefed_on = today
            return
        cur = self.svc.activity.current
        if cur and cur["category"] != "idle":
            self._briefed_on = today
            threading.Thread(target=self.assistant.briefing, args=("morning",), daemon=True).start()

    # ---- voice calibration ----------------------------------------------
    def start_calibration(self, calibrator=None) -> dict:
        """Run the calibration wizard in the background; progress arrives as 'calibration' events."""
        if self.listener is None:
            return {"ok": False, "error": "Voice is turned off (voice.enabled: false)."}
        if self.calibrator is not None:
            return {"ok": False, "error": "Calibration is already running."}
        listener = self.listener
        cal = calibrator or calib.Calibrator(
            self.cfg, self.bus, record=calib.mic_recorder(self.cfg["voice"].get("input_device")),
            transcribe=calib.engine_transcriber(self.cfg, listener),
        )
        self.calibrator = cal

        def run():
            listener.paused = True  # the wizard owns the mic; nothing it hears is a command
            result: dict = {"error": "calibration crashed"}
            try:
                if calibrator is None and self.verifier:
                    cal.emit(step="prepare", prompt="Getting the voice model ready (first run downloads ~26 MB)…")
                    cal.embedder = self.verifier.embedder()
                result = cal.run()
            finally:
                listener.paused = False
                self.calibrator = None
            if not result.get("cancelled") and not result.get("error"):
                self.apply_voice_calibration()
            else:
                self.bus.publish("voice_profile", self.voice_status(), sticky=True)

        threading.Thread(target=run, name="calibration", daemon=True).start()
        return {"ok": True}

    def cancel_calibration(self) -> dict:
        if self.calibrator is not None:
            self.calibrator.cancelled.set()
        return {"ok": True}

    def apply_voice_calibration(self) -> None:
        new = load_config(self.cfg.path)
        self.cfg["assistant"]["wake_words"] = new["assistant"]["wake_words"]
        for key in ("min_rms", "speaker_check"):
            self.cfg["voice"][key] = new["voice"][key]
        if self.listener:
            self.listener.wake_words = new["assistant"]["wake_words"]
            self.listener.cfg = self.cfg["voice"]
            self.listener.restart()  # new segmenter threshold takes effect in the fresh loop
        if self.verifier:
            self.verifier.mode = new["voice"]["speaker_check"]
            self.verifier.reload()
        self.bus.publish("voice_profile", self.voice_status(), sticky=True)

    def set_speaker_check(self, mode: str) -> dict:
        if self.verifier is None or mode not in self.verifier.MODES:
            return {"ok": False, "error": "unknown mode or voice disabled"}
        self.verifier.mode = mode
        save_setting(self.cfg, "voice.speaker_check", mode)  # survives a restart, over config.yaml
        self.bus.publish("voice_profile", self.voice_status(), sticky=True)
        return {"ok": True, "mode": mode}

    # ---- speech output (Setup tab) ---------------------------------------
    TTS_ENGINES = ("auto", *neural.ENGINES, "pyttsx3", "browser", "none")

    def set_voice(self, engine: str | None = None, voice: str | None = None, speed: float | None = None) -> dict:
        """Switch the speaking voice now and remember it (data/settings.yaml)."""
        engine = engine.lower() if engine else None
        if engine is not None and engine not in self.TTS_ENGINES:
            return {"ok": False, "error": f"unknown engine {engine!r}"}
        target = engine or self.speaker.requested
        if voice is not None:
            ids = [v.id for v in neural.VOICES.get(target if target != "auto" else "supertonic", [])]
            if ids and voice.lower() not in ids:
                return {"ok": False, "error": f"unknown voice {voice!r} for {target}"}
        if speed is not None:
            try:
                speed = max(0.6, min(1.6, float(speed)))
            except (TypeError, ValueError):
                return {"ok": False, "error": "speed must be a number"}
        for key, value in (("engine", engine), ("voice", voice.lower() if voice else voice), ("speed", speed)):
            if value is not None:
                save_setting(self.cfg, f"voice.tts.{key}", value)
        status = self.speaker.configure(engine, voice.lower() if voice else voice, speed)
        self.bus.publish("tts", status, sticky=True)
        return {"ok": True, **status}

    def preview_voice(self, text: str | None = None) -> dict:
        if self.speaker.engine_name == "none":
            return {"ok": False, "error": "speech is off (engine: none)"}
        self.speaker.preview(text)
        return {"ok": True}

    def delete_voice_profile(self) -> dict:
        existed = delete_profile(self.cfg.data_dir)
        if self.verifier:
            self.verifier.reload()
        self.bus.publish("voice_profile", self.voice_status(), sticky=True)
        return {"ok": True, "deleted": existed}

    def voice_status(self) -> dict:
        latest = self.bus.latest.get("calibration")
        return {
            "enabled": self.listener is not None,
            "profile": self.verifier.status() if self.verifier else {"enrolled": False, "mode": "off"},
            "wake_words": self.cfg["assistant"]["wake_words"],
            "min_rms": self.cfg["voice"].get("min_rms"),
            "pipeline": ({k: v for k, v in self.listener.status().items()
                          if k in ("engine", "vad", "latency_ms", "wake_mode", "wake", "triggers")}
                         if self.listener else None),
            "wake_models": self.wake_models(),
            "calibrating": self.calibrator is not None,
            "last_calibration": latest["data"] if latest else None,
            "macros": [{"name": m.name, "triggers": m.triggers, "steps": len(m.steps),
                        "needs_yes": bool(self.assistant.macro_risks(m.name))}
                       for m in self.assistant.macros.values()],
        }

    # ---- trained wake words / hard triggers -----------------------------
    def wake_models(self) -> list[dict]:
        from .voice import wakeword

        kinds = getattr(self.listener, "trigger_kind", None) or (lambda _n: "")
        models_dir = self.cfg.data_dir / "models"
        saved = wakeword.saved_thresholds(models_dir)
        return [{"name": n, "file": p.name, "kind": kinds(n), "kb": round(p.stat().st_size / 1024),
                 "threshold": saved.get(n)}
                for n, p in wakeword.model_files(models_dir).items()]

    def install_wake_model(self, name: str, data: bytes, threshold: float | None = None) -> dict:
        """Save a trained .onnx (from the Colab notebook) after checking it's a wake word classifier."""
        import re
        import tempfile

        from .voice import wakeword

        name = name.lower().removesuffix(".onnx")
        name = re.sub(r"\s*\(\d+\)$", "", name)  # "vesper (1)": the browser's re-download name, still the wake word
        name = re.sub(r"[^a-z0-9_]+", "_", name).strip("_")
        if not name or len(name) > 40:
            return {"ok": False, "error": "Name it after the phrase, e.g. vesper, stop or clip_that."}
        if not 1_000 < len(data) < 20_000_000:
            return {"ok": False, "error": "That doesn't look like a wake word model (expected 10 KB–20 MB)."}
        if not wakeword.available():
            return {"ok": False, "error": "Install the wake word runtime first: pip install -r requirements-voice.txt"}
        try:
            import onnxruntime as ort

            with tempfile.NamedTemporaryFile(suffix=".onnx", delete=False) as tmp:
                tmp.write(data)
            shape = ort.InferenceSession(tmp.name, providers=["CPUExecutionProvider"]).get_inputs()[0].shape
            Path(tmp.name).unlink(missing_ok=True)
        except Exception as exc:
            return {"ok": False, "error": f"Not a valid ONNX model: {exc}"}
        if list(shape)[-2:] != [16, 96]:
            return {"ok": False, "error": f"Expected a livekit-wakeword/openWakeWord classifier (input …×16×96), got {shape}."}
        folder = self.cfg.data_dir / "models" / wakeword.FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.onnx").write_bytes(data)
        side = folder / f"{name}.json"
        if threshold is not None and 0.05 <= threshold <= 0.99:
            import json

            side.write_text(json.dumps({"threshold": round(threshold, 3)}), encoding="utf-8")
        else:
            side.unlink(missing_ok=True)
        return self._reload_wake(name)

    def delete_wake_model(self, name: str) -> dict:
        from .voice import wakeword

        path = wakeword.model_files(self.cfg.data_dir / "models").get(name.lower())
        if path is None:
            return {"ok": False, "error": f"No wake word model called {name}."}
        path.unlink()
        path.with_suffix(".json").unlink(missing_ok=True)
        return self._reload_wake(None)

    def _reload_wake(self, name: str | None) -> dict:
        if hasattr(self.listener, "load_wake_words"):
            self.listener.load_wake_words()
        self.bus.publish("voice_profile", self.voice_status(), sticky=True)
        kind = self.listener.trigger_kind(name) if (hasattr(self.listener, "trigger_kind") and name) else None
        mode = self.listener.wake_mode() if hasattr(self.listener, "wake_mode") else None
        return {"ok": True, "name": name, "kind": kind, "mode": mode}

    # ---- PC scan -------------------------------------------------------
    def _rescan_if_stale(self) -> None:
        if discovery.is_stale(self.cfg.root):
            self.rescan()

    def rescan(self) -> dict:
        """Scan the PC, rewrite config.discovered.yaml and apply it without a restart."""
        result = discovery.discover(self.cfg)
        discovery.write_discovered(result, self.cfg.root, self.cfg.data_dir)
        self.apply_config(load_config(self.cfg.path))
        report = {k: v for k, v in result.items() if k != "suggested"}
        self.bus.publish("discovery", report, sticky=True)
        return report

    def apply_config(self, new: Config) -> None:
        """Hot-swap the parts of config that discovery can change."""
        for key in ("apps", "sites", "profiles", "projects"):
            self.cfg[key] = new[key]
        self.cfg["obs"]["scene_aliases"] = new["obs"]["scene_aliases"]
        self.cfg["voice"]["stt_device"] = new["voice"]["stt_device"]
        svc = self.svc
        svc.launcher.aliases = {k.lower(): v for k, v in new["apps"].items()}
        svc.launcher._shortcuts = None  # re-index Start Menu on next use
        svc.browser.sites = {k.lower(): v for k, v in new["sites"].items()}
        svc.obs.scene_aliases = {k.lower(): v for k, v in (new["obs"].get("scene_aliases") or {}).items()}
        svc.activity.categorize = Categorizer(new["profiles"])
        svc.projects.scan_dirs = new["projects"].get("scan_dirs") or []
        svc.projects._repos_scanned = 0.0

    def discovery_report(self) -> dict | None:
        latest = self.bus.latest.get("discovery")
        return latest["data"] if latest else discovery.load_report(self.cfg.data_dir)

    # ------------------------------------------------------------------
    def state(self) -> dict:
        latest = {k: v["data"] for k, v in self.bus.latest.items()}
        a = self.cfg["assistant"]
        return {
            "assistant": {
                "name": a["name"], "user": a["user_name"], "wake_words": a["wake_words"],
                "claude": self.assistant.claude_ready, "model": self.cfg["claude"]["model"],
                "tts": self.speaker.engine_name, "started": self.started,
            },
            "goals": self.cfg["goals"],
            "profiles": {k: {"label": v.get("label", k)} for k, v in self.cfg["profiles"].items()},
            "active_profile": self.assistant.active_profile,
            "voice": self.listener.status() if self.listener else {"state": "disabled"},
            "pending": self.assistant.pending_view(),
            "tasks": self.svc.storage.list_tasks(),
            "notes": self.svc.storage.list_notes(8),
            "jobs": self.svc.storage.list_jobs(15),
            "log": self.svc.storage.recent_log(40),
            "briefing": self.assistant.last_briefing,
            "discovery": self.discovery_report(),
            "doctor": latest.get("doctor"),
            "health": self.supervisor.snapshot(),
            "voice_profile": self.voice_status(),
            "tts": self.speaker.status(),
            "pc_control": self.assistant.control_status(),
            **{k: latest.get(k) for k in ("system", "obs", "twitch", "activity", "projects", "calendar", "news", "activity_now")},
        }
