"""Clip-worthy moments: chat, your voice and the big Twitch events, scored together while you're live.

Every couple of seconds while live, three signals (each 0..1) make one score:

- chat: messages in the last 10 s against the channel's own pace (``ChatSpike``), or a chat spike the runtime just
  marked;
- voice: your mic's peak loudness (OBS's meter for it) against how loud you normally talk this stream: a shout, a
  laugh, a "NO WAY";
- events: a raid, a hype train, a sub bomb, a big cheer, fading out over 30 s.

Above the threshold it's a moment: a stream marker goes in at once (harmless, invisible to viewers) and a HUD card asks
"Clip that?" (the clip is the usual ``twitch_clip``: tier 1, and long enough to reach back to the moment). Moments
within 60 s of each other are one moment, and at most ``max_per_10min`` are made.

After the stream: the highlights list (time, reason, score, marker, clip link, a one-line title idea) is saved to
``data/highlights/<date>-session.json``, shaped for the nuhh-promo-video skill, and the HUD has "Copy for promo video".
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from ..integrations.highlights import ChatSpike
from . import Skill
from ._stream import Cards, LiveState, clock_ts, run_on_click

log = logging.getLogger(__name__)

SCHEMA = "vesper.highlights/1"
WEIGHTS = {"chat": 0.5, "voice": 0.45, "event": 0.6}
# A highlight the runtime already logged, scored for the list when no moment of ours covers it.
KIND_SCORE = {"clip": 1.0, "manual": 0.9, "raid": 0.8, "hype_train": 0.8, "gift": 0.6, "cheer": 0.6, "chat_spike": 0.5}
PROMO_NOTE = ("For nuhh-promo-video: each highlight has the stream offset (uptime_s, at) and, when one was made, a Twitch "
              "clip_url (or a local replay_path). Use clips first; otherwise cut the VOD from window.start_s to "
              "window.end_s and beat-pick by loudness inside it.")


# ---- the signals (pure) ----------------------------------------------------------------------------------------

def clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def chat_signal(spike: ChatSpike, now: float, ratio: float = 3.0) -> tuple[float, dict]:
    """0..1 from how far chat is above its own pace: 1.5x is nothing yet, ``ratio`` x (a spike) is 1."""
    recent, baseline = spike.rates(now)
    people = len({u for t, u in spike.chatters if now - t <= spike.window_s})
    if recent < spike.min_msgs or people < 3:
        return 0.0, {"messages": recent, "baseline": round(baseline, 1)}
    x = recent / max(baseline, 1.0)
    return round(clamp((x - 1.5) / max(ratio - 1.5, 0.1)), 2), {"messages": recent, "baseline": round(baseline, 1),
                                                                  "chatters": people}


def voice_signal(peaks: list, now: float, recent_s: float = 4, base_s: float = 90, floor_db: float = -14.0) -> tuple[float, dict]:
    """0..1 from your mic's loudest peak in the last few seconds against how loud you talk (the 75th percentile of the
    minute and a half before). Needs ``floor_db`` (a raised voice, not a quiet room) and a baseline to judge by."""
    recent = [db for t, db in peaks if 0 <= now - t <= recent_s]
    base = sorted(db for t, db in peaks if recent_s < now - t <= base_s)
    if not recent or len(base) < 10:
        return 0.0, {}
    loud, talk = max(recent), base[int(len(base) * 0.75)]
    if loud < floor_db:
        return 0.0, {"peak_db": round(loud, 1), "talk_db": round(talk, 1)}
    return round(clamp((loud - max(talk, -40.0) - 6) / 9), 2), {"peak_db": round(loud, 1), "talk_db": round(talk, 1)}


def event_weight(kind: str, amount: int) -> tuple[float, str]:
    """How big a Twitch event is, 0..1, and what to call it."""
    if kind == "raid":
        return (1.0 if amount >= 5 else 0.6), f"raid ({amount})"
    if kind == "hype_train":
        return 1.0, "hype train"
    if kind == "gift":
        return (0.8 if amount >= 5 else 0.35 if amount >= 2 else 0.15), f"{amount} gifted subs"
    if kind == "cheer":
        return (0.7 if amount >= 500 else 0.35 if amount >= 100 else 0.0), f"{amount} bits"
    if kind in ("sub", "resub"):
        return 0.15, "a sub"
    return 0.0, kind


def combine(chat: float, voice: float, event: float, weights: dict | None = None) -> float:
    w = {**WEIGHTS, **(weights or {})}
    return round(clamp(w["chat"] * chat + w["voice"] * voice + w["event"] * event), 2)


def reasons_of(sig: dict, event_label: str = "") -> list[str]:
    out = []
    if sig.get("event", 0) >= 0.3 and event_label:
        out.append(event_label)
    if sig.get("chat", 0) >= 0.4:
        out.append("chat going off")
    if sig.get("voice", 0) >= 0.4:
        out.append("you got loud")
    return out or ["a bit of everything"]


def fallback_title(item: dict, game: str = "") -> str:
    r = " ".join(item.get("reasons") or [item.get("reason") or ""]).lower()
    if "raid" in r:
        m = re.search(r"raid(?: \(\d+\))? from (\w+)", item.get("reason") or "", re.I)
        return f"The {m.group(1)} raid lands" if m else "Raid incoming!"
    if "hype train" in r:
        return "The hype train leaves the station"
    if "gift" in r or "bits" in r:
        return "Chat showed up big"
    if "loud" in r and "chat" in r:
        return f"{game + ': ' if game else ''}chat and I lost it together"
    if "loud" in r:
        return f"{game + ': ' if game else ''}I did NOT see that coming"
    if "chat" in r:
        return f"{game + ': ' if game else ''}the moment chat went off"
    return (item.get("reason") or "Stream moment")[:80]


def promo_text(export: dict) -> str:
    """What "Copy for promo video" puts on the clipboard: a request plus the JSON."""
    s = export.get("stream") or {}
    head = (f"Use the nuhh-promo-video skill to cut a promo from my {s.get('date', '')} stream"
            f"{' (' + s['game'] + ')' if s.get('game') else ''}: clips first, else the VOD windows below. "
            "Highlights (best first):")
    return head + "\n\n```json\n" + json.dumps(export, indent=2, ensure_ascii=False) + "\n```\n"


# ---- the skill --------------------------------------------------------------------------------------------------

class StreamMoments(Skill):
    name, title = "stream_moments", "Clip-worthy moments"
    poll_s = 2
    delay_s = 5

    def __init__(self, svc, clock=time.time, loudness=None):
        super().__init__(svc)
        self.clock, self._lock = clock, threading.RLock()
        self.state = LiveState(svc, clock, float(self.setting("offline_grace_s", 90)))
        ratio = float((svc.cfg.get("twitch") or {}).get("spike_ratio", 3.0))
        self.spike_ratio = ratio
        self.spike = ChatSpike(ratio=ratio, cooldown_s=0, clock=clock)
        self.loudness = loudness  # () -> [(t, peak dB)]; set at start from OBS's mic meter
        self.run_tool = None  # set at start
        self.cards = Cards(clock)
        self.session: dict | None = None  # {"id", "start", "title", "game", "moments": [...], "extra": [...]}
        self.events: deque = deque()  # (t, weight, label) Twitch events
        self.marks: deque = deque()  # (t, highlight) the runtime's own highlights this stream
        self.meter = {"score": 0.0, "chat": 0.0, "voice": 0.0, "event": 0.0}
        self.last: dict | None = None  # the last stream's highlights list
        self.error: str | None = None
        self.skipped = 0  # moments over the rate limit
        self._last_chat, self._spike_at, self._pub = clock(), 0.0, 0.0
        st = svc.storage
        st.execute("CREATE TABLE IF NOT EXISTS stream_moments_sessions (id INTEGER PRIMARY KEY, start REAL, end REAL, "
                   "path TEXT, data TEXT)")
        row = st.query("SELECT path, data FROM stream_moments_sessions WHERE end IS NOT NULL ORDER BY end DESC LIMIT 1")
        if row:
            try:
                self.last = {**json.loads(row[0]["data"]), "path": row[0]["path"]}
            except (TypeError, ValueError):
                self.last = None
        svc.bus.on(self._on_event)

    def start(self, runtime) -> None:
        tools = getattr(getattr(runtime, "assistant", None), "tools", None)
        self.run_tool = getattr(tools, "run", None)
        mw = getattr(runtime, "mic_watch", None)
        if self.loudness is None and mw is not None:
            def peaks():
                try:
                    return list(mw.peaks)
                except RuntimeError:  # OBS's meter thread added one mid-copy: next tick
                    return []
            self.loudness = peaks

    # ---- the loop ---------------------------------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        changed = False
        try:
            edge = self.state.update()
            if edge == "start":
                self._open(now)
                changed = True
            elif edge == "end" and self.session is not None:
                self._close(self.state.ended_at)
                changed = True
            if self.state.live and self.state.raw():
                if self.session is None:
                    self._open(now)
                self._read_chat()
                changed = self.check(now) or changed
            self.error = None
        except Exception as exc:
            log.exception("moments tick failed")
            self.error = f"{type(exc).__name__}: {exc}"
        # the meter moves every tick while live; the HUD doesn't need it more than every 4 s
        if changed or now - self._pub >= (4 if self.state.live else 30):
            self._pub = now
            self.publish()

    def _open(self, now: float) -> None:
        tw = self._twitch()
        with self._lock:
            row = self.svc.storage.query("SELECT data FROM stream_moments_sessions WHERE end IS NULL ORDER BY start DESC LIMIT 1")
            old = json.loads(row[0]["data"]) if row else None
            if old and now - old.get("saved", 0) < 900:  # Vesper restarted mid-stream: carry on
                self.session = old
                return
            start = now - float(tw.get("uptime_s") or 0) if tw.get("live") and tw.get("uptime_s") else now
            self.session = {"id": int(now), "start": start, "title": tw.get("title") or "", "game": tw.get("game") or "",
                            "moments": [], "saved": now}
            self._persist()
            self.events.clear()
            self.marks.clear()
            self.cards.drop()
            self.skipped = 0
            self.spike = ChatSpike(ratio=self.spike_ratio, cooldown_s=0, clock=self.clock)

    def _persist(self) -> None:
        s = self.session
        if s is not None:
            s["saved"] = self.clock()
            self.svc.storage.execute("INSERT OR REPLACE INTO stream_moments_sessions (id, start, end, path, data) "
                                     "VALUES (?, ?, NULL, NULL, ?)", (s["id"], s["start"], json.dumps(s)))

    def _twitch(self) -> dict:
        return (self.svc.bus.latest.get("twitch") or {}).get("data") or {}

    def _read_chat(self) -> None:
        feed = self.svc.twitch_feed
        for m in (feed.recent_chat(300) if feed is not None else []):
            if m["ts"] > self._last_chat:
                self._last_chat = m["ts"]
                self.spike.add(m.get("user") or "", m.get("text") or "", m["ts"])

    def _event_signal(self, now: float) -> tuple[float, str]:
        best, label = 0.0, ""
        while self.events and now - self.events[0][0] > 30:
            self.events.popleft()
        for t, w, lab in self.events:
            v = w * (1 - max(0.0, now - t - 5) / 25)  # full for 5 s, gone by 30 s
            if v > best:
                best, label = v, lab
        return round(clamp(best), 2), label

    def check(self, now: float) -> bool:
        """Score this instant; a moment if it's over the threshold. True if something new happened."""
        with self._lock:
            chat, cdet = chat_signal(self.spike, now, self.spike_ratio)
            if now - self._spike_at <= 15:
                chat = 1.0
            voice, vdet = voice_signal(self.loudness() if self.loudness else [], now,
                                       floor_db=float(self.setting("voice_floor_db", -14)))
            event, label = self._event_signal(now)
            score = combine(chat, voice, event, self.setting("weights"))
            self.meter = {"score": score, "chat": chat, "voice": voice, "event": event, "chat_detail": cdet, "voice_detail": vdet}
            if score < float(self.setting("threshold", 0.6)):
                return False
            return self._moment(now, score, {"chat": chat, "voice": voice, "event": event}, label)

    def _moment(self, now: float, score: float, sig: dict, label: str) -> bool:
        s = self.session
        if s is None:
            return False
        reasons = reasons_of(sig, label)
        dedupe = float(self.setting("dedupe_s", 60))
        near = next((m for m in reversed(s["moments"]) if now - m["ts"] < dedupe), None)
        if near is not None:  # the same moment, still going: keep the best of it
            near["score"] = max(near["score"], score)
            near["signals"] = {k: max(near["signals"].get(k, 0), v) for k, v in sig.items()}
            near["reasons"] = list(dict.fromkeys(near["reasons"] + reasons))
            near["reason"] = "Clip-worthy: " + ", ".join(near["reasons"])
            return False
        if sum(1 for m in s["moments"] if now - m["ts"] < 600) >= int(self.setting("max_per_10min", 5)):
            self.skipped += 1
            return False
        m = {"id": f"m{len(s['moments']) + 1}-{int(now)}", "ts": now, "uptime_s": round(now - s["start"]), "score": score,
             "signals": dict(sig), "reasons": reasons, "reason": "Clip-worthy: " + ", ".join(reasons), "marker": False,
             "marker_id": None, "marker_position_s": None, "clip_url": None, "clip_id": None, "state": "asked"}
        s["moments"].append(m)
        self._mark(m, now)
        self.cards.add("clip", "Clip that?", yes="Clip it", data={"moment": m["id"], "reason": m["reason"]},
                       ttl=float(self.setting("clip_card_s", 50)), key=f"clip:{m['id']}")
        self._persist()
        self.svc.bus.publish("highlight", {"kind": "moment", "reason": m["reason"], "ts": now, "uptime_s": m["uptime_s"],
                                           "marker": m["marker"], "detail": {"score": score, "signals": sig}})
        return True

    def _mark(self, m: dict, now: float) -> None:
        """A stream marker for the moment, unless the runtime just put one in for the same thing."""
        for t, h in self.marks:
            if abs(now - t) <= 60 and h.get("marker"):
                m["marker"] = True
                return
        tw = self.svc.twitch
        if not (self.setting("auto_marker", True) and self.run_tool and getattr(getattr(tw, "auth", None), "connected", False)):
            return
        try:
            r = self.run_tool("twitch_marker", {"description": m["reason"][:140]}, context={"source": "stream_moments"})
        except Exception as exc:
            r = {"ok": False, "error": str(exc)}
        if isinstance(r, dict) and r.get("ok"):
            m.update(marker=True, marker_id=r.get("id"), marker_position_s=r.get("position_s"))

    # ---- events from the bus ----------------------------------------------------------------------------------
    def _on_event(self, ev: dict) -> None:
        typ, d = ev.get("type"), ev.get("data") or {}
        if typ not in ("twitch_event", "highlight") or not self.state.live:
            return
        now = self.clock()
        with self._lock:
            if typ == "twitch_event":
                w, label = event_weight(d.get("kind") or "", int(d.get("amount") or 0))
                if w > 0:
                    who = d.get("user")
                    self.events.append((now, w, f"{label} from {who}" if who and d.get("kind") in ("raid", "gift", "cheer")
                                        else label))
                return
            kind = d.get("kind")
            if kind == "moment":  # ours
                return
            self.marks.append((d.get("ts") or now, d))
            while self.marks and now - self.marks[0][0] > 4 * 3600:
                self.marks.popleft()
            if kind == "chat_spike":
                self._spike_at = now
            url = (d.get("detail") or {}).get("url")
            if kind == "clip" and url and self.session:
                near = min(self.session["moments"], key=lambda m: abs(m["ts"] - (d.get("ts") or now)), default=None)
                if near is not None and abs(near["ts"] - (d.get("ts") or now)) <= 90 and not near["clip_url"]:
                    near.update(clip_url=url, clip_id=url.rstrip("/").rsplit("/", 1)[-1], state="clipped")

    # ---- J's answers ------------------------------------------------------------------------------------------
    def _find(self, mid: str) -> dict | None:
        s = self.session
        return next((m for m in (s or {}).get("moments", []) if m["id"] == mid), None)

    def clip(self, mid: str) -> dict:
        """Make the Twitch clip for a moment: long enough to reach back to it (Twitch clips end now, at most 60 s)."""
        m = self._find(mid)
        if m is None:
            return {"ok": False, "error": "That moment is from an earlier stream."}
        back = self.clock() - m["ts"]
        if back > 55:
            return {"ok": False, "error": "Too late for a clip of that one (Twitch clips reach back 60 s). The marker is on the VOD."}
        out = run_on_click(self.run_tool, self.svc.bus, "twitch_clip",
                           {"title": m["reason"].removeprefix("Clip-worthy: ")[:100], "duration": round(max(30, min(60, back + 25)))},
                           "clip that")
        if out.get("ok"):
            m.update(clip_url=out.get("url"), clip_id=out.get("id"), state="clipped")
            self.cards.drop("clip", moment=mid)
            self._persist()
        return {"ok": bool(out.get("ok")), "error": out.get("error"), "url": out.get("url"),
                "reply": "Clipped." if out.get("ok") else out.get("error")}

    def answer_card(self, card_id: str, answer: str) -> dict:
        card = self.cards.take(card_id)
        if card is None:
            return {"ok": False, "error": "Too late for that clip. The marker is on the VOD."}
        mid = card["data"].get("moment", "")
        if answer == "yes":
            return self.clip(mid)
        m = self._find(mid)
        if m is not None:
            m["state"] = "skipped"
        return {"ok": True, "answer": "no"}

    # ---- after the stream: the highlights list ----------------------------------------------------------------
    def _close(self, end: float) -> None:
        with self._lock:
            s, self.session = self.session, None
            self.cards.drop()
        try:
            self.last = self.build_export(s, end)
            self.svc.bus.publish("notice", {"source": self.name, "level": "normal", "ts": self.clock(),
                                            "key": f"highlights:{s['id']}",
                                            "text": f"Highlights list ready: {len(self.last['highlights'])} moments, "
                                                    "on the Stream tab (Copy for promo video)."})
        except Exception:
            log.exception("highlights list failed")

    def items(self, s: dict, end: float) -> list[dict]:
        """Our moments plus the runtime's highlights from the same stream that none of them covers."""
        out = [dict(m) for m in s["moments"]]
        hl = list(self.svc.highlights() if self.svc.highlights else []) + [h for _, h in self.marks]
        seen = set()
        for h in sorted(hl, key=lambda h: h.get("ts") or 0):
            ts = h.get("ts") or 0
            key = (round(ts, 1), h.get("kind"))
            if h.get("kind") == "moment" or not (s["start"] <= ts <= end) or key in seen:
                continue
            seen.add(key)
            url = (h.get("detail") or {}).get("url")
            near = next((m for m in out if abs(m["ts"] - ts) <= 60), None)
            if near is not None:
                if url and not near.get("clip_url"):
                    near.update(clip_url=url, clip_id=url.rstrip("/").rsplit("/", 1)[-1])
                if (h.get("detail") or {}).get("path") and not near.get("replay_path"):
                    near["replay_path"] = h["detail"]["path"]
                continue
            out.append({"id": f"h{len(out) + 1}", "ts": ts, "uptime_s": round(h["uptime_s"]) if h.get("uptime_s") is not None
                        else round(ts - s["start"]), "score": KIND_SCORE.get(h.get("kind"), 0.5), "signals": {},
                        "reasons": [h.get("kind") or "moment"], "reason": h.get("reason") or h.get("kind") or "Moment",
                        "kind": h.get("kind"), "marker": bool(h.get("marker")), "marker_id": None, "marker_position_s": None,
                        "clip_url": url, "clip_id": url.rstrip("/").rsplit("/", 1)[-1] if url else None,
                        "replay_path": (h.get("detail") or {}).get("path"), "state": "logged"})
        return sorted(out, key=lambda m: m["ts"])

    def _titles(self, items: list[dict], game: str, title: str) -> list[str]:
        facts = [{"n": i + 1, "at": clock_ts(m["uptime_s"]), "what": m["reason"]} for i, m in enumerate(items)]
        try:
            out = self.svc.think("One punchy title (under 60 characters, no hashtags, no quotes) per stream highlight, for "
                                 f"a short-form clip. Game: {game!r}. Stream title: {title!r}. Highlights: " + json.dumps(facts),
                                 schema={"type": "object", "properties": {"titles": {"type": "array", "items": {"type": "string"}}},
                                         "required": ["titles"]})
        except Exception:
            out = None
        got = [str(t).strip().strip('"')[:80] for t in (out or {}).get("titles", [])] if isinstance(out, dict) else []
        return [got[i] if i < len(got) and got[i] else fallback_title(m, game) for i, m in enumerate(items)]

    def build_export(self, s: dict, end: float) -> dict:
        items = self.items(s, end)
        titles = self._titles(items, s.get("game") or "", s.get("title") or "") if items else []
        tz = self.svc.tz
        start_dt = datetime.fromtimestamp(s["start"], tz)
        hl = []
        for m, t in zip(items, titles):
            up = m["uptime_s"]
            hl.append({"at": clock_ts(up), "uptime_s": up, "time": datetime.fromtimestamp(m["ts"], tz).isoformat(timespec="seconds"),
                       "reason": m["reason"], "kind": m.get("kind") or "moment", "score": m["score"],
                       "signals": m.get("signals") or {}, "marker": m["marker"], "marker_id": m.get("marker_id"),
                       "marker_position_s": m.get("marker_position_s"), "clip_url": m.get("clip_url"),
                       "clip_id": m.get("clip_id"), "replay_path": m.get("replay_path"), "title": t,
                       "window": {"start_s": max(0, up - 20), "end_s": up + 10}})
        hl.sort(key=lambda h: (-h["score"], h["uptime_s"]))
        for i, h in enumerate(hl, 1):
            h["rank"] = i
        tw = self.svc.twitch
        export = {"schema": SCHEMA, "stream": {
            "date": start_dt.strftime("%Y-%m-%d"), "start": start_dt.isoformat(timespec="seconds"),
            "end": datetime.fromtimestamp(end, tz).isoformat(timespec="seconds"), "duration_s": round(end - s["start"]),
            "title": s.get("title") or "", "game": s.get("game") or "",
            "channel": getattr(tw, "login_name", "") or ""}, "highlights": hl, "note": PROMO_NOTE}
        export["path"] = self._save(s, end, export)
        return export

    def _save(self, s: dict, end: float, export: dict) -> str | None:
        folder = Path(self.svc.cfg.data_dir) / "highlights"
        date = export["stream"]["date"]
        path = folder / f"{date}-session.json"
        n = 2
        while path.exists():  # a second stream the same day keeps the first one's list
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("stream", {}).get("start") == export["stream"]["start"]:
                    break
            except (OSError, ValueError):
                pass
            path, n = folder / f"{date}-session-{n}.json", n + 1
        saved = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            body = {k: v for k, v in export.items() if k != "path"}
            path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
            saved = str(path)
        except OSError as exc:
            log.warning("couldn't save the highlights list: %s", exc)
        data = json.dumps({k: v for k, v in export.items() if k != "path"})
        self.svc.storage.execute("INSERT OR REPLACE INTO stream_moments_sessions (id, start, end, path, data) VALUES (?, ?, ?, ?, ?)",
                                 (s["id"], s["start"], end, saved, data))
        return saved

    # ---- what the HUD, the brain and the voice get ------------------------------------------------------------
    def moments(self, session: str = "now") -> dict:
        with self._lock:
            s = self.session
            if session != "last" and s is not None:
                ms = [{k: m[k] for k in ("id", "uptime_s", "score", "reason", "marker", "clip_url", "state")} | {"at": clock_ts(m["uptime_s"])}
                      for m in s["moments"]]
                return {"ok": True, "live": True, "moments": ms, "clipped": sum(1 for m in ms if m["clip_url"]),
                        "skipped_rate_limit": self.skipped}
        if not self.last:
            return {"ok": False, "error": "No highlights yet: they're collected while you're live and listed when the stream ends."}
        return {"ok": True, "live": False, "highlights": self.last["highlights"], "stream": self.last["stream"],
                "path": self.last.get("path"), "clipped": sum(1 for h in self.last["highlights"] if h["clip_url"])}

    def status(self) -> dict:
        with self._lock:
            s = self.session
            return {"live": s is not None, "start": s["start"] if s else None, "meter": self.meter if s else None,
                    "threshold": float(self.setting("threshold", 0.6)), "error": self.error,
                    "moments": [{**m, "at": clock_ts(m["uptime_s"])} for m in (s or {}).get("moments", [])][-20:],
                    "skipped": self.skipped, "cards": self.cards.live(),
                    # the last stream's list only when offline: live, this goes out every few seconds
                    "last": self.last if s is None else None,
                    "promo": promo_text({k: v for k, v in self.last.items() if k != "path"}) if self.last and s is None else None,
                    "voice_source": bool(self.loudness)}

    def actions(self):
        return {"card": lambda b: self.answer_card(str(b.get("id") or ""), str(b.get("answer") or "no")),
                "clip": lambda b: self.clip(str(b.get("id") or ""))}

    def tools(self):
        return [Tool("stream_moments", "Clip-worthy moments: while live, the ones found so far (chat spike + J's voice + "
                                       "raids/sub bombs, each with a stream marker); after, the last stream's highlights "
                                       "list (time, reason, score, marker, clip link, a title idea) saved as JSON for the "
                                       "promo-video skill. session: now or last.",
                     obj({"session": {"type": "string"}}), self.moments)]

    def route(self, t: str):
        if re.fullmatch(r"(?:show |give me |what (?:are|were) )?(?:the |my )?(?:clip[- ]?worthy|best) moments(?: (?:so far|of|from)"
                        r"(?: (?:the |my |that )?(?:last )?stream)?)?|(?:show |give me )?(?:the |my )?highlights? list"
                        r"|(?:any )?clip[- ]?worthy moments(?: so far)?", t):
            return Intent("tool", "stream_moments", {"session": "last" if "last" in t else "now"})
        return None

    def say(self, tool, args, result):
        if result.get("ok") is False:
            return result.get("error")
        if result.get("live"):
            n, c = len(result["moments"]), result["clipped"]
            return (f"{n} clip-worthy moment{'s' if n != 1 else ''} so far" + (f", {c} clipped" if c else "")
                    + ". Each has a marker on the VOD." if n else "Nothing clip-worthy yet. I'm watching chat and your mic.")
        n = len(result["highlights"])
        return (f"{n} highlight{'s' if n != 1 else ''} from the last stream. The list is on the Stream tab, ready to "
                "copy for a promo video.")

    def briefing(self):
        last = self.last
        if last and last.get("highlights") and self.clock() - datetime.fromisoformat(last["stream"]["end"]).timestamp() < 18 * 3600:
            return {"line": f"{len(last['highlights'])} highlights from last stream are ready for a promo (Stream tab)."}
        return None
