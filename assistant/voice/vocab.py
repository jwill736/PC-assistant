"""Words Vesper keeps mishearing, and the names it should expect.

``voice.corrections`` ({"belatro": "Balatro"}) rewrites a word every time it's heard (listener.correct). It learns
from J, never by itself: "you misheard Balatro as Belatro" (said or typed) or retyping in the HUD what he just said
with a word fixed only *proposes* a correction; it's saved on his yes (or a click) to data/settings.yaml, and the
HUD lists them with Remove. A correction applies to every sentence, so the words that steer Vesper (yes, no, stop,
cancel, the name) and little words ("to", "the") can never be rewritten.

``NameHints`` gathers the names worth expecting (the game you're playing, the Twitch category, chatters, people you
email) for speech recognition's hint words (stt.py: Whisper's prompt, Parakeet's hotwords).
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from typing import Callable

log = logging.getLogger(__name__)

MAX_WORDS = 4  # a correction is a word or a name, not a sentence
# Words a correction must never produce or replace: a "no" that became "yes" would answer a question J refused.
PROTECTED = {"yes", "yeah", "yep", "yup", "no", "nope", "nah", "ok", "okay", "sure", "confirm", "cancel", "stop", "wait",
             "never mind", "nevermind", "do it", "go ahead", "resume", "resume control", "kill switch", "stop everything",
             "emergency stop", "stop all actions", "stop talking", "that was me"}
LITTLE = {"a", "an", "the", "to", "too", "two", "of", "in", "on", "at", "it", "is", "i", "me", "my", "and", "or", "for",
          "you", "your", "be", "we", "he", "she", "they", "this", "that", "what", "so", "up", "off", "go"}

_SIDE = r"[“\"']?(.+?)[”\"']?"
FORMS = [  # (pattern on the cleaned sentence, which group is what J meant, needs a sound-alike check)
    (re.compile(rf"^(?:you )?mis-? ?heard (?:the word |me say )?{_SIDE} as {_SIDE}$"), 1, False),
    (re.compile(rf"^(?:you )?(?:heard|wrote|got) {_SIDE} (?:but|when) i said {_SIDE}$"), 2, False),
    (re.compile(rf"^i said {_SIDE} not {_SIDE}$"), 1, False),
    # "it's Tuesday, not Monday" is usually J changing what he wants: only a near-spelling counts as a mishearing
    (re.compile(rf"^(?:it'?s|it is|that'?s|that is) {_SIDE} not {_SIDE}$"), 1, True),
]


def words(text: str) -> list[str]:
    """The words, with their own capitals ("Balatro"), no punctuation."""
    return [w.strip("'") for w in re.sub(r"[^\w' ]+", " ", str(text)).split() if w.strip("'")]


def norm(text: str) -> str:
    return " ".join(words(text)).lower()


def alike(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a).replace(" ", ""), norm(b).replace(" ", "")).ratio()


def parse_correction(t: str) -> tuple[str, str, bool] | None:
    """(heard, meant, sound_alike_needed) from a cleaned sentence like "you misheard balatro as belatro"."""
    t = re.sub(r"\s+(?:please|thanks|thank you)$", "", t.strip())
    for pattern, meant_group, check in FORMS:
        m = pattern.match(t)
        if m:
            meant, heard = m.group(meant_group), m.group(3 - meant_group)
            return heard.strip(" .'"), meant.strip(" .'"), check
    return None


def refuse(heard: str, meant: str, wake_words=()) -> str | None:
    """Why this correction can't be learned (None: it can)."""
    h, m = norm(heard), norm(meant)
    if not h or not m:
        return "I need both words."
    if h == m:
        return (f"I heard both of those as “{meant}”. Type it on the HUD instead: you misheard {meant} as …, with the "
                "word I keep writing.")
    if len(h.split()) > MAX_WORDS or len(m.split()) > MAX_WORDS:
        return "That's more than a word or a name: I only learn single words and names."
    names = {norm(w) for w in wake_words}
    for side in (h, m):
        if side in PROTECTED or side in names:
            return f"I can't rewrite “{side}”: it steers me (yes, no, stop, my name), so it has to stay as heard."
    if h in LITTLE:
        return f"“{h}” is too common to rewrite every time I hear it."
    return None


def retype_fixes(said: str, typed: str) -> list[tuple[str, str]]:
    """J typed what he just said, with a word or two fixed: [(heard, meant)]. Anything else (a new sentence, words
    added or dropped, a change of mind) is []."""
    a, b = norm(said).split(), norm(typed).split()
    if len(a) < 2 or len(b) < 2 or a == b:
        return []
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    changes = [op for op in ops if op[0] != "equal"]
    same = sum(i2 - i1 for tag, i1, i2, _j1, _j2 in ops if tag == "equal")
    if not changes or len(changes) > 2 or same < max(1, len(a) // 2) or any(op[0] != "replace" for op in changes):
        return []
    out = []
    for _tag, i1, i2, j1, j2 in changes:
        heard, meant = " ".join(a[i1:i2]), " ".join(words(typed)[j1:j2])
        if i2 - i1 > 3 or j2 - j1 > 3 or alike(heard, meant) < 0.4:
            return []  # not a near-spelling: he changed what he wanted
        out.append((heard, meant))
    return out


# ---- the saved list -------------------------------------------------------------------------------------------------
class Corrections:
    """``voice.corrections``: config.yaml's own (kept, shown, not removable here) plus learned ones in
    data/settings.yaml. Changes are live: the listener reads the same dict."""

    def __init__(self, cfg):
        self.cfg = cfg  # the whole Config (save_setting needs its data_dir)

    def _learned(self) -> dict:
        from ..config import SETTINGS_FILE, _read_yaml

        layer = _read_yaml(self.cfg.data_dir / SETTINGS_FILE)
        return dict(((layer.get("voice") or {}).get("corrections")) or {})

    def items(self) -> list[dict]:
        learned = self._learned()
        live = (self.cfg.get("voice") or {}).get("corrections") or {}
        return [{"heard": str(k), "meant": str(v), "learned": k in learned} for k, v in live.items()]

    def _save(self, learned: dict) -> None:
        from ..config import save_setting

        voice = self.cfg["voice"]
        before = self._learned()
        static = {k: v for k, v in (voice.get("corrections") or {}).items() if k not in before}
        save_setting(self.cfg, "voice.corrections", learned)
        voice["corrections"] = {**static, **learned}

    def add(self, heard: str, meant: str) -> dict:
        why = refuse(heard, meant, self.cfg["assistant"].get("wake_words") or ())
        if why:
            return {"ok": False, "error": why}
        learned = self._learned()
        learned[norm(heard)] = " ".join(str(meant).split())
        self._save(learned)
        return {"ok": True, "heard": norm(heard), "meant": learned[norm(heard)]}

    def remove(self, heard: str) -> dict:
        learned = self._learned()
        if norm(heard) not in learned:
            return {"ok": False, "error": "That one isn't a learned word (it may be in config.yaml)."}
        del learned[norm(heard)]
        self._save(learned)
        return {"ok": True}


# ---- names to expect ------------------------------------------------------------------------------------------------
class NameHints:
    """The names speech recognition should expect, gathered from what Vesper already knows (no network calls),
    at most once a minute: it's asked on every sentence."""

    def __init__(self, sources: dict[str, Callable[[], list[str]]], every_s: float = 60, clock=time.monotonic):
        self.sources, self.every_s, self.clock = sources, every_s, clock
        self._at, self._words = -1e9, []

    def __call__(self) -> list[str]:
        now = self.clock()
        if now - self._at >= self.every_s:
            words: list[str] = []
            for name, fn in self.sources.items():
                try:
                    words += [str(w).strip() for w in fn() or [] if w and str(w).strip()]
                except Exception:  # one missing source (no Twitch, no contacts table) never stops the rest
                    log.debug("name hints: %s unavailable", name, exc_info=True)
            self._at, self._words = now, list(dict.fromkeys(words))
        return list(self._words)


def learned_words(cfg) -> list[str]:
    """What the fixes write ("Balatro"): names J cared enough to teach."""
    return [str(v) for v in ((cfg.get("voice") or {}).get("corrections") or {}).values()]


def playing(svc, bus, in_game: bool = False) -> list[str]:
    """The game on screen (its window title, while a fullscreen game runs) and the Twitch category, from what's
    already known (no network calls)."""
    cur = (getattr(getattr(svc, "activity", None), "current", None) or {}) if in_game else {}
    tw = ((bus.latest.get("twitch") or {}).get("data") or {}) if bus is not None else {}
    return [w for w in (cur.get("title"), tw.get("game")) if w]


def runtime_sources(svc, feed) -> dict[str, Callable[[], list[str]]]:
    """The slower-changing names, for NameHints: recent chatters and the people J emails most."""
    def chatters():
        if feed is None:
            return []
        users = [m["user"] for m in reversed(feed.recent_chat(80))] + [e.get("user") for e in feed.recent_events(20)]
        return list(dict.fromkeys(u for u in users if u))[:15]

    def people():
        rows = svc.storage.query("SELECT name FROM people_contacts WHERE name != '' ORDER BY count DESC LIMIT 20")
        return [r["name"] for r in rows]

    return {"chatters": chatters, "people": people}
