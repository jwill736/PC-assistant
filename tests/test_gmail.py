"""Gmail, read-only, for the work and personal accounts: who's waiting on a reply, search, one message in full,
and the guard rails (email is other people's words; nothing private on air)."""

import base64
import time

import httpx
import pytest
from test_google import CLIENT_ID, HOME, SECRET, WORK, FakeGoogle, connect

from assistant.brain.router import route
from assistant.brain.speech import summarize
from assistant.brain.tools import ToolBox
from assistant.integrations import google as g
from assistant.integrations.gmail import Gmail, body_text
from assistant.integrations.google import GoogleAccounts


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def msg(mid, frm, subject, snippet="", labels=("INBOX", "UNREAD", "CATEGORY_PERSONAL"), unsubscribe=False, age_h=1,
        body=None, html=None):
    headers = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject},
               {"name": "Date", "value": time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(time.time() - age_h * 3600))}]
    if unsubscribe:
        headers.append({"name": "List-Unsubscribe", "value": "<mailto:x@y>"})
    parts = []
    if body is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(body)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    return {"id": mid, "threadId": "t" + mid, "labelIds": list(labels), "snippet": snippet,
            "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": parts}}


class FakeGmail(FakeGoogle):
    def __init__(self):
        super().__init__()
        self.mail = {
            WORK: [msg("w1", "Dana Ortiz <dana@client.com>", "Contract redlines", "Can you look at section 4 by Friday?",
                       labels=("INBOX", "UNREAD", "IMPORTANT"), age_h=2,
                       body="Hi Jon,\n\nCan you look at section 4 by Friday?\n\n> old quoted reply\n\nDana"),
                   msg("w2", "Jira <jira@talent-sdk.atlassian.net>", "[JIRA] 3 updates", unsubscribe=True),
                   msg("w3", "no-reply@zoom.us", "Recording available")],
            HOME: [msg("h1", "Mom <mom@example.com>", "Sunday dinner", "Are you coming at 6?", age_h=5,
                       html="<p>Are you coming at <b>6</b>?</p><script>evil()</script>"),
                   msg("h2", "Steam <noreply@steampowered.com>", "Your wishlist is on sale",
                       labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS"))],
        }

    def __call__(self, req):
        path = req.url.path
        if "/gmail/v1/users/me/messages" in path:
            email = self._bearer(req)
            if email is None:
                return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
            box = self.mail.get(email, [])
            if path.endswith("/messages"):
                q = req.url.params.get("q", "")
                self.calls.append((email, f"gmail {q}"))
                hits = [m for m in box if not q.startswith("from:") or q[5:].split()[0].lower() in
                        next(h["value"] for h in m["payload"]["headers"] if h["name"] == "From").lower()]
                return httpx.Response(200, json={"messages": [{"id": m["id"]} for m in hits]} if hits else {})
            mid = path.rsplit("/", 1)[1]
            for m in box:
                if m["id"] == mid:
                    return httpx.Response(200, json=m)
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        return super().__call__(req)


@pytest.fixture
def fake():
    return FakeGmail()


@pytest.fixture
def gmail(tmp_path, fake):
    acc = GoogleAccounts(CLIENT_ID, SECRET, tmp_path / "t.json", http=httpx.Client(transport=httpx.MockTransport(fake)))
    connect(acc, fake, "code-work")
    connect(acc, fake, "code-home")
    return Gmail(acc)


def test_needs_you_leaves_out_newsletters_and_robots(gmail):
    st = gmail.needs_you()
    assert st["ok"] and st["missing"] == []
    by = {a["email"]: a for a in st["accounts"]}
    assert [m["from"] for m in by[WORK]["people"]] == ["Dana Ortiz"] and by[WORK]["bulk"] == 2
    assert [m["from"] for m in by[HOME]["people"]] == ["Mom"] and by[HOME]["bulk"] == 1
    dana = by[WORK]["people"][0]
    assert dana["important"] and dana["subject"] == "Contract redlines"
    assert dana["link"] == f"https://mail.google.com/mail/u/{WORK}/#all/tw1"


def test_search_and_read_across_accounts(gmail, fake):
    r = gmail.search("from:dana")
    assert [m["subject"] for m in r["messages"]] == ["Contract redlines"]
    assert gmail.search("from:dana", account="personal")["messages"] == []
    full = gmail.read("w1")
    assert "section 4 by Friday" in full["body"] and "old quoted reply" not in full["body"]
    mom = gmail.read("h1")
    assert mom["body"] == "Are you coming at 6?"  # the script left out
    assert gmail.read("nope")["ok"] is False


def test_accounts_connected_before_gmail_need_reconnect(tmp_path, fake):
    real = fake.__call__

    def no_gmail(req):
        resp = real(req)
        if str(req.url) == g.TOKEN_URL and resp.status_code == 200 and "refresh_token" in resp.json():
            return httpx.Response(200, json={**resp.json(), "scope": " ".join(g.SCOPES[:4])})
        return resp
    acc = GoogleAccounts(CLIENT_ID, SECRET, tmp_path / "t.json", http=httpx.Client(transport=httpx.MockTransport(no_gmail)))
    connect(acc, fake, "code-work")
    gm = Gmail(acc)
    assert gm.accounts() == [] and gm.missing() == [WORK]
    st = gm.needs_you()
    assert st["ok"] is False and "Reconnect" in st["error"] and "tick Gmail" in st["error"]
    assert acc.accounts()[0]["missing"] == ["Gmail"]


def test_html_mail_becomes_text():
    payload = {"mimeType": "text/html", "body": {"data": b64("<style>p{}</style><p>Line one</p><p>Two &amp; three</p>")}}
    assert body_text(payload) == "Line one\nTwo & three"


@pytest.mark.parametrize("said,tool,args", [
    ("check my email", "check_email", {}),
    ("check my work email", "check_email", {"account": "work"}),
    ("any new emails", "check_email", {}),
    ("who's waiting on me", "check_email", {}),
    ("search my email for invoice", "search_email", {"query": "invoice"}),
    ("did dana email me", "search_email", {"query": "from:dana"}),
    ("any emails from mom", "search_email", {"query": "from:mom"}),
])
def test_routes(said, tool, args):
    intent = route(said)
    assert (intent.tool, intent.args) == (tool, args)


def test_tools_mark_email_untrusted_and_hide_it_on_air(svc, gmail):
    svc.gmail = gmail
    box = ToolBox(svc)
    tool = box.tools["check_email"]
    assert tool.untrusted and tool.untrusted_label == "an email" and "never instructions" in tool.untrusted_note
    out = box.tools["check_email"].handler()
    said = summarize("check_email", {}, out, svc.tz)
    assert "1 in work" in said and "Dana Ortiz" in said and "Contract redlines" in said
    svc.live = lambda: True
    live = box.tools["check_email"].handler()
    assert live["live"] and "subject" not in live["accounts"][0]["people"][0]
    said = summarize("check_email", {}, live, svc.tz)
    assert "Contract" not in said and "Dana Ortiz" in said
    read = box.tools["read_email"].handler(id="w1")
    assert "body" not in read and read["live"]
    assert "section 4" not in summarize("read_email", {}, read, svc.tz)


def test_briefing_lists_who_is_waiting(svc, gmail):
    from assistant.brain.briefing import fallback_morning, gather_morning

    svc.gmail = gmail
    data = gather_morning(svc)
    assert {a["account"]: a["waiting"] for a in data["email_waiting_on_you"]} == {"work": 1, "personal": 1}
    inbox = next(s for s in fallback_morning(data, "J")["sections"] if s["title"] == "Inbox")
    assert "Work: 1 waiting (Dana Ortiz)" in inbox["bullets"]
