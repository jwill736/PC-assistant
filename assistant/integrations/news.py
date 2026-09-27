"""Headlines from RSS/Atom feeds you choose (no API keys, no tracking)."""

from __future__ import annotations

import calendar
import html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import feedparser
import httpx

log = logging.getLogger(__name__)

DEFAULT_FEEDS = [
    {"name": "Hacker News", "url": "https://hnrss.org/frontpage?points=150", "topic": "tech"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "topic": "tech"},
    {"name": "AI (Google News)", "url": "https://news.google.com/rss/search?q=Anthropic+OR+OpenAI+OR+%22AI+model%22&hl=en-US&gl=US&ceid=US:en", "topic": "ai"},
    {"name": "Business (Google News)", "url": "https://news.google.com/rss/search?q=business+when:1d&hl=en-US&gl=US&ceid=US:en", "topic": "business"},
]

_TAG = re.compile(r"<[^>]+>")


def parse_feed(raw: bytes | str, source: str, topic: str) -> list[dict]:
    parsed = feedparser.parse(raw)
    items = []
    for entry in parsed.entries:
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        summary = html.unescape(_TAG.sub("", entry.get("summary", "") or "")).strip()
        items.append({
            "source": source,
            "topic": topic,
            "title": html.unescape(entry.get("title", "")).strip(),
            "link": entry.get("link", ""),
            "published": calendar.timegm(stamp) if stamp else None,
            "summary": summary[:240],
        })
    return items


class NewsFeed:
    def __init__(self, feeds: list[dict] | None = None, refresh_minutes: int = 15, max_items: int = 40):
        self.feeds = feeds or DEFAULT_FEEDS
        self.refresh_s = refresh_minutes * 60
        self.max_items = max_items
        self._items: list[dict] = []
        self._fetched = 0.0
        self._errors: dict[str, str] = {}
        self._lock = threading.Lock()

    def _fetch_one(self, feed: dict) -> list[dict]:
        try:
            resp = httpx.get(feed["url"], timeout=10, follow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0 (PC assistant news reader)"})
            resp.raise_for_status()
            self._errors.pop(feed["name"], None)
            return parse_feed(resp.content, feed["name"], feed.get("topic", "general"))
        except Exception as exc:
            self._errors[feed["name"]] = type(exc).__name__
            return []

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.time() - self._fetched < self.refresh_s:
                return
            with ThreadPoolExecutor(max_workers=6) as pool:
                batches = list(pool.map(self._fetch_one, self.feeds))
            items = [item for batch in batches for item in batch]
            if items or not self._items:
                seen, unique = set(), []
                for it in sorted(items, key=lambda i: i["published"] or 0, reverse=True):
                    key = it["title"].lower()
                    if key and key not in seen:
                        seen.add(key)
                        unique.append(it)
                self._items = unique[: self.max_items]
            self._fetched = time.time()

    def headlines(self, topic: str | None = None, limit: int = 12) -> list[dict]:
        self.refresh()
        items = self._items
        if topic:
            t = topic.lower()
            items = [i for i in items if i["topic"].lower() == t or t in i["title"].lower()]
        return items[:limit]

    def status(self) -> dict:
        return {"feeds": len(self.feeds), "items": len(self._items), "errors": dict(self._errors), "fetched": self._fetched}
