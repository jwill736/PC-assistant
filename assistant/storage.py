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
            self._db.commit()

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
