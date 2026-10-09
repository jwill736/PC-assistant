"""Google accounts: sign in once per account (work and personal) and Vesper can read, never change:

- the text of your Google Docs, Sheets and Slides (a .gdoc in Google Drive for desktop is only a link, so the
  library asks Google for the words: Drive "export");
- your Google calendars, without the secret iCal links a Workspace admin can switch off.

Sign-in is Google's own page in your browser ("installed app" OAuth with PKCE); it comes back to Vesper on
127.0.0.1. You create the sign-in key once (a Google Cloud "Desktop app" client, docs/SETUP.md) because Google
gives every app its own. Tokens are kept in data/google_tokens.json on this PC.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
DRIVE = "https://www.googleapis.com/drive/v3"
CALENDAR = "https://www.googleapis.com/calendar/v3"
SCOPES = ["openid", "email", "https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/calendar.readonly"]
ALLOWS = {SCOPES[2]: "Google Drive", SCOPES[3]: "Google Calendar"}  # the boxes on Google's consent page
# What a Drive-for-desktop link stands for, and what Google can turn it into
EXPORT = {".gdoc": "text/plain", ".gsheet": "text/csv", ".gslides": "text/plain"}
OPEN_URL = {".gdoc": "https://docs.google.com/document/d/{}/edit",
            ".gsheet": "https://docs.google.com/spreadsheets/d/{}/edit",
            ".gslides": "https://docs.google.com/presentation/d/{}/edit"}
PENDING_S = 600  # a sign-in started in the HUD must come back within ten minutes
MAX_EXPORT = 2_000_000  # characters: past that it's a data dump, not a document


class GoogleError(Exception):
    pass


class GoogleAccounts:
    def __init__(self, client_id: str = "", client_secret: str = "", token_path: Path | str | None = None,
                 http: httpx.Client | None = None, clock: Callable[[], float] = time.time):
        self.client_id, self.client_secret = client_id or "", client_secret or ""
        self.token_path = Path(token_path) if token_path else None
        self.http = http or httpx.Client(timeout=30)
        self.clock = clock
        self._lock = threading.RLock()
        self._pending: dict[str, dict] = {}
        self._tokens: dict[str, dict] = self._load()
        self.on_change: Callable[[str], None] | None = None  # an account connected (email): read its docs again

    # ---- setup ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def set_client(self, client_id: str, client_secret: str) -> None:
        self.client_id, self.client_secret = client_id.strip(), client_secret.strip()

    def _load(self) -> dict:
        if not self.token_path or not self.token_path.exists():
            return {}
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        if not self.token_path:
            return
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._tokens, indent=2), encoding="utf-8")
        tmp.replace(self.token_path)

    def accounts(self) -> list[dict]:
        """``missing``: what was left unticked on Google's consent page (Reconnect and tick every box)."""
        with self._lock:
            return [{"email": email, "connected_at": t.get("connected_at"), "error": t.get("error"),
                     "missing": [name for scope, name in ALLOWS.items() if scope not in t.get("scope", "").split()]}
                    for email, t in sorted(self._tokens.items())]

    def emails(self) -> list[str]:
        with self._lock:
            return [e for e, t in self._tokens.items() if not t.get("error")]

    # ---- signing in --------------------------------------------------------------
    def begin(self, redirect_uri: str, login_hint: str = "") -> str:
        """The Google sign-in address for one more account (open it in the browser)."""
        if not self.configured:
            raise GoogleError("Add the Google sign-in key first (Setup → Google accounts).")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        with self._lock:
            now = self.clock()
            self._pending = {s: p for s, p in self._pending.items() if now - p["at"] < PENDING_S}
            self._pending[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "at": now}
        params = {"client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
                  "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge,
                  "code_challenge_method": "S256", "access_type": "offline",
                  "prompt": "consent select_account", "include_granted_scopes": "true"}
        if login_hint:
            params["login_hint"] = login_hint
        return f"{AUTH_URL}?{urlencode(params)}"

    def finish(self, state: str, code: str) -> str:
        """Google sent the browser back with a code: trade it for tokens. Returns the account's email."""
        with self._lock:
            pending = self._pending.pop(state or "", None)
        if not pending or self.clock() - pending["at"] > PENDING_S:
            raise GoogleError("That sign-in link expired or wasn't started here. Click Connect again.")
        resp = self.http.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret, "code": code,
            "code_verifier": pending["verifier"], "grant_type": "authorization_code",
            "redirect_uri": pending["redirect_uri"]})
        if resp.status_code != 200:
            raise GoogleError(f"Google refused the sign-in: {_reason(resp)}")
        tok = resp.json()
        who = self.http.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok['access_token']}"})
        email = (who.json().get("email") if who.status_code == 200 else "") or ""
        if not email:
            raise GoogleError("Google didn't say which account this is.")
        granted = set(tok.get("scope", "").split())
        missing = [name for scope, name in ALLOWS.items() if scope not in granted]
        with self._lock:
            old = self._tokens.get(email, {})
            self._tokens[email] = {
                "access_token": tok["access_token"], "expires_at": self.clock() + int(tok.get("expires_in", 3600)) - 60,
                "refresh_token": tok.get("refresh_token") or old.get("refresh_token"), "scope": tok.get("scope", ""),
                "connected_at": self.clock(), "error": None}
            self._save()
        if missing:
            log.warning("google: %s didn't grant %s", email, ", ".join(missing))
        if self.on_change:
            self.on_change(email)
        return email

    def remove(self, email: str) -> bool:
        with self._lock:
            tok = self._tokens.pop(email, None)
            self._save()
        if tok and tok.get("refresh_token"):
            try:  # tell Google too, so the access shows as removed in the account's security page
                self.http.post("https://oauth2.googleapis.com/revoke", data={"token": tok["refresh_token"]}, timeout=10)
            except httpx.HTTPError:
                pass
        return tok is not None

    def _access(self, email: str) -> str:
        with self._lock:
            tok = self._tokens.get(email)
            if not tok:
                raise GoogleError(f"{email} isn't connected.")
            if tok.get("error"):
                raise GoogleError(tok["error"])
            if self.clock() < tok.get("expires_at", 0):
                return tok["access_token"]
            if not tok.get("refresh_token"):
                tok["error"] = "Signed out: click Reconnect."
                self._save()
                raise GoogleError(tok["error"])
            resp = self.http.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                                   "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
            if resp.status_code != 200:
                if "invalid_grant" in resp.text:  # revoked, password changed, or a test app's 7 days ran out
                    tok["error"] = "Google ended the sign-in: click Reconnect."
                    self._save()
                raise GoogleError(f"Couldn't refresh the sign-in for {email}: {_reason(resp)}")
            data = resp.json()
            tok["access_token"] = data["access_token"]
            tok["expires_at"] = self.clock() + int(data.get("expires_in", 3600)) - 60
            self._save()
            return tok["access_token"]

    def get(self, email: str, url: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        resp = self.http.get(url, params=params, headers={"Authorization": f"Bearer {self._access(email)}",
                                                          **(headers or {})})
        if resp.status_code == 401:  # an access token Google already dropped: one fresh try
            with self._lock:
                self._tokens.get(email, {})["expires_at"] = 0
            resp = self.http.get(url, params=params, headers={"Authorization": f"Bearer {self._access(email)}",
                                                              **(headers or {})})
        return resp

    # ---- Drive -------------------------------------------------------------------
    def export(self, link: dict, ext: str) -> str:
        """The text of a Google Doc, Sheet (first tab, as CSV) or Slides deck, from its Drive-for-desktop link
        ({"doc_id", "email", "resource_key"}). Tries the account in the link first, then the others."""
        doc_id = link.get("doc_id") or ""
        if not doc_id or ext not in EXPORT:
            raise GoogleError("not a Google document link")
        order = [link["email"]] if link.get("email") in self.emails() else []
        order += [e for e in self.emails() if e not in order]
        if not order:
            raise GoogleError("no Google account connected")
        headers = {"X-Goog-Drive-Resource-Keys": f"{doc_id}/{link['resource_key']}"} if link.get("resource_key") else {}
        last = "not found"
        for email in order:
            try:
                resp = self.get(email, f"{DRIVE}/files/{doc_id}/export", {"mimeType": EXPORT[ext]}, headers)
            except GoogleError as exc:
                last = str(exc)
                continue
            if resp.status_code == 200:
                return resp.text[:MAX_EXPORT]
            last = _reason(resp)
        raise GoogleError(last)

    # ---- Calendar ----------------------------------------------------------------
    def calendars(self, email: str) -> list[dict]:
        resp = self.get(email, f"{CALENDAR}/users/me/calendarList", {"minAccessRole": "reader", "maxResults": 250})
        if resp.status_code != 200:
            raise GoogleError(_reason(resp))
        return [{"account": email, "id": c["id"], "name": c.get("summaryOverride") or c.get("summary") or c["id"],
                 "primary": bool(c.get("primary")), "color": c.get("backgroundColor")}
                for c in resp.json().get("items", [])]

    def events(self, email: str, calendar_id: str, start: datetime, end: datetime, tz: tzinfo) -> list[dict]:
        """Every event between start and end, repeats expanded, cancelled ones left out."""
        out, page = [], None
        for _ in range(20):
            params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                      "orderBy": "startTime", "maxResults": 2500}
            if page:
                params["pageToken"] = page
            resp = self.get(email, f"{CALENDAR}/calendars/{_quote(calendar_id)}/events", params)
            if resp.status_code != 200:
                raise GoogleError(_reason(resp))
            data = resp.json()
            for ev in data.get("items", []):
                norm = _event(ev, tz)
                if norm:
                    out.append(norm)
            page = data.get("nextPageToken")
            if not page:
                break
        return out


def _event(ev: dict, tz: tzinfo) -> dict | None:
    if ev.get("status") == "cancelled":
        return None
    s, e = ev.get("start") or {}, ev.get("end") or {}
    try:
        if "dateTime" in s:
            start = datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00")).astimezone(tz)
            end = datetime.fromisoformat((e.get("dateTime") or s["dateTime"]).replace("Z", "+00:00")).astimezone(tz)
            all_day = False
        else:
            start = datetime.fromisoformat(s["date"]).replace(tzinfo=tz)
            end = datetime.fromisoformat(e.get("date") or s["date"]).replace(tzinfo=tz)
            end = end if end > start else start + timedelta(days=1)
            all_day = True
    except (KeyError, ValueError):
        return None
    return {"title": ev.get("summary") or "(no title)", "start": start, "end": end, "all_day": all_day,
            "location": ev.get("location") or "", "description": (ev.get("description") or "")[:280]}


def _quote(calendar_id: str) -> str:
    from urllib.parse import quote

    return quote(calendar_id, safe="")


def _reason(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error")
        if isinstance(err, dict):
            return f"{err.get('message') or err.get('status') or resp.status_code}"
        return str(data.get("error_description") or err or resp.status_code)
    except ValueError:
        return f"HTTP {resp.status_code}"


def read_link(path: Path) -> dict:
    """What a Drive-for-desktop .gdoc/.gsheet/.gslides file points at: {doc_id, email, resource_key, url}."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    doc_id = data.get("doc_id") or ""
    if not doc_id and data.get("url"):  # the older Backup and Sync format: only a link
        import re

        m = re.search(r"(?:id=|/d/)([\w-]{20,})", data["url"])
        doc_id = m.group(1) if m else ""
    url = data.get("url") or (OPEN_URL.get(path.suffix.lower(), "").format(doc_id) if doc_id else "")
    return {"doc_id": doc_id, "email": data.get("email") or "", "resource_key": data.get("resource_key") or "",
            "url": url}
