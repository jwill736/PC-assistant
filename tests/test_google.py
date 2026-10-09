"""Connect Google: one sign-in per account (work and personal), Google Docs text for the library, Google calendars.

Google itself is faked with httpx.MockTransport: the token endpoint checks the PKCE pair the way Google does, Drive
only exports a document to the account that can see it, and a revoked refresh token answers invalid_grant."""

import base64
import hashlib
import json
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from assistant import doctor
from assistant.integrations import google as g
from assistant.integrations.calendars import CalendarHub
from assistant.integrations.google import GoogleAccounts, GoogleError, read_link
from assistant.library import Library
from assistant.runtime import Runtime
from assistant.server import create_app

WORK, HOME = "jon@talent-sdk.com", "jwill736@gmail.com"
CLIENT_ID, SECRET = "123-abc.apps.googleusercontent.com", "GOCSPX-test-secret-value"


class FakeGoogle:
    """Just enough of accounts.google.com, Drive and Calendar for Vesper's calls."""

    def __init__(self):
        self.challenges: dict[str, str] = {}  # code -> the code_challenge the sign-in page was opened with
        self.codes = {"code-work": WORK, "code-home": HOME}
        self.tokens: dict[str, str] = {}  # access token -> email
        self.revoked: set[str] = set()
        self.docs = {"work-doc": (WORK, "Q3 plan: hire two engineers for the Assured Space launch."),
                     "home-sheet": (HOME, "Item,Cost\nRing light,89\n")}
        self.calls: list[tuple[str, str]] = []  # (email, what) for Drive and Calendar calls
        self.n = 0

    def authorize(self, url: str, code: str) -> dict:
        """The browser half: Google remembers the challenge for the code it hands back."""
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.challenges[code] = q["code_challenge"]
        return q

    def _bearer(self, req) -> str | None:
        return self.tokens.get(req.headers.get("authorization", "").removeprefix("Bearer "))

    def _new_token(self, email: str) -> str:
        self.n += 1
        tok = f"at-{self.n}"
        self.tokens[tok] = email
        return tok

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == g.TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            if form["client_id"] != CLIENT_ID or form["client_secret"] != SECRET:
                return httpx.Response(401, json={"error": "invalid_client"})
            if form["grant_type"] == "authorization_code":
                code = form["code"]
                verifier = form["code_verifier"].encode()
                want = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=").decode()
                if self.challenges.get(code) != want or code not in self.codes:
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad code"})
                email = self.codes[code]
                return httpx.Response(200, json={"access_token": self._new_token(email), "expires_in": 3600,
                                                 "refresh_token": f"rt-{email}", "scope": " ".join(g.SCOPES)})
            rt = form["refresh_token"]
            if rt in self.revoked:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            return httpx.Response(200, json={"access_token": self._new_token(rt.removeprefix("rt-")), "expires_in": 3600})
        if url.endswith("/revoke"):
            return httpx.Response(200)
        email = self._bearer(req)
        if email is None:
            return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
        if url == g.USERINFO_URL:
            return httpx.Response(200, json={"email": email, "email_verified": True})
        path = req.url.path
        if "/files/" in path and path.endswith("/export"):
            doc_id = path.split("/files/")[1].split("/")[0]
            self.calls.append((email, f"export {doc_id}"))
            owner, text = self.docs.get(doc_id, (None, ""))
            if owner != email:
                return httpx.Response(404, json={"error": {"code": 404, "message": f"File not found: {doc_id}."}})
            return httpx.Response(200, text=text)
        if path.endswith("/calendarList"):
            self.calls.append((email, "calendarList"))
            items = [{"id": email, "summary": email, "primary": True, "backgroundColor": "#4986e7"}]
            if email == HOME:
                items.append({"id": "family@group.calendar.google.com", "summary": "Family"})
            return httpx.Response(200, json={"items": items})
        if "/calendars/" in path and path.endswith("/events"):
            self.calls.append((email, f"events {req.url.params.get('pageToken') or ''}".strip()))
            now = datetime.now().astimezone()
            if not req.url.params.get("pageToken"):
                return httpx.Response(200, json={"nextPageToken": "p2", "items": [
                    {"summary": "Standup", "start": {"dateTime": (now + timedelta(hours=1)).isoformat()},
                     "end": {"dateTime": (now + timedelta(hours=1, minutes=15)).isoformat()}},
                    {"summary": "Moved", "status": "cancelled", "start": {"dateTime": now.isoformat()}}]})
            day = (now + timedelta(days=1)).date()
            return httpx.Response(200, json={"items": [
                {"summary": "Offsite", "start": {"date": day.isoformat()}, "end": {"date": (day + timedelta(days=1)).isoformat()}}]})
        return httpx.Response(404, json={"error": {"message": f"no fake for {url}"}})


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def fake():
    return FakeGoogle()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def accounts(tmp_path, fake, clock):
    return GoogleAccounts(CLIENT_ID, SECRET, tmp_path / "google_tokens.json",
                          http=httpx.Client(transport=httpx.MockTransport(fake)), clock=clock)


def connect(accounts, fake, code: str) -> str:
    url = accounts.begin("http://127.0.0.1:8765/api/google/callback")
    q = fake.authorize(url, code)
    return accounts.finish(q["state"], code)


# ---- signing in ----------------------------------------------------------------------------------------

def test_signs_in_work_and_personal_with_pkce(accounts, fake, tmp_path):
    url = accounts.begin("http://127.0.0.1:8765/api/google/callback", login_hint=WORK)
    q = fake.authorize(url, "code-work")
    assert url.startswith(g.AUTH_URL)
    assert (q["code_challenge_method"], q["access_type"], q["response_type"]) == ("S256", "offline", "code")
    assert q["redirect_uri"] == "http://127.0.0.1:8765/api/google/callback" and q["login_hint"] == WORK
    assert "drive.readonly" in q["scope"] and "calendar.readonly" in q["scope"] and "gmail" not in q["scope"]
    seen = []
    accounts.on_change = seen.append
    assert accounts.finish(q["state"], "code-work") == WORK
    with pytest.raises(GoogleError, match="expired or wasn't started here"):
        accounts.finish(q["state"], "code-work")  # the state is single use
    assert connect(accounts, fake, "code-home") == HOME
    assert seen == [WORK, HOME]
    assert [a["email"] for a in accounts.accounts()] == [WORK, HOME] and not any(a["error"] for a in accounts.accounts())
    again = GoogleAccounts(CLIENT_ID, SECRET, tmp_path / "google_tokens.json")  # kept across restarts
    assert sorted(again.emails()) == sorted([HOME, WORK])


def test_a_sign_in_must_come_back_in_time_and_from_here(accounts, fake, clock):
    with pytest.raises(GoogleError, match="wasn't started here"):
        accounts.finish("made-up-state", "code-work")
    url = accounts.begin("http://127.0.0.1:8765/api/google/callback")
    q = fake.authorize(url, "code-work")
    clock.t += g.PENDING_S + 1
    with pytest.raises(GoogleError, match="expired"):
        accounts.finish(q["state"], "code-work")
    url = accounts.begin("http://127.0.0.1:8765/api/google/callback")
    q = fake.authorize(url, "code-work")
    fake.challenges["code-work"] = "someone-elses-challenge"  # a code stolen from another sign-in
    with pytest.raises(GoogleError, match="refused"):
        accounts.finish(q["state"], "code-work")
    assert accounts.emails() == []


def test_says_when_a_box_was_left_unticked(accounts, fake, cfg):
    real = fake.__call__

    def drive_only(req):  # Google's consent page has a box per permission: J ticked Drive, not Calendar
        resp = real(req)
        if str(req.url) == g.TOKEN_URL and resp.status_code == 200 and "refresh_token" in resp.json():
            body = {**resp.json(), "scope": "openid email https://www.googleapis.com/auth/drive.readonly"}
            return httpx.Response(200, json=body)
        return resp
    accounts.http = httpx.Client(transport=httpx.MockTransport(drive_only))
    connect(accounts, fake, "code-work")
    assert accounts.accounts()[0]["missing"] == ["Google Calendar"]
    check = doctor.check_google(cfg, accounts)
    assert check.status == "warn" and "didn't allow Google Calendar" in check.detail and "tick every box" in check.fix
    accounts.http = httpx.Client(transport=httpx.MockTransport(fake))
    connect(accounts, fake, "code-work")  # Reconnect with every box ticked
    assert accounts.accounts()[0]["missing"] == []


def test_needs_the_key_before_signing_in(tmp_path):
    with pytest.raises(GoogleError, match="sign-in key"):
        GoogleAccounts("", "", tmp_path / "t.json").begin("http://127.0.0.1/api/google/callback")


def test_refreshes_and_says_when_google_ended_the_sign_in(accounts, fake, clock):
    connect(accounts, fake, "code-work")
    first = accounts._access(WORK)
    clock.t += 3600  # the hour is up: a fresh token from the refresh token, without asking J
    assert accounts._access(WORK) != first and accounts.emails() == [WORK]
    fake.revoked.add(f"rt-{WORK}")  # a test app's 7 days ran out, or J removed access
    clock.t += 3600
    with pytest.raises(GoogleError):
        accounts._access(WORK)
    [acct] = accounts.accounts()
    assert "Reconnect" in acct["error"] and accounts.emails() == []
    fake.revoked.clear()
    connect(accounts, fake, "code-work")  # Reconnect clears it
    assert accounts.emails() == [WORK]


def test_a_dropped_token_is_retried_once(accounts, fake):
    connect(accounts, fake, "code-work")
    fake.tokens.clear()  # Google forgot the access token early
    assert accounts.calendars(WORK)[0]["primary"] is True


def test_disconnect_forgets_the_account(accounts, fake, tmp_path):
    connect(accounts, fake, "code-work")
    assert accounts.remove(WORK) and accounts.emails() == []
    assert WORK not in (tmp_path / "google_tokens.json").read_text()
    assert accounts.remove(WORK) is False


# ---- Google Docs in the library -----------------------------------------------------------------------

def link(folder, name, doc_id, email=""):
    path = folder / name
    path.write_text(json.dumps({"doc_id": doc_id, "email": email, "url": f"https://docs.google.com/document/d/{doc_id}/edit"}))
    return path


def test_export_tries_the_links_account_first_then_the_others(accounts, fake):
    connect(accounts, fake, "code-work")
    connect(accounts, fake, "code-home")
    assert "hire two engineers" in accounts.export({"doc_id": "work-doc", "email": WORK}, ".gdoc")
    assert fake.calls[-1] == (WORK, "export work-doc")
    fake.calls.clear()
    # a doc shared with the work account, sitting in the personal Drive's "Shared with me"
    assert "hire two engineers" in accounts.export({"doc_id": "work-doc", "email": HOME}, ".gdoc")
    assert fake.calls == [(HOME, "export work-doc"), (WORK, "export work-doc")]
    assert "Ring light" in accounts.export({"doc_id": "home-sheet"}, ".gsheet")
    with pytest.raises(GoogleError, match="not found"):
        accounts.export({"doc_id": "nobodys"}, ".gdoc")
    with pytest.raises(GoogleError):
        accounts.export({"doc_id": "work-doc"}, ".pdf")


def test_reads_drive_links_old_and_new(tmp_path):
    assert read_link(link(tmp_path, "Plan.gdoc", "work-doc", WORK)) == {
        "doc_id": "work-doc", "email": WORK, "resource_key": "", "url": "https://docs.google.com/document/d/work-doc/edit"}
    old = tmp_path / "Old.gsheet"
    old.write_text(json.dumps({"url": "https://docs.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUvWx"}))
    assert read_link(old)["doc_id"] == "1AbCdEfGhIjKlMnOpQrStUvWx"
    bare = tmp_path / "Bare.gslides"
    bare.write_text(json.dumps({"doc_id": "1Deck_0123456789abcdefghij"}))
    assert read_link(bare)["url"] == "https://docs.google.com/presentation/d/1Deck_0123456789abcdefghij/edit"
    (tmp_path / "Broken.gdoc").write_text("not json")
    assert read_link(tmp_path / "Broken.gdoc") == {} and read_link(tmp_path / "Missing.gdoc") == {}


def test_library_reads_google_docs_once_an_account_is_connected(tmp_path, accounts, fake):
    drive = tmp_path / "My Drive"
    drive.mkdir()
    link(drive, "Q3 plan.gdoc", "work-doc", WORK)
    link(drive, "Gear costs.gsheet", "home-sheet", HOME)
    lib = Library(tmp_path / "library.db", [drive], pause=0,
                  cloud=lambda ln, ext: accounts.export(ln, ext) if accounts.emails() else None)
    try:
        lib.index()
        assert lib.google_counts() == {"read": 0, "names_only": 2}
        assert lib.search("engineers") == []  # only the name is known
        connect(accounts, fake, "code-work")
        assert lib.reread_links() == 2
        lib.index()
        assert lib.google_counts() == {"read": 1, "names_only": 1}  # the personal sheet waits for that account
        [hit] = lib.search("hire engineers")
        assert hit["title"] == "Q3 plan" and "Assured Space" in hit["passage"]
        connect(accounts, fake, "code-home")
        lib.reread_links()
        lib.index()
        assert lib.google_counts() == {"read": 2, "names_only": 0}
        assert lib.search("ring light")[0]["title"] == "Gear costs"
    finally:
        lib.close()


def test_a_failing_export_leaves_the_name(tmp_path):
    drive = tmp_path / "My Drive"
    drive.mkdir()
    link(drive, "Q3 plan.gdoc", "work-doc", WORK)

    def broken(ln, ext):
        raise GoogleError("Couldn't refresh the sign-in")
    lib = Library(tmp_path / "library.db", [drive], pause=0, cloud=broken)
    try:
        assert lib.index()["added"] == 1 and lib.google_counts() == {"read": 0, "names_only": 1}
        assert lib.find("Q3 plan")["url"].endswith("/work-doc/edit")
    finally:
        lib.close()


# ---- Google calendars ---------------------------------------------------------------------------------

def test_google_calendars_join_the_agenda(accounts, fake):
    connect(accounts, fake, "code-work")
    chosen = [{"account": WORK, "id": WORK, "name": "Work", "profile": "work"}]
    hub = CalendarHub([], "America/New_York", google=accounts, google_sources=lambda: chosen)
    hub.refresh(force=True)
    now = datetime.now(hub.tz)
    events = hub.events(now - timedelta(hours=1), now + timedelta(days=3))
    assert [e["title"] for e in events] == ["Standup", "Offsite"]  # both pages, the cancelled one left out
    assert events[1]["all_day"] and events[0]["calendar"] == "Work" and events[0]["profile"] == "work"
    assert hub.events(now - timedelta(hours=1), now + timedelta(days=3), profile="stream") == []
    [st] = hub.status()
    assert (st["kind"], st["account"], st["ok"]) == ("google", WORK, True)
    fake.revoked.add(f"rt-{WORK}")
    accounts._tokens[WORK]["expires_at"] = 0
    hub.refresh(force=True)
    [st] = hub.status()
    assert st["ok"] is False and st["error"]


def test_old_ical_links_can_be_turned_off(tmp_path):
    hub = CalendarHub([{"name": "Work", "url": str(tmp_path / "gone.ics")}], "America/New_York")
    hub.refresh(force=True)
    assert hub.status()[0]["ok"] is False
    hub.set_sources([])
    assert hub.status() == [] and hub.events(datetime.now(hub.tz), datetime.now(hub.tz) + timedelta(days=1)) == []


# ---- the HUD and the sign-in coming back ----------------------------------------------------------------

@pytest.fixture
def hud(cfg, svc, accounts, tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    drive = tmp_path / "My Drive"
    drive.mkdir()
    link(drive, "Q3 plan.gdoc", "work-doc", WORK)
    accounts.set_client("", "")  # as on a fresh install: no key yet
    svc.google = svc.calendars.google = accounts
    svc.library = Library(cfg.data_dir / "library.db", [drive], pause=0,
                          cloud=lambda ln, ext: svc.google.export(ln, ext) if svc.google.emails() else None)
    cfg["calendars"] = [{"name": "Old work link", "url": str(tmp_path / "gone.ics"), "profile": "work"}]
    svc.calendars.set_sources(cfg["calendars"])
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=False)
    with TestClient(app) as c:
        c.headers["X-Assistant-Token"] = app.state.token
        yield c, rt, opened
    for t in threading.enumerate():  # the read that follows each sign-in
        if t.name == "google":
            t.join(10)
    svc.library.close()


def test_hud_connects_both_accounts(hud, fake, cfg, svc, monkeypatch):
    import os

    c, rt, opened = hud
    st = c.get("/api/google").json()
    assert st["configured"] is False and st["accounts"] == []
    assert c.post("/api/google/connect", json={}).json()["ok"] is False  # no key yet
    bad = c.post("/api/google/client", json={"client_id": "my-project", "client_secret": "x"}).json()
    assert bad["ok"] is False and "apps.googleusercontent.com" in bad["error"]
    ok = c.post("/api/google/client", json={"client_id": f" {CLIENT_ID} ", "client_secret": SECRET}).json()
    assert ok["ok"] and ok["configured"]
    env = (cfg.root / ".env").read_text()
    assert f"GOOGLE_CLIENT_ID={CLIENT_ID}" in env and f"GOOGLE_CLIENT_SECRET={SECRET}" in env
    assert os.environ["GOOGLE_CLIENT_ID"] == CLIENT_ID
    monkeypatch.delenv("GOOGLE_CLIENT_ID")
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET")

    r = c.post("/api/google/connect", json={"login_hint": WORK}).json()
    assert r["ok"] and opened == [r["url"]]
    q = fake.authorize(r["url"], "code-work")
    assert q["redirect_uri"] == "http://testserver/api/google/callback"
    page = c.get("/api/google/callback", params={"state": q["state"], "code": "code-work"}, headers={"X-Assistant-Token": ""})
    assert page.status_code == 200 and f"Connected {WORK}" in page.text  # Google's redirect carries no token
    again = c.get("/api/google/callback", params={"state": q["state"], "code": "code-work"}, headers={"X-Assistant-Token": ""})
    assert again.status_code == 400 and "expired" in again.text
    deadline = time.time() + 10
    while time.time() < deadline and svc.library.google_counts()["read"] < 1:
        time.sleep(0.05)
    assert svc.library.google_counts() == {"read": 1, "names_only": 0}  # read right after connecting
    assert svc.library.search("engineers")

    r = c.post("/api/google/connect", json={}).json()
    q = fake.authorize(r["url"], "code-home")
    assert c.get("/api/google/callback", params={"state": q["state"], "code": "code-home"}).status_code == 200
    denied = c.get("/api/google/callback", params={"error": "access_denied"})
    assert denied.status_code == 400 and "cancelled" in denied.text
    assert [a["email"] for a in c.get("/api/state").json()["google"]["accounts"]] == [WORK, HOME]


def test_hud_picks_calendars_and_turns_off_the_failing_links(hud, fake, cfg, svc):
    c, rt, _ = hud
    c.post("/api/google/client", json={"client_id": CLIENT_ID, "client_secret": SECRET})
    for code in ("code-work", "code-home"):
        q = fake.authorize(c.post("/api/google/connect", json={}).json()["url"], code)
        c.get("/api/google/callback", params={"state": q["state"], "code": code})
    rt._poll_calendar()
    assert [x["name"] for x in c.get("/api/google").json()["ical"] if not x["ok"]] == ["Old work link"]
    cals = c.get("/api/google/calendars").json()["calendars"]
    assert [(x["account"], x["name"], x["on"], x["profile"]) for x in cals] == [
        (WORK, WORK, False, "work"), (HOME, HOME, False, "personal"), (HOME, "Family", False, "personal")]
    pick = [{**cals[0], "profile": "work"}, {**cals[2], "profile": "personal"}]
    r = c.post("/api/google/calendars", json={"calendars": pick, "drop_ical": True}).json()
    assert r["ok"] and [x["name"] for x in r["calendars"]] == [WORK, "Family"] and r["ical"] == []
    saved = (cfg.data_dir / "settings.yaml").read_text()
    assert "family@group.calendar.google.com" in saved and "Old work link" not in json.dumps(cfg["calendars"])
    assert {x["name"] for x in svc.calendars.status()} == {WORK, "Family"}
    titles = [e["title"] for e in svc.calendars.agenda(3)]
    assert "Standup" in titles and "Offsite" in titles
    assert [x["on"] for x in c.get("/api/google/calendars").json()["calendars"]] == [True, False, True]
    r = c.post("/api/google/remove", json={"email": HOME}).json()
    assert r["ok"] and [a["email"] for a in r["accounts"]] == [WORK]
    assert [x["account"] for x in r["calendars"]] == [WORK]  # its calendars leave the agenda too


def test_only_the_callback_skips_the_token(hud):
    c, _, _ = hud
    no = {"X-Assistant-Token": ""}
    assert c.get("/api/google", headers=no).status_code == 401
    assert c.post("/api/google/client", json={}, headers=no).status_code == 401
    assert c.get("/api/google/calendars", headers=no).status_code == 401
    assert c.get("/api/google/callback", params={"state": "x", "code": "y"}, headers={**no, "Host": "evil.example"}).status_code == 403


# ---- the health check ---------------------------------------------------------------------------------

def test_doctor_reports_each_account(cfg, accounts, fake):
    assert doctor.check_google(cfg, GoogleAccounts("", "")).status == "skip"
    assert doctor.check_google(cfg, accounts).status == "warn"
    connect(accounts, fake, "code-work")
    connect(accounts, fake, "code-home")
    ok = doctor.check_google(cfg, accounts)
    assert ok.status == "pass" and WORK in ok.detail and HOME in ok.detail
    accounts._tokens[WORK]["error"] = "Google ended the sign-in: click Reconnect."
    bad = doctor.check_google(cfg, accounts)
    assert bad.status == "fail" and WORK in bad.detail and "Reconnect" in bad.fix
