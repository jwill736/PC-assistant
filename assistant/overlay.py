"""OBS overlay: /overlay?panels=nowplaying,goals,copilot&theme=transparent&key=… as an OBS Browser Source.

The overlay has its own key (data/overlay_token), never the install token: it opens the overlay page, its snapshot
and its live socket, and nothing else. What it gets is built here from a short whitelist, field by field, so nothing
private can reach the stream even if a skill's data grows:

- now playing (Spotify: track, artist, album, cover, progress), stream goals (this stream's subs / follows / bits
  against targets J sets in Setup), the chat mood gauge (score and word, never a message), a countdown (one J
  starts, else the next event on a *stream* calendar, without its title), the follower ticker (follows, subs,
  gifts, cheers, raids, hype trains: who and how many, never what they wrote) and timers (time left, never the label).
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PANELS = ("nowplaying", "goals", "mood", "countdown", "followers", "timers")
ALIASES = {"music": "nowplaying", "spotify": "nowplaying", "now_playing": "nowplaying", "copilot": "mood", "chat": "mood",
           "chatmood": "mood", "subgoal": "goals", "sub_goal": "goals", "goal": "goals", "ticker": "followers",
           "follows": "followers", "events": "followers", "alerts": "followers", "timer": "timers"}
THEMES = ("transparent", "bare", "dark")
# Bus events the overlay listens to; everything else never leaves the server on its socket.
WATCH = {"spotify", "stream_copilot", "automations", "twitch_event", "calendar", "hud_overlay"}
TICKER_KINDS = {"follow", "sub", "resub", "gift", "cheer", "raid", "hype_train"}
GOAL_KINDS = {"subs": "Sub goal", "follows": "Follower goal", "bits": "Bits goal"}
ART_HOSTS = ("i.scdn.co", "mosaic.scdn.co", "image-cdn-ak.spotifycdn.com", "image-cdn-fa.spotifycdn.com")


def load_overlay_token(data_dir: Path, rotate: bool = False) -> str:
    path = Path(data_dir) / "overlay_token"
    if path.exists() and not rotate:
        tok = path.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(18)
    path.write_text(tok, encoding="utf-8")
    return tok


def parse_panels(raw: str | None) -> list[str]:
    out = []
    for p in re.split(r"[,\s]+", (raw or "").lower()):
        p = ALIASES.get(p, p)
        if p in PANELS and p not in out:
            out.append(p)
    return out or list(PANELS[:3])


def settings(cfg: Any) -> dict:
    return dict(((cfg.get("hud") or {}).get("overlay") or {}))


# ---- the whitelist ---------------------------------------------------------------------------------------------

def _text(v: Any, n: int = 120) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def now_playing(spotify: dict | None) -> dict:
    now = (spotify or {}).get("now") or {}
    if not now.get("track"):
        return {"playing": False}
    art = now.get("art") if isinstance(now.get("art"), str) else None
    if art and not (urlparse(art).scheme == "https" and urlparse(art).hostname in ART_HOSTS):
        art = None
    return {"playing": bool(now.get("playing")), "track": _text(now.get("track")), "artist": _text(now.get("artist")),
            "album": _text(now.get("album")), "art": art, "progress_ms": int(_num(now.get("progress_ms")) or 0),
            "duration_ms": int(_num(now.get("duration_ms")) or 0), "at": _num(now.get("at")) or time.time()}


def mood(copilot: dict | None) -> dict:
    c = copilot or {}
    m = c.get("mood") or {}
    return {"live": bool(c.get("live")), "score": max(-1.0, min(1.0, _num(m.get("score")) or 0.0)),
            "label": _text(m.get("label"), 24), "rate": int(_num(m.get("rate")) or 0)}


def goals(copilot: dict | None, conf: dict) -> dict:
    now = ((copilot or {}).get("now") or {}) if (copilot or {}).get("live") else {}
    items = []
    for g in conf.get("goals") or []:
        if not isinstance(g, dict) or g.get("kind") not in GOAL_KINDS:
            continue
        target = int(_num(g.get("target")) or 0)
        if target <= 0:
            continue
        items.append({"kind": g["kind"], "label": _text(g.get("label") or GOAL_KINDS[g["kind"]], 40),
                      "current": int(_num(now.get(g["kind"])) or 0), "target": target})
    return {"live": bool((copilot or {}).get("live")), "items": items[:3]}


def timers(automations: dict | None, now: float | None = None) -> dict:
    now = now or time.time()
    out = []
    for i in (automations or {}).get("items") or []:
        if i.get("kind") != "timer" or not _num(i.get("at")):
            continue
        at = float(i["at"])
        if at <= now - 5:
            continue
        out.append({"at": at, "total_s": int(_num(i.get("total_s")) or max(1, round(at - now)))})
    return {"items": sorted(out, key=lambda x: x["at"])[:4]}


def countdown(calendar: dict | None, conf: dict, now: float | None = None) -> dict:
    """J's own countdown ("Starting soon", 5 minutes) wins; else the next event on a stream calendar, untitled."""
    now = now or time.time()
    own = conf.get("countdown") or {}
    at = _num(own.get("at"))
    if at and at > now:
        return {"at": at, "label": _text(own.get("label") or "Starting soon", 40), "source": "manual"}
    soonest = None
    for e in (calendar or {}).get("events") or []:
        if e.get("profile") != "stream" or e.get("all_day"):
            continue
        try:
            from datetime import datetime

            start = datetime.fromisoformat(str(e.get("start"))).timestamp()
        except (TypeError, ValueError):
            continue
        if now < start <= now + 24 * 3600 and (soonest is None or start < soonest):
            soonest = start
    if soonest:
        return {"at": soonest, "label": "Stream starts", "source": "calendar"}
    return {"at": None, "label": "", "source": None}


def follower(ev: dict | None) -> dict | None:
    if not isinstance(ev, dict) or ev.get("kind") not in TICKER_KINDS:
        return None
    amount = _num(ev.get("amount"))
    return {"kind": ev["kind"], "user": _text(ev.get("user"), 40) or None, "amount": int(amount) if amount else None,
            "ts": _num(ev.get("ts")) or time.time()}


def parts_for(kind: str, data: Any, latest: dict, conf: dict) -> dict:
    """The overlay pieces one bus event changes (``latest``: the bus's last payload per type)."""
    if kind == "spotify":
        return {"nowplaying": now_playing(data)}
    if kind == "stream_copilot":
        return {"mood": mood(data), "goals": goals(data, conf)}
    if kind == "automations":
        return {"timers": timers(data)}
    if kind == "calendar":
        return {"countdown": countdown(data, conf)}
    if kind == "hud_overlay":
        return {"goals": goals(latest.get("stream_copilot"), conf), "countdown": countdown(latest.get("calendar"), conf)}
    return {}


def snapshot(latest: dict, conf: dict, recent_events: list[dict] | None = None) -> dict:
    ticker = [x for x in (follower(e) for e in (recent_events or [])) if x][:6]
    return {"nowplaying": now_playing(latest.get("spotify")), "mood": mood(latest.get("stream_copilot")),
            "goals": goals(latest.get("stream_copilot"), conf), "timers": timers(latest.get("automations")),
            "countdown": countdown(latest.get("calendar"), conf), "followers": ticker}


def event_type(payload: str) -> str:
    """The bus's JSON always starts {"type": "<name>", …}: read the name without parsing the whole event."""
    if payload.startswith('{"type": "'):
        end = payload.find('"', 10)
        if end > 10:
            return payload[10:end]
    try:
        return str(json.loads(payload).get("type") or "")
    except ValueError:
        return ""
