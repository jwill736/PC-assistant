"""Wires everything together and runs the background pollers."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable

from .brain.assistant import Assistant
from .bus import EventBus
from .config import Config
from .integrations.jobs import JobRunner
from .services import Services, build_services
from .voice.hotkey import register_hotkey
from .voice.listener import VoiceListener
from .voice.tts import Speaker

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
        if cfg["voice"].get("enabled", True):
            self.listener = VoiceListener(
                self.bus, self.speaker, cfg["assistant"]["wake_words"],
                on_command=lambda text: self.assistant.handle(text, "voice"),
                cfg=cfg["voice"], hint_words=self._hint_words,
            )
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
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
        if self.cfg["tracking"].get("enabled", True):
            self.svc.activity.start()
        self.speaker.start()
        if self.listener:
            self.listener.start()
            register_hotkey(self.cfg["voice"].get("push_to_talk_hotkey"), self.listener.arm)
        self._every(2, self._poll_system, "system")
        self._every(3, self._poll_obs, "obs")
        self._every(60, self._poll_activity, "activity")
        self._every(90, self._poll_twitch, "twitch")
        self._every(120, self._poll_projects, "projects")
        self._every(300, self._poll_calendar, "calendar")
        self._every(900, self._poll_news, "news")
        self._every(60, self._morning_check, "morning")

    def stop(self) -> None:
        self._stop.set()
        self.svc.activity.stop()
        if self.listener:
            self.listener.stop()
        self.speaker.stop()
        if self.svc.jobs:
            self.svc.jobs.shutdown()

    def _every(self, seconds: float, fn: Callable[[], None], name: str) -> None:
        def loop():
            while not self._stop.is_set():
                try:
                    fn()
                except Exception:
                    log.exception("poller %s failed", name)
                self._stop.wait(seconds)

        t = threading.Thread(target=loop, name=f"poll-{name}", daemon=True)
        t.start()
        self._threads.append(t)

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
            **{k: latest.get(k) for k in ("system", "obs", "twitch", "activity", "projects", "calendar", "news", "activity_now")},
        }
