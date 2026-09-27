"""Fast path for common commands: regex + fuzzy matching, no network, ~1 ms.

Anything this doesn't recognise goes to Claude. Keeping the everyday stuff
("open discord", "switch to BRB") local means it works offline, instantly, and
without spending tokens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Intent:
    kind: str  # tool | briefing | recap | confirm | cancel | time | next | reset
    tool: str | None = None
    args: dict = field(default_factory=dict)


@dataclass
class RouterContext:
    is_site: Callable[[str], bool] = lambda t: False
    scene_match: Callable[[str], str | None] = lambda t: None
    profiles: list[str] = field(default_factory=lambda: ["work", "stream"])
    profile_aliases: dict[str, str] = field(default_factory=dict)
    macro_match: Callable[[str], str | None] = lambda t: None


FILLER = re.compile(r"^(?:(?:hey|ok|okay|yo|please|can you|could you|would you|will you|i need you to|go ahead and|and)\s+)+")
POLITE_TAIL = re.compile(r"\s+(?:please|for me|right now|now|real quick|thanks|thank you)$")

CONFIRM = re.compile(r"^(?:yes|yeah|yep|yup|sure|confirm(?:ed)?|do it|go ahead|affirmative|proceed|send it|let'?s go)(?: please| do it)?$")
CANCEL = re.compile(r"^(?:no|nope|nah|cancel|never ?mind|abort|don'?t|stop|forget it)(?: that| it)?$")

PROFILE_WORDS = {"work": "work", "working": "work", "office": "work", "stream": "stream", "streaming": "stream"}


def clean(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"[“”\"!?,;:]+", " ", t)
    t = re.sub(r"\.(?=\s|$)", " ", t)  # sentence dots go; dots inside "github.com" stay
    t = re.sub(r"\s+", " ", t).strip()
    t = FILLER.sub("", t)
    for _ in range(2):
        t = POLITE_TAIL.sub("", t)
    return t.strip()


def route(text: str, ctx: RouterContext | None = None) -> Intent | None:
    ctx = ctx or RouterContext()
    t = clean(text)
    if not t:
        return None

    if CONFIRM.match(t):
        return Intent("confirm")
    if CANCEL.match(t):
        return Intent("cancel")
    if re.search(r"\b(new conversation|start over|reset (the )?(chat|conversation))\b", t):
        return Intent("reset")

    # --- user-defined trigger phrases win over built-ins ------------------
    macro = ctx.macro_match(t)
    if macro:
        return Intent("macro", args={"name": macro})

    # --- briefings -------------------------------------------------------
    if re.search(r"\bgood morning\b|\bmorning (brief|briefing|report|rundown)\b|\bbrief me\b|\bwhat'?s (on )?(my|the) (day|agenda|plan)( today| look like)?\b|\bgive me (the|my) rundown\b", t):
        return Intent("briefing")
    if re.search(r"\b(recap|summar(y|ize)|rundown|review)\b.*\b(day|today)\b|\bend of (the )?day\b|\bhow did (i|my day) (do|go)\b|\bwhat did i (do|get done) today\b", t):
        return Intent("recap")
    if re.search(r"^what should i (do|work on|focus on)( next| now| today)?$|^what'?s next$|^what now$|^next move$", t):
        return Intent("next")
    if re.search(r"^what time is it$|^what'?s the time$|^time check$|^what'?s (the date|today'?s date)$|^what day is it$", t):
        return Intent("time")

    # --- calendar & news ------------------------------------------------
    if re.search(r"\b(next|upcoming) (meeting|event|call)\b|\bwhen'?s my next\b", t):
        return Intent("tool", "calendar", {"days": 2, "next_only": True})
    m = re.search(r"\b(?:what'?s|what is|anything) on (?:my|the) (?:calendar|schedule)(?: for)?(?: (today|tomorrow|this week))?\b|^(?:my )?(?:calendar|schedule)(?: for)?(?: (today|tomorrow|this week))?$", t)
    if m:
        when = m.group(1) or m.group(2) or "today"
        return Intent("tool", "calendar", {"days": {"today": 1, "tomorrow": 2, "this week": 7}[when], "when": when})
    if re.search(r"^(?:what'?s (?:the|in the) )?(?:news|headlines)(?: today)?$|^(?:read|give) me (?:the )?(?:news|headlines)$|^what'?s happening in the world$", t):
        return Intent("tool", "news")

    # --- where am I ------------------------------------------------------
    if re.search(r"\bwhere am i\b|\bwhat am i (looking at|on)\b|\bwhat'?s open\b|\bwhat windows are open\b", t):
        return Intent("tool", "where_am_i")

    # --- routines ---------------------------------------------------------
    m = re.search(r"^(?:start|begin|enter|activate|load|set up|launch|switch to|go into) (?:my )?(\w+) (?:mode|setup|session|workspace|profile)$", t) \
        or re.search(r"^(?:let'?s|time to) (work|stream|get to work)$", t)
    if m:
        word = m.group(1).replace("get to work", "work")
        prof = ctx.profile_aliases.get(word) or PROFILE_WORDS.get(word) or (word if word in ctx.profiles else None)
        if prof:
            return Intent("tool", "run_routine", {"profile": prof})

    # --- OBS ---------------------------------------------------------------
    if re.search(r"^(?:go live|start (?:the )?stream(?:ing)?(?: now)?)$", t):
        return Intent("tool", "obs_control", {"action": "start_stream"})
    if re.search(r"^(?:end|stop|kill) (?:the )?stream(?:ing)?$|^go offline$", t):
        return Intent("tool", "obs_control", {"action": "stop_stream"})
    m = re.search(r"^(start|stop|pause|resume) (?:the )?recording$", t)
    if m:
        return Intent("tool", "obs_control", {"action": f"{m.group(1)}_recording"})
    if re.search(r"^(?:clip (?:that|it)|save (?:the |a )?replay|replay that)$", t):
        return Intent("tool", "obs_control", {"action": "save_replay"})
    if re.search(r"\b(stream|obs) (status|stats|health)\b|\bhow'?s the stream\b|\bam i live\b|\bdropp(ed|ing) frames\b", t):
        return Intent("tool", "obs_status")
    m = re.search(r"^(?:switch|change|go|cut|flip|move|jump)(?: over)?(?: the)?(?: scene)? to (?:the )?(.+?)(?: scene)?$", t) \
        or re.search(r"^(?:scene|obs scene) (.+)$", t)
    if m:
        target = m.group(1).strip()
        explicit_scene = "scene" in t
        scene = ctx.scene_match(target)
        if scene or explicit_scene:
            return Intent("tool", "obs_switch_scene", {"scene": scene or target})
        if ctx.is_site(target):
            return Intent("tool", "open_urls", {"targets": [target]})
        return Intent("tool", "focus_window", {"query": target})
    m = re.search(r"^(mute|unmute) (?:my |the )?(mic|microphone|desktop audio|desktop|music|game audio|discord|alerts?)(?: audio)?$", t)
    if m:
        src = {"mic": "mic", "microphone": "mic", "desktop": "desktop audio"}.get(m.group(2), m.group(2))
        return Intent("tool", "obs_set_mute", {"source": src, "muted": m.group(1) == "mute"})

    # --- media / volume -------------------------------------------------
    if re.search(r"^(?:volume up|turn it up|louder|turn up (?:the )?volume)$", t):
        return Intent("tool", "media_control", {"action": "volume_up", "times": 5})
    if re.search(r"^(?:volume down|turn it down|quieter|turn down (?:the )?volume)$", t):
        return Intent("tool", "media_control", {"action": "volume_down", "times": 5})
    if re.search(r"^(?:mute|unmute)(?: (?:the )?(?:pc|computer|sound|volume|audio))?$", t):
        return Intent("tool", "media_control", {"action": "mute"})
    if re.search(r"^(?:pause|play|resume)(?: (?:the )?(?:music|song|spotify|video|media))?$", t):
        return Intent("tool", "media_control", {"action": "play_pause"})
    if re.search(r"^(?:next|skip)(?: (?:song|track))?$|^skip (?:this|it)$", t):
        return Intent("tool", "media_control", {"action": "next"})
    if re.search(r"^(?:previous|last|go back)(?: (?:song|track))?$", t):
        return Intent("tool", "media_control", {"action": "previous"})
    if re.search(r"^lock(?: (?:the|my))?(?: (?:pc|computer|screen|workstation))?$", t):
        return Intent("tool", "power", {"action": "lock"})

    # --- system ------------------------------------------------------------
    if re.search(r"\boptimi[sz]e\b|\b(system|pc|computer) (status|health|check)\b|\bhow'?s (my |the )?(pc|computer|system|rig)\b|\bwhy is (my |the )?(pc|computer) slow\b", t):
        return Intent("tool", "optimize_pc", {"streaming": "stream" in t})
    if re.search(r"^(?:clean|clear)(?: out| up)? (?:the |my )?temp(?: files| folder)?$", t):
        return Intent("tool", "clean_temp")

    # --- tasks & notes ------------------------------------------------
    m = re.search(r"^(?:add (?:a )?(?:task|to-?do)(?: to)?|remind me to|new task|put on my list) (.+)$", t)
    if m:
        return Intent("tool", "add_task", {"title": _restore_case(text, m.group(1))})
    m = re.search(r"^(?:mark|check off|complete|finish(?:ed)?|done with) (?:the )?(?:task )?(.+?)(?: as done| as complete)?$", t)
    if m and not m.group(1).startswith(("recording", "stream")):
        return Intent("tool", "complete_task", {"title": m.group(1)})
    if re.search(r"^(?:what are|list|show|read) (?:my )?(?:tasks|to-?dos|to do list)$|^my (?:tasks|to-?dos)$", t):
        return Intent("tool", "list_tasks")
    m = re.search(r"^(?:remember|note|make a note)(?: that)? (.+)$", t)
    if m:
        return Intent("tool", "remember", {"text": _restore_case(text, m.group(1))})

    # --- browser / search ---------------------------------------------
    m = re.search(r"^(?:search|google|look up)(?: (google|youtube|github|twitch|reddit|amazon|maps))?(?: for)? (.+?)(?: on (google|youtube|github|twitch|reddit|amazon|maps))?$", t)
    if m:
        engine = m.group(1) or m.group(3) or "google"
        return Intent("tool", "web_search", {"query": m.group(2), "engine": engine})
    m = re.search(r"^(?:youtube|play) (.+?)(?: on youtube)?$", t)
    if m and (t.startswith("youtube") or t.endswith("on youtube")):
        return Intent("tool", "web_search", {"query": m.group(1), "engine": "youtube"})
    m = re.search(r"^(?:open|pull up|bring up|go to)(?: up)? (?:a )?new tab(?: (?:with|for|to|on))? (.+)$", t)
    if m:
        return Intent("tool", "open_urls", {"targets": [m.group(1)]})

    # --- apps & sites ----------------------------------------------------
    m = re.search(r"^(?:close|quit|exit|kill|shut down) (?:the |my )?(.+?)(?: app| program| window)?$", t)
    if m and m.group(1) not in {"pc", "computer", "the pc", "the computer"}:
        return Intent("tool", "close_app", {"name": m.group(1)})
    m = re.search(r"^(?:open|launch|start|fire up|pull up|bring up|run|load up|boot up|go to|take me to)(?: up)? (?:the |my )?(.+?)(?: app| program| website| site)?(?: in chrome)?$", t)
    if m:
        target = m.group(1).strip()
        if ctx.is_site(target) or "in chrome" in t or t.startswith(("go to", "take me to")):
            return Intent("tool", "open_urls", {"targets": [target]})
        return Intent("tool", "open_app", {"name": target})
    m = re.search(r"^(?:focus|show me|switch to|bring) (?:on )?(?:the |my )?(.+?)(?: window)?(?: to the front)?$", t)
    if m:
        return Intent("tool", "focus_window", {"query": m.group(1)})
    return None


def _restore_case(original: str, lowered_fragment: str) -> str:
    """Recover the user's original capitalisation for free-text arguments."""
    idx = original.lower().find(lowered_fragment)
    return original[idx: idx + len(lowered_fragment)].strip() if idx >= 0 else lowered_fragment
