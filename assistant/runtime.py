"""Wires everything together and runs the background pollers."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from . import discovery
from .brain.assistant import Assistant
from .bus import EventBus
from .config import Config, load_config
from .integrations.activity import Categorizer
from .integrations.jobs import JobRunner
from .services import Services, build_services
from .voice.hotkey import register_hotkey
from .voice import calibrate as calib
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
        self.speaker = Speaker(self.bus, tts.get("engine", "pyttsx3"), tts.get("rate", 190), tts.get("voice_hint", ""))
        self.svc.speak = self.speaker.say
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
                on_command=lambda text: self.assistant.handle(text, "voice"),
                cfg=cfg["voice"], hint_words=self._hint_words, verifier=self.verifier,
            )
        self.supervisor = Supervisor(self.bus)
        self.started = time.time()

    def _hint_words(self) -> list[str]:
        """Vocabulary that biases speech recognition toward your scene/app names."""
        words = list(self.svc.obs.scenes)
        words += list(self.cfg["apps"].keys()) + list(self.cfg["sites"].keys())
        return words

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
            transcribe=calib.whisper_transcriber(self.cfg, listener._model),
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
        self.cfg["voice"]["speaker_check"] = mode
        self.bus.publish("voice_profile", self.voice_status(), sticky=True)
        return {"ok": True, "mode": mode}

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
            "calibrating": self.calibrator is not None,
            "last_calibration": latest["data"] if latest else None,
            "macros": [{"name": m.name, "triggers": m.triggers, "steps": len(m.steps),
                        "needs_yes": bool(self.assistant.macro_risks(m.name))}
                       for m in self.assistant.macros.values()],
        }

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
            **{k: latest.get(k) for k in ("system", "obs", "twitch", "activity", "projects", "calendar", "news", "activity_now")},
        }
