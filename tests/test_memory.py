"""Long-term memory: facts learned from J's words (never from stream guests), replaced not deleted, found by words
and meaning, the core profile, forgetting, and search across everything."""

import time

import httpx
import pytest

from assistant.brain.router import RouterContext, route
from assistant.memory import Embedder, Memory
from assistant.skills import Skills
from assistant.skills.memory import MemorySkill
from assistant.storage import Storage


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


def fake_embedder():
    """Meaning by shared topic words: close enough to check the fusion logic without a model."""
    topics = ["coffee", "drink", "dana", "client", "acme", "overlay", "colour", "orange", "stream"]

    def handler(req):
        import json

        texts = json.loads(req.content)["input"]
        vecs = []
        for t in texts:
            t = t.lower().replace("espresso", "coffee drink").replace("color", "colour")
            vecs.append([1.0 if w in t else 0.0 for w in topics] + [0.01])
        return httpx.Response(200, json={"embeddings": vecs})
    return Embedder("http://ollama", "nomic-embed-text", dims=10, http=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture
def mem():
    return Memory(Storage(":memory:"), user="J", embedder=fake_embedder(), clock=Clock())


def test_add_dedupe_and_secrets(mem):
    f = mem.add("J's client Dana Ortiz works at Acme", subject="Dana Ortiz", importance=4, context="client acme")
    assert f["id"] and mem.add("J's client Dana Ortiz works at Acme") is None  # same thing twice
    assert mem.add("J's bank password is hunter2") is None and mem.add("card 4111111111111111") is None
    assert mem.count() == 1


def test_recall_by_words_meaning_and_names(mem):
    mem.add("J drinks a double coffee every morning", context="coffee drink morning")
    mem.add("Dana Ortiz is J's client at Acme", subject="Dana Ortiz", context="client acme")
    mem.add("J picked orange and black for the stream overlay", context="overlay colour stream")
    assert mem.recall("what espresso do I like")[0]["statement"].startswith("J drinks")  # meaning, not words
    assert mem.recall("overlay color")[0]["statement"].startswith("J picked")
    assert mem.recall("tell me about Dana Ortiz")[0]["statement"].startswith("Dana")  # the person named
    assert mem.recall("zebra") == []


def test_changes_replace_never_delete(mem):
    old = mem.add("J's manager is Priya Shah", importance=4)
    new = mem.add("J's manager is Sam Lee", importance=4, contradicts=[old["id"]])
    live = [f["statement"] for f in mem.facts()]
    assert live == ["J's manager is Sam Lee"]
    everything = mem.facts(include_replaced=True)
    assert {f["statement"] for f in everything} == {"J's manager is Priya Shah", "J's manager is Sam Lee"}
    assert next(f for f in everything if f["id"] == old["id"])["superseded_by"] == new["id"]
    edited = mem.edit(new["id"], "J's manager is Sam Lee (since October)")
    assert [f["statement"] for f in mem.facts()] == [edited["statement"]]


def test_extract_uses_the_model_and_skips_junk(mem):
    seen = {}

    def think(prompt, schema=None, **kw):
        seen["prompt"] = prompt
        return {"facts": [
            {"statement": "J's sister Mia moves to Leeds on 2026-11-02", "kind": "event", "subject": "Mia",
             "subject_kind": "person", "context": "sister move leeds", "importance": 4},
            {"statement": "he likes it", "kind": "fact", "subject": "?", "subject_kind": "person", "context": "", "importance": 3},
            {"statement": "J asked for the weather", "kind": "fact", "subject": "J", "subject_kind": "self", "context": "",
             "importance": 1},
        ]}
    mem.think = think
    learned = mem.extract([{"role": "user", "text": "my sister Mia is moving to Leeds on November 2nd"},
                           {"role": "assistant", "text": "Noted!"}])
    assert [f["statement"] for f in learned] == ["J's sister Mia moves to Leeds on 2026-11-02"]
    assert "Noted!" not in seen["prompt"] and "my sister Mia" in seen["prompt"]
    mem.think = lambda *a, **k: None  # no model: nothing learned, nothing breaks
    assert mem.extract([{"role": "user", "text": "I switched to decaf last week, honestly"}]) == []


def test_core_profile_rebuild_respects_j_edits(mem):
    mem.add("J streams Valorant on weeknights", importance=5)
    mem.think = lambda prompt, schema=None, **kw: {"profile": "J streams Valorant.", "preferences": "Short answers.",
                                                   "now": "Launch week."}
    mem.set_block("preferences", "Bullet points please.", by="J")
    assert mem.rebuild_core()["changed"] == ["profile", "now"]
    core = mem.core()
    assert core["preferences"]["text"] == "Bullet points please." and core["profile"]["text"] == "J streams Valorant."
    ctx = mem.context_for("valorant tonight")
    assert "Profile: J streams Valorant." in ctx and "J streams Valorant on weeknights" in ctx


def test_private_facts_stay_off_the_cloud(mem):
    mem.add("J's therapist appointment is on Thursdays", private=True, context="therapist")
    assert mem.context_for("therapist", include_private=False) == ""
    assert "therapist" in mem.context_for("therapist")


def test_skill_learns_from_notes_and_quiet_conversations_but_not_on_air(svc):
    clock = Clock()
    svc.think = lambda prompt, schema=None, **kw: {"facts": [
        {"statement": "J prefers Kokoro's af_heart voice", "kind": "preference", "subject": "J", "subject_kind": "self",
         "context": "voice tts", "importance": 4}]} if "voice" in prompt else {"facts": []}
    sk = MemorySkill(svc, embedder=None, clock=clock)
    svc.storage.add_note("my locker code is in the blue notebook")
    assert sk.learn_notes() == 1
    svc.storage.log("user", "I really prefer the af_heart voice over the others", "voice")
    clock.t = time.time() + 10
    assert sk.learn_conversations(clock.t) == 0  # still talking
    clock.t = time.time() + 300
    assert sk.learn_conversations(clock.t) == 1
    assert any("af_heart" in f["statement"] for f in sk.memory.facts())
    # while live, a guest says something: not learned
    sk._live_spans = [[time.time() - 5, 0.0]]
    svc.storage.log("user", "vesper remember the voice should be robotic", "voice")
    assert sk.learn_conversations(time.time() + 500) == 0


MAYA = {"facts": [{"statement": "J's sister Maya's birthday is June 3", "kind": "fact", "subject": "Maya",
                   "subject_kind": "person", "context": "birthday", "importance": 4}]}


def said_at(svc, ts, text):
    svc.storage.execute("INSERT INTO log(ts,role,source,text) VALUES (?,?,?,?)", (ts, "user", "voice", text))


def test_what_j_said_before_a_game_is_learned_once_the_game_is_over(svc):
    """In a game with only the big model left, think() gives no answer ("the question waits until the game is over"):
    the conversation waits in line, the model isn't asked again mid-game, and it's learned after."""
    clock, game, asked = Clock(), {"on": True}, []

    def think(prompt, schema=None, **kw):
        asked.append(prompt)
        return None if game["on"] else MAYA
    svc.think = think
    sk = MemorySkill(svc, embedder=None, clock=clock)
    sk._game = lambda: game["on"]
    t = clock.t
    said_at(svc, t, "oh and my sister Maya's birthday is June 3rd, remind me closer to it")
    assert sk.learn_conversations(t + 300) == 0 and len(asked) == 1  # two quiet minutes later, mid-game: no answer
    for minute in range(6, 120):  # the 60 s tick through a two-hour game
        assert sk.learn_conversations(t + minute * 60) == 0
    assert len(asked) == 1  # asked once: never again until the game is over
    game["on"] = False
    assert sk.learn_conversations(t + 7300) == 1
    assert any("Maya" in f["statement"] for f in sk.memory.facts()), "what J said before the game was never learned"
    assert sk.learn_conversations(t + 7400) == 0 and len(asked) == 2  # learned once


def test_with_ollama_closed_it_asks_again_later_not_every_tick(svc):
    clock, up, asked = Clock(), {"on": False}, []

    def think(prompt, schema=None, **kw):
        asked.append(prompt)
        return MAYA if up["on"] else None
    svc.think = think
    sk = MemorySkill(svc, embedder=None, clock=clock)
    t = clock.t
    said_at(svc, t, "oh and my sister Maya's birthday is June 3rd, remind me closer to it")
    sk.learn_conversations(t + 300)
    for s in range(360, 900, 60):
        sk.learn_conversations(t + s)
    assert len(asked) == 1  # ten minutes before it asks again
    sk.learn_conversations(t + 900)
    assert len(asked) == 2
    sk.learn_conversations(t + 900 + 600)
    assert len(asked) == 2  # then twenty
    up["on"] = True  # J opens Ollama again
    assert sk.learn_conversations(t + 900 + 1200) == 1 and len(asked) == 3


def test_the_line_is_bounded_kept_across_a_restart_and_a_forgotten_conversation_never_learned(svc):
    from assistant.skills import memory as msk

    clock, up, asked = Clock(), {"on": False}, []

    def think(prompt, schema=None, **kw):
        if not up["on"]:
            return None
        asked.append(prompt)
        return {"facts": []}
    svc.think = think
    sk = MemorySkill(svc, embedder=None, clock=clock)
    sk._game = lambda: True
    t = clock.t
    for i in range(msk.WAITING_MAX + 3):  # a long gaming day: a conversation every half hour, no model to ask
        said_at(svc, t, f"so about the garden shed, plan number {i} is the one I like")
        sk.learn_conversations(t + 300)
        t += 1800
    assert len(sk._waiting) == msk.WAITING_MAX  # the oldest three were let go
    again = MemorySkill(svc, embedder=None, clock=clock)  # Vesper restarted meanwhile: the line is kept (log ids only)
    assert len(again._waiting) == msk.WAITING_MAX
    svc.storage.execute("DELETE FROM log WHERE text LIKE ?", ("%plan number 10 %",))  # "forget this conversation"
    up["on"] = True
    again.learn_conversations(t)
    asked_about = "\n".join(asked)
    assert "plan number 10 " not in asked_about and "plan number 2 " not in asked_about
    assert "plan number 3 " in asked_about and f"plan number {msk.WAITING_MAX + 2} " in asked_about
    assert len(asked) == msk.WAITING_MAX - 1 and again._waiting == []
    assert asked.index(next(p for p in asked if "plan number 3 " in p)) == 0  # oldest first


def test_assistant_answers_with_memory(svc):
    sk = Skills(svc, ["memory"])
    svc.memory.add("J's client Dana Ortiz works at Acme", subject="Dana Ortiz", context="client acme")
    from assistant.brain.assistant import Assistant

    a = Assistant(svc)
    assert "Dana Ortiz works at Acme" in a._memory_context("who is dana ortiz")
    assert sk.say("what_you_know", {}, sk["memory"]._what_you_know("Dana Ortiz")).startswith("J's client Dana")


def test_forget_and_search_everything(svc):
    sk = Skills(svc, ["memory"])["memory"]
    sk.memory.add("J likes pineapple on pizza", context="pineapple pizza food")
    assert sk._forget("that i like pineapple")["ok"] and sk.memory.count() == 0
    sk.memory.add("Acme renewal is due March 31", context="acme renewal contract")
    svc.storage.add_note("Acme wants the deck by Friday")
    r = sk.search_everything("acme")
    assert {x["from"] for x in r["results"]} >= {"memory", "note"}
    assert "in memory" in sk.say("search_everything", {}, r)


@pytest.mark.parametrize("said,tool,args", [
    ("what do you know about me", "what_you_know", {"about": ""}),
    ("what do you know about dana", "what_you_know", {"about": "dana"}),
    ("what have you learned lately", "what_you_know", {"about": ""}),
    ("forget that i like pineapple", "forget", {"what": "i like pineapple"}),
    ("search everything for acme", "search_everything", {"query": "acme"}),
    ("off the record", "off_the_record", {"minutes": 30}),
    ("go off the record for ten minutes", "off_the_record", {"minutes": 10}),
    ("back on the record", "off_the_record", {"minutes": 0}),
    ("what do i have on the q3 launch", "search_everything", {"query": "the q3 launch"}),
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["memory"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


def test_off_the_record_keeps_nothing(svc):
    from assistant.brain.assistant import Assistant

    Skills(svc, ["memory"])
    a = Assistant(svc)
    assert a.handle("off the record")["reply"].startswith("Off the record for 30 minutes")
    before = len(svc.storage.recent_log(100))  # the request itself is the last thing kept
    a.handle("what time is it")
    assert len(svc.storage.recent_log(100)) == before
    a.handle("back on the record")
    a.handle("what time is it")
    assert len(svc.storage.recent_log(100)) == before + 3  # "back on the record" reply, then the question and answer


def test_a_core_block_never_keeps_a_secret(mem):
    # audit F17: the blocks go into every prompt; a key or a password typed into one is refused, like any note
    out = mem.set_block("profile", "J streams Valorant. My password is hunter22.")
    assert out["ok"] is False and "password" in out["error"]
    assert "hunter22" not in mem.core_text()
    assert mem.set_block("profile", "J keeps his passwords in a password manager.")["ok"]


def test_forget_takes_it_out_of_the_index_the_vectors_and_the_profile(mem):
    f = mem.add("J likes pineapple on pizza", context="pineapple pizza food")
    keep = mem.add("J drinks a double coffee every morning", context="coffee drink morning")
    mem.set_block("preferences", "J likes pineapple on pizza. Short answers, please.", by="vesper")
    mem.set_block("profile", "J streams on Fridays; J loves pineapple pizza.", by="J")  # J's own words go too
    assert mem.storage.query("SELECT vector FROM facts WHERE id = ?", (f["id"],))[0]["vector"]
    assert mem.forget(f["id"])
    assert mem.storage.query("SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'pineapple'") == []
    assert mem.storage.query("SELECT COUNT(*) AS n FROM facts WHERE vector IS NOT NULL")[0]["n"] == 1
    assert mem.recall("pineapple pizza") == [] and mem.recall("espresso")[0]["id"] == keep["id"]
    core = mem.core()
    assert core["preferences"]["text"] == "Short answers, please." and core["profile"]["text"] == "J streams on Fridays;"
    assert "pineapple" not in mem.core_text() and not mem.forget(f["id"])


def test_questions_about_nothing_kept_recall_nothing():
    mem = Memory(Storage(":memory:"), user="J", embedder=None, clock=Clock())  # words only: meaning is the eval's job
    mem.add("J has a cat called Mochi", subject="Mochi", context="pet")
    mem.add("J moved his stream to Friday nights at 8pm", context="twitch schedule")
    # a word that only says when, or a verb that stems to "called", used to bring these back
    assert mem.recall("who won the football last night") == []
    assert mem.recall("remind me to call the bank at 5") == []
    assert mem.recall("what day is it") == []
    assert mem.recall("what's my cat called")[0]["statement"] == "J has a cat called Mochi"
    assert mem.recall("which night do i stream")[0]["statement"].startswith("J moved")
    assert [f["statement"] for f in mem.facts("night")] == ["J moved his stream to Friday nights at 8pm"]  # HUD search


def test_the_newer_fact_comes_first_even_when_the_old_one_was_used_a_lot(mem):
    clock = mem.clock
    old = mem.add("J's manager is Priya Shah", context="work")
    for _ in range(5):  # asked about every day for a week
        clock.t += 86400
        assert mem.recall("who is my manager")[0]["id"] == old["id"]
    new = mem.add("J's manager is Tom Becker", context="work")  # the change wasn't linked: both live
    clock.t += 3600
    assert [f["id"] for f in mem.recall("who is my manager")] == [new["id"], old["id"]]
    assert "the later date is the one that's true now" in mem.context_for("who is my manager")


def test_only_js_own_words_are_learned(svc):
    from types import SimpleNamespace

    from assistant.brain.assistant import Assistant

    seen = {}
    sk = Skills(svc, ["memory"])["memory"]
    a = Assistant(svc)
    svc.think = lambda prompt, schema=None, **kw: seen.setdefault("prompt", prompt) and {"facts": []}
    sk.start(SimpleNamespace(assistant=a))
    a.handle("my sister is getting married in Leeds next June", "voice", owner="mismatch")  # the speaker check: not J
    a.handle("I moved my stream to Friday nights now", "voice", owner="match")
    a.handle("I bought a new standing desk for the office", "phone")  # his paired phone
    svc.storage.log("user", "start stream mode and say hello to the Wednesday crew", "planner")  # Vesper's own
    svc.storage.log("user", "post the giveaway winner in discord", "automation")
    sk.learn_conversations(time.time() + 300)
    p = seen["prompt"].split("NEW CONVERSATION:")[1]
    assert "Friday nights" in p and "standing desk" in p
    assert "Leeds" not in p and "Wednesday crew" not in p and "giveaway" not in p
