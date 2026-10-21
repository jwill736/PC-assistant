"""The short replies Vesper says most, kept as ready-made audio so they play at once.

Neural synthesis costs ~150 ms before the first sound even for "Done."; a cached phrase costs nothing. The common
ones are made ahead when you wake Vesper (``Speaker.prewarm``), and any other short sentence is kept after it's
said once. Audio is kept per voice (engine, voice, speed): switching voices simply misses until it's refilled.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

COMMON = ("Done.", "Okay.", "On it.", "Got it.", "Sure.", "Cancelled.", "Copied.", "Noted.", "Stopped.", "Go ahead.",
          "Yes?", "Okay, done.", "Nothing waiting on a yes.")


class PhraseCache:
    def __init__(self, max_items: int = 96, max_words: int = 5):
        self.max_items, self.max_words = max_items, max_words
        self._items: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join((text or "").split())

    def cacheable(self, text: str) -> bool:
        t = self._norm(text)
        return bool(t) and len(t.split()) <= self.max_words

    def get(self, key, text: str):
        if not self.cacheable(text):
            return None
        with self._lock:
            audio = self._items.get((key, self._norm(text)))
            if audio is None:
                self.misses += 1
                return None
            self._items.move_to_end((key, self._norm(text)))
            self.hits += 1
            return audio

    def put(self, key, text: str, audio) -> None:
        if not self.cacheable(text) or audio is None:
            return
        with self._lock:
            self._items[(key, self._norm(text))] = audio
            self._items.move_to_end((key, self._norm(text)))
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

    def missing(self, key) -> list[str]:
        with self._lock:
            return [p for p in COMMON if (key, p) not in self._items]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
