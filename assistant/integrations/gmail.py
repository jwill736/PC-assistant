"""Gmail for every connected Google account (work and personal): reading, plus reply drafts. Nothing is ever sent.

- ``needs_you()``: unread mail in each inbox from a person (newsletters, receipts and notifications left out), newest
  first: what the HUD's Inbox panel and the morning briefing show.
- ``search()``: Gmail's own search syntax ("from:dana invoice", "newer_than:7d has:attachment") across the accounts.
- ``read()``: one message's text.
- ``create_draft()``: a reply saved in the conversation's Drafts (the "Gmail drafts" box on Google's page). There is
  no send here, and GoogleAccounts.request refuses any ``/send`` call.

Email is written by other people, so the brain treats it like a downloaded document: information, never orders.
"""

from __future__ import annotations

import base64
import html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

from .google import COMPOSE_SCOPE, GMAIL_SCOPE, GoogleAccounts, GoogleError, _reason

log = logging.getLogger(__name__)

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
# Gmail's tabs other than Primary, and the automatic senders: not "someone waiting on you"
BULK_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS", "SPAM", "TRASH"}
ROBOT = re.compile(r"(?:no-?reply|do-?not-?reply|notifications?|mailer-daemon|newsletter|alerts?|updates?|"
                   r"billing|receipts?|support@|info@|news@|marketing|hello@)", re.I)
NEEDS_YOU = "in:inbox is:unread newer_than:3d -from:me"
MAX_BODY = 12_000


class Gmail:
    def __init__(self, google: GoogleAccounts, cache_s: float = 120):
        self.google = google
        self.cache_s = cache_s
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def accounts(self) -> list[str]:
        """Connected accounts that allowed Gmail."""
        return [e for e in self.google.emails() if self.google.can(e, GMAIL_SCOPE)]

    def missing(self) -> list[str]:
        """Connected accounts that still need Reconnect to allow Gmail."""
        return [e for e in self.google.emails() if not self.google.can(e, GMAIL_SCOPE)]

    # ---- reading -------------------------------------------------------------------------------------
    def _ids(self, email: str, query: str, limit: int) -> list[str]:
        resp = self.google.get(email, f"{GMAIL}/messages", {"q": query, "maxResults": max(1, min(limit, 50))})
        if resp.status_code != 200:
            raise GoogleError(_reason(resp))
        return [m["id"] for m in resp.json().get("messages", [])]

    def _meta(self, email: str, msg_id: str) -> dict | None:
        resp = self.google.get(email, f"{GMAIL}/messages/{msg_id}", {
            "format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date", "List-Unsubscribe"]})
        if resp.status_code != 200:
            return None
        return summarize(email, resp.json())

    def _list(self, email: str, query: str, limit: int) -> list[dict]:
        ids = self._ids(email, query, limit)
        with ThreadPoolExecutor(max_workers=6) as pool:
            return [m for m in pool.map(lambda i: self._meta(email, i), ids) if m]

    def search(self, query: str, account: str = "", limit: int = 8) -> dict:
        """Gmail search across the accounts (or one: an email address or "work"/"personal")."""
        accounts = self._pick(account)
        if not accounts:
            return {"ok": False, "error": self._why_none()}
        hits, errors = [], {}
        for email in accounts:
            try:
                hits += self._list(email, query, limit)
            except GoogleError as exc:
                errors[email] = str(exc)
        hits.sort(key=lambda m: m["ts"], reverse=True)
        out = {"ok": True, "query": query, "messages": hits[:limit]}
        if errors:
            out["errors"] = errors
        return out

    def needs_you(self, limit: int = 6, fresh: bool = False) -> dict:
        """Per account: unread mail from people, newest first, plus how many unread are bulk."""
        key = f"needs:{limit}"
        with self._lock:
            hit = self._cache.get(key)
            if hit and not fresh and time.time() - hit[0] < self.cache_s:
                return hit[1]
        out = {"ok": True, "accounts": [], "missing": self.missing(), "at": time.time()}
        for email in self.accounts():
            try:
                msgs = self._list(email, NEEDS_YOU, 25)
            except GoogleError as exc:
                out["accounts"].append({"email": email, "error": str(exc), "people": [], "bulk": 0})
                continue
            people = [m for m in msgs if not m["bulk"]]
            out["accounts"].append({"email": email, "people": people[:limit], "waiting": len(people),
                                    "bulk": len(msgs) - len(people)})
        if not self.accounts():
            out = {**out, "ok": False, "error": self._why_none()}
        with self._lock:
            self._cache[key] = (time.time(), out)
        return out

    def read(self, msg_id: str, account: str = "") -> dict:
        for email in self._pick(account) or []:
            resp = self.google.get(email, f"{GMAIL}/messages/{msg_id}", {"format": "full"})
            if resp.status_code == 200:
                data = resp.json()
                meta = summarize(email, data)
                body = body_text(data.get("payload") or {})
                return {"ok": True, **meta, "body": body[:MAX_BODY], "truncated": len(body) > MAX_BODY}
        return {"ok": False, "error": "I can't find that email." if self.accounts() else self._why_none()}

    def thread(self, thread_id: str, account: str = "") -> dict:
        """Every message in a conversation, oldest first: who wrote, when, and the text (quoted replies left out)."""
        for email in self._pick(account) or []:
            resp = self.google.get(email, f"{GMAIL}/threads/{thread_id}", {"format": "full"})
            if resp.status_code == 200:
                msgs = []
                for m in resp.json().get("messages", []):
                    meta = summarize(email, m)
                    meta["to"] = _header(m, "to")
                    meta["cc"] = _header(m, "cc")
                    meta["reply_to"] = _header(m, "reply-to")
                    meta["message_id"] = _header(m, "message-id")
                    meta["references"] = _header(m, "references")
                    meta["from_me"] = "SENT" in (m.get("labelIds") or [])
                    meta["body"] = body_text(m.get("payload") or {})[:4000]
                    msgs.append(meta)
                return {"ok": True, "account": email, "thread_id": thread_id, "messages": msgs}
        return {"ok": False, "error": "I can't find that conversation." if self.accounts() else self._why_none()}

    def sent(self, days: int = 14, limit: int = 30, account: str = "") -> list[dict]:
        """What the user sent recently (each with ``to``), newest first, across the accounts."""
        out = []
        for email in self._pick(account):
            try:
                ids = self._ids(email, f"in:sent newer_than:{int(days)}d", limit)
            except GoogleError:
                continue
            for i in ids:
                resp = self.google.get(email, f"{GMAIL}/messages/{i}", {
                    "format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]})
                if resp.status_code == 200:
                    m = summarize(email, resp.json())
                    m["to"] = _header(resp.json(), "to")
                    out.append(m)
        return sorted(out, key=lambda m: m["ts"], reverse=True)

    def sent_bodies(self, account: str, limit: int = 30, days: int = 180) -> list[dict]:
        """The user's own recent emails from one account, newest first, each with ``body`` (only what he wrote:
        the quoted message below "On … wrote:" is cut off). For learning how he writes."""
        if account not in self.accounts():
            return []
        try:
            ids = self._ids(account, f"in:sent newer_than:{int(days)}d", limit)
        except GoogleError:
            return []
        out = []
        for i in ids:
            resp = self.google.get(account, f"{GMAIL}/messages/{i}", {"format": "full"})
            if resp.status_code != 200:
                continue
            data = resp.json()
            m = summarize(account, data)
            m["to"] = _header(data, "to")
            m["body"] = own_words(body_text(data.get("payload") or {}))[:4000]
            out.append(m)
        return sorted(out, key=lambda m: m["ts"], reverse=True)

    # ---- drafts (never sent) -------------------------------------------------------------------------
    def draft_accounts(self) -> list[str]:
        """Accounts where Vesper may save drafts: Gmail and Gmail drafts both allowed."""
        return [e for e in self.accounts() if self.google.can(e, COMPOSE_SCOPE)]

    def create_draft(self, account: str, *, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                     thread_id: str = "", in_reply_to: str = "", references: str = "") -> dict:
        """Save a reply in the conversation's Drafts. It is not sent: the user opens it in Gmail and sends it."""
        if account not in self.accounts() or not self.google.can(account, COMPOSE_SCOPE):
            raise GoogleError(self.google.need(COMPOSE_SCOPE, "Saving reply drafts", [account]))
        if not to:
            raise GoogleError("A draft needs someone to reply to.")
        msg = EmailMessage()
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = " ".join(x for x in (references, in_reply_to) if x).strip()
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
        payload = {"message": {"raw": raw, **({"threadId": thread_id} if thread_id else {})}}
        resp = self.google.request("POST", account, f"{GMAIL}/drafts", json=payload)
        if resp.status_code not in (200, 201):
            raise GoogleError(_reason(resp))
        d = resp.json()
        message = d.get("message") or {}
        tid = message.get("threadId") or thread_id
        return {"ok": True, "account": account, "draft_id": d.get("id"), "message_id": message.get("id"),
                "thread_id": tid, "link": draft_link(account, tid, message.get("id"))}

    def _pick(self, account: str) -> list[str]:
        accounts = self.accounts()
        a = (account or "").strip().lower()
        if not a:
            return accounts
        if "@" in a:
            return [e for e in accounts if e.lower() == a]
        if a in ("personal", "gmail", "home"):
            return [e for e in accounts if e.endswith("@gmail.com")] or accounts
        if a in ("work", "job", "office"):
            return [e for e in accounts if not e.endswith("@gmail.com")] or accounts
        return [e for e in accounts if a in e.lower()] or accounts

    def _why_none(self) -> str:
        if not self.google.configured or not self.google.emails():
            return "No Google account is connected: Setup → Google accounts."
        return ("Gmail isn't allowed yet for " + ", ".join(self.missing()) +
                ": Setup → Google accounts → Reconnect, and tick Gmail.")


def summarize(account: str, msg: dict) -> dict:
    headers = {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", [])}
    name, addr = parseaddr(headers.get("from", ""))
    labels = set(msg.get("labelIds") or [])
    try:
        ts = parsedate_to_datetime(headers["date"]).timestamp()
    except (KeyError, TypeError, ValueError):
        ts = int(msg.get("internalDate", 0)) / 1000
    bulk = bool(labels & BULK_LABELS) or "list-unsubscribe" in headers or bool(ROBOT.search(addr))
    return {"account": account, "id": msg.get("id"), "thread_id": msg.get("threadId"),
            "from": name or addr, "from_email": addr, "subject": headers.get("subject") or "(no subject)",
            "snippet": html.unescape(msg.get("snippet") or ""), "ts": ts,
            "unread": "UNREAD" in labels, "important": "IMPORTANT" in labels, "starred": "STARRED" in labels,
            "bulk": bulk, "link": f"https://mail.google.com/mail/u/{account}/#all/{msg.get('threadId') or msg.get('id')}"}


def body_text(payload: dict) -> str:
    """The readable text of a message: its text/plain part, else its HTML with the tags taken out."""
    plain, rich = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            rich.append(_strip_html(_b64(data)))
        for sub in part.get("parts") or []:
            walk(sub)
    walk(payload)
    text = "\n".join(plain) if plain else "\n".join(rich)
    text = re.sub(r"\n>.*", "", text)  # quoted replies below
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def own_words(text: str) -> str:
    """What the writer typed in a reply: everything above "On Mon, Dana wrote:" / "-----Original Message-----"."""
    return re.split(r"\n[^\n]*\bOn\b[^\n]{4,200}\bwrote:[ \t]*(?:\n|$)|\n-{2,}\s*Original Message|\n_{5,}\s*\n|\nFrom: ",
                    "\n" + (text or ""))[0].strip()


def draft_link(account: str, thread_id: str | None, message_id: str | None) -> str:
    """Opens the draft in Gmail (in its conversation when it has one)."""
    if thread_id:
        return f"https://mail.google.com/mail/u/{account}/#all/{thread_id}"
    return f"https://mail.google.com/mail/u/{account}/#drafts?compose={message_id or ''}"


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>|</li>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", " ", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))


def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers", []):
        if h["name"].lower() == name:
            return h["value"]
    return ""
