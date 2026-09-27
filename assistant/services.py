"""Builds every integration from config and holds them in one place."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Callable

from .bus import EventBus
from .config import Config
from .integrations.activity import ActivityTracker
from .integrations.browser import Browser
from .integrations.calendars import CalendarHub, local_tz
from .integrations.desktop import AppLauncher
from .integrations.jobs import JobRunner
from .integrations.news import NewsFeed
from .integrations.obs import OBSController
from .integrations.projects import GitHub, ProjectTracker
from .integrations.system import SystemMonitor
from .integrations.twitch import TwitchClient
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass
class Services:
    cfg: Config
    bus: EventBus
    storage: Storage
    tz: tzinfo
    system: SystemMonitor
    launcher: AppLauncher
    browser: Browser
    obs: OBSController
    twitch: TwitchClient
    calendars: CalendarHub
    news: NewsFeed
    projects: ProjectTracker
    activity: ActivityTracker
    jobs: JobRunner | None = None
    # Voice output; replaced by the voice subsystem once it starts.
    speak: Callable[..., None] = field(default=lambda text, **kw: None)

    @property
    def name(self) -> str:
        return self.cfg["assistant"]["name"]


def obs_password(cfg: Config) -> str:
    """.env wins; otherwise use the password OBS itself stores, so nothing has to be copied."""
    explicit = cfg.secret(cfg["obs"].get("password_env"))
    if explicit or cfg["obs"]["host"] not in ("localhost", "127.0.0.1"):
        return explicit
    from .discovery import read_obs_websocket

    return read_obs_websocket().get("password", "")


def build_services(cfg: Config, bus: EventBus | None = None, storage: Storage | None = None) -> Services:
    bus = bus or EventBus()
    storage = storage or Storage(cfg.data_dir / "assistant.db")
    tz = local_tz(cfg["assistant"].get("timezone"))
    obs_cfg, tw_cfg, proj_cfg = cfg["obs"], cfg["twitch"], cfg["projects"]
    svc = Services(
        cfg=cfg,
        bus=bus,
        storage=storage,
        tz=tz,
        system=SystemMonitor(heavy_process_mb=cfg["optimizer"]["heavy_process_mb"],
                             protected=cfg["optimizer"]["protected_processes"]),
        launcher=AppLauncher(cfg["apps"]),
        browser=Browser(cfg["sites"]),
        obs=OBSController(obs_cfg["host"], obs_cfg["port"], obs_password(cfg),
                          obs_cfg.get("scene_aliases"), enabled=obs_cfg.get("enabled", True)),
        twitch=TwitchClient(tw_cfg.get("channel", ""), cfg.secret(tw_cfg.get("client_id_env")),
                            cfg.secret(tw_cfg.get("client_secret_env")), enabled=tw_cfg.get("enabled", False)),
        calendars=CalendarHub(cfg["calendars"], cfg["assistant"].get("timezone")),
        news=NewsFeed(cfg["news"].get("feeds") or None, cfg["news"]["refresh_minutes"], cfg["news"]["max_items"]),
        projects=ProjectTracker(proj_cfg.get("scan_dirs") or [], proj_cfg.get("claude_dir", "~/.claude"),
                                GitHub(proj_cfg["github"].get("user", ""), cfg.secret(proj_cfg["github"].get("token_env")))),
        activity=ActivityTracker(storage, cfg["profiles"], cfg["tracking"]["sample_seconds"],
                                 cfg["tracking"]["idle_seconds"],
                                 on_change=lambda seg: bus.publish("activity_now", seg, sticky=True)),
    )
    return svc
