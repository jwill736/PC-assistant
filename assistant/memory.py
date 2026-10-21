"""Long-term memory: what Vesper knows about J and J's world, learned from what J says.

Three layers, all in the same SQLite file as everything else:

- **Core profile** (``core_blocks``): a few short paragraphs (who J is, how J likes things, what's going on now),
  always given to the model. Rebuilt every night from the facts; once J edits a block in the HUD, Vesper leaves it.
- **Facts** (``facts`` + ``entities``): single statements ("Dana Ortiz is J's client at Acme"), each with who it's
  about, how important it is, where it came from and when it was learned. A fact that changes is never deleted:
  the old one points at its replacement (``superseded_by``), so "what did I used to…" still works.
- **Episodes**: the existing conversation log.

Facts are found by words (FTS5) and by meaning (embeddings from Ollama, e.g. ``embeddinggemma``), fused by rank,
nudged by importance and recency. With no embedding model it is words only; with no model at all, only what J
says with "remember that…" is kept.
"""

from __future__ import annotations

import json
import logging
import math
import re
import struct
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Callable

import httpx

from .privacy import mentions_secret

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(kind, name));
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY, statement TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fact',
    subject_id INTEGER, object_id INTEGER, predicate TEXT, context TEXT NOT NULL DEFAULT '',
    importance INTEGER NOT NULL DEFAULT 3, confidence REAL NOT NULL DEFAULT 0.8, source TEXT NOT NULL DEFAULT 'said',
    source_ref TEXT, valid_from REAL, valid_to REAL, learned_at REAL NOT NULL, superseded_by INTEGER,
    last_used REAL, use_count INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0,
    private INTEGER NOT NULL DEFAULT 0, vector BLOB);
CREATE INDEX IF NOT EXISTS facts_live ON facts(superseded_by);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(statement, context, content='facts', content_rowid='id',
                                                         tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, statement, context) VALUES (new.id, new.statement, new.context); END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, statement, context) VALUES ('delete', old.id, old.statement, old.context); END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF statement, context ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, statement, context) VALUES ('delete', old.id, old.statement, old.context);
    INSERT INTO facts_fts(rowid, statement, context) VALUES (new.id, new.statement, new.context); END;
CREATE TABLE IF NOT EXISTS core_blocks (label TEXT PRIMARY KEY, text TEXT NOT NULL, updated REAL NOT NULL,
                                        edited_by TEXT NOT NULL DEFAULT 'vesper');
CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT);
"""

KINDS = ["fact", "preference", "relationship", "commitment", "event", "goal", "instruction"]
BLOCKS = {"profile": "Who J is: work, stream, people, places.",
          "preferences": "How J likes answers and actions done.",
          "now": "What's going on this week: projects, plans, open commitments."}
BLOCK_CHARS = 600
STOP = frozenset("a an the and or but of to in on at for with from by is are was were be been it its this that these "
                 "those i me my you your we our he she they them his her their what who when where why how do does "
                 "did about remember know tell".split())
SECRET = re.compile(r"\b(?:password|passcode|pin|ssn|social security|credit card|cvv|api key|token)\b|\b\d{13,19}\b", re.I)

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "maxItems": 12, "items": {
        "type": "object",
        "properties": {
            "statement": {"type": "string"},
            "kind": {"type": "string", "enum": KINDS},
            "subject": {"type": "string"},
            "subject_kind": {"type": "string", "enum": ["self", "person", "org", "project", "place", "app", "game", "topic"]},
            "context": {"type": "string"},
            "importance": {"type": "integer", "minimum": 1, "maximum": 5},
            "contradicts": {"type": "array", "items": {"type": "integer"}},
            "duplicate_of": {"type": ["integer", "null"]},
        },
        "required": ["statement", "kind", "subject", "subject_kind", "context", "importance"]}}},
    "required": ["facts"],
}

EXTRACT_PROMPT = """You keep {assistant}'s long-term memory about {user} (the user). Today is {today}.
From the NEW CONVERSATION (only {user}'s own words), extract things worth knowing in a week or a year: facts about
{user} and the people, projects, companies and places in {user}'s life; {user}'s preferences and standing
instructions; commitments (who owes what to whom, by when); plans and goals; relationships.
Do NOT store: small talk, one-off commands ("open discord", "what's the weather"), anything already in EXISTING
FACTS unless it changed, secrets (passwords, codes, card numbers), or health details unless asked to remember them.
Write each statement self-contained, in the third person, naming {user} and people by name, with absolute dates.
If a new fact changes an existing one, list the old id in "contradicts"; if it repeats one, set "duplicate_of".
An empty list is the usual answer.

CORE PROFILE:
{core}

EXISTING FACTS (id: statement):
{existing}

NEW CONVERSATION:
{turns}"""

PROFILE_SCHEMA = {"type": "object", "properties": {k: {"type": "string"} for k in BLOCKS}, "required": list(BLOCKS)}
PROFILE_PROMPT = """Rewrite {user}'s core profile for {assistant} from the facts below: three short blocks, each at
most {chars} characters, plain sentences, most important first, no speculation.
- profile: {profile}
- preferences: {preferences}
- now: {now}
Keep anything in the CURRENT blocks that the facts don't contradict.

CURRENT:
{current}

FACTS (importance, date, statement):
{facts}"""


def _fts_query(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in STOP and len(w) > 1]
    return " OR ".join(f'"{w}"' for w in words[:12])


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embedder:
    """Ollama /api/embed, truncated to ``dims`` and normalised. ``available`` is False until a call works."""

    PREFIX = {"embeddinggemma": ("task: search result | query: ", "title: none | text: "),
              "nomic-embed-text": ("search_query: ", "search_document: "),
              "qwen3-embedding": ("Instruct: Given a question, retrieve facts that answer it\nQuery: ", "")}

    def __init__(self, url: str = "http://127.0.0.1:11434", model: str = "embeddinggemma", dims: int = 256,
                 http: httpx.Client | None = None):
        self.url, self.model, self.dims = url.rstrip("/"), model, dims
        self.http = http or httpx.Client(timeout=30)
        self.available: bool | None = None
        self.error = ""

    def _prefix(self, query: bool) -> str:
        for name, (q, d) in self.PREFIX.items():
            if self.model.startswith(name):
                return q if query else d
        return ""

    def embed(self, texts: list[str], query: bool = False) -> list[list[float]] | None:
        if not texts or self.available is False:
            return None
        try:
            r = self.http.post(f"{self.url}/api/embed", json={
                "model": self.model, "input": [self._prefix(query) + t for t in texts], "keep_alive": "30m"})
            if r.status_code != 200:
                raise ValueError(r.text[:200])
            vecs = r.json()["embeddings"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self.available, self.error = False, f"{type(exc).__name__}: {exc}"[:200]
            log.info("memory: no embeddings (%s); words only", self.error)
            return None
        self.available = True
        return [_norm(v[: self.dims]) for v in vecs]


class Memory:
    def __init__(self, storage, *, user: str = "J", assistant: str = "Vesper", tz=None,
                 think: Callable[..., object] | None = None, embedder: Embedder | None = None,
                 clock: Callable[[], float] = time.time):
        self.storage, self.user, self.assistant, self.tz = storage, user, assistant, tz
        self.think = think or (lambda *a, **k: None)
        self.embedder = embedder
        self.clock = clock
        self._lock = threading.RLock()
        self.learned: list[dict] = []  # what was learned lately (the HUD shows it)
        with storage._lock:
            storage._db.executescript(SCHEMA)
            storage._db.commit()

    # ---- writing ---------------------------------------------------------------------------------------
    def _entity(self, name: str, kind: str = "person") -> int | None:
        name = (name or "").strip()
        if not name:
            return None
        if name.lower() in ("j", "me", "i", "myself", self.user.lower(), "user", "the user"):
            name, kind = self.user, "self"
        now = self.clock()
        rows = self.storage.query("SELECT id, aliases FROM entities WHERE lower(name) = ?", (name.lower(),))
        if rows:
            return rows[0]["id"]
        for r in self.storage.query("SELECT id, aliases FROM entities"):
            if name.lower() in [a.lower() for a in json.loads(r["aliases"] or "[]")]:
                return r["id"]
        cur = self.storage.execute("INSERT OR IGNORE INTO entities(kind, name, created, updated) VALUES (?,?,?,?)",
                                   (kind, name, now, now))
        return cur.lastrowid or self.storage.query("SELECT id FROM entities WHERE kind=? AND name=?", (kind, name))[0]["id"]

    def add(self, statement: str, *, kind: str = "fact", subject: str = "", subject_kind: str = "person",
            context: str = "", importance: int = 3, source: str = "said", source_ref: str | None = None,
            contradicts: list[int] | None = None, pinned: bool = False, private: bool = False) -> dict | None:
        statement = re.sub(r"\s+", " ", (statement or "").strip())
        if len(statement) < 6 or SECRET.search(statement) or mentions_secret(statement):  # never a password or a key
            return None
        with self._lock:
            dup = self.storage.query("SELECT id FROM facts WHERE lower(statement) = ? AND superseded_by IS NULL",
                                     (statement.lower(),))
            if dup:
                self.storage.execute("UPDATE facts SET use_count = use_count + 1, last_used = ? WHERE id = ?",
                                     (self.clock(), dup[0]["id"]))
                return None
            vec = None
            if self.embedder is not None:
                got = self.embedder.embed([f"{statement} {context}".strip()])
                vec = _pack(got[0]) if got else None
            now = self.clock()
            cur = self.storage.execute(
                "INSERT INTO facts(statement, kind, subject_id, context, importance, source, source_ref, learned_at,"
                " pinned, private, vector) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (statement, kind if kind in KINDS else "fact", self._entity(subject or self.user, subject_kind), context,
                 max(1, min(5, int(importance or 3))), source, source_ref, now, int(pinned), int(private), vec))
            fid = cur.lastrowid
            for old in contradicts or []:
                self.storage.execute("UPDATE facts SET superseded_by = ?, valid_to = ? WHERE id = ? AND superseded_by IS NULL",
                                     (fid, now, int(old)))
            fact = {"id": fid, "statement": statement, "kind": kind, "importance": importance, "source": source,
                    "learned_at": now, "replaced": list(contradicts or [])}
            self.learned = ([fact] + self.learned)[:30]
            return fact

    def forget(self, fact_id: int) -> bool:
        """J asked: gone for real (the one exception to never deleting)."""
        cur = self.storage.execute("DELETE FROM facts WHERE id = ?", (int(fact_id),))
        self.learned = [f for f in self.learned if f["id"] != fact_id]
        return cur.rowcount > 0

    def forget_matching(self, text: str) -> list[dict]:
        hits = self.recall(text, k=3, mark=False)
        strong = [h for h in hits if h["score"] >= hits[0]["score"] * 0.8] if hits else []
        for h in strong[:1]:
            self.forget(h["id"])
        return strong[:1]

    def pin(self, fact_id: int, pinned: bool = True) -> bool:
        return self.storage.execute("UPDATE facts SET pinned = ?, importance = MAX(importance, ?) WHERE id = ?",
                                    (int(pinned), 5 if pinned else 1, int(fact_id))).rowcount > 0

    def edit(self, fact_id: int, statement: str) -> dict | None:
        """J corrected a fact: the new wording replaces it (the old one is kept, marked replaced)."""
        old = self.storage.query("SELECT * FROM facts WHERE id = ?", (int(fact_id),))
        if not old:
            return None
        o = old[0]
        return self.add(statement, kind=o["kind"], context=o["context"], importance=max(o["importance"], 4),
                        source="edited", contradicts=[o["id"]], pinned=bool(o["pinned"]))

    # ---- learning from conversations ---------------------------------------------------------------------
    def extract(self, turns: list[dict], source_ref: str | None = None) -> list[dict]:
        """``turns``: [{"role": "user", "text": ...}] from one conversation. Asks the model; returns what was learned."""
        said = [t["text"] for t in turns if t.get("role") == "user" and len(t.get("text", "")) > 12]
        if not said:
            return []
        existing = self.recall(" ".join(said)[-1500:], k=20, mark=False)
        prompt = EXTRACT_PROMPT.format(
            assistant=self.assistant, user=self.user, today=datetime.now(self.tz).strftime("%A %Y-%m-%d"),
            core=self.core_text() or "(empty)",
            existing="\n".join(f"{f['id']}: {f['statement']}" for f in existing) or "(none)",
            turns="\n".join(f"{self.user}: {t}" for t in said)[-6000:])
        out = self.think(prompt, schema=EXTRACT_SCHEMA, max_tokens=1500)
        if not isinstance(out, dict):
            return []
        learned = []
        known = {f["id"] for f in existing}
        for item in (out.get("facts") or [])[:12]:
            try:
                if item.get("duplicate_of") or int(item.get("importance", 3)) < 2:
                    continue
                st = str(item["statement"])
                if not re.search(re.escape(self.user) + r"\b|[A-Z][a-z]+", st):  # "he likes it" names nobody
                    continue
                fact = self.add(st, kind=item.get("kind", "fact"), subject=item.get("subject", ""),
                                subject_kind=item.get("subject_kind", "person"), context=item.get("context", ""),
                                importance=int(item.get("importance", 3)), source="said", source_ref=source_ref,
                                contradicts=[int(i) for i in item.get("contradicts") or [] if int(i) in known])
                if fact:
                    learned.append(fact)
            except (KeyError, TypeError, ValueError):
                continue
        return learned

    # ---- finding -----------------------------------------------------------------------------------------
    def recall(self, query: str, k: int = 6, *, mark: bool = True, include_private: bool = True) -> list[dict]:
        """The live facts most relevant to ``query``: words + meaning, fused by rank, nudged by importance/recency."""
        scores: dict[int, float] = defaultdict(float)
        q = _fts_query(query)
        if q:
            try:
                for rank, r in enumerate(self.storage.query(
                        "SELECT rowid FROM facts_fts WHERE facts_fts MATCH ? ORDER BY bm25(facts_fts, 1.0, 0.4) LIMIT 30", (q,))):
                    scores[r["rowid"]] += 1 / (60 + rank)
            except Exception:  # an odd query string
                pass
        if self.embedder is not None and self.embedder.available is not False and query.strip():
            qv = self.embedder.embed([query], query=True)
            if qv:
                sims = []
                for r in self.storage.query("SELECT id, vector FROM facts WHERE superseded_by IS NULL AND vector IS NOT NULL"):
                    v = _unpack(r["vector"])
                    sims.append((sum(a * b for a, b in zip(qv[0], v)), r["id"]))
                sims.sort(reverse=True)
                for rank, (sim, fid) in enumerate(sims[:30]):
                    if sim > 0.35:
                        scores[fid] += 1 / (60 + rank)
        # people and projects named in the question
        for r in self.storage.query("SELECT id, name FROM entities WHERE kind != 'self'"):
            if r["name"].lower() in query.lower():
                for rank, f in enumerate(self.storage.query(
                        "SELECT id FROM facts WHERE subject_id = ? AND superseded_by IS NULL ORDER BY importance DESC LIMIT 10",
                        (r["id"],))):
                    scores[f["id"]] += 0.7 / (60 + rank)
        if not scores:
            return []
        ids = ",".join(str(i) for i in scores)
        rows = self.storage.query(f"SELECT * FROM facts WHERE id IN ({ids}) AND superseded_by IS NULL"
                                  + ("" if include_private else " AND private = 0"))
        now = self.clock()
        out = []
        for f in rows:
            hours = (now - (f["last_used"] or f["learned_at"])) / 3600
            s = scores[f["id"]] * (1 + 0.15 * f["importance"] / 5 + 0.10 * 0.995 ** max(0, hours) + 0.2 * f["pinned"])
            out.append({"id": f["id"], "statement": f["statement"], "kind": f["kind"], "importance": f["importance"],
                        "learned_at": f["learned_at"], "source": f["source"], "pinned": bool(f["pinned"]),
                        "private": bool(f["private"]), "score": round(s * 1000, 3)})
        out.sort(key=lambda x: x["score"], reverse=True)
        top = out[:k]
        if mark and top:
            self.storage.execute(f"UPDATE facts SET last_used = ?, use_count = use_count + 1 WHERE id IN "
                                 f"({','.join(str(f['id']) for f in top)})", (now,))
        return top

    def context_for(self, text: str, k: int = 6, include_private: bool = True) -> str:
        """The lines given to the model with each question: the core profile and the relevant facts."""
        parts = []
        core = self.core_text()
        if core:
            parts.append(f"What you know about {self.user}:\n{core}")
        facts = self.recall(text, k=k, include_private=include_private)
        if facts:
            parts.append("Facts that may help (use only if relevant; never mention this list):\n" + "\n".join(
                f"- [{datetime.fromtimestamp(f['learned_at'], self.tz):%Y-%m-%d}] {f['statement']}" for f in facts))
        return "\n\n".join(parts)

    def facts(self, query: str = "", limit: int = 50, include_replaced: bool = False) -> list[dict]:
        if query.strip():
            return self.recall(query, k=limit, mark=False)
        where = "" if include_replaced else "WHERE superseded_by IS NULL"
        return [dict(r, vector=None) for r in self.storage.query(
            f"SELECT id, statement, kind, importance, source, learned_at, pinned, private, superseded_by FROM facts {where} "
            "ORDER BY pinned DESC, learned_at DESC LIMIT ?", (limit,))]

    def count(self) -> int:
        return self.storage.query("SELECT COUNT(*) AS n FROM facts WHERE superseded_by IS NULL")[0]["n"]

    # ---- the core profile ----------------------------------------------------------------------------------
    def core(self) -> dict:
        return {r["label"]: {"text": r["text"], "updated": r["updated"], "edited_by": r["edited_by"]}
                for r in self.storage.query("SELECT * FROM core_blocks")}

    def core_text(self) -> str:
        blocks = self.core()
        return "\n".join(f"{label.title()}: {blocks[label]['text']}" for label in BLOCKS
                         if label in blocks and blocks[label]["text"].strip())

    def set_block(self, label: str, text: str, by: str = "J") -> dict:
        if label not in BLOCKS:
            return {"ok": False, "error": f"No block called {label}."}
        self.storage.execute("INSERT INTO core_blocks(label, text, updated, edited_by) VALUES (?,?,?,?) "
                             "ON CONFLICT(label) DO UPDATE SET text=excluded.text, updated=excluded.updated, "
                             "edited_by=excluded.edited_by", (label, text.strip()[: BLOCK_CHARS * 2], self.clock(), by))
        return {"ok": True, "core": self.core()}

    def rebuild_core(self) -> dict:
        """Nightly: rewrite the blocks J hasn't edited from the important facts."""
        facts = self.storage.query("SELECT statement, importance, learned_at FROM facts WHERE superseded_by IS NULL "
                                   "ORDER BY pinned DESC, importance DESC, use_count DESC, learned_at DESC LIMIT 60")
        if not facts:
            return {"ok": False, "error": "Nothing learned yet."}
        current = self.core()
        out = self.think(PROFILE_PROMPT.format(
            user=self.user, assistant=self.assistant, chars=BLOCK_CHARS, **BLOCKS,
            current="\n".join(f"{k}: {v['text']}" for k, v in current.items()) or "(empty)",
            facts="\n".join(f"{f['importance']}, {datetime.fromtimestamp(f['learned_at'], self.tz):%Y-%m-%d}, "
                            f"{f['statement']}" for f in facts)), schema=PROFILE_SCHEMA, max_tokens=900)
        if not isinstance(out, dict):
            return {"ok": False, "error": "No model to write it with."}
        changed = []
        for label in BLOCKS:
            text = str(out.get(label) or "").strip()
            if text and (current.get(label) or {}).get("edited_by") != "J":
                self.set_block(label, text[:BLOCK_CHARS], by="vesper")
                changed.append(label)
        return {"ok": True, "changed": changed}

    def meta(self, key: str, value: str | None = None) -> str | None:
        if value is None:
            r = self.storage.query("SELECT value FROM memory_meta WHERE key = ?", (key,))
            return r[0]["value"] if r else None
        self.storage.execute("INSERT OR REPLACE INTO memory_meta(key, value) VALUES (?, ?)", (key, value))
        return value

    def embed_missing(self, batch: int = 32) -> int:
        """Give facts learned while the embedding model was away their vectors."""
        if self.embedder is None or self.embedder.available is False:
            return 0
        rows = self.storage.query("SELECT id, statement, context FROM facts WHERE vector IS NULL AND superseded_by IS NULL "
                                  "LIMIT ?", (batch,))
        if not rows:
            return 0
        vecs = self.embedder.embed([f"{r['statement']} {r['context']}".strip() for r in rows])
        if not vecs:
            return 0
        for r, v in zip(rows, vecs):
            self.storage.execute("UPDATE facts SET vector = ? WHERE id = ?", (_pack(v), r["id"]))
        return len(rows)
