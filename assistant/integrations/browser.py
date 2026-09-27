"""Opening sites, tabs and searches — in Chrome when it's installed."""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus

SEARCH_ENGINES = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "github": "https://github.com/search?q={q}",
    "twitch": "https://www.twitch.tv/search?term={q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
}


def find_chrome() -> str | None:
    candidates = []
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(Path(base) / "Google/Chrome/Application/chrome.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    for c in candidates:
        if c.exists():
            return str(c)
    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def looks_like_url(text: str) -> bool:
    t = text.strip().lower()
    if t.startswith(("http://", "https://")):
        return True
    return " " not in t and "." in t and len(t.rsplit(".", 1)[-1]) >= 2


class Browser:
    def __init__(self, sites: dict[str, str] | None = None):
        self.sites = {k.lower(): v for k, v in (sites or {}).items()}
        self.chrome = find_chrome()

    def resolve(self, target: str) -> str | None:
        t = target.strip().lower().removeprefix("the ").removesuffix(" website").removesuffix(" site")
        if t in self.sites:
            return self.sites[t]
        close = difflib.get_close_matches(t, list(self.sites), n=1, cutoff=0.8)
        if close:
            return self.sites[close[0]]
        if looks_like_url(t):
            return t if t.startswith("http") else f"https://{t}"
        return None

    def open(self, urls: list[str] | str, new_window: bool = False) -> dict:
        if isinstance(urls, str):
            urls = [urls]
        resolved = []
        for u in urls:
            url = self.resolve(u) or (u if u.startswith("http") else None)
            if url:
                resolved.append(url)
        if not resolved:
            return {"ok": False, "error": f"I don't know a site called {', '.join(urls)}. Add it under 'sites' in config.yaml."}
        if self.chrome:
            args = [self.chrome, *(["--new-window"] if new_window else []), *resolved]
            subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            for i, url in enumerate(resolved):
                webbrowser.open(url, new=1 if (new_window and i == 0) else 2)
        return {"ok": True, "opened": resolved, "browser": "chrome" if self.chrome else "default"}

    def search(self, query: str, engine: str = "google") -> dict:
        template = SEARCH_ENGINES.get(engine.lower(), SEARCH_ENGINES["google"])
        return self.open(template.format(q=quote_plus(query)))

    def app_window(self, url: str) -> bool:
        """Open the dashboard as a chromeless app window (Chrome --app)."""
        if not self.chrome:
            return False
        subprocess.Popen(
            [self.chrome, f"--app={url}", "--window-size=1600,960"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
