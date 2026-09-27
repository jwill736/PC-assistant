"""Twitch live status via the Helix API (app access token, read-only)."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx


class TwitchClient:
    def __init__(self, channel: str, client_id: str, client_secret: str, enabled: bool = True):
        self.channel = channel.strip().lower()
        self.client_id, self.client_secret = client_id, client_secret
        self.enabled = enabled and bool(self.channel and client_id and client_secret)
        self._token: tuple[str, float] | None = None

    def _auth(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        resp = httpx.post("https://id.twitch.tv/oauth2/token", params={
            "client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "client_credentials",
        }, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._token[0]

    def status(self) -> dict:
        if not self.enabled:
            return {"enabled": False}
        try:
            headers = {"Client-Id": self.client_id, "Authorization": f"Bearer {self._auth()}"}
            resp = httpx.get("https://api.twitch.tv/helix/streams", params={"user_login": self.channel},
                             headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except httpx.HTTPError as exc:
            return {"enabled": True, "error": str(exc)}
        if not data:
            return {"enabled": True, "channel": self.channel, "live": False}
        s = data[0]
        started = datetime.fromisoformat(s["started_at"].replace("Z", "+00:00"))
        return {
            "enabled": True, "channel": self.channel, "live": True,
            "viewers": s.get("viewer_count"), "title": s.get("title"), "game": s.get("game_name"),
            "uptime_s": (datetime.now(timezone.utc) - started).total_seconds(),
        }
