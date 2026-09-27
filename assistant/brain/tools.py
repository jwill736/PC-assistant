"""Every action the assistant can take, described once and used twice: by the
fast-path voice router and by Claude as tool definitions.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from ..integrations import desktop
from ..integrations.calendars import free_blocks
from ..services import Services


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[..., dict]
    # True, or a predicate over the args, when the action needs a spoken "yes".
    confirm: bool | Callable[[dict], bool] = False
    confirm_text: Callable[[dict], str] | None = None

    def needs_confirmation(self, args: dict) -> bool:
        return self.confirm(args) if callable(self.confirm) else bool(self.confirm)

    def describe(self, args: dict) -> str:
        if self.confirm_text:
            return self.confirm_text(args)
        return f"{self.name.replace('_', ' ')} {json.dumps(args)}"

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def obj(props: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props or {}, "required": required or []}


def day_bounds(svc: Services, day: str | None) -> datetime:
    now = datetime.now(svc.tz)
    if not day or day == "today":
        return now
    if day == "yesterday":
        return now - timedelta(days=1)
    return datetime.fromisoformat(day).replace(tzinfo=svc.tz)


class ToolBox:
    def __init__(self, svc: Services):
        self.svc = svc
        self.tools: dict[str, Tool] = {t.name: t for t in self._build()}

    def definitions(self) -> list[dict]:
        return [t.definition() for t in self.tools.values()]

    def run(self, name: str, args: dict | None = None) -> dict:
        tool = self.tools.get(name)
        if not tool:
            return {"ok": False, "error": f"Unknown tool {name}"}
        try:
            return tool.handler(**(args or {}))
        except TypeError as exc:
            return {"ok": False, "error": f"Bad arguments for {name}: {exc}"}
        except Exception as exc:  # integration failures become answers, not crashes
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------
    def _build(self) -> list[Tool]:
        s = self.svc
        profiles = list(s.cfg["profiles"].keys())
        return [
            Tool("open_app", "Launch a desktop program or game by name (e.g. 'discord', 'obs', 'photoshop', 'spotify').",
                 obj({"name": {"type": "string"}}, ["name"]), lambda name: s.launcher.launch(name)),
            Tool("close_app", "Close every window/process of a running program by name.",
                 obj({"name": {"type": "string"}}, ["name"]), self._close_app, confirm=True,
                 confirm_text=lambda a: f"close {a.get('name')}"),
            Tool("focus_window", "Bring an already-open window to the front, matched by app or title text.",
                 obj({"query": {"type": "string"}}, ["query"]), lambda query: desktop.focus_window(query)),
            Tool("where_am_i", "Report the active window and every open app/window — what the user is looking at.",
                 obj(), lambda: desktop.where_am_i()),
            Tool("open_urls", "Open one or more websites or saved site aliases (e.g. 'gmail', 'youtube') as Chrome tabs.",
                 obj({"targets": {"type": "array", "items": {"type": "string"}},
                      "new_window": {"type": "boolean"}}, ["targets"]),
                 lambda targets, new_window=False: s.browser.open(targets, new_window)),
            Tool("web_search", "Open a search in the browser. engine: google, youtube, github, twitch, maps, amazon, reddit.",
                 obj({"query": {"type": "string"}, "engine": {"type": "string"}}, ["query"]),
                 lambda query, engine="google": s.browser.search(query, engine)),
            Tool("media_control", "System media keys: play_pause, next, previous, volume_up, volume_down, mute.",
                 obj({"action": {"type": "string", "enum": list(desktop.MEDIA_KEYS)},
                      "times": {"type": "integer", "description": "repeat count, for volume steps (2% each)"}}, ["action"]),
                 lambda action, times=1: desktop.media_key(action, times)),
            Tool("power", "Lock, sleep, restart or shut down the PC.",
                 obj({"action": {"type": "string", "enum": ["lock", "sleep", "restart", "shutdown"]}}, ["action"]),
                 lambda action: desktop.power_action(action),
                 confirm=lambda a: a.get("action") != "lock", confirm_text=lambda a: f"{a.get('action')} the PC"),
            Tool("system_status", "Live CPU, GPU, RAM, disk, network and the heaviest processes.",
                 obj(), self._system_status),
            Tool("optimize_pc", "Analyze the PC and return concrete optimization findings with suggested actions.",
                 obj({"streaming": {"type": "boolean"}}), lambda streaming=False: s.system.analyze(streaming)),
            Tool("clean_temp", "Delete temp files older than a day to free disk space.",
                 obj(), lambda: s.system.clean_temp(), confirm=True, confirm_text=lambda a: "clear out old temp files"),
            Tool("set_power_plan", "Switch the Windows power plan: high, balanced, saver.",
                 obj({"plan": {"type": "string", "enum": ["high", "balanced", "saver"]}}, ["plan"]),
                 lambda plan: s.system.set_power_plan(plan)),
            Tool("obs_status", "OBS state: current scene, scene list, live/recording status, dropped frames, bitrate, FPS.",
                 obj(), lambda: s.obs.status()),
            Tool("obs_switch_scene", "Switch the OBS program scene (fuzzy-matched by name).",
                 obj({"scene": {"type": "string"}}, ["scene"]), lambda scene: s.obs.switch_scene(scene)),
            Tool("obs_control", "Start/stop the stream, recording, replay buffer or virtual cam; save a replay clip.",
                 obj({"action": {"type": "string", "enum": list(s.obs.ACTIONS)}}, ["action"]),
                 lambda action: s.obs.control(action),
                 confirm=lambda a: a.get("action") in {"start_stream", "stop_stream", "stop_recording"},
                 confirm_text=lambda a: {"start_stream": "go live", "stop_stream": "end the stream",
                                          "stop_recording": "stop recording"}.get(a.get("action"), a.get("action"))),
            Tool("obs_set_mute", "Mute, unmute or toggle an OBS audio source (e.g. 'mic', 'desktop audio'). muted omitted = toggle.",
                 obj({"source": {"type": "string"}, "muted": {"type": "boolean"}}, ["source"]),
                 lambda source, muted=None: s.obs.set_mute(source, muted)),
            Tool("twitch_status", "Twitch channel live status, viewers, title, uptime.", obj(), lambda: s.twitch.status()),
            Tool("calendar", "Events across all of the user's calendars. days=1 is today. profile filters to one area.",
                 obj({"days": {"type": "integer"}, "profile": {"type": "string"}}), self._calendar),
            Tool("news", "Latest headlines from the user's feeds, optionally filtered by topic or keyword.",
                 obj({"topic": {"type": "string"}}), lambda topic=None: {"headlines": s.news.headlines(topic, 10)}),
            Tool("projects", "The user's projects: local git repos (dirty files, commits today), Claude Code sessions, GitHub PRs.",
                 obj(), lambda: s.projects.summary()),
            Tool("activity", "How time was spent on a day (today, yesterday or YYYY-MM-DD): by category, app, focus sessions.",
                 obj({"day": {"type": "string"}}), self._activity),
            Tool("list_tasks", "Open tasks, highest priority first.", obj(), lambda: {"tasks": s.storage.list_tasks()}),
            Tool("add_task", f"Add a task. profile: {', '.join(profiles)} or personal. priority 1 (urgent) to 3.",
                 obj({"title": {"type": "string"}, "profile": {"type": "string"}, "priority": {"type": "integer"},
                      "due": {"type": "string", "description": "YYYY-MM-DD"}}, ["title"]),
                 lambda title, profile="work", priority=2, due=None: {"ok": True, "task": s.storage.add_task(title, profile, priority, due)}),
            Tool("complete_task", "Mark a task done by id or by (fuzzy) title.",
                 obj({"id": {"type": "integer"}, "title": {"type": "string"}}), self._complete_task),
            Tool("remember", "Save a note the user wants remembered; notes feed future briefings.",
                 obj({"text": {"type": "string"}}, ["text"]), lambda text: {"ok": True, "note": s.storage.add_note(text)}),
            Tool("run_routine", f"Set up a whole workspace in one go: {', '.join(profiles)}. Opens its apps, tabs and OBS scene.",
                 obj({"profile": {"type": "string", "enum": profiles}}, ["profile"]), self._run_routine),
            Tool("start_job", "Run work in the background while the user does something else. kind=claude_code runs "
                              "Claude Code headless inside a repo (needs project); kind=research does web research and writes a brief.",
                 obj({"kind": {"type": "string", "enum": ["claude_code", "research"]}, "prompt": {"type": "string"},
                      "project": {"type": "string"}, "title": {"type": "string"}}, ["kind", "prompt"]),
                 self._start_job, confirm=lambda a: a.get("kind") == "claude_code",
                 confirm_text=lambda a: f"have Claude Code work on '{a.get('prompt', '')[:60]}' in {a.get('project')}"),
            Tool("list_jobs", "Background jobs and their status/output.", obj(),
                 lambda: {"jobs": [dict(j, output=(j.get("output") or "")[:600]) for j in s.storage.list_jobs(10)]}),
        ]

    # ---- handlers that need more than one line ---------------------------
    def _close_app(self, name: str) -> dict:
        names = self.svc.launcher.process_names_for(name)
        return desktop.close_processes(names, set(self.svc.cfg["optimizer"]["protected_processes"]))

    def _system_status(self) -> dict:
        snap = self.svc.system.snapshot()
        snap["cpu"].pop("per_core", None)
        return snap

    def _calendar(self, days: int = 1, profile: str | None = None) -> dict:
        s = self.svc
        days = max(1, min(int(days or 1), 14))
        events = s.calendars.agenda(days, profile)
        now = datetime.now(s.tz)
        hours = s.cfg["assistant"]["work_hours"]
        today = [e for e in events if datetime.fromisoformat(e["start"]).date() == now.date()]
        return {"events": events, "free_today": free_blocks(today, now.date(), s.tz, hours["start"], hours["end"], now=now),
                "calendars": s.calendars.status()}

    def _activity(self, day: str | None = None) -> dict:
        summary = self.svc.activity.summary_for_day(day_bounds(self.svc, day), self.svc.tz)
        summary.pop("timeline", None)
        return summary

    def _complete_task(self, id: int | None = None, title: str | None = None) -> dict:
        import difflib

        tasks = self.svc.storage.list_tasks()
        target = next((t for t in tasks if t["id"] == id), None) if id else None
        if not target and title:
            titles = {t["title"].lower(): t for t in tasks}
            match = difflib.get_close_matches(title.lower(), list(titles), n=1, cutoff=0.4)
            target = titles[match[0]] if match else next((t for t in tasks if title.lower() in t["title"].lower()), None)
        if not target:
            return {"ok": False, "error": "No open task matches that."}
        return {"ok": True, "task": self.svc.storage.update_task(target["id"], status="done")}

    def _run_routine(self, profile: str) -> dict:
        s = self.svc
        prof = s.cfg["profiles"].get(profile)
        if not prof:
            return {"ok": False, "error": f"No profile '{profile}'."}
        launch = prof.get("launch") or {}
        results: dict[str, Any] = {"ok": True, "profile": profile, "launched": [], "failed": []}
        for app in launch.get("apps") or []:
            r = s.launcher.launch(app)
            (results["launched"] if r.get("ok") else results["failed"]).append(app)
            time.sleep(0.3)
        if launch.get("urls"):
            results["tabs"] = s.browser.open(launch["urls"], new_window=True).get("opened", [])
        if launch.get("obs_scene"):
            time.sleep(1.0)
            results["scene"] = s.obs.switch_scene(launch["obs_scene"])
        running = [a for a in prof.get("close_apps") or []
                   if desktop.find_processes(s.launcher.process_names_for(a))]
        if running:
            results["close_candidates"] = running
        s.bus.publish("profile", {"active": profile}, sticky=True)
        return results

    def _start_job(self, kind: str, prompt: str, project: str | None = None, title: str | None = None) -> dict:
        if self.svc.jobs is None:
            return {"ok": False, "error": "Job runner isn't running."}
        return self.svc.jobs.submit(kind, prompt, project, title)


def compact(result: Any, limit: int = 6000) -> str:
    text = json.dumps(result, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…(truncated)"
