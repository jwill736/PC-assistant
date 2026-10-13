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
    ("what do i have on the q3 launch", "search_everything", {"query": "the q3 launch"}),
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["memory"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)
