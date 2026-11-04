"""Words Vesper keeps mishearing, and the names it should expect.

``voice.corrections`` ({"belatro": "Balatro"}) rewrites a word every time it's heard (listener.correct). It learns
from J, never by itself: "you misheard Balatro as Belatro" (said or typed) or retyping in the HUD what he just said
with a word fixed only *proposes* a correction; it's saved on his yes (or a click) to data/settings.yaml, and the
HUD lists them with Remove. A correction applies to every sentence, so no word of the phrases that steer Vesper (every
yes and no it takes as an answer, the kill switch and its resume, the stop words, the name) can be written or
rewritten by one, on either side, and neither can little words ("to", "the").

The names speech recognition expects (stt.py: Whisper's prompt, Parakeet's hotwords) are only ones J chose: words he
taught, the game, the category, his scenes, apps and sites (runtime._hint_words). Never a chatter's, a YouTube viewer's
or an email sender's: anyone can pick those, and a hotword pulls what J said toward it. Measured with Kokoro voices
into Parakeet 110M (4 voices, hotwords_score 2.0): 34 hostile names moved 20 of 136 transcripts, 6 of them onto the
name ("start recording" -> "stop recording" with a chatter called stop_recording). Keeping only one-word names far from
any command or answer word, at a quarter or half the boost, still turned "pause the music" into "paws the music" in 4
of 4 voices with a chatter called Paws. Leaving them out costs 6 of 48 names said in commands coming out spelled right
(8 instead of 14); a shoutout still finds the chatter by sound (integrations/eventsub.py resolve).
"""

from __future__ import annotations

import difflib
import functools
import re

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
    (re.compile(rf"^(?:you )?(?:heard|wrote) {_SIDE} (?:but|when) i said {_SIDE}$"), 2, False),
    # "I said Tuesday, not Monday", "you got Monday but I said Tuesday", "it's Tuesday, not Monday" are usually J
    # changing what he wants (or the booking going wrong): only a near-spelling just heard counts as a mishearing
    (re.compile(rf"^(?:you )?got {_SIDE} (?:but|when) i said {_SIDE}$"), 2, True),
    (re.compile(rf"^i said {_SIDE} not {_SIDE}$"), 1, True),
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


def _expand(pattern: str) -> list[str]:
    """Every phrase a small regex (words, ``|``, ``(?:…)``, ``?`` after a character or a group) matches:
    "let'?s go|confirm(?:ed)?" -> let's go, lets go, confirmed, confirm."""
    def alternatives(i: int) -> tuple[list[str], int]:
        out, cur = [], [""]
        while i < len(pattern) and pattern[i] != ")":
            if pattern[i] == "|":
                out, cur, i = out + cur, [""], i + 1
                continue
            if pattern[i] in "^$":
                i += 1
                continue
            if pattern.startswith("(?:", i):
                part, i = alternatives(i + 3)
                i += 1  # the ")"
            else:
                part, i = [pattern[i]], i + 1
            if i < len(pattern) and pattern[i] == "?":
                part, i = [*part, ""], i + 1
            cur = [a + b for a in cur for b in part]
        return out + cur, i

    return alternatives(0)[0]


@functools.lru_cache(maxsize=1)
def steering_words() -> frozenset[str]:
    """Each word of every phrase that steers Vesper: the yes and no it takes as an answer (confirm_window, the typed
    confirm), the kill switch and its resume (router), the stop words (listener) and PROTECTED. Read from the
    patterns themselves, so a word added there is protected here too."""
    from ..brain import router
    from . import confirm_window as cw, listener

    phrases = set(PROTECTED) | listener.STOP_PHRASES | listener.KILL_PHRASES | listener.CONVERSATION_STOP
    for pattern in (cw._YES, cw._NO, cw._YES_AFTER, cw._NO_AFTER, router.CONFIRM.pattern, router.CANCEL.pattern,
                    router.KILL.pattern, router.RESUME.pattern):
        phrases |= set(_expand(pattern))
    return frozenset(w for p in phrases for w in norm(p).split())


def steers(heard: str, meant: str, wake_words=()) -> str | None:
    """The word of this correction (either side) that steers Vesper, or None. A fix that touched one would rewrite it
    in every sentence: "Kyle" for "kill" switches off the voice kill switch, "yes yes" for "no no" turns a refusal
    into a yes."""
    from .confirm_window import answer

    names = {w for name in wake_words for w in norm(name).split()}
    for side in (norm(heard), norm(meant)):
        if side in PROTECTED or answer(side) is not None:
            return side
        for word in side.split():
            if word in names or word in steering_words() or word.replace("'", "") in steering_words():
                return word
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
    word = steers(h, m, wake_words)
    if word:
        return (f"I can't rewrite “{word}”: it steers me (yes, no, stop, the kill switch, my name), so it has to stay "
                "as heard.")
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


# ---- names to expect (runtime._hint_words) ------------------------------------------------------------------
def learned_words(cfg) -> list[str]:
    """What the fixes write ("Balatro"): names J cared enough to teach (not one the listener skips: see steers)."""
    wake = (cfg.get("assistant") or {}).get("wake_words") or ()
    fixes = ((cfg.get("voice") or {}).get("corrections") or {}).items()
    return [str(v) for k, v in fixes if not steers(str(k), str(v), wake)]


def playing(svc, bus, in_game: bool = False) -> list[str]:
    """The game on screen (its window title, while a fullscreen game runs) and the Twitch category, from what's
    already known (no network calls)."""
    cur = (getattr(getattr(svc, "activity", None), "current", None) or {}) if in_game else {}
    tw = ((bus.latest.get("twitch") or {}).get("data") or {}) if bus is not None else {}
    return [w for w in (cur.get("title"), tw.get("game")) if w]

