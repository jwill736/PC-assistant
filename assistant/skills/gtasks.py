"""Google Tasks, two-way: Vesper's tasks and a "Vesper · <area>" list in Google Tasks kept in step.

- Work tasks go to "Vesper · Work" in the work account, personal (and stream) ones to "Vesper · Personal" /
  "Vesper · Stream" in the personal account (skills.gtasks.accounts changes that). The lists are made when missing.
- New tasks, ticking one off, renaming and due dates go both ways. A task added in Google Tasks on the phone comes in.
- Both sides changed the same task since the last sync: the newer change wins (Google's "updated" time against when
  Vesper saw its own change).
- Every 5 minutes, and a few seconds after any change on Vesper's side. Each pair is remembered by Google's task id,
  so nothing is ever copied twice; a task deleted in Google Tasks is left alone here (and not sent back).

Needs the "Google Tasks" box on Google's page for each account (google.can); says which box when it's missing.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from datetime import datetime

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from ..integrations.google import TASKS_API, TASKS_SCOPE, GoogleError, _reason
from . import Skill
from .people import account_kind

DEBOUNCE_S = 3.0


def _hash(title: str, done: bool, due: str | None) -> str:
    return hashlib.sha1(f"{title.strip()}|{int(done)}|{due or ''}".encode()).hexdigest()[:16]


def _ts(rfc: str | None) -> float:
    if not rfc:
        return 0.0
    try:
        return datetime.fromisoformat(rfc.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def g_due(due: str | None) -> str | None:
    """Google keeps a date only, as midnight UTC."""
    return f"{due}T00:00:00.000Z" if due else None


def v_due(due: str | None) -> str | None:
    return due[:10] if due and re.match(r"\d{4}-\d\d-\d\d", due) else None


class GTasks(Skill):
    name, title = "gtasks", "Google Tasks"
    poll_s = 300
    delay_s = 40

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self.last: dict = {}
        self.error: str | None = None
        db = svc.storage
        db.execute("CREATE TABLE IF NOT EXISTS gtasks_lists (profile TEXT PRIMARY KEY, account TEXT NOT NULL, "
                   "list_id TEXT NOT NULL, title TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS gtasks_map (task_id INTEGER PRIMARY KEY, profile TEXT NOT NULL, "
                   "account TEXT NOT NULL, list_id TEXT NOT NULL, google_id TEXT NOT NULL UNIQUE, hash TEXT NOT NULL, "
                   "local_seen REAL NOT NULL, google_updated TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'linked')")
        db.execute("CREATE TABLE IF NOT EXISTS gtasks_seen (task_id INTEGER PRIMARY KEY, hash TEXT NOT NULL, ts REAL NOT NULL)")
        svc.bus.on(self._on_event)

    # ---- where each area's tasks go ---------------------------------------------------------------------------
    def plan(self) -> tuple[list[dict], list[str]]:
        """[{profile, account, title}] for each area with an account that allowed Google Tasks, and the problems."""
        g = self.svc.google
        if g is None or not g.configured or not g.emails():
            return [], ["No Google account is connected: Setup → Google accounts."]
        emails = g.emails()
        work = [e for e in emails if account_kind(e) == "work"]
        personal = [e for e in emails if account_kind(e) == "personal"]
        want = dict(self.setting("accounts") or {})
        profiles = list((self.svc.cfg.get("profiles") or {}).keys())
        if "personal" not in profiles:
            profiles.append("personal")
        labels = {k: (v or {}).get("label") or k.capitalize() for k, v in (self.svc.cfg.get("profiles") or {}).items()}
        out, problems, missing = [], [], []
        for p in profiles:
            choice = str(want.get(p) or ("work" if p == "work" else "personal")).lower()
            if choice in ("off", "none", "false"):
                continue
            pick = [e for e in emails if e.lower() == choice] if "@" in choice else \
                (work if choice == "work" else personal) or (personal if choice == "work" else work)
            if not pick:
                continue
            acct = pick[0]
            if not g.can(acct, TASKS_SCOPE):
                if acct not in missing:
                    missing.append(acct)
                continue
            out.append({"profile": p, "account": acct, "title": f"Vesper · {labels.get(p, p.capitalize())}"})
        if missing:
            problems.append(g.need(TASKS_SCOPE, "Syncing with Google Tasks", missing))
        return out, problems

    # ---- Google's side ---------------------------------------------------------------------------------------
    def _call(self, method: str, acct: str, path: str, **kw):
        resp = self.svc.google.request(method, acct, f"{TASKS_API}/{path}", **kw)
        if resp.status_code == 404:
            return None
        if resp.status_code not in (200, 201, 204):
            raise GoogleError(_reason(resp))
        return resp.json() if resp.content else {}

    def _list_id(self, p: dict) -> str:
        rows = self.svc.storage.query("SELECT * FROM gtasks_lists WHERE profile=? AND account=?", (p["profile"], p["account"]))
        if rows and self._call("GET", p["account"], f"users/@me/lists/{rows[0]['list_id']}") is not None:
            return rows[0]["list_id"]
        lists, page = [], None
        for _ in range(10):
            data = self._call("GET", p["account"], "users/@me/lists", params={"maxResults": 100, **({"pageToken": page} if page else {})}) or {}
            lists += data.get("items") or []
            page = data.get("nextPageToken")
            if not page:
                break
        found = next((x for x in lists if x.get("title") == p["title"]), None) or \
            self._call("POST", p["account"], "users/@me/lists", json={"title": p["title"]})
        self.svc.storage.execute("INSERT OR REPLACE INTO gtasks_lists(profile, account, list_id, title) VALUES (?,?,?,?)",
                                 (p["profile"], p["account"], found["id"], p["title"]))
        return found["id"]

    def _remote(self, acct: str, list_id: str) -> dict[str, dict]:
        out, page = {}, None
        for _ in range(20):
            params = {"showCompleted": "true", "showHidden": "true", "maxResults": 100}
            if page:
                params["pageToken"] = page
            data = self._call("GET", acct, f"lists/{list_id}/tasks", params=params) or {}
            for t in data.get("items") or []:
                if not t.get("deleted"):
                    out[t["id"]] = t
            page = data.get("nextPageToken")
            if not page:
                break
        return out

    # ---- the sync ------------------------------------------------------------------------------------------
    def sync(self) -> dict:
        with self._lock:
            started = self.clock()
            plan, problems = self.plan()
            counts = {"sent": 0, "brought_in": 0, "updated_here": 0, "updated_there": 0, "conflicts": 0, "unlinked": 0}
            lists, errors = [], list(problems)
            for p in plan:
                try:
                    n = self._sync_one(p, counts)
                    lists.append({**p, "tasks": n})
                except (GoogleError, KeyError, TypeError) as exc:
                    errors.append(f"{p['title']} ({p['account']}): {exc}")
            self.last = {"at": started, "lists": lists, "counts": counts, "errors": errors, "ok": not errors or bool(lists)}
            self.error = "; ".join(errors) or None
        if counts["brought_in"] or counts["updated_here"]:
            self._publish_tasks()
        self.publish()
        return {"ok": bool(lists) or not errors, **self.last}

    def _publish_tasks(self) -> None:
        self._quiet_until = self.clock() + DEBOUNCE_S + 1  # our own change: no sync loop
        self.svc.bus.publish("tasks", self.svc.storage.list_tasks())

    def _sync_one(self, p: dict, counts: dict) -> int:
        db, acct, prof = self.svc.storage, p["account"], p["profile"]
        list_id = self._list_id(p)
        remote = self._remote(acct, list_id)
        local = {t["id"]: t for t in db.list_tasks(None) if t["profile"] == prof}
        maps = {}
        for r in db.query("SELECT * FROM gtasks_map WHERE profile=? AND account=?", (prof, acct)):
            if r["list_id"] != list_id:  # the list was deleted and made again: send these again
                db.execute("DELETE FROM gtasks_map WHERE task_id=?", (r["task_id"],))
            else:
                maps[r["task_id"]] = r
        linked_g = {r["google_id"] for r in db.query("SELECT google_id FROM gtasks_map")}
        now = self.clock()
        for tid, m in maps.items():
            if m["state"] != "linked":
                continue
            t, r = local.get(tid), remote.get(m["google_id"])
            if t is None:  # gone here: gone there too
                if r is not None:
                    self._call("DELETE", acct, f"lists/{m['list_id']}/tasks/{m['google_id']}")
                db.execute("DELETE FROM gtasks_map WHERE task_id=?", (tid,))
                continue
            if r is None:  # deleted in Google Tasks: keep it here, never send it back
                db.execute("UPDATE gtasks_map SET state='gone' WHERE task_id=?", (tid,))
                counts["unlinked"] += 1
                continue
            lh = _hash(t["title"], t["status"] == "done", t.get("due"))
            here, there = lh != m["hash"], (r.get("updated") or "") != m["google_updated"]
            if here and there:
                counts["conflicts"] += 1
                seen = self._local_changed_at(t, m)
                there = _ts(r.get("updated")) > seen
                here = not there
            if here:
                r = self._push(acct, list_id, m["google_id"], t) or r
                counts["updated_there"] += 1
            elif there:
                self._pull(t, r)
                counts["updated_here"] += 1
                t = db.query("SELECT * FROM tasks WHERE id=?", (tid,))[0]
            if here or there:
                h = _hash(t["title"], t["status"] == "done", t.get("due"))
                db.execute("UPDATE gtasks_map SET hash=?, google_updated=?, local_seen=? WHERE task_id=?",
                           (h, r.get("updated") or "", now, tid))
                self._seen(tid, h)
        mapped_local = set(maps)
        free_remote = {gid: r for gid, r in remote.items() if gid not in linked_g}
        for tid, t in local.items():
            if tid in mapped_local or t["status"] != "open":
                continue
            twin = next((gid for gid, r in free_remote.items() if (r.get("title") or "").strip().lower() == t["title"].strip().lower()
                         and r.get("status") != "completed"), None)
            if twin:  # the same task already there (a lost link): pair them, don't copy
                r = free_remote.pop(twin)
            else:
                r = self._call("POST", acct, f"lists/{list_id}/tasks", json=self._body(t))
                counts["sent"] += 1
            self._link(tid, p, list_id, r, t)
        for gid, r in free_remote.items():
            title = (r.get("title") or "").strip()
            if not title or r.get("status") == "completed":
                continue
            t = db.add_task(title[:200], prof, 2, v_due(r.get("due")))
            self._link(t["id"], p, list_id, r, t)
            counts["brought_in"] += 1
        return db.query("SELECT COUNT(*) AS n FROM gtasks_map WHERE profile=? AND account=? AND state='linked'", (prof, acct))[0]["n"]

    def _body(self, t: dict) -> dict:
        body = {"title": t["title"], "status": "completed" if t["status"] == "done" else "needsAction",
                "notes": "From Vesper"}
        if t.get("due"):
            body["due"] = g_due(t["due"])
        return body

    def _push(self, acct: str, list_id: str, gid: str, t: dict) -> dict | None:
        body = {"title": t["title"], "status": "completed" if t["status"] == "done" else "needsAction",
                "due": g_due(t.get("due"))}
        if t["status"] != "done":
            body["completed"] = None
        return self._call("PATCH", acct, f"lists/{list_id}/tasks/{gid}", json=body)

    def _pull(self, t: dict, r: dict) -> None:
        fields = {"title": (r.get("title") or t["title"]).strip()[:200] or t["title"],
                  "status": "done" if r.get("status") == "completed" else "open", "due": v_due(r.get("due"))}
        if fields["status"] == t["status"]:
            fields.pop("status")  # keeps done_at when only the title changed
        self.svc.storage.update_task(t["id"], **fields)

    def _link(self, tid: int, p: dict, list_id: str, r: dict, t: dict) -> None:
        cur = self.svc.storage.query("SELECT * FROM tasks WHERE id=?", (tid,))[0]
        h = _hash(cur["title"], cur["status"] == "done", cur.get("due"))
        self.svc.storage.execute(
            "INSERT OR REPLACE INTO gtasks_map(task_id, profile, account, list_id, google_id, hash, local_seen, "
            "google_updated, state) VALUES (?,?,?,?,?,?,?,?, 'linked')",
            (tid, p["profile"], p["account"], list_id, r["id"], h, self.clock(), r.get("updated") or ""))
        self._seen(tid, h)

    def _seen(self, tid: int, h: str) -> None:
        """Both sides agree on this version now."""
        self.svc.storage.execute("INSERT OR REPLACE INTO gtasks_seen(task_id, hash, ts) VALUES (?,?,?)", (tid, h, self.clock()))

    def _local_changed_at(self, t: dict, m: dict) -> float:
        """When Vesper's side changed: when the change was first seen (a few seconds after it happened)."""
        if t["status"] == "done" and t.get("done_at"):
            return float(t["done_at"])
        rows = self.svc.storage.query("SELECT ts FROM gtasks_seen WHERE task_id=?", (t["id"],))
        return rows[0]["ts"] if rows else self.clock()

    # ---- on change -----------------------------------------------------------------------------------------
    _quiet_until = 0.0
    _running = False  # syncs on change once Vesper has started (not while it's only being built, as in a test)

    def start(self, runtime) -> None:
        self._running = True

    def _on_event(self, ev: dict) -> None:
        if ev.get("type") != "tasks" or self.clock() < self._quiet_until:
            return
        now = self.clock()
        for t in ev.get("data") or []:  # remember when each change was first seen (for "newest wins")
            if not isinstance(t, dict) or "id" not in t:
                continue
            h = _hash(t.get("title", ""), t.get("status") == "done", t.get("due"))
            self.svc.storage.execute("INSERT INTO gtasks_seen(task_id, hash, ts) VALUES (?,?,?) ON CONFLICT(task_id) DO "
                                     "UPDATE SET ts=CASE WHEN hash<>excluded.hash THEN excluded.ts ELSE ts END, hash=excluded.hash",
                                     (t["id"], h, now))
        if not self._running or not self.plan()[0]:
            return
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(DEBOUNCE_S, self._safe_sync)
        self._timer.daemon = True
        self._timer.start()

    def _safe_sync(self) -> None:
        try:
            self.sync()
        except Exception as exc:
            self.error = f"Sync failed ({type(exc).__name__})."
            self.publish()

    # ---- skill surface ---------------------------------------------------------------------------------------
    def tools(self):
        return [Tool("sync_google_tasks", "Sync Vesper's tasks with Google Tasks now (both ways: new, done, renamed, "
                                          "due dates).", obj(), self.sync, tier=1)]

    def route(self, t: str):
        if re.fullmatch(r"(?:sync|synchroni[sz]e|update) (?:my )?(?:google )?tasks(?: (?:with|to) google(?: tasks)?)?(?: now)?|"
                        r"sync (?:with )?google tasks", t):
            return Intent("tool", "sync_google_tasks", {})
        return None

    def say(self, tool, args, result):
        if tool != "sync_google_tasks":
            return None
        if not result.get("lists"):
            return (result.get("errors") or ["Google Tasks isn't set up."])[0]
        c = result["counts"]
        bits = [f"{c['sent']} sent" if c["sent"] else "", f"{c['brought_in']} brought in" if c["brought_in"] else "",
                f"{c['updated_here'] + c['updated_there']} updated" if c["updated_here"] + c["updated_there"] else ""]
        bits = [b for b in bits if b]
        return "Google Tasks is in step" + (f": {', '.join(bits)}." if bits else "; nothing new.")

    def status(self) -> dict:
        plan, problems = self.plan()
        out = {"ok": bool(plan), "lists": [{**p, "tasks": self.svc.storage.query(
            "SELECT COUNT(*) AS n FROM gtasks_map WHERE profile=? AND account=? AND state='linked'",
            (p["profile"], p["account"]))[0]["n"]} for p in plan],
               "at": self.last.get("at"), "counts": self.last.get("counts"), "errors": self.last.get("errors") or [],
               "missing": problems[0] if problems else None}
        if not plan:
            out["error"] = problems[0] if problems else "Google Tasks isn't set up."
        return out

    def actions(self):
        return {"sync": lambda b: self.sync()}

    def tick(self) -> None:
        try:
            if self.plan()[0]:
                self.sync()
                return
        except Exception as exc:
            self.error = f"Sync failed ({type(exc).__name__})."
        self.publish()
