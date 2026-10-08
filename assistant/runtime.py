"""Wires everything together and runs the background pollers."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx

from . import discovery
from .brain import local_llm, model_bench
from .brain.assistant import Assistant
from .brain.router import KILL, clean
from .bus import EventBus
from .config import Config, load_config, save_setting
from .integrations import prestream, toast
from .integrations.activity import Categorizer
from .integrations.eventsub import EventSub, TwitchFeed, callout, normalize
from .integrations.highlights import ChatSpike, Highlight, HighlightLog, spike_reason
from .integrations.stream_health import MicWatch, StreamHealth
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
        self._bench_running = False  # the model test (one at a time)
        self.svc.free_model_memory = self.free_model_memory
        self.svc.system.extra_findings = self._model_memory_findings
        # Stream health while live, and the pre-stream check (Phase 5).
        obs_cfg = cfg["obs"]
        self.stream_health = StreamHealth()
        self.mic_watch = MicWatch(obs_cfg.get("mic_source") or "")
        self._prestream_seen: set = set()
        self.svc.stream_health = lambda: {**self.stream_health.snapshot(), "mic": self.mic_watch.snapshot()}
        self.svc.prestream = self.prestream_check
        self.svc.connections = lambda: {"findings": (self.discovery_report() or {}).get("findings") or [],
                                        "brain": self.assistant.brain_status()}
        # Twitch live events, callouts and highlight markers (Phase 5b).
        tw_cfg = cfg["twitch"]
        self.twitch_feed = TwitchFeed()
        self.highlights = HighlightLog(cfg.data_dir / "highlights")
        self.chat_spike = ChatSpike(ratio=float(tw_cfg.get("spike_ratio", 3.0)))
        self.svc.twitch_feed = self.twitch_feed
        self.svc.highlights = lambda: list(self.highlights.recent)
        twitch = self.svc.twitch
        self.eventsub: EventSub | None = None
        if getattr(twitch, "configured", False) and tw_cfg.get("events", True):
            self.eventsub = EventSub(twitch, self._on_twitch_event,
                                     on_state=lambda st: self.bus.publish("twitch_events_state", st, sticky=True))
        if getattr(twitch, "auth", None) is not None:
            twitch.auth.on_change = self._on_twitch_auth
        self.bus.on(self._after_tool)
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
        if self.eventsub:
            events = self.eventsub
            events.heartbeat = lambda: sup.beat("twitch events")
            sup.service("twitch events", events.restart, heartbeat_s=60,
                        is_disabled=lambda: not self.svc.twitch.auth.connected)
        sup.poller("projects", 120, self._poll_projects)
        sup.poller("calendars", 300, self._poll_calendar)
        sup.poller("pre-stream check", 60, self._prestream_tick, delay=30)
        sup.poller("news", 900, self._poll_news)
        sup.poller("morning briefing", 60, self._morning_check)
        sup.poller("pc scan", 6 * 3600, self._rescan_if_stale, delay=6 * 3600)  # startup already scanned
        sup.poller("model test", 3600, self._auto_bench, delay=45)
        sup.start()

    def stop(self) -> None:
        self.supervisor.stop()
        self.svc.activity.stop()
        if self.listener:
            self.listener.stop()
        self.speaker.stop()
        self.svc.obs.close_events()
        if self.eventsub:
            self.eventsub.stop()
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
        obs = self.svc.obs
        status = obs.status()
        self._stream_brain(bool(status.get("connected") and (status.get("streaming") or {}).get("active")))
        if self.cfg["obs"].get("health_alerts", True):
            if status.get("connected"):
                obs.ensure_events(on_meters=self.mic_watch.on_meters,
                                  on_replay_saved=lambda path: self.bus.publish("replay_saved", {"path": path}))
            self.stream_health.expected_stop_at = max(self.stream_health.expected_stop_at, obs.stop_requested_at)
            alerts = self.stream_health.update(status)
            live = bool(status.get("connected") and (status.get("streaming") or {}).get("active"))
            mic = next((a for a in status.get("audio") or [] if a["name"] == self.mic_watch.name), None)
            heard = self.listener.heard_speech(self.mic_watch.window_s) if self.listener else 0
            alerts += self.mic_watch.check(live, mic.get("muted") if mic else None, heard)
            for alert in alerts:
                self._stream_alert(alert)
            status["health"] = self.svc.stream_health()
        self.bus.publish("obs", status, sticky=True)

    def _stream_brain(self, live: bool) -> None:
        """Live: answer with the light model and give the big one's video memory back to the game and encoder."""
        change = self.assistant.local.set_streaming(live)
        if not change:
            return
        if live:
            freed = f", freed {change['freed_gb']:.0f} GB of video memory" if change.get("freed_gb") else ""
            text = f"You're live: answering with {change['to']}{freed}."
        else:
            text = f"Stream over: back to {change['to']} for your next question."
        self.bus.publish("brain", self.assistant.brain_status(), sticky=True)
        self.bus.publish("announce", {"text": text})

    def _stream_alert(self, alert) -> None:
        """Show it in the HUD; say it out loud if it matters and speaking is on."""
        self.bus.publish("stream_alert", alert.as_dict())
        if alert.speak and alert.level != "info" and self.cfg["obs"].get("speak_alerts", True):
            self.speaker.say(alert.text, expects_reply=False)

    # ---- pre-stream check -------------------------------------------------
    def prestream_check(self) -> dict:
        snap = (self.bus.latest.get("system") or {}).get("data")
        result = prestream.run_checklist(self.svc, self.mic_watch if self.mic_watch.name else None, snap)
        self.bus.publish("prestream", result, sticky=True)
        return result

    def _prestream_tick(self) -> None:
        """Run the check by itself shortly before a stream on your calendar."""
        lead = int(self.cfg["obs"].get("prestream_minutes", 15) or 0)
        if lead <= 0 or not self.svc.obs.enabled:
            return
        now = datetime.now(self.svc.tz)
        event = prestream.next_stream_event(self.svc.calendars.agenda(2), now, lead, self._prestream_seen)
        if event is None:
            return
        result = self.prestream_check()
        self.bus.publish("announce", {"text": f"{event['title']} starts soon. {result['spoken']}"})
        self.speaker.say(f"{event['title']} starts soon. {result['spoken']}", expects_reply=False)

    def _poll_twitch(self) -> None:
        tw = self.svc.twitch
        if not getattr(tw, "configured", tw.enabled):
            return
        if tw.auth.connected:
            tw.auth.validate()  # Twitch requires this hourly; it's a no-op in between
        st = tw.status()
        st["events"] = self.eventsub.status() if self.eventsub else None
        st["feed"] = self.twitch_feed.snapshot()
        st["highlights"] = list(self.highlights.recent)[:20]
        self.bus.publish("twitch", st, sticky=True)

    # ---- Twitch: login, live events, highlights ---------------------------
    def twitch_connect(self) -> dict:
        out = self.svc.twitch.auth.start_login()
        self._poll_twitch_soon()
        return out

    def twitch_logout(self) -> dict:
        if self.eventsub:
            self.eventsub.stop()
        out = self.svc.twitch.auth.logout()
        self._poll_twitch_soon()
        return out

    def _poll_twitch_soon(self) -> None:
        threading.Thread(target=self._safe(self._poll_twitch), name="twitch-refresh", daemon=True).start()

    @staticmethod
    def _safe(fn):
        def run(*a):
            try:
                fn(*a)
            except Exception:
                log.exception("%s failed", getattr(fn, "__name__", "task"))
        return run

    def _on_twitch_auth(self, status: dict) -> None:
        self.bus.publish("twitch_auth", status, sticky=True)
        if status.get("connected") and status.get("login") and not status.get("pending"):
            if self.eventsub:
                self.eventsub.start()
            self.announce(f"Twitch is connected as {status['login']}.")
        elif status.get("error"):
            self.bus.publish("announce", {"text": status["error"]})
        self._poll_twitch_soon()

    def _live(self) -> tuple[bool, float | None]:
        """(live, seconds since the stream started), from OBS or else Twitch."""
        obs = (self.bus.latest.get("obs") or {}).get("data") or {}
        stream = obs.get("streaming") or {}
        if obs.get("connected") and stream.get("active"):
            ms = stream.get("duration_ms")
            return True, ms / 1000 if ms else None
        tw_event = self.bus.latest.get("twitch") or {}
        tw = tw_event.get("data") or {}
        if tw.get("live") and tw.get("uptime_s") is not None:
            return True, tw["uptime_s"] + (time.time() - tw_event.get("ts", time.time()))
        return bool(tw.get("live")), None

    def _on_twitch_event(self, sub_type: str, event: dict) -> None:
        ev = normalize(sub_type, event)
        if ev is None:
            return
        ev = self.twitch_feed.add(ev)
        tw_cfg = self.cfg["twitch"]
        kind = ev["kind"]
        if kind == "chat":
            self.chat_spike.add(ev.get("user") or "", ev.get("text") or "", ev["ts"])
            spike = self.chat_spike.check(ev["ts"])
            if spike:
                self._highlight_async("chat_spike", spike_reason(spike), spike)
            return
        if kind in ("online", "offline"):
            if kind == "online":
                self.chat_spike = ChatSpike(ratio=float(tw_cfg.get("spike_ratio", 3.0)))
            self._poll_twitch_soon()
            return
        self.bus.publish("twitch_event", {k: v for k, v in ev.items() if k != "text"})  # viewer text stays off the HUD feed
        said = callout(ev, tw_cfg.get("callouts") or {})
        if kind == "raid" and said and ev.get("user"):
            login = (ev.get("detail") or {}).get("login") or ev["user"]
            self.assistant.offer([("twitch_shoutout", {"user": login})], f"shout out {login}")
        if said:
            self.bus.publish("announce", {"text": said})
            self.speaker.say(said, expects_reply=True if kind == "raid" else False)
        amount = int(ev.get("amount") or 0)
        big = (kind in ("raid", "hype_train") or (kind == "cheer" and amount >= 5 * int((tw_cfg.get("callouts") or {}).get("cheer_min", 100)))
               or (kind == "gift" and amount >= 5))
        if big:
            what = {"raid": f"Raid from {ev.get('user')} ({amount})", "hype_train": "Hype train",
                    "cheer": f"{amount} bits from {ev.get('user') or 'anonymous'}",
                    "gift": f"{amount} gifted subs from {ev.get('user') or 'anonymous'}"}[kind]
            self._highlight_async(kind, what, {k: v for k, v in ev.items() if k != "text"})

    def _highlight_async(self, kind: str, reason: str, detail: dict | None = None, place_marker: bool = True) -> None:
        # Off the event thread: a marker is an HTTP call, and EventSub must keep reading.
        threading.Thread(target=self._safe(self.highlight), args=(kind, reason, detail, place_marker),
                         name="highlight", daemon=True).start()

    def highlight(self, kind: str, reason: str, detail: dict | None = None, place_marker: bool = True) -> dict:
        live, uptime = self._live()
        h = Highlight(kind, reason[:140], time.time(), uptime, detail=detail or {})
        twitch = self.svc.twitch
        if (place_marker and live and self.cfg["twitch"].get("auto_markers", True)
                and getattr(twitch, "auth", None) is not None and twitch.auth.connected):
            r = self.assistant.tools.run("twitch_marker", {"description": h.reason}, context={"source": "highlight"})
            h.marker = bool(r.get("ok"))
        self.highlights.add(h)
        self.bus.publish("highlight", h.as_dict())
        return h.as_dict()

    def _after_tool(self, event: dict) -> None:
        """Keep the HUD in step with what a spoken or Claude-run tool changed; clips and markers are highlights."""
        if event.get("type") != "tool":
            return
        data = event.get("data") or {}
        name, args, result = data.get("name"), data.get("args") or {}, data.get("result") or {}
        if not isinstance(result, dict) or result.get("ok") is False:
            return
        if name in ("add_task", "complete_task"):  # said, not clicked: the HUD's list and count must follow
            self.bus.publish("tasks", self.svc.storage.list_tasks())
        if name == "obs_control" and args.get("action") == "save_replay":
            self._highlight_async("clip", "Clip that (replay saved)", {"path": result.get("path")})
        elif name == "twitch_clip":
            self._highlight_async("clip", "Twitch clip" + (f": {result['title']}" if result.get("title") else ""),
                                  {"url": result.get("url")})
        elif name == "twitch_marker":
            self._highlight_async("manual", args.get("description") or "Marker", {}, place_marker=False)

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

    BRAIN_PROVIDERS = ("auto", "claude", "local")

    def set_brain(self, provider: str | None = None, model: str | None = None) -> dict:
        """Which model answers, remembered in data/settings.yaml like the voice."""
        if provider is not None:
            provider = provider.lower()
            if provider not in self.BRAIN_PROVIDERS:
                return {"ok": False, "error": f"unknown provider {provider!r}"}
            save_setting(self.cfg, "brain.provider", provider)
        if model is not None:
            save_setting(self.cfg, "brain.local.model", model)
            self.assistant.local.cfg = (self.cfg.get("brain") or {}).get("local") or {}
            self.assistant.local.refresh(force=True)
        status = self.assistant.brain_status()
        self.bus.publish("brain", status, sticky=True)
        return {"ok": True, **status}

    def test_brain(self) -> dict:
        """One tiny request to whichever model answers now: is it there, and how fast?"""
        a = self.assistant
        brain = a.brain()
        t0 = time.perf_counter()
        try:
            if brain == "local":
                llm = a.local.llm
                out = llm.chat([{"role": "user", "content": "Reply with exactly one word: ready"}],
                               temperature=0, max_tokens=5)
                reply, ms = out["content"], round((time.perf_counter() - t0) * 1000)
                # Answering isn't enough: the same tools Vesper gives it, and did it pick the right one? (Never run.)
                acts = False
                if llm.can_act:
                    probe = llm.chat([{"role": "system", "content": model_bench.SYSTEM},
                                      {"role": "user", "content": "Set the volume to 20."}],
                                     tools=a._local_tools(), temperature=0, max_tokens=150)
                    acts = any(c["name"] == "set_volume" for c in probe["tool_calls"])
                return {"ok": True, "reply": reply.strip()[:60], "ms": ms, "acts": acts, **a.brain_status()}
            elif brain == "claude":
                resp = a._create([{"role": "user", "content": "Reply with exactly one word: ready"}], max_tokens=16)
                reply = " ".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            else:
                return {"ok": False, "error": "No model is connected.", **a.brain_status()}
        except Exception as exc:  # the point of a test button: say what went wrong
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], **a.brain_status()}
        return {"ok": True, "reply": reply.strip()[:60], "ms": round((time.perf_counter() - t0) * 1000), **a.brain_status()}

    def bench_brain(self) -> dict:
        """Test every local model in the background (Setup → Brain → Test my models, or once by itself)."""
        if self._bench_running:
            return {"ok": True, "started": False, "running": True}
        self._bench_running = True

        def progress(p: dict) -> None:
            self.bus.publish("brain_bench", {"running": True, **p})

        def work() -> None:
            try:
                result = self.assistant.bench_models(on_progress=progress)
            except Exception as exc:  # a broken server or model must not take the runtime down
                log.exception("model test failed")
                result = {"error": f"The model test stopped: {type(exc).__name__}: {exc}"[:300]}
            finally:
                self._bench_running = False
            line = model_bench.summary(result)
            self.bus.publish("brain_bench", {"running": False, "result": result, "summary": line}, sticky=True)
            self.bus.publish("brain", self.assistant.brain_status(), sticky=True)
            self.bus.publish("announce", {"text": line})
            log.info("model test: %s", line)
        threading.Thread(target=work, name="model test", daemon=True).start()
        return {"ok": True, "started": True}

    def _model_memory_findings(self) -> list[dict]:
        """For the PC optimizer: a model Ollama keeps loaded can hold tens of GB, in RAM when it didn't fit the GPU."""
        local = self.assistant.local
        current = local.llm.model if local.llm else None
        out = []
        for m in local.loaded():
            if m["ram_gb"] >= 1:
                out.append({"severity": "high" if m["ram_gb"] >= 8 else "medium",
                            "title": f"{m['name']} holds {m['ram_gb']:.0f} GB of RAM",
                            "detail": f"Ollama has it loaded and {m['ram_gb']:.0f} of its {m['size_gb']:.0f} GB didn't fit "
                                      "on the GPU. It stays until Ollama's keep-alive runs out; unloading frees it now, and "
                                      "it loads again when used.",
                            "action": {"tool": "free_model_memory", "args": {"keep_current": m["name"] != current}}})
            elif m["name"] != current and m["vram_gb"] >= 4:
                out.append({"severity": "info", "title": f"{m['name']} holds {m['vram_gb']:.0f} GB of video memory",
                            "detail": "Loaded in Ollama but not the model Vesper answers with.",
                            "action": {"tool": "free_model_memory", "args": {"keep_current": True}}})
        return out

    def free_model_memory(self, keep_current: bool = True) -> dict:
        local = self.assistant.local
        found = local.found or {}
        if found.get("kind") != "ollama":
            return {"ok": False, "error": "Only Ollama can be told to unload its models."}
        current = local.llm.model if local.llm else None
        held = local.loaded()
        freed = [m for m in held if not (keep_current and m["name"] == current)]
        http = local.http or (local.llm.http if local.llm else None) or httpx.Client(timeout=15, trust_env=False)
        for m in freed:
            local_llm.unload(http, found["url"], m["name"])
        gb = sum(m["size_gb"] for m in freed)
        return {"ok": True, "unloaded": [m["name"] for m in freed], "freed_gb": round(gb, 1),
                "kept": current if keep_current and any(m["name"] == current for m in held) else None}

    def _auto_bench(self) -> None:
        """Once by itself, and again when your list of models changes: unless you picked a model yourself, Claude
        answers instead, or you're live (loading big models then would hurt the stream)."""
        brain = self.cfg.get("brain") or {}
        lcfg = brain.get("local") or {}
        local = self.assistant.local
        if (brain.get("provider") == "claude" or not lcfg.get("enabled", True) or not lcfg.get("auto_test", True)
                or lcfg.get("model") or local.streaming or self._bench_running):
            return
        local.refresh(force=True)  # a fresh list: a model you just pulled counts
        found = local.found or {}
        if found.get("kind") != "ollama" or len(found.get("models") or []) < 2:
            return
        if sorted(local.bench.get("models") or []) == sorted(m["name"] for m in found["models"]):
            return
        self.bench_brain()

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
                "brain": self.assistant.brain_status(),
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
