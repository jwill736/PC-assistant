"""SQLite persistence: activity log, tasks, notes, conversation log, jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    start REAL NOT NULL,
    end REAL NOT NULL,
    app TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_start ON activity(start);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT 'work',
    priority INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'open',
    due TEXT,
    created REAL NOT NULL,
    done_at REAL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    created REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    source TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    cwd TEXT,
    status TEXT NOT NULL,
    created REAL NOT NULL,
    finished REAL,
    output TEXT
);
"""


# The second brain's index: everything you told it (notes, tasks, the conversation), searchable by word.
# Triggers keep it in step with the tables, so nothing has to remember to index.
MEMORY_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory USING fts5(kind UNINDEXED, ref UNINDEXED, ts UNINDEXED, text,
                                                     tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS memory_note AFTER INSERT ON notes BEGIN
    INSERT INTO memory(kind, ref, ts, text) VALUES ('note', new.id, new.created, new.text); END;
CREATE TRIGGER IF NOT EXISTS memory_task AFTER INSERT ON tasks BEGIN
    INSERT INTO memory(kind, ref, ts, text) VALUES ('task', new.id, new.created, new.title); END;
CREATE TRIGGER IF NOT EXISTS memory_log AFTER INSERT ON log BEGIN
    INSERT INTO memory(kind, ref, ts, text)
    VALUES (CASE new.role WHEN 'user' THEN 'said' ELSE 'replied' END, new.id, new.ts, new.text); END;
"""
STOPWORDS = frozenset("""a about am an and any anything are as at be did do does for from have how i i'd i'm in is it
    its me my note notes of on or our remember said say says tell that the this to told us was we were what when where
    which who why with you your know knew""".split())


class Storage:
    """Thread-safe wrapper; every integration thread shares one connection."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            # Jobs that were running when the app last exited never finished.
            self._db.execute(
                "UPDATE jobs SET status='interrupted' WHERE status IN ('queued','running')"
            )
            self.fts = self._init_memory()
            self._db.commit()

    def _init_memory(self) -> bool:
        """FTS5 ships with Python's SQLite on Windows, macOS and Linux; without it, search falls back to LIKE."""
        try:
            fresh = not self._db.execute("SELECT 1 FROM sqlite_master WHERE name='memory'").fetchone()
            self._db.executescript(MEMORY_SCHEMA)
        except sqlite3.OperationalError:
            return False
        if fresh:  # index what an older install already has
            self._db.executescript("""
                INSERT INTO memory(kind, ref, ts, text) SELECT 'note', id, created, text FROM notes;
                INSERT INTO memory(kind, ref, ts, text) SELECT 'task', id, created, title FROM tasks;
                INSERT INTO memory(kind, ref, ts, text)
                    SELECT CASE role WHEN 'user' THEN 'said' ELSE 'replied' END, id, ts, text FROM log;""")
        return True

    # ---- memory (the second brain) --------------------------------------
    def search_memory(self, query: str, limit: int = 8, before: float | None = None) -> list[dict]:
        """Best matches for the words in ``query``: notes first, then tasks, then conversation.
        ``before`` hides rows newer than that (the question being asked right now is logged too)."""
        terms = [w for w in _words(query) if w not in STOPWORDS]
        terms += [v for t in terms for v in _spellings(t) if v not in terms]  # colour / color
        before = before if before is not None else time.time() + 1
        if not terms:  # "what are my notes": the latest ones
            return self.query("SELECT 'note' AS kind, id AS ref, created AS ts, text FROM notes WHERE created < ? "
                              "ORDER BY created DESC LIMIT ?", (before, limit))
        if self.fts:
            match = " OR ".join(f'"{t}"*' if len(t) > 3 else f'"{t}"' for t in terms)
            try:
                return self.query(
                    "SELECT kind, ref, ts, text FROM memory WHERE memory MATCH ? AND ts < ? "
                    "ORDER BY bm25(memory) * CASE kind WHEN 'note' THEN 2.0 WHEN 'task' THEN 1.5 "
                    "WHEN 'said' THEN 1.2 ELSE 1.0 END, ts DESC LIMIT ?", (match, before, limit))
            except sqlite3.OperationalError:
                pass
        like = " OR ".join(["text LIKE ?"] * len(terms))
        args = tuple(f"%{t}%" for t in terms)
        return self.query(
            f"SELECT * FROM (SELECT 'note' AS kind, id AS ref, created AS ts, text FROM notes WHERE {like} "
            f"UNION ALL SELECT 'task', id, created, title FROM tasks WHERE {like.replace('text', 'title')} "
            f"UNION ALL SELECT CASE role WHEN 'user' THEN 'said' ELSE 'replied' END, id, ts, text FROM log WHERE {like}) "
            "WHERE ts < ? ORDER BY CASE kind WHEN 'note' THEN 0 WHEN 'task' THEN 1 ELSE 2 END, ts DESC LIMIT ?",
            args * 3 + (before, limit))

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    # ---- activity -------------------------------------------------------
    def add_activity(self, start: float, end: float, app: str, title: str, category: str) -> None:
        self.execute(
            "INSERT INTO activity(start,end,app,title,category) VALUES (?,?,?,?,?)",
            (start, end, app, title[:300], category),
        )

    def activity_between(self, start: float, end: float) -> list[dict]:
        return self.query(
            "SELECT * FROM activity WHERE end > ? AND start < ? ORDER BY start", (start, end)
        )

    # ---- tasks ----------------------------------------------------------
    def add_task(self, title: str, profile: str = "work", priority: int = 2, due: str | None = None) -> dict:
        cur = self.execute(
            "INSERT INTO tasks(title,profile,priority,due,created) VALUES (?,?,?,?,?)",
            (title.strip(), profile, int(priority), due, time.time()),
        )
        return self.query("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,))[0]

    def list_tasks(self, status: str | None = "open") -> list[dict]:
        if status:
            return self.query(
                "SELECT * FROM tasks WHERE status=? ORDER BY priority ASC, COALESCE(due,'9999') ASC, created ASC",
                (status,),
            )
        return self.query("SELECT * FROM tasks ORDER BY status DESC, priority ASC, created ASC")

    def update_task(self, task_id: int, **fields: Any) -> dict | None:
        allowed = {"title", "profile", "priority", "status", "due"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if sets.get("status") == "done":
            sets["done_at"] = time.time()
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*sets.values(), task_id))
        rows = self.query("SELECT * FROM tasks WHERE id=?", (task_id,))
        return rows[0] if rows else None

    def tasks_done_between(self, start: float, end: float) -> list[dict]:
        return self.query(
            "SELECT * FROM tasks WHERE status='done' AND done_at BETWEEN ? AND ? ORDER BY done_at",
            (start, end),
        )

    # ---- notes ----------------------------------------------------------
    def add_note(self, text: str) -> dict:
        cur = self.execute("INSERT INTO notes(text,created) VALUES (?,?)", (text.strip(), time.time()))
        return {"id": cur.lastrowid, "text": text.strip()}

    def list_notes(self, limit: int = 20) -> list[dict]:
        return self.query("SELECT * FROM notes ORDER BY created DESC LIMIT ?", (limit,))

    # ---- conversation log ----------------------------------------------
    def log(self, role: str, text: str, source: str = "text") -> None:
        self.execute("INSERT INTO log(ts,role,source,text) VALUES (?,?,?,?)", (time.time(), role, source, text))

    def recent_log(self, limit: int = 50) -> list[dict]:
        rows = self.query("SELECT * FROM log ORDER BY ts DESC LIMIT ?", (limit,))
        return list(reversed(rows))

    # ---- jobs -----------------------------------------------------------
    def add_job(self, kind: str, title: str, prompt: str, cwd: str | None) -> int:
        cur = self.execute(
            "INSERT INTO jobs(kind,title,prompt,cwd,status,created) VALUES (?,?,?,?,?,?)",
            (kind, title, prompt, cwd, "queued", time.time()),
        )
        return int(cur.lastrowid)

    def update_job(self, job_id: int, status: str, output: str | None = None) -> None:
        finished = time.time() if status in {"done", "failed", "cancelled"} else None
        self.execute(
            "UPDATE jobs SET status=?, output=COALESCE(?, output), finished=COALESCE(?, finished) WHERE id=?",
            (status, output, finished, job_id),
        )

    def list_jobs(self, limit: int = 20) -> list[dict]:
        return self.query("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))

    def get_job(self, job_id: int) -> dict | None:
        rows = self.query("SELECT * FROM jobs WHERE id=?", (job_id,))
        return rows[0] if rows else None


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


def _words(text: str) -> list[str]:
    import re

    return [w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 1]


def _spellings(word: str) -> list[str]:
    """US/UK variants, so "colors" finds "colours" (the index can't know they're the same word)."""
    out = []
    if "our" in word:
        out.append(word.replace("our", "or"))
    elif word.endswith(("or", "ors")) and len(word) > 4:
        out.append(word[:word.rindex("or")] + "our" + word[word.rindex("or") + 2:])
    if word.endswith("re") and len(word) > 4:
        out.append(word[:-2] + "er")
    return out

