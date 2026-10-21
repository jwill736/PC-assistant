"""Conversation mode: talking with Vesper, not just giving it commands.

"Vesper, let's talk" (or the HUD's Talk button) starts it. Until it ends, every sentence in J's voice is a turn: no
wake word, the mic stays open between turns, and talking over a reply stops it and becomes the next turn
(voice/listener.py). Commands still run instantly on the fast path; everything else goes to the model with a
conversational persona and a longer memory of what was said (a rolling summary keeps long talks small).

It ends on "that's all" / "thanks Vesper, that's it" / "stop talking", after a minute of silence, from the HUD, or when
the stream goes live (unless J said "let's talk on stream", which also keeps private things out of it).

Vesper 2.2: conversations that evolve. Each talk is kept as a *thread* (brain/threads.py) that "let's continue our
talk about the move" picks back up; what J says that's worth keeping goes into memory, tagged with the thread; modes
("brainstorm", "devil's advocate", "coach me", "teach me X", "rubber duck", "plan with me", "tell me a story") change the
persona; a small style profile ("too long", "be funnier", "stop asking me questions") shapes every reply; "hmm, let me
think" holds the quiet-minute clock, "go on" continues and "wait, back up" re-explains. Off the record keeps nothing.

This object holds the state; the brain (assistant.py) and the skill (skills/conversation.py) both use it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable

from . import threads as threads_mod
from .threads import ThreadKeeper, first_sentence, when_words

log = logging.getLogger(__name__)

PERSONA = """
Right now you're in a conversation with {user}, not just taking commands. Be good company:
- Warm, direct and a bit witty. Never sycophantic: no "great question", no flattery, no gushing. Disagree when you
  disagree, and say why. Have opinions when asked for them.
- Speak like a person across a desk: 1-3 short sentences unless {user} asks for more. No lists, no markdown.
- Ask {user} a question back when it moves things along (not every turn). Remember what was said earlier in this
  conversation and build on it.
- You know {user}: use what you know naturally, never recite it.
- Brainstorming: offer concrete ideas and build on {user}'s. "Help me think through X": one sharp question or
  observation at a time. "Quiz me": one question at a time, wait for the answer, keep score.
- "How was your day" or "what have you been up to": call conversation_my_day and tell it like a person would.
- You can still use tools mid-conversation when something needs doing. If {user} signals they're done talking
  ("gotta go", "talk later"), call end_conversation and say a short goodbye."""

ON_STREAM = """
You're live on stream: viewers hear everything you say. Never mention anything private: nothing you remember about
{user}, no email, calendar details, documents, money, health or other people's details. Keep it fun and friendly."""

SUMMARY_PROMPT = """Update the running summary of a spoken conversation between {user} and {assistant}.
Keep what matters for carrying on: topics, what {user} said about themselves and their plans, opinions given,
questions still open, any quiz score. At most 120 words, plain sentences, no lists.

Summary so far: {previous}

Newer turns:
{turns}

The updated summary:"""

NO_MODEL = ("Conversation mode needs a model to talk with, and none is connected. Set up the local model in Setup, "
            "then Brain, and we can talk. Until then I'm on commands.")
LOST_MODEL = "I've lost my model, so I'm back to commands. You can reconnect it in Setup, then Brain."
LIVE_REFUSE = ("You're live, so I'll stick to commands. Say let's talk on stream if you want to chat on air, "
               "and I'll keep anything private out of it.")
GREETINGS = ("I'm listening. What's on your mind?", "Sure. What are we talking about?",
             "Go for it. I'm all ears.", "Happy to. What's up?")
ON_STREAM_GREETING = "We're on air, so I'll keep it public. What's up?"
GOODBYE = "Okay. I'm here when you need me."
ASIDE = """
Background jobs that just finished (mention one in a short line at a natural pause, not mid-thought, and only once):
{items}"""

START_RE = (r"(?:let'?s|can we|could we|i (?:want|wanna|would like|'d like) to|i'?d like to|wanna) "
            r"(?:have a |do some |have a little )?(?:talk|chat|conversation|catch up)"
            r"|talk (?:to|with) me|chat with me|keep me company|let'?s have a (?:chat|talk|conversation)"
            r"|(?:start|begin|enter|turn on|switch to) (?:a )?(?:conversation|chat)(?: mode)?"
            r"|conversation mode(?: on)?")
# (the router's clean() already dropped a trailing "now" / "thanks": "that's all for now" arrives as "that's all for")
END_RE = (r"that'?s (?:all|it|everything|enough)(?: for(?: now| today| tonight)?)?|that'?ll be all|that will be all"
          r"|stop talking|end (?:the |this |our )?(?:conversation|chat)|(?:exit|leave|stop|end|turn off) (?:the )?"
          r"conversation mode|conversation mode off|(?:we'?re|i'?m) done(?: talking)?(?: for(?: now| today)?)?"
          r"|(?:good ?)?bye(?: for now)?|talk (?:to you )?later|catch you later|over and out|nothing else")
LEAD = r"(?:(?:thanks|thank you|cheers|ok|okay|alright|all right|right|cool|great|perfect|anyway)\s+)*"


def _names(cfg) -> str:
    """The assistant's name and wake words, as a regex alternation."""
    a = cfg["assistant"]
    words = {str(a.get("name") or "").lower(), *[str(w).lower() for w in a.get("wake_words") or []]}
    return "|".join(sorted((re.escape(w) for w in words if w), key=len, reverse=True)) or "vesper"


def match_start(t: str, cfg) -> dict | None:
    """'let's talk', 'Vesper let's chat on stream', "let's talk about my week", "let's talk off the record"
    -> {on_stream, topic?, off_record?}."""
    name = rf"(?:(?:{_names(cfg)})\s+)?"  # typed lines keep the name
    off = t.startswith("off the record ")
    t = t.removeprefix("off the record ")
    m = re.fullmatch(rf"{LEAD}{name}(?:{START_RE})(?P<stream> (?:on|with) (?:the )?(?:stream|air|chat))?"
                     rf"(?P<off> off the record)?(?: for a (?:bit|while|minute|second|few minutes))?"
                     rf"(?: about (?P<topic>.+?))?(?P<off2> off the record)?", t)
    if not m:
        return None
    topic = (m.group("topic") or "").strip()
    off = off or bool(m.group("off") or m.group("off2"))
    return {"on_stream": bool(m.group("stream")), **({"topic": topic} if topic else {}),
            **({"off_record": True} if off else {})}


def match_end(t: str, cfg) -> bool:
    """'that's all', 'thanks Vesper that's it', 'stop talking', 'bye Vesper' (only checked while talking)."""
    names = _names(cfg)
    return bool(re.fullmatch(rf"{LEAD}(?:(?:{names})\s+)?{LEAD}(?:{END_RE})(?:\s+(?:{names}))?", t))


# ---- modes (Vesper 2.2) -------------------------------------------------------------------------------------
# Each mode is a persona tweak plus behaviour rules, added to the conversation prompt. {topic} is what J named.
MODES: dict[str, dict] = {
    "chat": {"label": "Just chat", "rules": "",
             "hello": "Just chatting, then. What's on your mind?", "switch": "Back to just chatting."},
    "brainstorm": {"label": "Brainstorm", "hello": "Brainstorm it is. Throw me the problem and we'll go wide.",
                   "switch": "Switching to brainstorm. No bad ideas for now.",
                   "rules": "Brainstorm {topic} with {user}: offer two or three concrete, genuinely different ideas per "
                            "turn, build on {user}'s ideas ('yes, and'), no judging until {user} asks which are best; "
                            "then pick favourites and say why."},
    "devil": {"label": "Devil's advocate",
              "hello": "Devil's advocate, at your service. Tell me what you believe and I'll argue against it.",
              "switch": "Devil's advocate on. I'll take the other side.",
              "rules": "Play devil's advocate on {topic}: take the side against whatever {user} argues, firmly but "
                       "fairly. Steelman the other view, go for the weakest point in {user}'s case, and don't agree "
                       "just to be nice; concede a point only when it's genuinely good, and say so."},
    "coach": {"label": "Coach", "hello": "Coach mode. What do you want to work through?",
              "switch": "Coach mode. I'll ask, you answer.",
              "rules": "Coach {user} on {topic}: don't give answers or advice. Ask one short open question at a time "
                       "(what, how, what else, what would it take) that helps {user} find their own answer, and "
                       "reflect back what you heard. If {user} asks for your answer, turn it back once; after that, "
                       "a short hint only."},
    "interview": {"label": "Interview me", "hello": "Alright, I'm the interviewer. Tell me what it's for, or say go.",
                  "switch": "Interview mode. I'll ask the questions.",
                  "rules": "Interview {user} about {topic}: one question at a time, follow up on interesting answers and "
                           "dig deeper, like a good podcast host. Keep your own talking to a line."},
    "teach": {"label": "Teach me", "hello": "Happy to teach. What do you want to learn?",
              "switch": "Teaching mode. Stop me whenever.",
              "rules": "Teach {user} {topic} in small steps: one idea per turn, plain words and an example, then ask "
                       "a short check question and wait for the answer. If the answer is wrong, explain it a different "
                       "way. Keep track of what's been covered and build on it."},
    "duck": {"label": "Rubber duck", "hello": "Quack. Walk me through it from the start.",
             "switch": "Rubber duck mode. Talk it through; I'll mostly listen.",
             "rules": "Be {user}'s rubber duck for {topic} (often code): mostly listen. Short prompts only: 'and then "
                      "what happens?', 'what did you expect instead?', 'how do you know that part works?'. Question "
                      "assumptions; don't jump to a solution unless {user} asks or is clearly stuck."},
    "plan": {"label": "Plan with me", "hello": "Let's plan. What are we planning, and by when?",
             "switch": "Planning mode. Goal first, then steps.",
             "rules": "Plan {topic} with {user}: goal, constraints, steps, dates. One question at a time. When the "
                      "plan is clear, or {user} says that's the plan, call conversation_propose_tasks with the concrete "
                      "tasks: they show on a card for {user} to approve. Never add tasks yourself."},
    "story": {"label": "Story", "hello": "Story time. Any setting in mind, or shall I pick?",
              "switch": "Story mode. Settle in.",
              "rules": "Tell or co-create a story about {topic}: vivid, short beats, spoken (no lists), and end each "
                       "turn with a hook or a choice for {user}."},
}
MODE_WORDS = {"brainstorm": "brainstorm", "brainstorming": "brainstorm", "devil's advocate": "devil",
              "devils advocate": "devil", "debate": "devil", "debating": "devil", "coach": "coach",
              "coaching": "coach", "interview": "interview", "interviewer": "interview", "teach": "teach",
              "teaching": "teach", "teacher": "teach", "lesson": "teach", "rubber duck": "duck",
              "rubber ducking": "duck", "rubber duck debugging": "duck", "plan": "plan",
              "planning": "plan", "story": "story", "storytelling": "story", "story time": "story", "chat": "chat",
              "just chat": "chat", "normal": "chat", "regular": "chat", "default": "chat", "chatting": "chat"}
_MW = "|".join(sorted((re.escape(w).replace("'", "'?") for w in MODE_WORDS), key=len, reverse=True))


def _topic(name: str) -> str:
    return rf" (?:about|on|for|through|with) (?P<{name}>.+)"


MODE_STARTS = [
    ("brainstorm", rf"(?:let'?s |can we |help me |i want to |i wanna )?brainstorm(?: with me)?(?:{_topic('a')}|"
                   rf" (?P<b>(?!with me$).+))?"),
    ("devil", rf"(?:play|be) (?:the |my )?devil'?s advocate(?:{_topic('a')})?"),
    ("devil", rf"(?:let'?s (?:have a )?|can we )?debate(?: me)?(?:{_topic('a')}| (?P<b>(?!me$).+))?"),
    ("devil", rf"argue (?:with me|the other side|against me)(?:{_topic('a')})?"),
    ("coach", rf"be my coach|coach me(?:{_topic('a')})?"),
    ("interview", rf"interview me(?:{_topic('a')})?|(?:let'?s do |do )?(?:a )?mock interview(?:{_topic('b')})?"),
    ("teach", r"teach me(?: about)? (?P<a>.+)|(?:give me a |let'?s do a )lesson (?:on|about) (?P<b>.+)"),
    ("duck", r"(?:let'?s |can we |can i |i (?:want|need) to )?rubber ?duck(?: (?:this|it|with you|with me|something"
             r"|a (?:problem|bug)))?|be my rubber duck|i need a rubber duck"),
    ("plan", r"plan (?:with me|(?P<a>.+?) with me)"
             r"|(?:help me|let'?s) plan (?P<b>(?!(?:out )?(?:my |the )?(?:day|today|tomorrow|week)$).+)"),
    ("story", r"tell me a story(?: about (?P<a>.+))?|(?:let'?s )?(?:write|make up|tell|do) a story(?: together)?"
              r"(?: about (?P<b>.+))?|story ?time"),
    ("chat", r"(?:let'?s )?just (?:chat|talk)"),
]
MODE_SWITCH = (rf"(?:switch|change|go|move) (?:to|into) (?:the )?(?P<a>{_MW})(?: mode)?|(?P<b>{_MW}) mode(?: on)?"
               rf"|(?P<stop>(?:stop|drop|end|enough) (?:the |with the |playing )?(?:{_MW})(?: mode)?|(?:go )?back to normal"
               rf"(?: mode)?)")
RESUME_RE = (r"(?:let'?s |can we |could we |i want to |i'?d like to )?(?:continue|resume|carry on|pick up|get back to"
             r"|go back to|return to)(?: with)? (?:our|the|that|this|my) (?:talk|chat|conversation|discussion)"
             r"(?: (?:about|on|re) (?P<a>.+))?"
             r"|(?:let'?s )?pick (?:it |things )?(?:back )?up where we left (?:off|it)(?: (?:about|on|with) (?P<b>.+))?"
             r"|where (?:did we leave (?:off|it)|were we)(?: (?:on|with|about) (?P<c>.+))?"
             r"|what were we (?:saying|talking about|discussing)(?: (?:about|on|re) (?P<d>.+))?"
             r"|(?:let'?s )?(?:continue|carry on) (?:where we left off|from last time)"
             r"|(?:let'?s )?(?:get back to|go back to|continue|resume) (?:talking|chatting) about (?P<e>.+)"
             r"|remind me what we (?:said|decided|talked about) (?:about|on) (?P<f>.+)")
HOLD_RE = (r"(?:(?:hmm+|um+|uh+|so|well|right|okay|ok)\s+)*(?:let me think(?: (?:about|on) (?:it|that|this))?"
           r"(?: for a (?:sec|second|minute|moment|bit))?|give me a (?:sec|second|minute|moment)(?: to think)?"
           r"|hold on,? let me think|let me mull (?:it|that) over|i need to think(?: about (?:it|that))?"
           r"|one sec(?:ond)?(?: let me think)?)")
LINK_RE = (r"(?:(?:yes|yeah|yep|sure|ok|okay)\s+)?(?:let'?s\s+)?(?:pick (?:that|it) (?:back )?up|link (?:it|them|that|those)"
           r"(?: up)?|same (?:thread|conversation|talk)|carry on from (?:there|that)|continue (?:that|that one|from there))")
FORGET_RE = (r"(?:forget|delete|erase)(?: about)? (?:this|our|the|that)(?: whole)? (?:conversation|chat|talk|thread)"
             r"(?: (?:about|on) (?P<a>.+))?|forget (?:everything )?(?:what )?we (?:just )?(?:talked|said|chatted) about"
             r"(?: (?P<b>.+))?|forget (?:the |our )?last (?:conversation|chat|talk)")
NO_FOLLOWUP_RE = (r"(?:don'?t|stop|no more) (?:follow(?:ing)?[- ]up|asking(?: me)?|checking in) (?:on|about) (?P<a>.+)"
                  r"|(?:don'?t|stop) follow(?:ing)?[- ]up|no more follow[- ]?ups")
STYLE_FEEDBACK: list[tuple[str, str, Any]] = [
    (r"(?:(?:that'?s|that was|you'?re being|you'?re|way|a bit|bit|much)\s+)*too (?:long|wordy)"
     r"|(?:keep it |be |give me )?(?:shorter|briefer|more brief|more concise)(?: (?:answers|replies))?"
     r"|keep (?:it|them|your answers) (?:short|brief|snappy)|less detail|too much detail|get to the point"
     r"|you talk too much", "length", -1),
    (r"(?:give me |go into )?more detail|(?:be )?more detailed|(?:give me )?longer (?:answers|replies)"
     r"|that was too short|too short|you can say more", "length", +1),
    (r"(?:be )?funnier|more jokes|(?:be )?more (?:fun|playful)|lighten up|make me laugh more", "humour", +1),
    (r"(?:be )?(?:more )?serious|(?:less|fewer) jokes|(?:no more|stop the|stop with the) jokes|stop joking"
     r"|not so many jokes|tone down the (?:jokes|humou?r)", "humour", -1),
    (r"stop asking (?:me )?(?:so many )?questions|no more questions|don'?t ask (?:me )?(?:so many )?questions",
     "questions", "=0"),
    (r"(?:less|fewer) questions|(?:you )?ask too many questions|not so many questions|too many questions",
     "questions", -1),
    (r"ask me more questions|be more curious", "questions", +1),
    (r"(?:be )?more formal|(?:be )?more professional|less casual", "formality", +1),
    (r"(?:be )?(?:more casual|less formal|less stiff)|loosen up", "formality", -1),
]
STYLE_RESET_RE = r"reset (?:my |your )?(?:conversation |talking )?style|(?:go )?back to (?:your )?(?:normal|default) style"
STYLE_ASK_RE = r"what'?s my (?:conversation|talking) style|how do i like you to talk"
LOOK_RE = (r"(?:look into|research|dig into|find out about|check out) (?P<a>.+?) (?:while we (?:talk|chat|keep talking)"
           r"|in the background)|(?:look into|research|dig into) (?:that|this|it)(?: for me)?(?: later)?")
ADD_TASKS_RE = (r"(?:(?:yes|yeah|yep|sure|ok|okay)\s+)?(?:add|save) (?:them|those|these|those tasks|these tasks|the tasks"
                r"|them all|all of them)(?: (?:to my (?:tasks|list)|as tasks))?")
# Not routed (the model answers them), but they change what it's told for this turn.
CONTINUE_RE = (r"(?:(?:ok|okay|yeah|and|so)\s+)?(?:go on|keep going|tell me more|carry on|continue|and then|then what"
               r"|what else|more)")
BACKUP_RE = (r"(?:(?:wait|hang on|hold on|sorry)\s+)*(?:back up|say (?:that|it) again|run (?:that|it) by me again"
             r"|i'?m lost|you lost me|i don'?t (?:follow|get it)|slow down|what do you mean|come again|one more time)")
DIRECTIVES = {
    "continue": "{user} said \"{said}\": carry on from where your last reply ended (it may have been cut off). Add the "
                "next part; don't repeat what you already said.",
    "backup": "{user} said \"{said}\": they lost you. Re-explain your last point from the start, more simply, in "
              "shorter steps, with a different example.",
}


def _norm_t(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[,.!?;:]+", " ", (t or "").lower())).strip()


def match_mode(t: str, cfg) -> dict | None:
    """'let's brainstorm names for the channel', 'play devil's advocate', 'teach me chess', 'switch to coach mode'
    -> {mode, topic?}."""
    t = re.sub(rf"^{LEAD}(?:(?:{_names(cfg)})\s+)?", "", t)
    for mode, pat in MODE_STARTS:
        m = re.fullmatch(pat, t)
        if m:
            topic = next((g for g in m.groups() if g), "")
            return {"mode": mode, **({"topic": topic.strip()} if topic.strip() else {})}
    m = re.fullmatch(MODE_SWITCH, t)
    if m:
        if m.group("stop"):
            return {"mode": "chat"}
        word = (m.group("a") or m.group("b") or "").replace("devils", "devil's")
        return {"mode": MODE_WORDS.get(word, "chat")}
    return None


def match_resume(t: str) -> str | None:
    """'let's continue our talk about the move' -> 'the move'; 'pick up where we left off' -> ''; else None."""
    m = re.fullmatch(RESUME_RE, t)
    if not m:
        return None
    return next((g for g in m.groups() if g), "").strip()


def match_style(t: str) -> tuple[str, Any] | None:
    """'too long' -> ('length', -1); 'stop asking me questions' -> ('questions', '=0')."""
    for pat, dim, step in STYLE_FEEDBACK:
        if re.fullmatch(pat, t):
            return dim, step
    return None


def turn_directive(text: str) -> str:
    """'continue' for "go on" / "tell me more", 'backup' for "wait, back up", else ''."""
    t = _norm_t(text)
    if re.fullmatch(CONTINUE_RE, t):
        return "continue"
    if re.fullmatch(BACKUP_RE, t):
        return "backup"
    return ""


def you_form(text: str, user: str) -> str:
    """'J is leaning towards the 9800X3D' -> 'you were leaning towards the 9800X3D' (said back to J)."""
    u = re.escape(user)
    s = (text or "").strip().rstrip(".")
    for pat, rep in ((rf"^{u} (?:is|was) ", "you were "), (rf"^{u} has ", "you had "), (rf"^{u}'s ", "your "),
                     (rf"^{u} (\w+?)s ", r"you \1 "), (rf"^{u} ", "you ")):
        new = re.sub(pat, rep, s, count=1, flags=re.I)
        if new != s:
            return new
    return s


class Conversation:
    """Is J talking with Vesper right now, and what's been said."""

    def __init__(self, svc: Any, clock: Callable[[], float] = time.time):
        self.svc, self.clock = svc, clock
        self.active = False
        self.on_stream = False
        self.started = 0.0
        self.last_activity = 0.0
        self.last_source = ""
        self.busy = 0  # turns being answered right now (never time out mid-reply)
        self.turns = 0
        self.summary = ""
        self.summarised_until = 0.0  # transcript entries up to this time are folded into the summary
        self.transcript: list[dict] = []  # {"role": "user"|"assistant", "text", "ts", "source"}
        self.interrupted = False  # J cut the last reply short: the model is told on the next turn
        self.ended: dict | None = None  # {"reason", "ts"} of the last one
        self.asides_seen = 0.0  # background jobs (skills/tasks_agent.py) finished before this were already mentioned
        self.speaker = None  # the runtime's Speaker: talking now counts as activity
        self.brain: Callable[[], str | None] = lambda: None  # set by the assistant: which model answers, if any
        self.on_change: list[Callable[[dict], None]] = []
        self._lock = threading.RLock()
        self._fold: threading.Thread | None = None
        # Vesper 2.2: threads, modes, style, off the record
        self.threads = ThreadKeeper(svc, clock)
        self.mode = "chat"
        self.topic = ""
        self.thread: dict | None = None  # the earlier talk this one continues (or the one it was saved as)
        self.link_offer: dict | None = None  # an earlier talk this one sounds like: {"id", "title", "when"}
        self.link_checked = False
        self.off_record = False  # nothing from this talk is kept
        self.off_record_check: Callable[[], bool] = lambda: False  # the assistant's own "off the record" timer
        self.hold_until = 0.0  # "hmm, let me think": the quiet-minute clock waits until then
        self.saved_until = 0.0  # transcript entries up to here are already in a thread
        self.covered: dict | None = None  # "what we covered", for the HUD once it ends
        self.proposals: list[dict] = []  # plan mode: tasks proposed on a card, never added without a yes
        self.followup_note: dict | None = None  # a follow-up Vesper just asked: {"text", "thread_id", "title", "at"}
        bus = getattr(svc, "bus", None)
        if bus is not None and hasattr(bus, "on"):
            bus.on(self._on_event)

    # ---- settings -------------------------------------------------------------------------------------
    def setting(self, key: str, default: Any) -> Any:
        return (self.svc.cfg.get("conversation") or {}).get(key, default)

    @property
    def timeout_s(self) -> float:
        return float(self.setting("timeout_seconds", 60))

    # ---- life cycle -----------------------------------------------------------------------------------
    def begin(self, *, on_stream: bool = False, source: str = "voice", mode: str = "",
              off_record: bool = False, fresh: bool = False) -> tuple[bool, str]:
        """Start (or keep going). (False, why) when there's no model, or the stream is live and J didn't say
        "on stream". ``mode``: brainstorm, devil, coach… (MODES); ``off_record``: keep nothing from this talk;
        ``fresh``: a new topic or mode, so not the talk that just ended carried on (a plain "let's talk" within
        ``summary_keep_minutes`` is)."""
        if self.brain() is None:
            return False, NO_MODEL
        if self._live() and not on_stream and not (self.active and self.on_stream):  # (a mode switch on air is fine)
            return False, LIVE_REFUSE
        with self._lock:
            already = self.active
            now = self.clock()
            if not already:
                self.started, self.turns, self.interrupted = now, 0, False
                self.mode, self.topic, self.link_offer, self.link_checked = "chat", "", None, False
                self.hold_until, self.covered, self.proposals, self.off_record = 0.0, None, [], False
                self.asides_seen = now
                if (self.ended is None or fresh
                        or now - self.ended["ts"] > float(self.setting("summary_keep_minutes", 30)) * 60):
                    self.summary, self.summarised_until = "", 0.0
                    self.transcript, self.thread, self.saved_until = [], None, 0.0
            if mode in MODES:
                self.mode = mode
            self.off_record = self.off_record or bool(off_record)
            self.active, self.on_stream = True, bool(on_stream or (already and self.on_stream))
            self.last_activity, self.last_source = now, source
            self.ended = None
        self._changed()
        return True, "already" if already else "started"

    def end(self, reason: str = "said") -> bool:
        with self._lock:
            if not self.active:
                return False
            was_on_stream = self.on_stream
            self.active, self.on_stream = False, False
            self.ended = {"reason": reason, "ts": self.clock()}
            self.hold_until = 0.0
        log.info("conversation ended (%s)", reason)
        try:
            self._keep(was_on_stream)
        except Exception:
            log.exception("saving the conversation thread failed")
        self._changed()
        return True

    def is_active(self) -> bool:
        self.check()
        return self.active

    def check(self, now: float | None = None) -> str | None:
        """End it if the stream went live (and it wasn't started on stream) or J went quiet. Returns why it ended."""
        if not self.active:
            return None
        if not self.on_stream and self._live():
            self.end("live")
            return "live"
        if self.idle_for(now) > self.timeout_s:
            self.end("timeout")
            return "timeout"
        return None

    def idle_for(self, now: float | None = None) -> float:
        now = self.clock() if now is None else now
        sp = self.speaker
        if self.busy or (sp is not None and sp.speaking.is_set()) or now < self.hold_until:
            return 0.0  # answering, talking, or J said "let me think"
        last = max(self.last_activity, getattr(sp, "last_end", 0.0) if sp is not None else 0.0)
        return max(0.0, now - last)

    def touch(self) -> None:
        self.last_activity = self.clock()

    # ---- turns ----------------------------------------------------------------------------------------
    def turn_started(self, said: str, source: str) -> None:
        """J said something in the conversation: on the HUD at once, with Vesper "thinking" until the reply."""
        with self._lock:
            self.busy += 1
            if source == "voice":  # J is talking out loud: typed lines in between get spoken replies too
                self.last_source = source
            self._add("user", said, source)
            self.touch()
            self.hold_until = 0.0  # J is talking again
            if self.off_record_check():
                self.off_record = True  # "off the record" at any point: nothing from this talk is kept
        self._check_link(said)
        self._changed()

    def turn_done(self, reply: str, source: str, said: str | None = None, started: bool = True) -> None:
        """The reply (``said``: J's words, when they weren't recorded at the start: the "let's talk" turn)."""
        with self._lock:
            if started:
                self.busy = max(0, self.busy - 1)
            if said:
                self._add("user", said, source)
            if reply:
                self._add("assistant", reply, source)
            self.touch()
        self._changed()

    def _add(self, role: str, text: str, source: str) -> None:
        if not text:
            return
        now = max(self.clock(), (self.transcript[-1]["ts"] + 0.001) if self.transcript else 0.0)
        self.transcript.append({"role": role, "text": text[:1200 if role == "assistant" else 600], "ts": now,
                                "source": source})
        self.transcript = self.transcript[-200:]
        if role == "user":
            self.turns += 1

    def asides(self) -> str:
        """Jobs that finished since the last turn (svc.tasks.done_since), for the model to mention at a pause. Each is
        told once, and its nudge (held until the conversation ends) is dropped. Never on stream: results are private."""
        tasks = getattr(self.svc, "tasks", None)
        if tasks is None or self.on_stream:
            return ""
        try:
            done = tasks.done_since(self.asides_seen, mark=True)
        except Exception:
            log.exception("job asides failed")
            return ""
        self.asides_seen = self.clock()
        user = self.svc.cfg["assistant"].get("user_name") or "J"
        lines = [f"- {d['title']}: " + (f"didn't work ({d['line']})" if d["state"] == "failed" else
                                        f"waiting on {user}'s OK to {d['waiting']}" if d.get("waiting") else
                                        d["line"] or "done") for d in done[-3:]]
        return ASIDE.format(items="\n".join(lines)) if lines else ""

    def take_interrupted(self) -> bool:
        was, self.interrupted = self.interrupted, False
        return was

    def _on_event(self, event: dict) -> None:
        if event.get("type") == "interrupted" and self.active:
            self.interrupted = True

    # ---- the rolling summary --------------------------------------------------------------------------
    def fold(self, think: Callable[..., Any], keep_turns: int, *, wait: bool = False) -> None:
        """Fold what's older than the last ``keep_turns`` exchanges into the summary (in the background: the
        next reply shouldn't wait for it). Without a model the old turns just drop out."""
        with self._lock:
            fresh = [e for e in self.transcript if e["ts"] > self.summarised_until]
            old = fresh[:max(0, len(fresh) - 2 * keep_turns)]
            if not old:
                return
            self.summarised_until = old[-1]["ts"]
        a = self.svc.cfg["assistant"]
        prompt = SUMMARY_PROMPT.format(
            user=a.get("user_name") or "J", assistant=a.get("name") or "Vesper", previous=self.summary or "(nothing yet)",
            turns="\n".join(f"{a.get('user_name') or 'J' if e['role'] == 'user' else a.get('name') or 'Vesper'}: {e['text']}"
                            for e in old)[-6000:])

        def run() -> None:
            try:
                text = think(prompt, max_tokens=400)
            except Exception:
                log.exception("conversation summary failed")
                text = None
            if isinstance(text, str) and text.strip():
                self.summary = _clip(text.strip(), 1200)
        if wait:
            run()
            return
        self._fold = threading.Thread(target=run, name="conversation-summary", daemon=True)
        self._fold.start()

    def wait_summary(self, timeout: float = 10) -> None:
        if self._fold is not None:
            self._fold.join(timeout)

    def reset(self) -> None:
        self.summary, self.summarised_until = "", self.clock()

    # ---- threads, modes, style (Vesper 2.2) --------------------------------------------------------------
    @property
    def user(self) -> str:
        return self.svc.cfg["assistant"].get("user_name") or "J"

    def set_mode(self, mode: str, topic: str = "") -> bool:
        if mode not in MODES:
            return False
        self.mode = mode
        if topic:
            self.topic = topic
        self._changed()
        return True

    def hold(self, minutes: float | None = None) -> float:
        """"Hmm, let me think": the quiet-minute clock waits (until J talks again, or this runs out)."""
        mins = float(self.setting("think_minutes", 3) if minutes is None else minutes)
        self.hold_until = self.clock() + max(0.0, mins) * 60
        self._changed()
        return self.hold_until

    def attach(self, thread: dict) -> dict:
        """Carry on an earlier talk: its notes go into the prompt and this talk is saved onto it."""
        self.thread = {k: thread.get(k) for k in ("id", "title", "summary", "points", "stances", "open", "promises",
                                                  "follow_up", "updated", "last_turns", "summarised")}
        self.thread["resumed"] = True
        self.link_offer = None
        self.link_checked = True
        if thread.get("mode") and thread["mode"] != "chat" and self.mode == "chat":
            self.mode = thread["mode"]
        self._changed()
        return self.thread

    def link(self, thread_id: int | None = None) -> dict | None:
        """J said yes to the offer ("pick that up"): this talk continues that thread."""
        tid = thread_id or (self.link_offer or {}).get("id")
        t = self.threads.get(int(tid)) if tid else None
        return self.attach(t) if t else None

    def _check_link(self, said: str) -> None:
        """The first real thing J says in a new talk: does it sound like an earlier one? Then offer to pick it up."""
        if (self.link_checked or self.thread or self.on_stream or self.off_record or not self.active
                or len(said.split()) < 3 or match_start(_norm_t(said), self.svc.cfg)):
            return
        self.link_checked = True
        try:
            hit = self.threads.match(said, strict=True)
        except Exception:
            log.exception("thread match failed")
            hit = None
        if hit:
            t, score = hit
            self.link_offer = {"id": t["id"], "title": t["title"], "when": when_words(t["updated"], self.clock(), self.svc.tz),
                               "score": score, "line": self.offer_line(t)}

    def recap_line(self, t: dict) -> str:
        """One line to open a resumed talk with."""
        when = when_words(t["updated"], self.clock(), self.svc.tz).removeprefix("on ").removeprefix("back in ")
        if t.get("summary"):
            line = f"Picking up {t['title']} from {when}: {first_sentence(t['summary'])}"
            if t.get("open"):
                line = line.rstrip(".") + f". Still open: {t['open'][0].rstrip('.?')}."
            return line
        said = next((e["text"] for e in reversed(t.get("last_turns") or []) if e.get("role") == "user"), "")
        return f"Picking up {t['title']} from {when}." + (f" Last time you said: “{first_sentence(said, 120)}”" if said else "")

    def offer_line(self, t: dict) -> str:
        """'Sounds like our talk about New PC: last week you were leaning towards the 9800X3D. Still?'"""
        when = when_words(t["updated"], self.clock(), self.svc.tz)
        stance = next((s["text"] for s in t.get("stances") or [] if s.get("kind") in ("leaning", "decision")), "") \
            or next((s["text"] for s in t.get("stances") or []), "")
        if stance:
            body = f"{when} {you_form(stance, self.user)}. Still?"
        elif t.get("summary"):
            body = f"we got into it {when}: {first_sentence(t['summary'], 120).rstrip('.')}."
        else:
            body = f"we talked about it {when}."
        return f"Sounds like our talk about {t['title']}: {body} Say pick that up and I'll carry on from there."

    def opener(self, *, topic: str = "", resume: str | None = None, mode: str = "", already: bool = False) -> str | None:
        """What Vesper opens with (no model call): a recap of a resumed thread, an offer to link an earlier talk, or
        a mode's hello. None: let the model answer (a topic) or use a plain greeting."""
        if resume is not None:
            if self.on_stream:
                return "Our past talks stay off air. Let's start fresh: what's up?"
            if self.off_record:
                return "Off the record, so I'll leave our old talks out of it too. What's on your mind?"
            hit = self.threads.latest() if not resume else (self.threads.match(resume, strict=False) or (None, 0))[0]
            if hit is None:
                if resume:
                    self.topic = resume
                    return f"I don't have an earlier talk about {resume} yet. Let's start one: where are you with it?"
                return "We haven't had a conversation I kept yet. What's on your mind?"
            self.attach(hit)
            self._changed()
            return self.recap_line(hit)
        if topic:
            self.topic = topic
            if not (self.on_stream or self.off_record or self.thread):
                self.link_checked = True
                hit = self.threads.match(topic, strict=True)
                if hit:
                    t = hit[0]
                    self.link_offer = {"id": t["id"], "title": t["title"], "score": hit[1],
                                       "when": when_words(t["updated"], self.clock(), self.svc.tz), "line": self.offer_line(t)}
                    self._changed()
                    return self.link_offer["line"]
            return None
        if mode in MODES:
            return MODES[mode]["switch" if already else "hello"]
        return None

    def prompt_extra(self) -> str:
        """Added to the conversation persona every turn: the style profile, the mode, the thread being continued, an
        offer to link an earlier talk, and what "go on" / "wait, back up" mean."""
        user = self.user
        out = threads_mod.style_prompt(self.threads.style(), user)
        m = MODES.get(self.mode) or MODES["chat"]
        if m["rules"]:
            out += (f"\nMode: {m['label']} (where this clashes with the style above, the mode wins). "
                    + m["rules"].format(user=user, topic=self.topic or "whatever they bring up"))
        if self.thread and not self.on_stream and self.thread.get("resumed"):
            t = self.thread
            out += f"\nYou're continuing an earlier conversation with {user}, \"{t['title']}\"."
            if t.get("summary"):
                out += f" Where it got to: {t['summary']}"
            for label, key in (("Still open", "open"), ("What you promised to come back to", "promises")):
                if t.get(key):
                    out += f" {label}: " + "; ".join(t[key][:4]) + "."
            if t.get("stances"):
                out += f" {user}'s views so far: " + "; ".join(s["text"] for s in t["stances"][:4]) + "."
            if not t.get("summary") and t.get("last_turns"):
                out += " The last things said: " + " | ".join(
                    f"{user if e['role'] == 'user' else 'You'}: {e['text'][:200]}" for e in t["last_turns"][-6:])
            out += " Build on it; don't re-ask what's settled."
        elif self.link_offer and not self.on_stream:
            out += (f"\nThis may continue an earlier talk, \"{self.link_offer['title']}\" ({self.link_offer['when']}). If "
                    f"you haven't yet, ask {user} in a few words whether to pick that up. If they say yes, call "
                    "conversation_link; if not, carry on fresh.")
        last = next((e for e in reversed(self.transcript) if e["role"] == "user"), None)
        d = turn_directive(last["text"]) if last else ""
        if d:
            out += "\n" + DIRECTIVES[d].format(user=user, said=last["text"][:40])
        return out

    def context_note(self) -> str:
        """Outside a conversation, right after a follow-up was asked: so "I went with the 4080" makes sense."""
        n = self.followup_note
        if not n or self.active or self.clock() - n["at"] > float(self.setting("followup_answer_s", 300)):
            return ""
        return (f"[A moment ago you asked {self.user}: \"{n['text']}\" (a follow-up on your earlier talk \"{n['title']}\"). "
                "If this is the answer, respond to it naturally.]")

    def _keep(self, on_stream: bool) -> None:
        """It ended: save it as a thread (in the background: the model writes the notes) unless nothing's to be kept."""
        now = self.clock()
        turns = [dict(e) for e in self.transcript if e["ts"] > self.saved_until]
        if self.transcript:
            self.saved_until = self.transcript[-1]["ts"]
        cfg = self.svc.cfg
        real = [e for e in turns if e["role"] == "user" and len(e["text"].split()) >= 3
                and e["text"] != "(journal entry)" and not match_start(_norm_t(e["text"]), cfg)
                and not match_end(_norm_t(e["text"]), cfg)]
        base = {"ts": now, "mode": self.mode, "thread_id": (self.thread or {}).get("id"),
                "title": (self.thread or {}).get("title") or ""}
        if self.off_record or self.off_record_check():
            self.threads.add_span(self.started, now, None)  # memory never learns from these minutes either
            self.covered = {**base, "state": "off_record"}
            self.thread = None
            return
        if on_stream:
            self.covered = {**base, "state": "on_stream"}
            return
        if len(real) < (1 if self.thread else 2):  # "let's talk" … "that's it": nothing to keep
            self.covered = {**base, "state": "short"} if turns else None
            return
        earlier = self.thread if self.thread and self.thread.get("id") else None
        tid = earlier["id"] if earlier else self.threads.create(
            threads_mod.fallback_title(self.topic, turns), mode=self.mode, started=self.started)
        if earlier is None:
            self.thread = {"id": tid, "title": threads_mod.fallback_title(self.topic, turns), "resumed": False}
        self.threads.add_span(self.started, now, tid)
        self.covered = {**base, "thread_id": tid, "title": self.thread["title"], "state": "saving"}
        mode, user = self.mode, self.user
        a_name = self.svc.cfg["assistant"].get("name") or "Vesper"
        resumed = earlier is not None  # another talk on an existing thread
        think = self.svc.think

        def job() -> None:
            prompt = self.threads.notes_prompt(turns, earlier=self.threads.get(tid) if earlier else None, mode=mode,
                                               user=user, assistant=a_name)
            try:
                notes = think(prompt, schema=threads_mod.THREAD_SCHEMA, max_tokens=1200)
            except Exception:
                log.exception("thread notes failed")
                notes = None
            ok = isinstance(notes, dict) and bool(str(notes.get("summary") or "").strip())
            t = self.threads.write(tid, notes if ok else {}, turns=turns, mode=mode, resumed=resumed, summarised=ok)
            learned = self.threads.learn(tid, turns) if ok else []
            if t is None:
                return
            if ok and mode == "plan" and not self.proposals:
                self.proposals = [{"title": str(x)[:100], "keep": True} for x in notes.get("tasks") or [] if str(x).strip()][:8]
            if self.thread and self.thread.get("id") == tid:
                self.thread.update({"title": t["title"], "saved": True})
            if self.covered and self.covered.get("thread_id") == tid:
                self.covered = {**base, "thread_id": tid, "state": "saved" if ok else "raw", "title": t["title"],
                                "summary": t["summary"], "points": t["points"], "stances": t["stances"],
                                "open": t["open"], "promises": t["promises"], "learned": [f["statement"] for f in learned]}
            self._changed()
        self.threads.run(job, wait=bool(self.setting("save_wait", False)))

    def forget_current(self) -> dict:
        """"Forget this conversation": nothing from it is kept, and if it continued (or was saved as) a thread, that
        thread and everything it taught go too."""
        out = {"ok": True, "title": "", "facts": [], "log": 0}
        tid = (self.thread or {}).get("id")
        if tid:
            out = self.threads.forget(int(tid))
        out["log"] = out.get("log", 0) + self.threads.forget_session(self.started or self.clock(), self.clock())
        self.thread, self.link_offer = None, None
        self.off_record = True  # the rest of this talk isn't kept either
        if self.covered and self.covered.get("thread_id") == tid:
            self.covered = {**self.covered, "state": "forgotten"}
        self._changed()
        return out

    # ---- the HUD --------------------------------------------------------------------------------------
    def status(self) -> dict:
        now = self.clock()
        live = self._live()
        m = MODES.get(self.mode) or MODES["chat"]
        style = self.threads.style()
        try:
            threads = [] if live else [self.threads.card(t) for t in self.threads.list(int(self.setting("hud_threads", 12)))]
            n_threads = self.threads.count()
        except Exception:
            log.exception("threads list failed")
            threads, n_threads = [], 0
        thread = None
        if self.thread and not live:
            thread = {"id": self.thread.get("id"), "title": self.thread.get("title"),
                      "resumed": bool(self.thread.get("resumed")), "open": len(self.thread.get("open") or [])}
        return {"active": self.active, "on_stream": self.on_stream, "started": self.started or None,
                "turns": self.turns, "thinking": bool(self.busy), "idle_s": round(self.idle_for(now), 1) if self.active else None,
                "timeout_s": self.timeout_s, "summarised": bool(self.summary), "ended": self.ended,
                "model": self.brain() is not None, "live": live,
                "transcript": [dict(e) for e in self.transcript[-40:]],
                "mode": self.mode, "mode_label": m["label"], "topic": self.topic,
                "modes": [{"id": k, "label": v["label"]} for k, v in MODES.items()],
                "thread": thread, "off_record": bool(self.off_record),
                "held": bool(self.active and now < self.hold_until),
                "link_offer": None if live or not self.link_offer else {k: self.link_offer[k] for k in ("id", "title", "when")},
                "covered": None if live else self.covered, "proposals": [] if live else list(self.proposals),
                "threads": threads, "thread_count": n_threads,
                "style": {**{k: style[k] for k in threads_mod.STYLE_DEFAULT}, "words": threads_mod.style_words(style),
                          "labels": threads_mod.STYLE_LABELS, "default": threads_mod.STYLE_DEFAULT,
                          "changed": style["changed"][-3:]}}

    def _changed(self) -> None:
        data = self.status()
        bus = getattr(self.svc, "bus", None)
        if bus is not None:
            bus.publish("conversation", data, sticky=True)
        for fn in list(self.on_change):
            try:
                fn(data)
            except Exception:
                log.exception("conversation listener failed")

    def _live(self) -> bool:
        try:
            return bool(self.svc.live and self.svc.live())
        except Exception:
            return False


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"
