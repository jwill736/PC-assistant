"""A fake Google Workspace for the skills that write (drafts, Google Tasks, focus blocks) and the debrief.

Builds on test_google.FakeGoogle (sign-in, Drive, Calendar reads) and adds: Gmail (search, messages, threads, drafts,
and a /send that must never be called), Google Tasks (lists and tasks with "updated" times), and calendar event
inserts. ``grant`` narrows what one account ticked on Google's consent page."""

import base64
import email
import json
import re
import time
from email import policy
from urllib.parse import parse_qs

import httpx
from test_google import CLIENT_ID, HOME, SECRET, WORK, FakeGoogle, connect

from assistant.integrations import google as g
from assistant.integrations.google import GoogleAccounts

ME = {WORK: "Jon Williams <jon@talent-sdk.com>", HOME: "J <jwill736@gmail.com>"}


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def mail(mid, thread, frm, to, subject, body="", age_h=1.0, labels=("INBOX",), cc="", reply_to="", refs=""):
    ts = time.time() - age_h * 3600
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
               {"name": "Date", "value": time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(ts))},
               {"name": "Message-ID", "value": f"<{mid}@mail.example>"}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    if reply_to:
        headers.append({"name": "Reply-To", "value": reply_to})
    if refs:
        headers.append({"name": "References", "value": refs})
    return {"id": mid, "threadId": thread, "labelIds": list(labels), "snippet": body[:80],
            "internalDate": str(int(ts * 1000)),
            "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": b64(body)}}}


def sent(mid, thread, account, to, subject, body="", age_h=1.0, refs=""):
    return mail(mid, thread, ME[account], to, subject, body, age_h, labels=("SENT",), refs=refs)


class FakeWorkspace(FakeGoogle):
    def __init__(self):
        super().__init__()
        self.mail: dict[str, list[dict]] = {WORK: [], HOME: []}
        self.drafts: list[dict] = []  # {account, threadId, mime (email.message), raw}
        self.send_calls: list[str] = []
        self.grant: dict[str, str] = {}  # email -> the scopes that account ticked (default: every box)
        self.lists: dict[str, dict[str, dict]] = {WORK: {}, HOME: {}}  # email -> list id -> {title, tasks: {id: task}}
        self.events: list[dict] = []  # {account, calendar, body}
        self.t = 1_800_000_000.0  # Google's clock for "updated"
        self.n_ids = 0
        self.writes: list[tuple[str, str, str]] = []  # (method, email, path) for every write

    # ---- helpers for tests ------------------------------------------------------------------------------
    def tick(self, s: float = 60) -> None:
        self.t += s

    def rfc(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.t))

    def _id(self, prefix: str) -> str:
        self.n_ids += 1
        return f"{prefix}{self.n_ids}"

    def gtask(self, email_, title, *, status="needsAction", due=None, list_title=None) -> dict:
        """Add a task straight in Google Tasks (as if on the phone)."""
        lid = next(i for i, li in self.lists[email_].items() if list_title in (None, li["title"]))
        t = {"id": self._id("gt"), "title": title, "status": status, "updated": self.rfc(),
             **({"due": f"{due}T00:00:00.000Z"} if due else {})}
        self.lists[email_][lid]["tasks"][t["id"]] = t
        return t

    def tasks_in(self, email_, list_title) -> list[dict]:
        for li in self.lists[email_].values():
            if li["title"] == list_title:
                return list(li["tasks"].values())
        return []

    # ---- the fake ---------------------------------------------------------------------------------------------
    def __call__(self, req: httpx.Request) -> httpx.Response:
        url, path = str(req.url), req.url.path
        if url == g.TOKEN_URL:
            resp = super().__call__(req)
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            who = self.codes.get(form.get("code", ""))
            if resp.status_code == 200 and who in self.grant and "refresh_token" in resp.json():
                return httpx.Response(200, json={**resp.json(), "scope": self.grant[who]})
            return resp
        if path.rstrip("/").endswith("/send"):
            self.send_calls.append(path)
            return httpx.Response(403, json={"error": {"message": "The test fake never sends."}})
        is_gmail, is_tasks = "/gmail/v1/users/me/" in path, path.startswith("/tasks/v1/")
        is_insert = req.method == "POST" and "/calendar/v3/calendars/" in path and path.endswith("/events")
        if not (is_gmail or is_tasks or is_insert):
            return super().__call__(req)
        who = self._bearer(req)
        if who is None:
            return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
        if req.method != "GET":
            self.writes.append((req.method, who, path))
        if is_gmail:
            return self._gmail(req, who, path)
        if is_tasks:
            return self._tasks(req, who, path)
        body = json.loads(req.content or b"{}")
        cal = path.split("/calendars/")[1].split("/")[0]
        ev = {"id": self._id("ev"), "htmlLink": f"https://calendar.google.com/event?eid={self.n_ids}", **body}
        self.events.append({"account": who, "calendar": cal, "body": body, "event": ev})
        return httpx.Response(200, json=ev)

    def _gmail(self, req, who, path) -> httpx.Response:
        box = self.mail.get(who, [])
        if path.endswith("/drafts") and req.method == "POST":
            body = json.loads(req.content)
            raw = body["message"]["raw"]
            mime = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default)
            d = {"id": self._id("r"), "message": {"id": self._id("m"), "threadId": body["message"].get("threadId") or "new"}}
            self.drafts.append({"account": who, "threadId": body["message"].get("threadId"), "mime": mime, "draft": d})
            return httpx.Response(200, json=d)
        if path.endswith("/messages"):
            q = req.url.params.get("q", "")
            hits = []
            for m in sorted(box, key=lambda m: -int(m["internalDate"])):
                h = {x["name"].lower(): x["value"].lower() for x in m["payload"]["headers"]}
                if "in:sent" in q and "SENT" not in m["labelIds"]:
                    continue
                if ("-in:sent" in q or "-from:me" in q) and "SENT" in m["labelIds"]:
                    continue
                frm = re.search(r"(?:^|\s)from:(\S+)", q)
                if frm and frm.group(1).lower() not in h["from"]:
                    continue
                hits.append(m)
            hits = hits[:int(req.url.params.get("maxResults", 50))]
            return httpx.Response(200, json={"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in hits]}
                                  if hits else {})
        kind, ident = path.rsplit("/", 2)[-2:]
        if kind == "threads":
            msgs = sorted((m for m in box if m["threadId"] == ident), key=lambda m: int(m["internalDate"]))
            return httpx.Response(200, json={"id": ident, "messages": msgs}) if msgs else \
                httpx.Response(404, json={"error": {"message": "Not Found"}})
        for m in box:
            if m["id"] == ident:
                return httpx.Response(200, json=m)
        return httpx.Response(404, json={"error": {"message": "Not Found"}})

    def _tasks(self, req, who, path) -> httpx.Response:
        lists = self.lists.setdefault(who, {})
        parts = path.removeprefix("/tasks/v1/").strip("/").split("/")
        body = json.loads(req.content or b"{}") if req.content else {}
        nf = httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
        if parts[:2] == ["users", "@me"] and parts[2] == "lists":
            if len(parts) == 3:
                if req.method == "POST":
                    lid = self._id("L")
                    lists[lid] = {"title": body["title"], "tasks": {}}
                    return httpx.Response(200, json={"id": lid, "title": body["title"]})
                return httpx.Response(200, json={"items": [{"id": i, "title": li["title"]} for i, li in lists.items()]})
            return httpx.Response(200, json={"id": parts[3], "title": lists[parts[3]]["title"]}) if parts[3] in lists else nf
        if parts[0] != "lists" or parts[1] not in lists:
            return nf
        tasks = lists[parts[1]]["tasks"]
        if len(parts) == 3:
            if req.method == "POST":
                t = {"id": self._id("gt"), "status": "needsAction", **body, "updated": self.rfc()}
                tasks[t["id"]] = t
                return httpx.Response(200, json=t)
            return httpx.Response(200, json={"items": list(tasks.values())})
        t = tasks.get(parts[3])
        if t is None:
            return nf
        if req.method == "DELETE":
            del tasks[parts[3]]
            return httpx.Response(204)
        if req.method == "PATCH":
            for k, v in body.items():
                if v is None:
                    t.pop(k, None)
                else:
                    t[k] = v
            t["updated"] = self.rfc()
            return httpx.Response(200, json=t)
        return httpx.Response(200, json=t)


def accounts_for(tmp_path, fake: FakeWorkspace, *codes: str) -> GoogleAccounts:
    acc = GoogleAccounts(CLIENT_ID, SECRET, tmp_path / "google_tokens.json",
                         http=httpx.Client(transport=httpx.MockTransport(fake)))
    for code in codes or ("code-work", "code-home"):
        connect(acc, fake, code)
    return acc


ALL_BUT = {name: " ".join(s for s in g.SCOPES if s != scope) for scope, name in g.ALLOWS.items()}
