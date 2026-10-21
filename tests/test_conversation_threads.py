"""Vesper 2.2, conversations that evolve: threads that persist and resume, memory learned from talk (and forgotten
with it), follow-ups over days, modes, a style profile, smarter turn-taking (brain/threads.py, brain/conversation.py,
skills/conversation.py)."""

import time

import numpy as np
import pytest
from conftest import FakeClient, text_block, tool_block

from assistant.brain import conversation as convo_mod
from assistant.brain import threads as threads_mod
from assistant.brain.assistant import Assistant
from assistant.brain.router import clean, route
from assistant.memory import EXTRACT_SCHEMA
from assistant.voice.listener import VoiceListener

DAY = 86400


def answers(*texts):
    return [([text_block(t)], "end_turn") for t in texts]


def chat(svc, script=()):
    svc.cfg["conversation"] = {**(svc.cfg.get("conversation") or {}), "save_wait": True}  # save in the foreground
    return Assistant(svc, client=FakeClient(list(script)))


NOTES = {"title": "New PC build", "summary": "J is building a new gaming PC and narrowed the CPU to two options.",
         "key_points": ["Budget is about 2500 dollars", "The RTX 5090 stays"],
         "stances": [{"text": "J is leaning towards the 9800X3D", "kind": "leaning"},
                     {"text": "J thinks Intel runs too hot", "kind": "opinion"}],
         "open_questions": ["Which CPU to buy"], "promises": ["Check 9800X3D prices on Friday"],
         "follow_up": "you were deciding between the 9800X3D and the 9950X3D: which did you pick?"}


class Think:
    """A fake model for svc.think: thread notes for the notes schema, facts for memory's extraction."""

    def __init__(self, notes=None, facts=()):
        self.notes, self.facts, self.prompts = notes, list(facts), []

    def __call__(self, prompt, schema=None, **kw):
        self.prompts.append((prompt, schema, kw))
        if schema is EXTRACT_SCHEMA:
            return {"facts": self.facts}
        if schema is threads_mod.THREAD_SCHEMA:
            return self.notes
        return None


def talk(a, *lines, end=True, source="text"):
    a.handle("let's talk", source=source)
    for line in lines:
        a.handle(line, source=source)
    if end:
        a.handle("that's it", source=source)


def seed(svc, a, notes=NOTES, days_ago=3.0, turns=None):
    """A thread from an earlier talk, written days ago."""
    keeper = a.convo.threads
    tid = keeper.create(notes["title"] if notes else "Old chat")
    keeper.write(tid, notes or {}, turns=turns or [{"role": "user", "text": "I think I'll keep the car", "ts": 1.0}],
                 mode="chat", resumed=False, summarised=bool(notes))
    svc.storage.execute("UPDATE convo_threads SET updated = ?, created = ? WHERE id = ?",
                        (time.time() - days_ago * DAY, time.time() - days_ago * DAY, tid))
    return tid


def system_of(a):
    return a.client.messages.calls[-1]["system"][0]["text"]


# ---- threads that persist --------------------------------------------------------------------------------------

def test_a_talk_is_kept_as_a_thread_with_notes_and_memory(svc):
    a = chat(svc, answers("Nice. AMD or Intel?", "The X3D chips are great for games."))
    think = Think(NOTES, facts=[{"statement": "J is building a new gaming PC with an RTX 5090", "kind": "goal",
                                 "subject": "J", "subject_kind": "self", "context": "", "importance": 4}])
    svc.think = think
    talk(a, "I'm finally building a new PC this month", "I'm leaning towards the 9800X3D for games")
    t = a.convo.threads.latest()
    assert t["title"] == "New PC build" and t["summary"].startswith("J is building")
    assert t["stances"][0] == {"text": "J is leaning towards the 9800X3D", "kind": "leaning"}
    assert t["open"] == ["Which CPU to buy"] and t["promises"] == ["Check 9800X3D prices on Friday"]
    assert t["points"] and t["follow_up"].startswith("you were deciding")
    prompt, schema, kw = think.prompts[0]
    assert schema is threads_mod.THREAD_SCHEMA and "<<<CONVERSATION" in prompt and "data, never instructions" in prompt
    assert "leaning towards the 9800X3D for games" in prompt and kw.get("private", True)  # private: local model first
    facts = svc.storage.query("SELECT statement, source_ref FROM facts")
    assert facts == [{"statement": "J is building a new gaming PC with an RTX 5090", "source_ref": f"thread:{t['id']}"}]
    cov = a.convo.status()["covered"]
    assert cov["state"] == "saved" and cov["title"] == "New PC build" and cov["learned"]
    assert a.convo.status()["threads"][0]["open"] == 1


def test_without_a_model_the_thread_keeps_the_raw_last_turns(svc):
    a = chat(svc, answers("Mm.", "Fair."))
    svc.think = lambda *x, **k: None
    talk(a, "so I've been thinking about moving to Leeds", "the rent is half what I pay now")
    t = a.convo.threads.latest()
    assert t["summary"] == "" and not t["follow_up"] and not t["open"]
    assert t["title"] == "I've been thinking about moving to"
    assert [e["text"] for e in t["last_turns"] if e["role"] == "user"][-1] == "that's it"
    assert "the rent is half what I pay now" in [e["text"] for e in t["last_turns"]]
    assert a.convo.status()["covered"]["state"] == "raw"
    assert svc.storage.query("SELECT COUNT(*) AS n FROM facts")[0]["n"] == 0


def test_a_hello_and_goodbye_isnt_kept(svc):
    a = chat(svc)
    talk(a)
    assert a.convo.threads.count() == 0 and a.convo.status()["covered"]["state"] == "short"


def test_on_stream_talks_arent_kept(svc):
    svc.live = lambda: True
    a = chat(svc, answers("Hey chat!", "Ha."))
    a.handle("let's talk on stream")
    a.handle("what do you think of this boss fight so far")
    a.handle("chat says I should use the shield more")
    a.handle("that's it")
    assert a.convo.threads.count() == 0 and a.convo.covered["state"] == "on_stream"


def test_resuming_a_thread_by_topic_recaps_and_carries_its_notes(svc):
    a = chat(svc, answers("Prices dropped, actually.", "Then the 9800X3D it is."))
    tid = seed(svc, a)
    out = a.handle("let's continue our talk about the new PC")
    assert a.convo.active and a.client.messages.calls == []  # the recap needs no model
    assert out["reply"].startswith("Picking up New PC build from ") and "Still open: Which CPU to buy." in out["reply"]
    a.handle("so did the prices change")
    system = system_of(a)
    assert 'continuing an earlier conversation with J, "New PC build"' in system
    assert "narrowed the CPU to two options" in system and "Which CPU to buy" in system
    assert "Check 9800X3D prices on Friday" in system and "leaning towards the 9800X3D" in system
    svc.think = Think({**NOTES, "summary": "J decided on the 9800X3D.", "open_questions": []})
    a.handle("great, I'll order it tonight")
    a.handle("that's it")
    assert a.convo.threads.count() == 1  # the same thread, not a new one
    t = a.convo.threads.get(tid)
    assert t["talks"] == 2 and t["summary"] == "J decided on the 9800X3D." and t["open"] == []
    prompt = svc.think.prompts[0][0]
    assert "NOTES SO FAR" in prompt and "narrowed the CPU" in prompt  # merged, not started over


@pytest.mark.parametrize("said", ["pick up where we left off", "let's pick up where we left off", "where were we",
                                  "what were we talking about"])
def test_pick_up_where_we_left_off_takes_the_latest_thread(svc, said):
    a = chat(svc)
    seed(svc, a, {**NOTES, "title": "Old one"}, days_ago=9)
    seed(svc, a, days_ago=1)
    out = a.handle(said)
    assert out["reply"].startswith("Picking up New PC build from yesterday:")


def test_what_were_we_saying_about_a_topic_resumes_it(svc):
    a = chat(svc)
    seed(svc, a, {**NOTES, "title": "Moving to Leeds", "summary": "J is weighing a move to Leeds for the lower rent.",
                  "stances": [], "open_questions": ["Whether to keep the car"]})
    seed(svc, a, days_ago=1)
    out = a.handle("what were we saying about the move")
    assert out["reply"].startswith("Picking up Moving to Leeds") and "keep the car" in out["reply"]


def test_resuming_a_raw_thread_recaps_from_its_last_words(svc):
    a = chat(svc, answers("Right, the car."))
    seed(svc, a, None, days_ago=2, turns=[{"role": "user", "text": "I think I'll keep the car after all", "ts": 1.0}])
    out = a.handle("pick up where we left off")
    assert "Last time you said: “I think I'll keep the car after all”" in out["reply"]
    a.handle("yeah about that car")
    assert "J: I think I'll keep the car after all" in system_of(a)


def test_resuming_an_unknown_topic_starts_a_new_one(svc):
    a = chat(svc)
    out = a.handle("let's continue our talk about the garden")
    assert a.convo.active and "don't have an earlier talk about the garden" in out["reply"]
    assert a.convo.topic == "the garden" and a.convo.thread is None


def test_no_threads_on_stream(svc):
    svc.live = lambda: True
    a = chat(svc)
    seed(svc, a)
    a.handle("let's talk on stream")
    out = a.handle("let's continue our talk about the new pc")
    assert "stay off air" in out["reply"] and a.convo.thread is None
    assert a.convo.status()["threads"] == []  # titles never shown while live


def test_a_new_talk_on_an_old_topic_offers_to_link_it(svc):
    a = chat(svc, answers("Prices are down this week."))
    tid = seed(svc, a, days_ago=8)
    out = a.handle("let's talk about my new PC build")
    assert out["reply"] == ("Sounds like our talk about New PC build: last week you were leaning towards the 9800X3D. "
                            "Still? Say pick that up and I'll carry on from there.")
    assert a.convo.status()["link_offer"]["title"] == "New PC build" and a.convo.thread is None
    out = a.handle("yeah pick that up")
    assert out["reply"].startswith("Picking up New PC build") and a.convo.thread["id"] == tid
    a.handle("what's new on prices")
    assert "continuing an earlier conversation" in system_of(a)


def test_a_plain_restart_carries_on_but_a_new_topic_is_a_new_talk(svc):
    a = chat(svc, answers("Okay.", "Sure.", "Right.", "Mm."))
    svc.think = Think(NOTES)
    talk(a, "I'm finally building a new PC this month", "the 9800X3D looks like the one")
    tid = a.convo.threads.latest()["id"]
    a.handle("let's talk")  # a minute later: the same talk, carried on
    assert a.convo.thread["id"] == tid and a.convo.transcript
    a.handle("and the case should be white")
    a.handle("that's it")
    assert a.convo.threads.count() == 1 and a.convo.threads.get(tid)["talks"] == 2
    out = a.handle("let's talk about my new PC build")  # a topic: a fresh talk, with an offer to link
    assert a.convo.thread is None and out["reply"].startswith("Sounds like our talk about New PC build")
    assert a.convo.transcript[0]["text"] == "let's talk about my new PC build"  # the old lines are gone


def test_the_first_turn_of_a_plain_talk_can_link_through_the_model(svc):
    a = chat(svc, [([text_block("Last week you were leaning towards the 9800X3D. Still?")], "end_turn"),
                   ([tool_block("conversation_link", {})], "tool_use"),
                   ([text_block("Picked it up.")], "end_turn")])
    tid = seed(svc, a, days_ago=8)
    a.handle("let's talk")
    a.handle("I keep going back and forth on the new PC build")
    assert a.convo.link_offer["id"] == tid
    assert "call conversation_link" in system_of(a) and '"New PC build"' in system_of(a)
    a.handle("yes")  # nothing is waiting on a yes: the model hears it, and links
    assert a.convo.thread["id"] == tid


def test_an_unrelated_talk_offers_nothing(svc):
    a = chat(svc, answers("Ooh, lasagne."))
    seed(svc, a)
    a.handle("let's talk")
    a.handle("what should I cook for dinner tonight")
    assert a.convo.link_offer is None and "conversation_link" not in system_of(a)


class Embedder:
    """Two meanings: computers and food. Words don't matter, meaning does."""
    available = True

    def embed(self, texts, query=False):
        out = []
        for t in texts:
            t = t.lower()
            v = [1.0, 0.0] if any(w in t for w in ("pc", "cpu", "processor", "rig", "gpu")) else [0.0, 1.0]
            out.append(v)
        return out


def test_threads_match_by_meaning_when_memory_has_embeddings(svc):
    svc.memory.embedder = Embedder()
    a = chat(svc)
    seed(svc, a)
    assert svc.storage.query("SELECT vector FROM convo_threads")[0]["vector"] is not None
    hit = a.convo.threads.match("which processor should go in the rig")  # no shared topic word
    assert hit and hit[0]["title"] == "New PC build" and hit[1] >= 0.99
    assert a.convo.threads.match("what's for dinner") is None


def test_keyword_matching_stems_and_needs_a_real_overlap(svc):
    a = chat(svc)
    seed(svc, a, {**NOTES, "title": "Moving to Leeds", "summary": "Rent and the car."})
    k = a.convo.threads
    assert k.match("the move", strict=False)[0]["title"] == "Moving to Leeds"
    assert k.match("leeds")[0]["title"] == "Moving to Leeds"
    assert k.match("the weather tomorrow") is None and k.match("") is None


# ---- memory that learns from talk, and forgets with it ----------------------------------------------------------

def test_forget_this_conversation_takes_back_the_thread_its_log_and_what_it_taught(svc):
    a = chat(svc, answers("Okay.", "Noted.", "Sure."))
    mem = svc.memory
    old = mem.add("J's favourite CPU brand is Intel", importance=3)
    svc.think = Think(NOTES, facts=[{"statement": "J's favourite CPU brand is AMD", "kind": "preference", "subject": "J",
                                     "subject_kind": "self", "context": "", "importance": 4, "contradicts": [old["id"]]}])
    mem.recall = lambda *x, **k: [{"id": old["id"], "statement": old["statement"], "score": 1.0}]  # extraction sees it
    talk(a, "honestly I've gone off Intel this year", "AMD is my favourite now for the new PC build")
    tid = a.convo.threads.latest()["id"]
    assert [f["statement"] for f in svc.storage.query(f"SELECT statement FROM facts WHERE {'superseded_by IS NULL'}")] == \
        ["J's favourite CPU brand is AMD"]  # memory's own supersede rule handled the contradiction
    a.handle("pick up where we left off")
    a.handle("actually I'd rather you forget all this")
    out = a.handle("forget this conversation")
    assert "Nothing from this conversation is kept" in out["reply"]
    assert a.convo.threads.get(tid) is None
    assert svc.storage.query("SELECT statement FROM facts WHERE superseded_by IS NULL") == \
        [{"statement": "J's favourite CPU brand is Intel"}]  # what it had replaced counts again
    assert not svc.storage.query("SELECT text FROM log WHERE text LIKE '%Intel this year%' OR text LIKE '%forget all%'")
    a.handle("and one more thing about the case")
    a.handle("that's it")
    assert a.convo.threads.count() == 0 and a.convo.covered["state"] == "off_record"
    assert not svc.storage.query("SELECT text FROM log WHERE text LIKE '%one more thing%'")


def test_forget_a_conversation_by_topic_or_the_last_one(svc):
    a = chat(svc)
    seed(svc, a, {**NOTES, "title": "Moving to Leeds", "summary": "Rent."})
    seed(svc, a, days_ago=1)
    out = a.handle("forget our conversation about the move")
    assert out["reply"] == "Forgotten: our talk about Moving to Leeds."
    assert [t["title"] for t in a.convo.threads.list()] == ["New PC build"]
    a.handle("forget the last conversation")
    assert a.convo.threads.count() == 0
    assert "don't have a conversation about" in a.handle("forget our conversation about tennis")["reply"]


def test_forget_from_the_hud(svc):
    a = chat(svc)
    tid = seed(svc, a)
    sk = svc.skills.get("conversation")
    assert sk.actions()["forget"]({"id": tid})["ok"] and a.convo.threads.count() == 0


def test_memorys_own_learning_leaves_kept_conversations_to_their_thread(svc):
    a = chat(svc, answers("Okay.", "Sure."))
    svc.think = Think(NOTES)
    talk(a, "I'm finally building a new PC this month", "the 9800X3D looks like the one")
    a.handle("what's the weather like in my head", source="text")  # not part of the conversation
    prompts = []
    svc.think = lambda prompt, schema=None, **kw: prompts.append(prompt) or {"facts": []}
    svc.skills.get("memory").learn_conversations(time.time() + 600)
    assert prompts and "weather like in my head" in prompts[0] and "9800X3D looks like" not in prompts[0]


def test_off_the_record_talk_saves_nothing(svc):
    a = chat(svc, answers("Your secret's safe.", "Mm."))
    svc.think = think = Think(NOTES)
    out = a.handle("let's talk off the record")
    assert a.convo.active and a.convo.off_record and out["reply"] in convo_mod.GREETINGS
    a.handle("I'm thinking about quitting my job")
    a.handle("don't tell anyone, obviously")
    a.handle("that's it")
    assert a.convo.threads.count() == 0 and think.prompts == [] and a.convo.covered["state"] == "off_record"
    assert not svc.storage.query("SELECT text FROM log WHERE text LIKE '%quitting%'")
    assert a.convo.threads.owns(a.convo.started + 0.001)  # and memory's log learning won't touch those minutes either


def test_going_off_the_record_mid_talk_keeps_none_of_it(svc):
    a = chat(svc, answers("Sure.", "Okay."))
    svc.think = think = Think(NOTES)
    talk(a, "let me tell you about the new PC build plan", "off the record", "the budget is from my bonus",
         "and that's between us")
    assert a.convo.threads.count() == 0 and think.prompts == []


# ---- follow-ups over days ---------------------------------------------------------------------------------------

def follow_rig(svc, days_ago=3.0):
    a = chat(svc)
    tid = seed(svc, a, days_ago=days_ago)
    nudges = []
    svc.nudge = lambda text, **kw: nudges.append((text, kw)) or object()
    return a, svc.skills.get("conversation"), tid, nudges


def test_an_open_decision_comes_back_once_with_a_why(svc):
    a, sk, tid, nudges = follow_rig(svc)
    out = sk.follow_up()
    assert out["thread_id"] == tid
    text, kw = nudges[0]
    when = threads_mod.when_words(time.time() - 3 * DAY, time.time(), svc.tz)
    assert text == f"{when[:1].upper()}{when[1:]} you were deciding between the 9800X3D and the 9950X3D: which did you pick?"
    assert kw["key"] == f"conversation:followup:{tid}" and kw["priority"] == "normal"
    assert kw["why"]["rule"].startswith("Conversation follow-up") and "Thread: New PC build" in kw["why"]["evidence"][0]
    assert "Still open: Which CPU to buy" in kw["why"]["evidence"]
    assert kw["action"]["command"] == "let's continue our talk about New PC build"
    assert sk.follow_up() is None  # asked once


def test_follow_ups_at_most_one_a_day(svc):
    a, sk, tid, nudges = follow_rig(svc)
    seed(svc, a, {**NOTES, "title": "Moving to Leeds"}, days_ago=2)
    assert sk.follow_up() and sk.follow_up() is None
    assert len(nudges) == 1
    a.convo.threads.meta("followup_day", "2000-01-01")  # a new day
    assert sk.follow_up() and len(nudges) == 2


def test_no_follow_up_while_live_in_a_conversation_too_soon_or_too_late(svc):
    a, sk, tid, nudges = follow_rig(svc, days_ago=0.2)
    assert sk.follow_up() is None  # talked about a few hours ago
    svc.storage.execute("UPDATE convo_threads SET updated = ?", (time.time() - 30 * DAY,))
    assert sk.follow_up() is None  # a month ago: let it go
    svc.storage.execute("UPDATE convo_threads SET updated = ?", (time.time() - 3 * DAY,))
    svc.live = lambda: True
    assert sk.follow_up() is None
    svc.live = lambda: False
    a.convo.brain = lambda: "claude"
    a.convo.begin()
    assert sk.follow_up() is None
    a.convo.end()
    assert sk.follow_up() is not None and len(nudges) == 1


def test_follow_ups_can_be_switched_off_for_good_per_thread(svc):
    a, sk, tid, nudges = follow_rig(svc)
    sk.actions()["followups"]({"id": tid, "on": False})
    assert sk.follow_up() is None
    a.convo.threads.set_followups(tid, True)
    svc.cfg["skills"]["conversation"] = {"followups": False}
    sk.cfg["followups"] = False
    assert sk.follow_up() is None


def test_dont_follow_up_on_that_after_one_was_asked(svc):
    a, sk, tid, nudges = follow_rig(svc)
    sk.follow_up()
    svc.bus.publish("notice", {"key": f"conversation:followup:{tid}", "text": nudges[0][0], "spoken": True})
    out = a.handle("don't follow up on that")
    assert out["reply"] == "Okay, I won't follow up on New PC build." and not a.convo.threads.get(tid)["followups"]
    out = a.handle("stop asking about the new pc")
    assert "won't follow up on New PC build" in out["reply"]


def test_answering_a_follow_up_updates_the_thread(svc):
    a, sk, tid, nudges = follow_rig(svc)
    a.client = FakeClient(answers("Nice, the 9800X3D is a great pick."))
    sk.follow_up()
    svc.bus.publish("notice", {"key": f"conversation:followup:{tid}", "text": nudges[0][0], "spoken": True})
    svc.think = think = Think({**NOTES, "summary": "J bought the 9800X3D.", "open_questions": [], "follow_up": "",
                               "stances": [{"text": "J chose the 9800X3D", "kind": "decision"}]})
    a.handle("I went with the 9800X3D in the end")
    assert "A moment ago you asked J" in str(a.client.messages.calls[-1]["messages"])  # the model knew the question
    for th in list(__import__("threading").enumerate()):
        if th.name == "conversation-followup":
            th.join(5)
    t = a.convo.threads.get(tid)
    assert t["summary"] == "J bought the 9800X3D." and t["open"] == [] and t["follow_up"] == ""
    assert "I went with the 9800X3D in the end" in think.prompts[0][0]
    assert any(e["text"] == "I went with the 9800X3D in the end" for e in t["last_turns"])


def test_answering_a_follow_up_without_a_model_keeps_the_answer(svc):
    a, sk, tid, nudges = follow_rig(svc)
    svc.think = lambda *x, **k: None
    sk._on_event({"type": "notice", "data": {"key": f"conversation:followup:{tid}", "text": "Which did you pick?"}})
    sk._on_event({"type": "user_said", "data": {"text": "the cheaper one", "source": "voice"}})
    sk._on_event({"type": "assistant_said", "data": {"kind": "offline", "text": "..."}})
    for th in list(__import__("threading").enumerate()):
        if th.name == "conversation-followup":
            th.join(5)
    t = a.convo.threads.get(tid)
    assert t["follow_up"] == "" and t["open"] == [] and t["last_turns"][-1]["text"] == "the cheaper one"


def test_a_command_after_a_follow_up_isnt_taken_as_the_answer(svc):
    a, sk, tid, nudges = follow_rig(svc)
    sk._on_event({"type": "notice", "data": {"key": f"conversation:followup:{tid}", "text": "Which did you pick?"}})
    sk._on_event({"type": "user_said", "data": {"text": "what's the weather"}})
    sk._on_event({"type": "assistant_said", "data": {"kind": "tool", "text": "Sunny."}})
    assert a.convo.threads.get(tid)["follow_up"] and sk._awaiting is not None


# ---- modes -------------------------------------------------------------------------------------------------------

MODE_PROMPTS = {"brainstorm": "two or three concrete, genuinely different ideas", "devil": "take the side against",
                "coach": "don't give answers or advice", "interview": "one question at a time, follow up",
                "teach": "short check question", "duck": "mostly listen", "plan": "call conversation_propose_tasks",
                "story": "end each turn with a hook"}


@pytest.mark.parametrize("said, mode, topic", [
    ("let's brainstorm names for my channel", "brainstorm", "names for my channel"), ("brainstorm with me", "brainstorm", None),
    ("play devil's advocate", "devil", None), ("be my devil's advocate on remote work", "devil", "remote work"),
    ("let's debate pineapple on pizza", "devil", "pineapple on pizza"), ("coach me on public speaking", "coach", "public speaking"),
    ("be my coach", "coach", None), ("interview me", "interview", None), ("teach me chess", "teach", "chess"),
    ("teach me how to cook rice", "teach", "how to cook rice"), ("let's rubber duck this", "duck", None),
    ("plan the move with me", "plan", "the move"), ("help me plan the move", "plan", "the move"),
    ("tell me a story", "story", None), ("tell me a story about dragons", "story", "dragons"),
    ("let's just chat", "chat", None), ("switch to devil's advocate", "devil", None), ("coach mode", "coach", None),
])
def test_mode_phrases(svc, said, mode, topic):
    chat(svc)
    intent = route(said, Assistant(svc).router_context())
    assert intent.kind == "conversation" and intent.args["mode"] == mode and intent.args.get("topic") == topic


@pytest.mark.parametrize("mode", list(MODE_PROMPTS))
def test_each_mode_shapes_the_prompt(svc, mode):
    a = chat(svc, answers("Okay."))
    out = a.start_conversation(mode=mode, source="text")
    assert out["reply"] == convo_mod.MODES[mode]["hello"] and a.convo.mode == mode
    a.handle("here's what I've got")
    system = system_of(a)
    assert MODE_PROMPTS[mode] in system and f"Mode: {convo_mod.MODES[mode]['label']}" in system
    assert a.convo.status()["mode_label"] == convo_mod.MODES[mode]["label"]


def test_switching_mode_mid_conversation(svc):
    a = chat(svc, answers("Fair point.", "Then again, remote work kills mentoring."))
    talk(a, "remote work is obviously better", end=False)
    assert "Mode:" not in system_of(a)
    out = a.handle("switch to devil's advocate")
    assert out["reply"] == convo_mod.MODES["devil"]["switch"] and a.convo.active
    a.handle("remote work is obviously better")
    assert "Play devil's advocate on whatever they bring up" in system_of(a)
    assert a.handle("back to normal")["reply"] == convo_mod.MODES["chat"]["switch"] and a.convo.mode == "chat"


def test_a_mode_with_a_topic_starts_the_lesson(svc):
    a = chat(svc, answers("Chess has six kinds of pieces. Which one moves in an L?"))
    out = a.handle("teach me chess")
    assert out["reply"].startswith("Chess has six") and a.convo.mode == "teach"
    assert "Teach J chess in small steps" in system_of(a)


def test_plan_mode_proposes_tasks_and_adds_only_on_a_yes(svc):
    a = chat(svc, [([text_block("Here's the plan."), tool_block("conversation_propose_tasks",
                                                                {"tasks": ["Book the van", "Pack the kitchen"]})], "tool_use"),
                   ([text_block("They're on the card.")], "end_turn")])
    a.handle("plan the move with me")
    a.handle("that's the plan, let's lock it in")
    assert [p["title"] for p in a.convo.status()["proposals"]] == ["Book the van", "Pack the kitchen"]
    assert svc.storage.list_tasks() == []  # proposed, not added
    out = a.handle("yes add them")
    assert out["reply"] == "Added 2 tasks." and {t["title"] for t in svc.storage.list_tasks()} == {"Book the van", "Pack the kitchen"}
    assert a.convo.proposals == []


def test_the_model_cant_add_the_plan_without_js_yes(svc):
    a = chat(svc, [([tool_block("conversation_add_tasks", {})], "tool_use"), ([text_block("Want me to add them?")], "end_turn"),
                   ([tool_block("conversation_add_tasks", {}, id_="toolu_2")], "tool_use"), ([text_block("Done.")], "end_turn")])
    a.start_conversation(mode="plan", source="text")
    a.convo.proposals = [{"title": "Book the van", "keep": True}]
    a.handle("the van is the big one")
    assert svc.storage.list_tasks() == [] and a.convo.proposals  # no yes yet
    a.handle("sure, go for it")
    assert [t["title"] for t in svc.storage.list_tasks()] == ["Book the van"]


def test_plan_mode_proposes_tasks_from_the_notes_at_the_end(svc):
    a = chat(svc, answers("Goal first: when?", "Good, three weeks."))
    svc.think = Think({**NOTES, "title": "The move", "tasks": ["Book the van", "Tell the landlord"]})
    a.handle("help me plan the move")
    a.handle("I want to be out by the end of the month")
    a.handle("so about three weeks from now")
    a.handle("that's it")
    assert [p["title"] for p in a.convo.status()["proposals"]] == ["Book the van", "Tell the landlord"]
    assert svc.storage.list_tasks() == []
    sk = svc.skills.get("conversation")
    assert sk.actions()["add_tasks"]({"titles": ["Tell the landlord"]})["added"] == ["Tell the landlord"]
    assert [t["title"] for t in svc.storage.list_tasks()] == ["Tell the landlord"]


# ---- style that adapts ---------------------------------------------------------------------------------------------

def test_style_feedback_adjusts_the_profile_and_the_prompt(svc):
    a = chat(svc, answers("A long answer about everything.", "Short.", "Ok."))
    talk(a, "tell me about the history of pizza", end=False)
    assert "How J likes you to talk" not in system_of(a)
    assert a.handle("that was too long")["reply"] == "Got it, shorter."
    assert a.handle("stop asking me questions")["reply"].startswith("Okay. No more questions")
    a.handle("so anyway, the history of pizza")
    system = system_of(a)
    assert "very short: one or two short sentences" in system and "Don't ask J questions" in system
    style = a.convo.threads.style()
    assert style["length"] == 1 and style["questions"] == 0
    assert style["changed"][-1]["said"] == "stop asking me questions"
    assert a.handle("be funnier")["reply"] == "Noted. I'll lighten up."
    assert "Be playful" in a.convo.prompt_extra()


def test_style_has_limits_and_persists(svc):
    a = chat(svc)
    a.convo.brain = lambda: "claude"
    a.convo.begin()
    for _ in range(3):
        a.handle("shorter")
    assert a.convo.threads.style()["length"] == 0
    assert a.handle("shorter")["reply"] == "That's already as short as I go."
    assert "one short sentence" in a.convo.prompt_extra()
    b = Assistant(svc)  # a restart: same profile
    assert b.convo.threads.style()["length"] == 0


def test_style_from_the_hud_and_reset(svc):
    a = chat(svc)
    sk = svc.skills.get("conversation")
    assert sk.actions()["style"]({"dim": "formality", "value": 4})["style"]["formality"] == 4
    assert "Formal: no slang" in a.convo.prompt_extra()
    st = a.convo.status()["style"]
    assert st["formality"] == 4 and st["labels"]["formality"][4] == "Formal" and "formal" in st["words"]
    assert a.handle("what's my conversation style")["reply"].startswith("Right now I keep it balanced, warm, some questions, formal")
    a.handle("reset my conversation style")
    assert a.convo.threads.style()["formality"] == 1 and a.convo.prompt_extra() == ""


def test_style_phrases_only_mean_style_in_a_conversation(svc):
    a = chat(svc, answers("Sure."))
    assert route("shorter", a.router_context()) is None  # outside a conversation: the model's, as before
    assert route("too long", a.router_context()) is None


# ---- smarter turn-taking -------------------------------------------------------------------------------------------

def test_let_me_think_holds_the_quiet_minute(svc):
    a = chat(svc)
    t = [1000.0]
    a.convo.clock = lambda: t[0]
    a.convo.brain = lambda: "claude"
    a.handle("let's talk")
    out = a.handle("hmm let me think")
    assert out["reply"] == "Take your time." and a.convo.status()["held"]
    t[0] += 150
    assert a.convo.is_active()  # well past a quiet minute
    t[0] += 60
    assert a.convo.check() == "timeout"


def test_talking_again_ends_the_hold(svc):
    a = chat(svc, answers("Go on then."))
    t = [1000.0]
    a.convo.clock = lambda: t[0]
    a.handle("let's talk")
    a.handle("give me a second")
    a.handle("okay so here's my idea")
    assert a.convo.hold_until == 0.0
    t[0] += 61
    assert a.convo.check() == "timeout"


@pytest.mark.parametrize("said, directive", [("go on", "carry on from where your last reply ended"),
                                             ("tell me more", "carry on from where"),
                                             ("wait, back up", "Re-explain your last point"),
                                             ("you lost me", "Re-explain your last point")])
def test_go_on_and_back_up_are_told_to_the_model(svc, said, directive):
    a = chat(svc, answers("Pizza started in Naples.", "Then it went to New York."))
    talk(a, "tell me about the history of pizza", end=False)
    assert route(said, a.router_context()) is None  # the model answers
    a.handle(said)
    assert directive in system_of(a)
    a.client.messages.script += answers("More.")
    a.handle("cool and what about Chicago")
    assert "carry on from where" not in system_of(a) and "Re-explain" not in system_of(a)


class _Speaker:
    def __init__(self):
        import threading

        self.speaking = threading.Event()
        self.last_text, self.last_end, self.expects_reply = "", 0.0, False

    def say(self, text, expects_reply=None, turn=None):
        self.last_text, self.last_end = text, time.time()

    def chime(self):
        pass

    def interrupt(self, silence_turn=True):
        return True


class _STT:
    name = "parakeet"
    text = ""

    def transcribe(self, _samples, hints=()):
        return self.text


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, type_, data, sticky=False):
        self.events.append((type_, data))


def test_a_hmm_in_a_conversation_keeps_it_open(svc):
    a = chat(svc)
    sp = _Speaker()
    svc.speak = sp.say
    lst = VoiceListener(_Bus(), sp, ["friday"], lambda text: a.handle(text, "voice"), {})
    lst.stt = _STT()
    svc.skills.get("conversation").start(type("RT", (), {"speaker": sp, "listener": lst, "assistant": a})())
    a.convo.brain = lambda: "claude"
    a.handle("let's talk", "voice")
    a.convo.last_activity = 0.0
    lst.stt.text = "hmm"
    lst._handle_audio(np.zeros(16000, dtype=np.int16), started_at=time.time())
    assert lst.bus.events[-1][1]["ignored"] == "not a turn"
    assert time.time() - a.convo.last_activity < 5  # the quiet minute started over


# ---- look into that while we talk ----------------------------------------------------------------------------------

def test_look_into_that_uses_the_tasks_engine_when_there_is_one(svc):
    a = chat(svc, answers("The 9800X3D is the gaming pick."))
    started = []
    svc.tasks = type("Tasks", (), {"start": lambda self, prompt, **kw: started.append((prompt, kw)) or {"ok": True}})()
    a.handle("let's talk about the 9800X3D")
    out = a.handle("look into that while we talk")
    assert out["reply"].startswith("On it") and a.convo.active
    assert "9800X3D" in started[0][0] and started[0][1]["source"] == "conversation"


def test_look_into_that_falls_back_to_a_research_job(svc):
    a = chat(svc, answers("Hm."))
    jobs = []
    svc.jobs = type("Jobs", (), {"submit": lambda self, kind, prompt, project, title: jobs.append((kind, prompt)) or {"ok": True}})()
    a.handle("let's talk")
    out = a.handle("research AM5 motherboards while we talk")
    assert out["reply"].startswith("On it") and jobs[0][0] == "research" and "am5 motherboards" in jobs[0][1]
    svc.jobs = None
    assert "Background jobs aren't running" in a.handle("research AM5 motherboards while we talk")["reply"]


# ---- the HUD ------------------------------------------------------------------------------------------------------

def test_hud_status_resume_rename_and_privacy_while_live(svc):
    a = chat(svc)
    tid = seed(svc, a)
    sk = svc.skills.get("conversation")
    st = sk.status()
    assert st["threads"][0]["title"] == "New PC build" and st["threads"][0]["open"] == 1 and st["thread_count"] == 1
    assert {m["id"] for m in st["modes"]} == set(convo_mod.MODES) and st["style"]["words"]
    assert sk.actions()["rename"]({"id": tid, "title": "The new rig"})["ok"]
    a.convo.brain = lambda: "claude"
    sk.runtime = type("RT", (), {"assistant": a})()
    out = sk.actions()["resume"]({"id": tid})
    assert out["ok"] and out["reply"].startswith("Picking up The new rig") and a.convo.thread["id"] == tid
    assert sk.status()["thread"]["title"] == "The new rig"
    svc.live = lambda: True
    st = sk.status()
    assert st["threads"] == [] and st["thread"] is None and st["covered"] is None


def test_hud_mode_and_link_actions(svc):
    a = chat(svc)
    sk = svc.skills.get("conversation")
    sk.runtime = type("RT", (), {"assistant": a})()
    a.convo.brain = lambda: "claude"
    assert sk.actions()["mode"]({"mode": "coach"})["ok"] and a.convo.active and a.convo.mode == "coach"
    assert sk.actions()["mode"]({"mode": "story"})["ok"] and a.convo.mode == "story"
    tid = seed(svc, a)
    a.convo.link_offer = {"id": tid, "title": "New PC build", "when": "last week"}
    assert sk.actions()["link"]({})["recap"].startswith("Picking up") and a.convo.thread["id"] == tid
    a.convo.link_offer = {"id": tid, "title": "x", "when": "y"}
    sk.actions()["unlink"]({})
    assert a.convo.link_offer is None


# ---- phrases: no collisions ----------------------------------------------------------------------------------------

IN_CONVERSATION = ["hmm let me think", "let me think", "give me a second", "one sec", "too long", "that was too long",
                   "shorter", "keep it short", "more detail", "be funnier", "be more serious", "stop asking me questions",
                   "fewer questions", "be more formal", "be more casual", "look into that while we talk",
                   "research the 9800x3d while we talk", "forget this conversation", "back to normal",
                   "stop playing devil's advocate", "switch to coach mode", "that's it"]


def test_conversation_phrases_have_one_owner_while_talking(svc):
    a = chat(svc)
    a.convo.brain = lambda: "claude"
    a.convo.begin()
    a.convo.proposals = [{"title": "Book the van", "keep": True}]
    a.convo.link_offer = {"id": 1, "title": "x", "when": "y"}
    for said in IN_CONVERSATION + ["add them", "pick that up"]:
        t = clean(said)
        owners = [s.name for s in svc.skills if s.route(t) is not None]
        assert owners == ["conversation"], (said, owners)
    for said in ["go on", "tell me more", "keep going", "continue", "wait back up", "say that again", "what were we talking about"]:
        assert route(said, a.router_context()) is None, said  # the model answers these, told what they mean


def test_memory_still_forgets_facts_but_not_conversations(svc):
    a = chat(svc)
    ctx = a.router_context()
    assert route("forget that i like pineapple", ctx).tool == "forget"
    for said in ["forget that conversation", "forget about this conversation", "forget our chat about the move"]:
        assert route(said, ctx).tool == "conversation_forget", said
        assert [s.name for s in svc.skills if s.route(clean(said)) is not None] == ["conversation"]
