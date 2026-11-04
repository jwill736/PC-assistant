"""Memory recall eval: does Vesper's long-term memory find the right fact, and keep quiet when it has none?

    python -m evals.memory_recall            # the table: words only, and with a stand-in embedding model

No model and no network: 40 facts a streamer with a day job would tell Vesper, learned over two months, then 32
questions in J's own words (paraphrases among them: "who's my boss" for a fact about his manager) and 10 that no
fact answers. Scored on what ``Memory.recall`` hands the model with each question (the top 3):

- **recall@3**: the right fact is in the top 3.
- **wrong recall**: a question nothing answers still gets facts (the model is then tempted to use them).
- **old answer**: a fact that changed ("I moved my stream to Fridays") still shows its old version.

The stand-in embedding model (``concept_embedder``) knows a few dozen synonyms and nothing else. It checks the
fusion and the cut-offs, not how good a real model is; ``embeddinggemma`` on J's PC is the real test
(``--ollama`` runs the same questions through it when Ollama is up).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import zlib

import httpx

from assistant.memory import Embedder, Memory
from assistant.storage import Storage

DAY = 86400.0
START = 1_790_000_000.0  # mid-September 2026: facts are learned over the next two months

# (key, statement, subject, context). Learned in this order, a day and a half apart.
FACTS = [
    ("stream_old", "J streams on Tuesday and Thursday nights at 8pm", "J", "twitch schedule"),
    ("job", "J works as a QA engineer at Northwind Logistics", "J", "day job"),
    ("manager_old", "J's manager is Priya Shah", "Priya Shah", "work"),
    ("hours", "J works 9 to 5:30 on weekdays, from home on Mondays and Fridays", "J", "day job hours"),
    ("standup", "J's team standup is at 9:30 every weekday", "J", "work meeting"),
    ("work_tools", "J's work uses Jira and Slack", "J", "work tools"),
    ("mod", "Kai is the lead moderator in J's Twitch chat", "Kai", "twitch chat"),
    ("editor", "Marcus edits J's YouTube highlight videos and charges $40 per video", "Marcus", "editing"),
    ("game", "J's main game on stream is Elden Ring", "J", "twitch"),
    ("next_game", "J plans to start a Baldur's Gate 3 playthrough in December", "J", "twitch"),
    ("mic", "J uses a Shure SM7B microphone with a GoXLR", "J", "stream gear audio"),
    ("camera", "J's stream camera is a Sony ZV-E10", "J", "stream gear"),
    ("overlay", "J's stream overlay colours are orange and black", "J", "branding"),
    ("sponsor", "J has a sponsorship deal with Glytch Energy that pays $300 a month", "Glytch Energy", "sponsor"),
    ("sponsor_rule", "J must show the Glytch Energy ad once per stream", "Glytch Energy", "sponsor"),
    ("girlfriend", "J's girlfriend is Leah", "Leah", "family"),
    ("leah_bday", "Leah's birthday is on March 14", "Leah", "family"),
    ("mum", "J's mum lives in Leeds", "J", "family"),
    ("cat", "J has a cat called Mochi", "Mochi", "pet"),
    ("vet", "Mochi's vet appointment is on 2026-11-20", "Mochi", "pet"),
    ("coffee", "J drinks oat milk flat whites with no sugar", "J", "food"),
    ("allergy", "J is allergic to peanuts", "J", "health food"),
    ("gym", "J goes to the gym on Monday, Wednesday and Saturday mornings", "J", "routine"),
    ("bed", "J wants to be in bed by midnight on work nights", "J", "routine sleep"),
    ("short", "J likes short answers and hates being read long lists", "J", "preference"),
    ("celsius", "J prefers temperatures in Celsius", "J", "preference"),
    ("follower_goal", "J's goal is 1,000 Twitch followers by the end of the year", "J", "twitch goal"),
    ("affiliate", "J reached Twitch affiliate in August 2026", "J", "twitch"),
    ("discord", "J's Discord server is called The Couch", "J", "community"),
    ("emotes", "Bea draws J's Twitch emotes and charges $25 per emote", "Bea", "art"),
    ("isp", "J's internet provider is Virgin Media", "J", "home"),
    ("car", "J drives a 2018 Honda Civic", "J", "car"),
    ("insurance", "J's car insurance renews on 2027-01-05", "J", "car bills"),
    ("clipforge", "J is building a Stream Deck plugin called ClipForge", "ClipForge", "side project"),
    ("clipforge_lang", "ClipForge is written in TypeScript", "ClipForge", "side project code"),
    ("promise", "J promised Kai a mod shirt by the end of November", "Kai", "promise"),
    ("raid", "J usually raids LunaPlays at the end of his streams", "J", "twitch"),
    ("lisbon", "J is flying to Lisbon on 2026-12-18 for a week", "J", "travel trip"),
    ("pc", "J's PC has an RTX 5090 and 64 GB of RAM", "J", "hardware"),
    # what changed: newer facts that replace older ones
    ("manager", "J's manager is Tom Becker", "Tom Becker", "work"),
    ("stream", "J moved his stream to Friday nights at 8pm", "J", "twitch schedule"),
]
CHANGED = {"manager": "manager_old", "stream": "stream_old"}
# Before it changed, J asked about it now and then (so the old fact was "used lately" too).
ASKED_BEFORE = {"manager": "who is my manager", "stream": "when do i stream"}
# How J says it changed. The extractor is shown the facts these words recall and asked which one changed.
CHANGE_SAID = {"manager": "I've got a new boss, Tom Becker", "stream": "I moved my stream to Fridays now, same time"}

# (question, the facts that answer it). Lower case and loose, the way J types or says it.
QUESTIONS = [
    ("when do i stream", {"stream"}),
    ("what night am i live", {"stream"}),
    ("who's my boss", {"manager"}),
    ("who is my manager", {"manager"}),
    ("what does marcus charge for an edit", {"editor"}),
    ("what game do i mainly play on stream", {"game"}),
    ("what am i playing next", {"next_game"}),
    ("what mic do i have", {"mic"}),
    ("what colours is my overlay", {"overlay"}),
    ("how much does glytch pay me", {"sponsor"}),
    ("do i have to do the energy drink ad", {"sponsor_rule"}),
    ("when's leah's birthday", {"leah_bday"}),
    ("where does my mum live", {"mum"}),
    ("what's my cat called", {"cat"}),
    ("when is mochi at the vet", {"vet"}),
    ("how do i take my coffee", {"coffee"}),
    ("am i allergic to anything", {"allergy"}),
    ("which days do i hit the gym", {"gym"}),
    ("what time should i go to bed", {"bed"}),
    ("how many followers am i aiming for", {"follower_goal"}),
    ("what's my discord called", {"discord"}),
    ("who does my emotes", {"emotes"}),
    ("who's my internet with", {"isp"}),
    ("when does my car insurance renew", {"insurance"}),
    ("what language is clipforge in", {"clipforge_lang"}),
    ("what did i promise kai", {"promise"}),
    ("when's my lisbon trip", {"lisbon"}),
    ("what time is standup", {"standup"}),
    ("who do i usually raid", {"raid"}),
    ("what graphics card is in my pc", {"pc"}),
    ("where do i work", {"job"}),
    ("who mods my chat", {"mod"}),
]
# Nothing in memory answers these: the right amount of facts to hand the model is none.
NEGATIVES = [
    "what's the weather tomorrow",
    "what's my dentist's name",
    "open spotify",
    "how much is a ps5 right now",
    "who won the football last night",
    "when is my sister's wedding",
    "remind me to call the bank at 5",
    "what's the time in tokyo",
    "how do i make pancakes",
    "turn the lights off",
    "what should i have for dinner tonight",
    "can you turn the volume down",
    "what's on tv tonight",
    "how long until christmas",
    "what day is it",
]

# ---- the stand-in embedding model ------------------------------------------------------------------------------
# Words that mean the same thing share one direction; every other word gets its own (hashed). Enough to show
# whether a paraphrase is found once meaning is in play, without pretending to be a real model.
SYNONYMS = [
    "stream streams streaming streamed live broadcast twitch",
    "boss manager supervisor",
    "mod mods moderator moderators",
    "fly flying flight trip travel holiday",
    "graphics gpu rtx",
    "night nights evening evenings",
    "edit edits editor editing",
    "emote emotes draws artist",
    "internet broadband provider wifi",
    "work works job employer office",
    "aiming goal target",
    "language written typescript code",
    "called name named",
    "energy glytch sponsor sponsorship",
    "ad ads advert",
    "bed sleep midnight",
    "gym workout",
    "coffee flat whites espresso",
    "mum mom mother",
    "pay pays paid charge charges",
]
STOPWORDS = frozenset("a an the and or of to in on at for with by is are was be it i me my you your what who when "
                      "where why how do does did am s that this from his her has have j".split())
_CONCEPT = {w: i for i, group in enumerate(SYNONYMS) for w in group.split()}


def concept_vector(text: str, dims: int = 256) -> list[float]:
    vec = [0.0] * dims
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOPWORDS:
            continue
        slot = _CONCEPT.get(w)
        idx = slot if slot is not None else len(SYNONYMS) + zlib.crc32(w.encode()) % (dims - len(SYNONYMS))
        vec[idx] += 1.0
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def concept_embedder() -> Embedder:
    def handler(req: httpx.Request) -> httpx.Response:
        texts = json.loads(req.content)["input"]
        return httpx.Response(200, json={"embeddings": [concept_vector(t) for t in texts]})
    return Embedder("http://ollama", "stand-in", dims=256, http=httpx.Client(transport=httpx.MockTransport(handler)))


class Clock:
    def __init__(self, t: float = START):
        self.t = t

    def __call__(self) -> float:
        return self.t


# ---- the run -----------------------------------------------------------------------------------------------------
def build(embedder: Embedder | None, *, link_changes: bool = True, skip=()) -> tuple[Memory, dict[str, int], Clock]:
    """A memory with every fact, learned in order. ``link_changes``: the new version of a changed fact says which one
    it replaces (what the extractor does when it spots it); False: both stay live, as when it doesn't."""
    clock = Clock()
    mem = Memory(Storage(":memory:"), user="J", embedder=embedder, clock=clock)
    ids: dict[str, int] = {}
    for key, statement, subject, context in FACTS:
        if key in skip:
            continue
        clock.t += 1.5 * DAY
        old = CHANGED.get(key)
        for _ in range(4 if old else 0):
            clock.t += 0.25 * DAY
            mem.recall(ASKED_BEFORE[key], k=6)
        fact = mem.add(statement, subject=subject, context=context, importance=3,
                       contradicts=[ids[old]] if old and link_changes else None)
        ids[key] = fact["id"]
    clock.t += DAY
    return mem, ids, clock


def score(mem: Memory, ids: dict[str, int], k: int = 3) -> dict:
    by_id = {v: key for key, v in ids.items()}
    hits, misses, wrong, stale, newest = 0, [], [], [], 0
    for q, want in QUESTIONS:
        got = [by_id[f["id"]] for f in mem.recall(q, k=k, mark=False)]
        if want & set(got):
            hits += 1
        else:
            misses.append((q, got))
        olds = {CHANGED[w] for w in want if w in CHANGED}
        if olds & set(got):
            stale.append((q, got))
        if olds:
            ranks = [i for i, g in enumerate(got) if g in want]
            old_ranks = [i for i, g in enumerate(got) if g in olds]
            newest += bool(ranks) and (not old_ranks or ranks[0] < old_ranks[0])
    for q in NEGATIVES:
        got = [by_id[f["id"]] for f in mem.recall(q, k=k, mark=False)]
        if got:
            wrong.append((q, got))
    changed_qs = [q for q, want in QUESTIONS if want & set(CHANGED)]
    return {"recall@3": hits / len(QUESTIONS), "wrong_recall": len(wrong) / len(NEGATIVES),
            "old_answer": len(stale) / len(changed_qs), "newest_first": newest / len(changed_qs),
            "misses": misses, "wrong": wrong, "stale": stale}


def changes(embedder: Embedder | None) -> dict:
    """Is the old fact retired? Two chances: the extractor, shown the facts J's words recall (``Memory.extract`` asks
    for 20), names the one that changed; or the weekly tidy-up (rules only, no model here) asks J about the pair."""
    mem, ids, _ = build(embedder, skip=set(CHANGED))
    seen = sum(ids[old] in {f["id"] for f in mem.recall(CHANGE_SAID[new][-1500:], k=20, mark=False)}
               for new, old in CHANGED.items())
    mem, ids, _ = build(embedder, link_changes=False)
    mem.cleanup(use_model=False)
    asked = {frozenset((c["a"]["id"], c["b"]["id"])) for c in mem.confirms()}
    tidy = sum(frozenset((ids[new], ids[old])) in asked for new, old in CHANGED.items())
    return {"extractor_sees_old": seen / len(CHANGED), "tidy_up_asks": tidy / len(CHANGED)}


def _embedder(emb):
    return concept_embedder() if emb == "concept" else emb


def run(embedders: dict[str, Embedder | None] | None = None) -> dict[str, dict]:
    embedders = embedders if embedders is not None else {"words only": None, "stand-in embeddings": "concept"}
    out = {}
    for name, emb in embedders.items():
        for linked in (True, False):
            mem, ids, _ = build(_embedder(emb), link_changes=linked)
            out[f"{name}{'' if linked else ', change not linked'}"] = score(mem, ids)
        out[f"{name}, change not linked"].update(changes(_embedder(emb)))
    return out


def table(results: dict[str, dict]) -> str:
    rows = ["| setup | recall@3 | wrong recall | old answer shown | newest first |", "|---|---|---|---|---|"]
    for name, r in results.items():
        rows.append(f"| {name} | {r['recall@3']:.0%} | {r['wrong_recall']:.0%} | {r['old_answer']:.0%} | "
                    f"{r['newest_first']:.0%} |")
    rows.append("")
    for name, r in results.items():
        if "extractor_sees_old" in r:
            rows.append(f"{name.split(',')[0]}: when J says a fact changed, the extractor is shown the old one "
                        f"{r['extractor_sees_old']:.0%} of the time; the weekly tidy-up asks about the pair "
                        f"{r['tidy_up_asks']:.0%} of the time.")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ollama", default="", help="also run with this embedding model from Ollama (e.g. embeddinggemma)")
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("--verbose", "-v", action="store_true", help="list every miss and every wrong recall")
    args = ap.parse_args(argv)
    embedders: dict = {"words only": None, "stand-in embeddings": "concept"}
    if args.ollama:
        embedders[args.ollama] = Embedder(args.url, args.ollama, 256)
    results = run(embedders)
    print(table(results))
    if args.verbose:
        for name, r in results.items():
            print(f"\n## {name}")
            for q, got in r["misses"]:
                print(f"  miss   {q!r}: {got}")
            for q, got in r["wrong"]:
                print(f"  wrong  {q!r}: {got}")
            for q, got in r["stale"]:
                print(f"  old    {q!r}: {got}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
