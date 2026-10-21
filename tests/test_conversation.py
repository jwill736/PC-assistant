"""Conversation mode: "let's talk", no wake word between turns, a persona with a longer memory, barge-in, and the
quick follow-up window after any reply (brain/conversation.py, skills/conversation.py, voice/listener.py)."""

import threading
import time

import numpy as np
import pytest
from conftest import FakeClient, text_block, tool_block

from assistant.brain import conversation as convo_mod
from assistant.brain.assistant import Assistant
from assistant.brain.router import RouterContext, clean, route
from assistant.voice.listener import VoiceListener

AUDIO = np.zeros(16000 * 2, dtype=np.int16)


def answers(*texts):
    return [([text_block(t)], "end_turn") for t in texts]


def chat(svc, script=()):
    """An assistant on a scripted Claude, with the conversation skill wired the way the runtime does it."""
    a = Assistant(svc, client=FakeClient(list(script)))
    return a


def ctx(svc):
    return RouterContext(skill_route=svc.skills.route)


# ---- the phrases -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("said, on_stream, topic", [
    ("let's talk", False, None), ("Let's chat.", False, None), ("talk to me", False, None),
    ("Friday, let's talk", False, None), ("hey friday let's have a chat", False, None), ("can we talk", False, None),
    ("I want to talk for a bit", False, None), ("conversation mode", False, None), ("start a conversation", False, None),
    ("let's talk on stream", True, None), ("let's chat on air", True, None),
    ("let's talk about my week", False, "my week"),
])
def test_start_phrases(svc, said, on_stream, topic):
    chat(svc)
    intent = route(said, ctx(svc))
    assert intent is not None and intent.kind == "conversation"
    assert intent.args["on_stream"] is on_stream and intent.args.get("topic") == topic


@pytest.mark.parametrize("said", ["talk to dana about the invoice", "let's talk to the team", "I talked to Sam",
                                  "chat is wild today", "open chat"])
def test_not_start_phrases(svc, said):
    chat(svc)
    intent = route(said, ctx(svc))
    assert intent is None or intent.kind != "conversation"


@pytest.mark.parametrize("said", ["that's all", "That's it, thanks.", "thanks Friday, that's it", "stop talking",
                                  "end the conversation", "okay that's all for now", "bye friday", "talk later",
                                  "we're done", "exit conversation mode"])
def test_end_phrases_only_while_talking(svc, said):
    a = chat(svc)
    intent = route(said, ctx(svc))
    assert intent is None or intent.kind != "conversation_end"  # nothing to end
    a.convo.brain = lambda: "claude"
    a.convo.begin()
    assert route(said, ctx(svc)).kind == "conversation_end"


def test_end_phrase_matcher_is_anchored(cfg):
    assert not convo_mod.match_end(clean("that's all I wanted to say about the budget"), cfg)
    assert not convo_mod.match_end(clean("is that it"), cfg)
    assert convo_mod.match_end(clean("Alright, thank you Friday, that's all"), cfg)


# ---- the brain -------------------------------------------------------------------------------------------

def test_no_model_says_so_once_and_stays_on_commands(svc):
    a = Assistant(svc)  # no Claude, no local model
    out = a.handle("let's talk")
    assert out["reply"] == convo_mod.NO_MODEL and "Setup" in out["reply"] and "Brain" in out["reply"]
    assert not a.convo.active
    assert a.handle("add task Ship it")["reply"] == "Added: Ship it."  # commands still work


def test_model_lost_mid_conversation_says_so_once(svc):
    a = chat(svc, answers("Sure."))
    a.handle("let's talk")
    a.client = None  # the model went away
    out = a.handle("what do you think about pineapple on pizza")
    assert out["reply"] == convo_mod.LOST_MODEL and not a.convo.active
    assert a.convo.ended["reason"] == "no model"
    assert "ANTHROPIC_API_KEY" in a.handle("what do you think about olives")["reply"]  # the usual offline answer


def test_a_conversation_uses_the_persona_and_remembers(svc):
    a = chat(svc, answers("Bold. I'm for it, mostly because it annoys people. You?", "Ha. Fair enough."))
    out = a.handle("Let's talk")
    assert a.convo.active and out["kind"] == "conversation" and out["reply"] in convo_mod.GREETINGS
    assert a.handle("what do you think about pineapple on pizza")["reply"].startswith("Bold.")
    a.handle("I hate it")
    calls = a.client.messages.calls
    system = calls[-1]["system"][0]["text"]
    assert "Never sycophantic" in system and "1-3 short sentences" in system
    assert "pineapple" in str(calls[-1]["messages"])  # the earlier turn is still there
    turns = [m["role"] for m in a.convo.status()["transcript"]]
    assert turns == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert a.handle("thanks friday, that's it")["reply"] == convo_mod.GOODBYE
    assert not a.convo.active and a.convo.ended["reason"] == "said"


def test_commands_stay_instant_in_a_conversation(svc):
    a = chat(svc, [])
    a.handle("let's talk")
    out = a.handle("add task Call the bank")
    assert out["kind"] == "tool" and a.client.messages.calls == []  # the fast path, no model call
    assert a.convo.active


def test_yes_and_no_answer_questions_but_cancel_still_cancels(svc):
    a = chat(svc, answers("Then you should go.", "Okay, then don't."))
    a.handle("let's talk")
    assert a.handle("yeah")["reply"] == "Then you should go."  # nothing waiting on a yes: it's an answer
    assert a.handle("nope")["reply"] == "Okay, then don't."
    a.handle("go live")  # a T2 action parks for a yes
    assert a.pending is not None
    assert a.handle("cancel")["reply"] == "Cancelled."


def test_command_mode_history_is_unchanged_outside_a_conversation(svc):
    a = chat(svc, answers("Done."))
    a.handle("what's a good name for a cat")
    assert "Never sycophantic" not in a.client.messages.calls[0]["system"][0]["text"]


def test_long_conversations_are_summarised(svc):
    svc.cfg["conversation"] = {"summarize_after": 6, "keep_turns": 3}
    a = chat(svc, answers(*[f"Reply {i}." for i in range(12)]))
    prompts = []

    def think(prompt, schema=None, **kw):
        prompts.append(prompt)
        return "J is planning a trip to Lisbon in May and wants cheap flights."
    a.think = think
    a.handle("let's talk")
    for i in range(8):
        a.handle(f"thought number {i} about the Lisbon trip")
    a.convo.wait_summary()
    assert prompts and "thought number 0" in prompts[0] and "J:" in prompts[0]
    assert a.convo.summary.startswith("J is planning a trip")
    user_turns = [m for m in a.history if m["role"] == "user" and isinstance(m["content"], str)]
    assert len(user_turns) <= 6  # folded down to the last few, then grows again
    a.handle("so what do you think")
    system = a.client.messages.calls[-1]["system"][0]["text"]
    assert "Earlier in this conversation (summary): J is planning a trip" in system
    assert a.convo.status()["summarised"] is True


def test_history_is_longer_while_talking(svc):
    a = chat(svc)
    assert a._history_limits(False) == (10, 12)
    assert a._history_limits(True) == (30, 40)
    svc.cfg["conversation"] = {"history_turns": 60}
    assert a._history_limits(True)[1] == 60


def test_without_a_model_old_turns_just_drop(svc):
    svc.cfg["conversation"] = {"summarize_after": 4, "keep_turns": 2}
    a = chat(svc, answers(*["Mm." for _ in range(8)]))
    a.think = lambda *x, **k: None
    a.handle("let's talk")
    for i in range(6):
        a.handle(f"line {i} of my rant")
    a.convo.wait_summary()
    assert a.convo.summary == ""
    assert sum(1 for m in a.history if m["role"] == "user" and isinstance(m["content"], str)) <= 4


def test_talking_over_a_reply_is_mentioned_to_the_model(svc):
    a = chat(svc, answers("Long answer.", "Okay, mushrooms."))
    a.handle("let's talk")
    a.handle("tell me about pizza")
    svc.bus.publish("interrupted", {"text": "Long answer."})
    a.handle("actually tell me about mushrooms")
    last_user = a.client.messages.calls[-1]["messages"][-1]["content"]
    assert "talked over your last reply" in last_user


def test_tools_still_work_mid_conversation(svc):
    a = chat(svc, [([text_block("Adding it."), tool_block("add_task", {"title": "Buy flights"})], "tool_use"),
                   ([text_block("Added. Anything else on the trip?")], "end_turn")])
    a.handle("let's talk")
    out = a.handle("make sure I don't forget the flights")
    assert out["reply"].startswith("Added.")
    assert svc.storage.list_tasks()[0]["title"] == "Buy flights"


def test_the_model_can_end_it(svc):
    a = chat(svc, [([tool_block("end_conversation", {})], "tool_use"), ([text_block("Later!")], "end_turn")])
    a.handle("let's talk")
    a.handle("gotta run, the pizza's here")
    assert not a.convo.active


def test_let_us_talk_about_a_topic_starts_and_answers(svc):
    a = chat(svc, answers("Lisbon in May is lovely. Budget or splurge?"))
    out = a.handle("let's talk about my Lisbon trip")
    assert a.convo.active and out["reply"].startswith("Lisbon")
    assert "Let's talk about my lisbon trip" in str(a.client.messages.calls[0]["messages"])


def test_kill_switch_ends_it(svc):
    a = chat(svc)
    a.handle("let's talk")
    a.stop_everything("hotkey")
    assert not a.convo.active and a.convo.ended["reason"] == "stopped"


# ---- on air ------------------------------------------------------------------------------------------------

def test_wont_start_while_live_unless_on_stream(svc):
    svc.live = lambda: True
    a = chat(svc, answers("Hi chat!"))
    assert a.handle("let's talk")["reply"] == convo_mod.LIVE_REFUSE and not a.convo.active
    assert a.handle("let's talk on stream")["reply"] == convo_mod.ON_STREAM_GREETING
    assert a.convo.active and a.convo.on_stream
    calls = []
    svc.memory.context_for = lambda *x, **k: calls.append(x) or "J's sister is called Dana."
    a.handle("say hi to everyone")
    system = a.client.messages.calls[-1]["system"][0]["text"]
    assert "live on stream" in system and "Never mention anything private" in system
    assert "Dana" not in str(a.client.messages.calls[-1]["messages"]) and calls == []  # nothing remembered on air


def test_going_live_ends_it(svc):
    live = [False]
    svc.live = lambda: live[0]
    a = chat(svc)
    a.handle("let's talk")
    live[0] = True
    assert a.convo.is_active() is False and a.convo.ended["reason"] == "live"


def test_on_stream_conversation_survives_going_live(svc):
    live = [False]
    svc.live = lambda: live[0]
    a = chat(svc)
    a.handle("let's chat on stream")
    live[0] = True
    assert a.convo.is_active()


# ---- the quiet minute --------------------------------------------------------------------------------------

class Speaker:
    """Speaks instantly: what's said, and when it finished (for the listener's windows)."""

    def __init__(self):
        self.speaking = threading.Event()
        self.last_text, self.last_end, self.expects_reply = "", 0.0, False
        self.said, self.interrupts = [], 0

    def say(self, text, expects_reply=None, turn=None):
        self.said.append(text)
        self.last_text = text
        self.expects_reply = text.rstrip().endswith("?") if expects_reply is None else expects_reply
        self.last_end = time.time()

    def chime(self):
        pass

    def interrupt(self, silence_turn=True):
        self.interrupts += 1
        self.speaking.clear()
        self.last_end = time.time()
        return True


def test_a_quiet_minute_ends_it_but_not_while_vesper_talks(svc):
    a = chat(svc)
    sp = Speaker()
    a.convo.speaker = sp
    t = [1000.0]
    a.convo.clock = lambda: t[0]
    a.handle("let's talk")
    t[0] += 59
    assert a.convo.is_active()
    sp.speaking.set()  # a long reply: never "quiet"
    t[0] += 120
    assert a.convo.is_active()
    sp.speaking.clear()
    assert a.convo.check() == "timeout" and not a.convo.active


# ---- memory: J's words feed the learning loop --------------------------------------------------------------

def test_conversation_turns_feed_memory_with_js_words_only(svc):
    a = chat(svc, answers("Sounds like a great plan, Lisbon is lovely in spring."))
    a.handle("let's talk", source="voice")
    a.handle("I'm going to Lisbon with my sister Dana in May", source="voice")
    rows = svc.storage.query("SELECT role, source, text FROM log ORDER BY id")
    assert ("user", "voice", "I'm going to Lisbon with my sister Dana in May") in [(r["role"], r["source"], r["text"]) for r in rows]
    prompts = []
    svc.think = lambda prompt, schema=None, **kw: prompts.append(prompt) or {"facts": []}
    svc.skills.get("memory").learn_conversations(time.time() + 600)
    assert prompts and "Lisbon with my sister Dana" in prompts[0] and "lovely in spring" not in prompts[0]


# ---- the voice side: a whole conversation through the listener ------------------------------------------------

class STT:
    name = "parakeet"

    def __init__(self):
        self.text = ""

    def transcribe(self, _samples, hints=()):
        return self.text


class Verifier:
    active = True

    def __init__(self):
        self.accept = True

    def check(self, _samples):
        return self.accept, (0.8 if self.accept else 0.1)


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, type_, data, sticky=False):
        self.events.append((type_, data))

    def of(self, kind):
        return [d for t, d in self.events if t == kind]


def voice_rig(svc, script, verifier=None):
    """Assistant + listener + speaker, wired like runtime + skills/conversation.py; commands run synchronously."""
    a = chat(svc, script)
    sp = Speaker()
    svc.speak = sp.say
    a.convo.speaker = sp
    lst = VoiceListener(Bus(), sp, ["friday", "hey friday"], lambda text: a.handle(text, "voice"),
                        {"follow_up_seconds": 8}, verifier=verifier)
    lst.stt = STT()
    lst.state = "listening"  # the mic loop is running
    svc.skills.get("conversation").start(type("RT", (), {"speaker": sp, "listener": lst, "assistant": a})())
    return a, lst, sp


def say(lst, text, during=False):
    lst.stt.text = text
    lst._handle_audio(AUDIO, started_at=time.time(), during_speech=during)


def test_voice_conversation_needs_no_wake_word_after_it_starts(svc):
    v = Verifier()
    a, lst, sp = voice_rig(svc, answers("Mostly yes. Sweet and salty works.", "Mushrooms are underrated.",
                                        "Fine, olives. Briny little things."), verifier=v)
    say(lst, "so what are we doing tonight")  # before: no name, no window -> ignored
    assert not a.convo.active and a.client.messages.calls == []
    say(lst, "Friday, let's talk")
    assert a.convo.active and sp.said[-1] in convo_mod.GREETINGS
    say(lst, "do you like pineapple on pizza")  # no name needed now
    assert sp.said[-1].startswith("Mostly yes")
    assert lst._arm_source() == "conversation"
    assert lst.bus.of("voice_state")[-1]["state"] == "armed"  # the orb stays listening between turns

    v.accept = False  # someone else in the room
    say(lst, "can you pass me the remote")
    assert lst.bus.of("heard")[-1]["ignored"] == "voice not recognised" and len(a.client.messages.calls) == 1
    v.accept = True

    say(lst, "hmm")  # a noise, not a turn
    assert lst.bus.of("heard")[-1]["ignored"] == "not a turn"

    sp.speaking.set()  # Vesper is mid-reply and J talks over it: it stops, and that's the next turn
    sp.last_text = "Here's the thing about pizza, it is a canvas for"
    say(lst, "what about mushrooms though", during=True)
    assert sp.interrupts == 1 and sp.said[-1] == "Mushrooms are underrated."

    sp.speaking.set()
    sp.last_text = "Mushrooms are underrated and honestly"
    say(lst, "stop", during=True)  # just stop the reply; still talking
    assert sp.interrupts == 2 and a.convo.active
    say(lst, "and olives")
    assert sp.said[-1].startswith("Fine, olives")

    sp.speaking.set()
    sp.last_text = "Fine, olives. Briny little things."
    say(lst, "stop talking", during=True)  # over a reply: stop it and end the conversation
    assert sp.interrupts == 3 and not a.convo.active and a.convo.ended["reason"] == "said"
    assert lst._arm_source() != "conversation"


def test_barge_in_without_a_voice_profile_needs_a_real_sentence(svc):
    a, lst, sp = voice_rig(svc, answers("Sure.", "Got it, the blue one."))
    say(lst, "friday let's talk")
    say(lst, "tell me something")
    sp.speaking.set()
    sp.last_text = "Here is a long answer about the red one and the blue one"
    say(lst, "nah blue", during=True)  # too short to tell from the reply coming back through the speakers
    assert sp.interrupts == 0 and lst.bus.of("heard")[-1]["ignored"] == "while speaking"
    say(lst, "no I meant the blue one please", during=True)
    assert sp.interrupts == 1 and sp.said[-1] == "Got it, the blue one."


def test_own_voice_is_never_a_turn(svc):
    a, lst, sp = voice_rig(svc, answers("Pineapple belongs on pizza."))
    say(lst, "friday let's talk")
    say(lst, "what about pineapple")
    calls = len(a.client.messages.calls)
    say(lst, "Pineapple belongs on pizza.")  # the speakers, picked up by the mic
    assert lst.bus.of("heard")[-1]["ignored"] == "own voice" and len(a.client.messages.calls) == calls


def test_a_quiet_minute_through_the_listener(svc):
    a, lst, sp = voice_rig(svc, answers("Hi."))
    say(lst, "friday let's talk")
    a.convo.clock = lambda: time.time() + 61
    sp.last_end = time.time() - 61
    say(lst, "and another thing")  # a minute later, no name: the conversation is over and so is the window
    assert not a.convo.active and a.convo.ended["reason"] == "timeout"
    assert a.client.messages.calls == []


def test_going_live_ends_it_through_the_listener(svc):
    live = [False]
    svc.live = lambda: live[0]
    a, lst, sp = voice_rig(svc, answers("Hi."))
    say(lst, "friday let's talk")
    live[0] = True
    sp.last_end = 0  # (its greeting asked a question: that window has passed too)
    lst.refresh_state()
    assert not a.convo.active and a.convo.ended["reason"] == "live"
    assert lst.bus.of("voice_state")[-1]["state"] == "listening"
    say(lst, "chat that was insane")  # talking to chat: not for Vesper
    assert a.client.messages.calls == []


# ---- the follow-up window after any reply --------------------------------------------------------------------

def plain_listener(**cfg):
    commands = []
    sp = Speaker()
    lst = VoiceListener(Bus(), sp, ["friday", "hey friday"], commands.append, {"follow_up_seconds": 8, **cfg})
    lst.stt = STT()
    return lst, sp, commands


def replied(lst, sp, ago=1.0, text="It's sunny, 22 degrees."):
    lst.last_command = time.time() - ago - 2
    sp.say(text)
    sp.last_end = time.time() - ago


def test_after_any_reply_you_can_just_keep_talking(svc):
    lst, sp, commands = plain_listener()
    replied(lst, sp)
    say(lst, "and tomorrow")
    assert commands == ["and tomorrow"]
    assert lst.bus.of("heard")[-1]["armed"] is True


def test_the_window_closes_early_on_silence(svc):
    lst, sp, commands = plain_listener()
    replied(lst, sp, ago=5)  # 5 s of quiet: closed (4 s), though the window is 7
    say(lst, "and tomorrow")
    assert commands == []
    lst.last_voice_end = time.time() - 2  # but you were talking 2 s ago: still open
    say(lst, "and tomorrow")
    assert commands == ["and tomorrow"]
    replied(lst, sp, ago=7.5)
    lst.last_voice_end = time.time() - 1
    say(lst, "why")  # past the whole window
    assert commands == ["and tomorrow"]


def test_the_window_ignores_background_reactions_and_chatty_rooms(svc):
    lst, sp, commands = plain_listener()
    replied(lst, sp)
    say(lst, "lol")
    assert commands == [] and lst.bus.of("heard")[-1]["ignored"] == "not a turn"
    for _ in range(2):
        lst.unaddressed.append(time.time() - 3)  # people talking around the PC
    say(lst, "and tomorrow")
    assert commands == []


def test_no_window_after_something_said_unprompted_or_while_live(svc):
    lst, sp, commands = plain_listener()
    lst.last_command = time.time() - 600  # a reminder spoken 10 minutes after your last command
    sp.say("Your meeting starts in 5 minutes.")
    say(lst, "thanks")
    assert commands == []
    replied(lst, sp)
    lst.is_live = lambda: True  # on air you talk to chat all the time
    say(lst, "and tomorrow")
    assert commands == []
    lst.cfg["follow_up_live"] = True
    lst.unaddressed.clear()  # (what it ignored above counts as a chatty room)
    say(lst, "and tomorrow")
    assert commands == ["and tomorrow"]


def test_question_window_still_takes_short_answers(svc):
    lst, sp, commands = plain_listener()
    replied(lst, sp, ago=6, text="Want me to close Steam?")
    say(lst, "yeah")
    assert commands == ["yeah"]


def test_the_orb_shows_the_window(svc):
    lst, sp, commands = plain_listener()
    lst.state = "listening"
    replied(lst, sp)
    lst.refresh_state()
    assert lst.bus.of("voice_state")[-1] == {"state": "armed", "error": "", "window": "next"}
    sp.last_end = time.time() - 5
    lst.refresh_state()
    assert lst.bus.of("voice_state")[-1]["state"] == "listening"


def test_window_can_be_turned_off(svc):
    lst, sp, commands = plain_listener(follow_up_any_seconds=0)
    replied(lst, sp)
    say(lst, "and tomorrow")
    assert commands == []


# ---- the skill: HUD, "how was your day" ------------------------------------------------------------------------

def test_hud_start_and_end(svc):
    a = chat(svc)
    skill = svc.skills.get("conversation")
    spoken = []
    svc.speak = lambda text, **kw: spoken.append(text)
    skill.start(type("RT", (), {"speaker": None, "listener": object.__new__(VoiceListener), "assistant": a})())
    a.convo.on_change.clear()  # that bare listener has no state to refresh
    out = svc.skills.action("conversation", "start", {})
    assert out["ok"] and a.convo.active and spoken == [out["reply"]]
    st = svc.bus.latest["conversation"]["data"]
    assert st["active"] and st["transcript"][-1] == {**st["transcript"][-1], "role": "assistant", "text": out["reply"]}
    assert svc.skills.action("conversation", "end", {})["ended"] is True
    assert not a.convo.active and a.convo.ended["reason"] == "hud"


def test_hud_start_without_a_model(svc):
    Assistant(svc)
    skill = svc.skills.get("conversation")
    skill.start(type("RT", (), {"speaker": None, "listener": None, "assistant": None})())  # the bare-state path
    out = svc.skills.action("conversation", "start", {})
    assert out["ok"] is False and out["error"] == convo_mod.NO_MODEL
    assert skill.status()["model"] is False


def test_tick_ends_it_when_live(svc):
    live = [False]
    svc.live = lambda: live[0]
    a = chat(svc)
    a.handle("let's talk")
    live[0] = True
    svc.skills.get("conversation").tick()
    assert not a.convo.active and svc.bus.latest["conversation"]["data"]["ended"]["reason"] == "live"


def test_how_was_your_day(svc):
    a = chat(svc)
    nudges = svc.skills.get("nudges")
    nudges.add("Your 3 pm with Sam starts in 10 minutes.", source="calendar", key="cal1")
    nudges.deliver(at_break=True)
    svc.storage.add_job("research", "Lisbon flights", "find cheap flights", None)
    svc.memory.add("J prefers aisle seats.", importance=4, source="said")
    a.handle("add task Pack")
    out = a.handle("how was your day")
    assert out["kind"] == "tool" and out["tool"] == "conversation_my_day"
    data = out["data"]
    assert data["requests"] >= 2 and data["jobs"][0]["title"] == "Lisbon flights"
    assert "J prefers aisle seats." in data["learned"]
    assert "background job" in out["reply"] and "aisle seats" in out["reply"]


def test_how_was_your_day_on_air_gives_counts_only(svc):
    a = chat(svc)
    svc.memory.add("J's sister Dana lives in Leeds.", importance=4, source="said")
    svc.live = lambda: True
    out = a.handle("tell me about your day")
    assert "Leeds" not in out["reply"] and "off air" in out["reply"]
    assert isinstance(out["data"]["learned"], int)


def test_quiet_day(svc):
    chat(svc)
    skill = svc.skills.get("conversation")
    assert "Quiet so far" in skill.say("conversation_my_day", {}, {"ok": True, "requests": 0, "brought_up": [],
                                                                    "jobs": [], "learned": []})


def test_status_without_an_assistant(svc):
    skill = svc.skills.get("conversation")
    assert skill.status() == {"active": False, "available": False, "transcript": []}
    assert skill.route("let's talk") is None
    assert svc.skills.action("conversation", "start", {})["ok"] is False


# ---- the local model ---------------------------------------------------------------------------------------

class LocalLLM:
    can_act, label, model = True, "Llama", "llama3.1:8b"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, tools=None, on_text=None, temperature=0.3, should_stop=None, **kw):
        self.calls.append({"messages": [dict(m) for m in messages], "temperature": temperature, **kw})
        return {"content": self.replies.pop(0) if self.replies else "Okay.", "tool_calls": []}


class LocalBrain:
    ready, streaming = True, False

    def __init__(self, llm):
        self.llm = llm

    def refresh(self, force=False):
        return self.llm

    def status(self):
        return {"ready": True}


def test_local_model_conversation_is_warmer_and_remembers(svc):
    a = Assistant(svc)
    llm = LocalLLM(["Honestly? Ship it Friday.", "Because Monday you'll second-guess it."])
    a.local = LocalBrain(llm)
    assert a.handle("let's talk")["kind"] == "conversation"
    a.handle("should I ship the HUD on Friday or Monday")
    a.handle("why")
    last = llm.calls[-1]
    assert last["temperature"] == 0.7
    assert "Never sycophantic" in last["messages"][0]["content"]
    assert "ship the HUD on Friday" in str(last["messages"][1:])
    a.handle("that's all")
    a.handle("what's the capital of peru")
    assert llm.calls[-1]["temperature"] == 0.3 and "Never sycophantic" not in llm.calls[-1]["messages"][0]["content"]
