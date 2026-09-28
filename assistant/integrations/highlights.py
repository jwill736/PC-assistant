"""Live highlight detection: mark the moments worth clipping while you stream.

A moment is a highlight when chat suddenly moves much faster than it has been,
or when something big happens (a raid, a hype train, a large cheer, "clip that").
Each one becomes a Twitch stream marker (invisible to viewers, listed on the
VOD in the Highlighter) and a line in ``data/highlights/<date>.jsonl`` with the
stream-relative time, so cutting shorts later starts from a list instead of
rewatching four hours.

Chat speed is judged against the channel's own recent pace, not a fixed number:
20 messages in 10 seconds is a spike for a 30-viewer stream and noise for a
3,000-viewer one.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# Short reactions that say "something just happened" better than any sentence.
REACTIONS = re.compile(r"^(?:\w*(?:lul|kekw?)|omegalul|pog\w*|pepe\w*|monka\w*|lmf?ao+|lo+l+|wtf|w+|l+|no+|holy|"
                       r"ez+|gg+|sheesh|o+m+g+|(?:ha)+h?|clip|\?+|!+)$", re.I)


@dataclass
class Highlight:
    kind: str              # chat_spike | raid | hype_train | cheer | clip | manual
    reason: str            # shown on the marker and in the HUD
    ts: float              # wall clock
    uptime_s: float | None = None
    marker: bool = False   # a Twitch marker was placed
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class ChatSpike:
    """Messages per window compared with the baseline pace of the minutes before."""

    def __init__(self, window_s: float = 10, baseline_s: float = 300, min_msgs: int = 8, ratio: float = 3.0,
                 cooldown_s: float = 90, clock=time.time):
        self.window_s, self.baseline_s, self.min_msgs, self.ratio = window_s, baseline_s, min_msgs, ratio
        self.cooldown_s, self.clock = cooldown_s, clock
        self.times: deque = deque()
        self.words: deque = deque()     # (t, reaction) for naming the spike
        self.chatters: deque = deque()  # (t, user) so one spammer isn't a spike
        self.last_spike = -1e9

    def add(self, user: str, text: str, t: float | None = None) -> None:
        t = self.clock() if t is None else t
        self.times.append(t)
        self.chatters.append((t, user))
        first = (text or "").split()[0] if (text or "").split() else ""
        word = first.strip("!?.,") or first  # "LUL!!!" is LUL; "???" stays "???"
        if word and REACTIONS.match(word):
            self.words.append((t, word.upper()[:10]))
        horizon = t - self.baseline_s - self.window_s
        for q in (self.times,):
            while q and q[0] < horizon:
                q.popleft()
        for q in (self.words, self.chatters):
            while q and q[0][0] < horizon:
                q.popleft()

    def rates(self, now: float | None = None) -> tuple[int, float]:
        """(messages in the last window, baseline messages per window before it)."""
        now = self.clock() if now is None else now
        recent = sum(1 for t in self.times if now - t <= self.window_s)
        before = [t for t in self.times if self.window_s < now - t <= self.window_s + self.baseline_s]
        span = min(self.baseline_s, max(now - self.times[0] - self.window_s, self.window_s)) if self.times else self.baseline_s
        return recent, len(before) / span * self.window_s

    def check(self, now: float | None = None) -> dict | None:
        now = self.clock() if now is None else now
        if now - self.last_spike < self.cooldown_s:
            return None
        recent, baseline = self.rates(now)
        people = len({u for t, u in self.chatters if now - t <= self.window_s})
        if recent < self.min_msgs or people < max(3, self.min_msgs // 3) or recent < self.ratio * max(baseline, 1.0):
            return None
        self.last_spike = now
        top = Counter(w for t, w in self.words if now - t <= self.window_s).most_common(1)
        return {"messages": recent, "chatters": people, "baseline": round(baseline, 1),
                "reaction": top[0][0] if top else None}


class HighlightLog:
    """Today's highlights, on disk and in memory for the HUD."""

    def __init__(self, folder: Path | None, clock=time.time):
        self.folder, self.clock = folder, clock
        self.recent: deque = deque(maxlen=50)

    def add(self, h: Highlight) -> Highlight:
        self.recent.appendleft(h.as_dict())
        if self.folder:
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                day = datetime.fromtimestamp(h.ts).strftime("%Y-%m-%d")
                with open(self.folder / f"{day}.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(h.as_dict(), ensure_ascii=False) + "\n")
            except OSError:
                pass
        return h


def stamp(uptime_s: float | None) -> str:
    if uptime_s is None:
        return ""
    h, rem = divmod(int(uptime_s), 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}"


def spike_reason(spike: dict) -> str:
    what = f" ({spike['reaction']})" if spike.get("reaction") else ""
    return f"Chat spike{what}: {spike['messages']} messages from {spike['chatters']} people in 10s"
