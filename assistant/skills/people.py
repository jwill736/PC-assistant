"""People memory: who J deals with, from his own Gmail (work and personal) and calendar, plus what he asked Vesper to
remember about them.

- "who is Dana", "what do I know about Dana": last contact, recent subjects, meetings, notes.
- "remember that Dana's daughter starts college in the fall" (only when Dana is someone Vesper knows: otherwise the
  ordinary "remember that ..." note), "ask Dana about her move next time": shown in meeting prep and "who is".
- "who haven't I talked to": people J wrote to a lot who have gone quiet for a month.

Kept in the local database (people_contacts, people_notes); rebuilt a little at a time every 6 hours.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from email.utils import getaddresses

from ..brain.router import Intent
from ..brain.tools import EMAIL_NOTE, Tool, obj
from . import Skill, gmail_checks

ROBOT = re.compile(r"(?:no-?reply|do-?not-?reply|notifications?|mailer-daemon|newsletter|alerts?|updates?|billing|"
                   r"receipts?|support@|info@|news@|marketing|hello@|calendar-notification|@resource\.calendar)", re.I)
# first names that are also everyday words: only taken as a person in "remember that ..." with "'s" or a full name
AMBIGUOUS = set("will may june april august grace hope joy faith mark bill pat rob sue frank art max chase rich sunny "
                "summer autumn dawn eve ray rose jack jean drew gene lane reed page cliff dean miles hunter".split())
NOT_NAMES = set("the a an my me it that this i you we they he she him her them our your there here what who".split())
WINDOWS = [(0, 7), (7, 30), (30, 90)]  # the first build reads these Gmail windows (days back), 50 messages each


# ---- small helpers the other email skills share ---------------------------------------------------------------

def gmail_or_reason(svc) -> tuple[object | None, str]:
    """(gmail, "") when at least one account allowed Gmail; else (None, what J should do)."""
    gm = getattr(svc, "gmail", None)
    if gm is None:
        return None, "Gmail isn't set up."
    try:
        if gm.accounts():
            return gm, ""
        why = getattr(gm, "_why_none", None)
        return None, why() if why else "No Gmail account is connected."
    except Exception as exc:  # a broken token file must not take the skill down
        return None, f"Gmail isn't answering ({type(exc).__name__})."


def account_kind(email: str) -> str:
    return "personal" if (email or "").lower().endswith("@gmail.com") else "work"


def display_name(name: str, email: str) -> str:
    name = (name or "").strip().strip('"')
    if name and "@" not in name:
        return name
    local = (email or name).split("@")[0]
    return " ".join(w.capitalize() for w in re.split(r"[._\-+]+", local) if w and not w.isdigit()) or email


def first_name(name: str) -> str:
    return (name or "").split()[0] if (name or "").split() else ""


def day_phrase(ts: float, tz, now: float | None = None) -> str:
    """'today', 'yesterday', 'on Tuesday' (this week), 'on Oct 3'."""
    now_dt = datetime.fromtimestamp(now or time.time(), tz)
    dt = datetime.fromtimestamp(ts, tz)
    days = (now_dt.date() - dt.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"on {dt.strftime('%A')}"
    return f"on {dt.strftime('%b')} {dt.day}"


def clean_subject(s: str) -> str:
    return re.sub(r"^(?:(?:re|fwd?|aw|sv)\s*:\s*)+", "", (s or "").strip(), flags=re.I) or "(no subject)"


def on_air(svc) -> bool:
    return bool(svc.live and svc.live())


# ---- the skill --------------------------------------------------------------------------------------------------

class People(Skill):
    name, title = "people", "People"
    poll_s = 6 * 3600
    delay_s = 90

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        self.error: str | None = None
        self.warning: str | None = None
        self._mine: set[str] = set()  # J's own addresses: never a contact
        db = svc.storage
        db.execute("CREATE TABLE IF NOT EXISTS people_contacts (email TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
                   "account TEXT NOT NULL DEFAULT '', last_contact REAL NOT NULL DEFAULT 0, count INTEGER NOT NULL DEFAULT 0, "
                   "sent INTEGER NOT NULL DEFAULT 0, meetings INTEGER NOT NULL DEFAULT 0, last_subject TEXT NOT NULL DEFAULT '', "
                   "subjects TEXT NOT NULL DEFAULT '[]', last_meeting TEXT NOT NULL DEFAULT '', "
                   "next_meeting TEXT NOT NULL DEFAULT '', next_meeting_at TEXT NOT NULL DEFAULT '')")
        db.execute("CREATE TABLE IF NOT EXISTS people_notes (id INTEGER PRIMARY KEY, email TEXT NOT NULL, kind TEXT NOT NULL, "
                   "text TEXT NOT NULL, created REAL NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS people_seen (id TEXT PRIMARY KEY, ts REAL NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS people_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    # ---- building the table -------------------------------------------------------------------------------
    def _meta(self, key: str, default: str = "") -> str:
        rows = self.svc.storage.query("SELECT value FROM people_meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def _set_meta(self, key: str, value) -> None:
        self.svc.storage.execute("INSERT OR REPLACE INTO people_meta(key, value) VALUES (?, ?)", (key, str(value)))

    def _first_seen(self, ident: str, ts: float) -> bool:
        cur = self.svc.storage.execute("INSERT OR IGNORE INTO people_seen(id, ts) VALUES (?, ?)", (ident, ts))
        return cur.rowcount > 0

    def touch(self, email: str, name: str, account: str, ts: float, subject: str = "", *, sent: bool = False,
              meeting: str = "") -> None:
        email = (email or "").strip().lower()
        if "@" not in email or ROBOT.search(email) or email in self._mine:
            return
        db = self.svc.storage
        row = (db.query("SELECT * FROM people_contacts WHERE email=?", (email,)) or [None])[0]
        nice = display_name(name, email)
        if row is None:
            db.execute("INSERT INTO people_contacts(email, name, account) VALUES (?, ?, ?)", (email, nice, account_kind(account)))
            row = {"name": nice, "last_contact": 0, "subjects": "[]", "last_subject": "", "last_meeting": ""}
        elif "@" in (name or "") or not (name or "").strip() or len(row["name"].split()) > len(nice.split()):
            nice = row["name"] or nice  # keep the fuller real name we saw before
        newer = ts >= (row["last_contact"] or 0)
        subjects = json.loads(row["subjects"] or "[]")
        if subject:
            s = clean_subject(subject)
            subjects = sorted([x for x in subjects if x["s"] != s] + [{"s": s, "ts": ts}], key=lambda x: -x["ts"])[:6]
        db.execute("UPDATE people_contacts SET name=?, account=CASE WHEN ? THEN ? ELSE account END, "
                   "last_contact=MAX(last_contact, ?), count=count+1, sent=sent+?, meetings=meetings+?, "
                   "last_subject=CASE WHEN ? AND ?<>'' THEN ? ELSE last_subject END, subjects=?, "
                   "last_meeting=CASE WHEN ? AND ?<>'' THEN ? ELSE last_meeting END WHERE email=?",
                   (nice, newer, account_kind(account), ts, int(sent), int(bool(meeting)), newer, subject,
                    clean_subject(subject), json.dumps(subjects), newer, meeting, meeting, email))

    def build(self, full: bool = False) -> dict:
        """Read what's new since the last build (the last 90 days the first time)."""
        now = self.clock()
        last = float(self._meta("built", "0") or 0)
        full = full or not last
        gm, why = gmail_or_reason(self.svc)
        self.warning = why or None
        self._mine = set()
        added = 0
        if gm is not None:
            accounts = gm.accounts()
            self._mine = {a.lower() for a in accounts}
            days = 90 if full else max(1, int((now - last) // 86400) + 2)
            for acct in accounts:
                windows = WINDOWS if full else [(0, days)]
                for a, b in windows:
                    q = f"-in:chats -in:sent newer_than:{b}d" + (f" older_than:{a}d" if a else "")
                    res = gm.search(q, account=acct, limit=50)
                    for m in res.get("messages", []) if res.get("ok") else []:
                        if not m.get("bulk") and self._first_seen(f"m:{m['id']}", m["ts"]):
                            self.touch(m.get("from_email", ""), m.get("from", ""), acct, m["ts"], m.get("subject", ""))
                            added += 1
                for m in gm.sent(days=days, limit=50, account=acct):
                    if not self._first_seen(f"m:{m['id']}", m["ts"]):
                        continue
                    for nm, addr in getaddresses([m.get("to") or ""]):
                        self.touch(addr, nm, acct, m["ts"], m.get("subject", ""), sent=True)
                    added += 1
        added += self._from_calendar(now)
        self.svc.storage.execute("DELETE FROM people_seen WHERE ts < ?", (now - 120 * 86400,))
        self._set_meta("built", now)
        n = self.svc.storage.query("SELECT COUNT(*) AS n FROM people_contacts")[0]["n"]
        self.error = None if n or gm is not None else why
        return {"ok": True, "added": added, "contacts": n}

    def _from_calendar(self, now: float) -> int:
        tz = self.svc.tz
        now_dt = datetime.fromtimestamp(now, tz)
        try:
            events = self.svc.calendars.events(now_dt - timedelta(days=14), now_dt + timedelta(days=14))
        except Exception:
            return 0
        db, n = self.svc.storage, 0
        db.execute("UPDATE people_contacts SET next_meeting='', next_meeting_at=''")
        for e in events:
            people = [a for a in e.get("attendees") or [] if not a.get("self") and a.get("email")]
            if e.get("all_day") or not people:
                continue
            start, end = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
            for a in people:
                email = a["email"].lower()
                if end <= now_dt:
                    if self._first_seen(f"ev:{e['title']}|{e['start']}|{email}", end.timestamp()):
                        self.touch(email, a.get("name", ""), e.get("account") or "", end.timestamp(), meeting=e["title"])
                        n += 1
                elif start > now_dt:
                    if not db.query("SELECT 1 FROM people_contacts WHERE email=?", (email,)):
                        self.touch(email, a.get("name", ""), e.get("account") or "", 0)
                        db.execute("UPDATE people_contacts SET count=0 WHERE email=?", (email,))
                    db.execute("UPDATE people_contacts SET next_meeting=?, next_meeting_at=? WHERE email=? AND "
                               "(next_meeting_at='' OR next_meeting_at>?)", (e["title"], e["start"], email, e["start"]))
        return n

    # ---- looking people up -----------------------------------------------------------------------------------
    def find(self, name: str) -> list[dict]:
        """Contacts matching a spoken name ("dana", "dana ortiz", "dana's"), best first."""
        q = re.sub(r"'s$|[^a-z0-9@.\- ]", "", (name or "").lower().strip()).strip()
        if len(q) < 2 or q in NOT_NAMES:
            return []
        hits = []
        for r in self.svc.storage.query("SELECT * FROM people_contacts"):
            full, email = r["name"].lower(), r["email"]
            parts = full.split()
            rank = (0 if q in (full, email) else 1 if parts and q == parts[0] else 2 if q in parts[1:] else
                    3 if email.split("@")[0].startswith(q.replace(" ", ".")) else None)
            if rank is not None:
                hits.append((rank, -r["count"], -r["last_contact"], r))
        return [h[-1] for h in sorted(hits, key=lambda h: h[:3])]

    def known(self, name: str) -> dict | None:
        hits = self.find(name)
        return hits[0] if hits else None

    def about(self, email: str) -> dict:
        rows = self.svc.storage.query("SELECT id, kind, text, created FROM people_notes WHERE email=? ORDER BY created DESC",
                                      ((email or "").lower(),))
        return {"notes": [r for r in rows if r["kind"] == "note"], "ask": [r for r in rows if r["kind"] == "ask"]}

    def _view(self, r: dict, full: bool = False) -> dict:
        now = self.clock()
        v = {"email": r["email"], "name": r["name"], "account": r["account"], "count": r["count"], "sent": r["sent"],
             "meetings": r["meetings"], "last_contact": r["last_contact"] or None,
             "days_since": int((now - r["last_contact"]) // 86400) if r["last_contact"] else None,
             "last_subject": r["last_subject"], "next_meeting": r["next_meeting"] or None,
             "next_meeting_at": r["next_meeting_at"] or None, **self.about(r["email"])}
        if full:
            v["subjects"] = [x["s"] for x in json.loads(r["subjects"] or "[]")]
            v["last_meeting"] = r["last_meeting"] or None
            fn = first_name(r["name"]).lower()
            v["tasks"] = [t["title"] for t in self.svc.storage.list_tasks() if fn and re.search(rf"\b{re.escape(fn)}\b", t["title"].lower())][:5]
        return v

    # ---- tools ------------------------------------------------------------------------------------------------
    def who_is(self, name: str) -> dict:
        hits = self.find(name)
        if not hits:
            return {"ok": False, "error": f"I don't know anyone called {name} yet." +
                    (f" {self.error}" if self.error else "")}
        out = {"ok": True, "person": self._view(hits[0], full=True),
               "also": [h["name"] for h in hits[1:4] if h["name"] != hits[0]["name"]]}
        if on_air(self.svc):
            p = out["person"]
            out = {"ok": True, "live": True, "person": {k: p[k] for k in ("name", "account", "days_since", "count")}}
        return out

    def remember_about(self, name: str, text: str) -> dict:
        text = (text or "").strip().rstrip(".")
        if not text:
            return {"ok": False, "error": "What should I remember?"}
        p = self.known(name)
        if p is None:
            note = self.svc.storage.add_note(f"{name}: {text}" if name.lower() not in text.lower() else text)
            return {"ok": True, "person": None, "note": note["text"]}
        kind = "ask" if re.search(r"^(?:to )?ask\b|\bnext time\b", text, re.I) else "note"
        text = re.sub(r"\s*\bnext time(?: (?:we talk|we meet|i see (?:him|her|them)))?$", "", text, flags=re.I)
        cur = self.svc.storage.execute("INSERT INTO people_notes(email, kind, text, created) VALUES (?, ?, ?, ?)",
                                       (p["email"], kind, text[0].upper() + text[1:], self.clock()))
        self.publish()
        return {"ok": True, "person": p["name"], "kind": kind, "id": cur.lastrowid, "text": text}

    def forget(self, note_id: int) -> dict:
        cur = self.svc.storage.execute("DELETE FROM people_notes WHERE id=?", (int(note_id),))
        return {"ok": cur.rowcount > 0, **({} if cur.rowcount else {"error": "That note is gone."})}

    def overdue(self, days: int = 30, min_count: int = 0) -> dict:
        min_count = min_count or int(self.setting("overdue_min_count", 4))
        cutoff = self.clock() - int(days or 30) * 86400
        rows = self.svc.storage.query("SELECT * FROM people_contacts WHERE count>=? AND last_contact>0 AND last_contact<? "
                                      "ORDER BY count DESC, last_contact DESC LIMIT 8", (min_count, cutoff))
        out = [{k: v for k, v in self._view(r).items() if k in ("name", "email", "account", "days_since", "count")}
               for r in rows]
        return {"ok": True, "people": out, "days": days, **({"error_note": self.error} if self.error else {})}

    def search(self, q: str = "") -> dict:
        q = (q or "").strip().lower()
        if q:
            rows = self.svc.storage.query("SELECT * FROM people_contacts WHERE lower(name) LIKE ? OR email LIKE ? "
                                          "ORDER BY count DESC LIMIT 12", (f"%{q}%", f"%{q}%"))
        else:
            rows = self.svc.storage.query("SELECT * FROM people_contacts WHERE count>0 ORDER BY last_contact DESC LIMIT 12")
        return {"ok": True, "q": q, "people": [self._view(r, full=True) for r in rows]}

    def tools(self):
        return [
            Tool("who_is", "What Vesper knows about a person J emails or meets: last contact, recent email subjects, "
                           "meetings, his notes and things to ask them. Use for 'who is Dana', 'when did I last talk to …'.",
                 obj({"name": {"type": "string"}}, ["name"]), self.who_is,
                 untrusted=True, untrusted_label="an email", untrusted_note=EMAIL_NOTE),
            Tool("remember_about", "Remember something about a person (shown in meeting prep and who_is). Start text "
                                   "with 'ask about …' for something to bring up next time.",
                 obj({"name": {"type": "string"}, "text": {"type": "string"}}, ["name", "text"]), self.remember_about, tier=1),
            Tool("people_overdue", "People J was in touch with a lot who have gone quiet for over a month.",
                 obj({"days": {"type": "integer"}}), self.overdue,
                 untrusted=True, untrusted_label="an email", untrusted_note=EMAIL_NOTE),
        ]

    def route(self, t: str):
        m = re.fullmatch(r"(?:who is|who's|tell me about|what do (?:i|you) know about|when did i last (?:talk|speak) "
                         r"(?:to|with)|when did i last (?:email|see|hear from)) (.+)", t)
        if m and self.known(m.group(1)):
            return Intent("tool", "who_is", {"name": m.group(1)})
        m = re.fullmatch(r"(?:remember|note|make a note)(?: that)? (.+)", t)
        if m:
            rest = m.group(1)
            ask = re.fullmatch(r"to ask (\S+(?: \S+)?) about (.+)", rest)
            if ask and self.known(ask.group(1)):
                return Intent("tool", "remember_about", {"name": ask.group(1), "text": f"ask about {ask.group(2)}"})
            name = self._leading_name(rest)
            if name:
                return Intent("tool", "remember_about", {"name": name, "text": rest[0].upper() + rest[1:]})
        m = re.fullmatch(r"(?:next time i (?:talk to|see|meet) (\S+(?: \S+)?),? ask (?:him|her|them) about (.+)|"
                         r"ask (\S+(?: \S+)?) about (.+?) next time(?: (?:we talk|we meet|i see (?:him|her|them)))?)", t)
        if m and self.known(m.group(1) or m.group(3)):
            return Intent("tool", "remember_about", {"name": m.group(1) or m.group(3), "text": f"ask about {m.group(2) or m.group(4)}"})
        if re.fullmatch(r"who (?:haven't|have i not) i (?:talked|spoken) to(?: in a while| lately| recently)?|who have i lost "
                        r"touch with|who should i (?:reach out to|catch up with|get back in touch with)", t):
            return Intent("tool", "people_overdue", {})
        return None

    def _leading_name(self, rest: str) -> str | None:
        """'dana's daughter …' / 'dana ortiz is moving …' -> the contact's name, only if Vesper knows them."""
        words = rest.split()
        for n in (2, 1):
            if len(words) <= n:
                continue
            cand = " ".join(words[:n])
            poss = cand.endswith("'s")
            base = cand[:-2] if poss else cand
            if n == 1 and base in AMBIGUOUS and not poss:
                continue
            p = self.known(base)
            if p and (n == 2 and base == p["name"].lower() or n == 1):
                return base
        return None

    def say(self, tool, args, result):
        if tool == "remember_about":
            if not result.get("person"):
                return "Noted."
            return f"Got it. I'll bring that up next time you talk to {first_name(result['person'])}." \
                if result.get("kind") == "ask" else f"Got it, I'll remember that about {first_name(result['person'])}."
        if tool == "who_is":
            p = result["person"]
            when = (f"last in touch {p['days_since']} days ago" if p.get("days_since") else "in touch today") \
                if p.get("days_since") is not None else "no emails yet"
            if result.get("live"):
                return f"{p['name']}, {p['account']}: {when}. The rest is on the HUD."
            line = f"{p['name']}, {p['account']}: {when}"
            line += f", about “{p['last_subject']}”." if p.get("last_subject") and p.get("days_since") is not None else "."
            if p.get("next_meeting"):
                line += f" Next meeting: {p['next_meeting']}."
            if p.get("notes"):
                line += f" You noted: {p['notes'][0]['text']}."
            if p.get("ask"):
                item = p["ask"][0]["text"]
                line += f" Ask {first_name(p['name'])} {item[4:]}." if item.lower().startswith("ask ") else f" To ask: {item}."
            return line
        if tool == "people_overdue":
            ppl = result.get("people") or []
            if not ppl:
                return "Nobody you talk to a lot has gone quiet."
            names = ", ".join(f"{p['name']} ({p['days_since']} days)" for p in ppl[:3])
            return f"You haven't been in touch with {names}." + (f" {len(ppl) - 3} more on the HUD." if len(ppl) > 3 else "")
        return None

    # ---- HUD ------------------------------------------------------------------------------------------------
    def selftest(self) -> list[dict]:
        return gmail_checks(self.svc, per_account=False)

    def status(self) -> dict:
        n = self.svc.storage.query("SELECT COUNT(*) AS n FROM people_contacts")[0]["n"]
        built = float(self._meta("built", "0") or 0) or None
        if not n and self.error:
            return {"ok": False, "error": self.error}
        if not n and built is None:
            gm, why = gmail_or_reason(self.svc)
            if gm is None:
                return {"ok": False, "error": why}
        return {"ok": True, "count": n, "built": built, "warning": self.warning, "recent": self.search()["people"],
                "overdue": self.overdue()["people"][:5]}

    def actions(self):
        return {"search": lambda b: self.search(str(b.get("q") or "")),
                "remember": lambda b: self.remember_about(str(b.get("name") or b.get("email") or ""), str(b.get("text") or "")),
                "forget": lambda b: self.forget(int(b.get("id") or 0)),
                "rebuild": lambda b: self.build(full=bool(b.get("full")))}

    def briefing(self):
        ov = self.overdue()["people"]
        return {"quiet": [p["name"] for p in ov[:3]]} if ov else None

    def tick(self) -> None:
        try:
            self.build()
        except Exception as exc:
            self.error = f"Couldn't update people ({type(exc).__name__})."
        self.publish()
