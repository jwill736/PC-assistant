"""Small helpers for the skills that connect to an outside service (Spotify, Home Assistant, YouTube, Discord):
saving a pasted key to .env, PKCE for a sign-in, the address a sign-in comes back to, and "did the stream just start".

Not a skill itself (it isn't in MODULES).
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Any


def save_secret(svc: Any, name: str, value: str) -> None:
    """KEY=value in the .env next to config.yaml, and in this process at once. Never in config.yaml or a log."""
    from ..firstrun import set_env_value

    value = (value or "").strip()
    env = svc.cfg.root / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    text = set_env_value(text, name, value)
    env.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    os.environ[name] = value


def save_setting(skill: Any, key: str, value: Any) -> None:
    """skills.<name>.<key> in data/settings.yaml (kept for this run if the data folder is read-only)."""
    from ..config import save_setting as save

    skill.cfg[key] = value
    try:
        save(skill.svc.cfg, f"skills.{skill.name}.{key}", value)
    except Exception:
        pass


def pkce() -> tuple[str, str]:
    """(verifier, S256 challenge) for an installed-app sign-in."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def callback_url(svc: Any, skill: str) -> str:
    """Where a sign-in comes back: 127.0.0.1 (Spotify refuses "localhost") on Vesper's own port."""
    port = int(((svc.cfg.get("server") or {}).get("port")) or 8765)
    return f"http://127.0.0.1:{port}/api/skill/{skill}/callback"


class LiveEdge:
    """Call it every tick: "start" when the stream just went live, "end" when it just stopped, else None."""

    def __init__(self, svc: Any):
        self.svc, self.was = svc, False

    def __call__(self) -> str | None:
        try:
            live = bool(self.svc.live and self.svc.live())
        except Exception:
            live = self.was
        edge = "start" if live and not self.was else "end" if self.was and not live else None
        self.was = live
        return edge
