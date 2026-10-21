"""Spoken confirmations: ask once, listen ten seconds for a yes or no, then wait quietly on the HUD.

When an action needs a yes, Vesper asks once ("Post to Discord: “giveaway at 9”? Say yes or no."). As soon as the
question has been spoken, the mic takes a yes or a no for ``window_s`` seconds without the wake word, from your voice
only (a voice the speaker check doesn't recognise is ignored). No answer: Vesper says nothing more, and the HUD card
and the Windows toast keep the action waiting until it expires (two minutes). Answered by voice, the yes runs the
action and says what happened; the no says "Cancelled."

``tick()`` is the whole state machine, so tests drive it with a fake clock, speaker and listener; in the app a small
thread ticks it while something is waiting.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

_YES = (r"yes|yeah|yep|yup|ya|sure|ok|okay|confirm(?:ed)?|do it|go ahead|go for it|affirmative|correct|send it|"
        r"post it|absolutely|definitely|please do|let'?s go|let'?s do it|proceed")
_NO = (r"no|nope|nah|cancel|don'?t|do not|never ?mind|stop|abort|forget it|not now|hold off|negative|skip it|leave it")
YES = re.compile(rf"(?:{_YES})(?: (?:please|thanks|thank you|do it|go ahead|send it|post it|yes|that'?s right))*")
NO = re.compile(rf"(?:{_NO})(?: (?:thanks|thank you|thank|don'?t|do it|that|it|cancel|not now|wait))*")


def answer(text: str) -> str | None:
    """'Yeah, go ahead.' -> 'yes'; 'No thanks' -> 'no'; anything else (a new request, chatter) -> None."""
    t = re.sub(r"[^\w' ]+", " ", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(?:uh|um|oh|hmm|er|well) ", "", t)
    if YES.fullmatch(t):
        return "yes"
    if NO.fullmatch(t):
        return "no"
    return None


def question(what: str) -> str:
    """The one spoken question for a waiting action: 'post to Discord: “x”' -> 'Post to Discord: “x”? Say yes or no.'"""
    what = (what or "").strip().rstrip("?.")
    return f"{what[:1].upper()}{what[1:]}? Say yes or no." if what else "Should I go ahead? Say yes or no."


class ConfirmWindow:
    """Hooked in as ``Assistant.on_pending`` and as a voice tap (``Runtime.voice_taps``)."""

    def __init__(self, bus, speaker, listener: Callable[[], Any] | Any, assistant, *, toast: Callable | None = None,
                 window_s: float = 10.0, start_wait_s: float = 8.0, speak_wait_s: float = 30.0,
                 clock: Callable[[], float] = time.time, threads: bool = True):
        self.bus, self.speaker, self.assistant, self.toast = bus, speaker, assistant, toast
        self._listener = listener if callable(listener) else (lambda: listener)
        self.window_s, self.start_wait_s, self.speak_wait_s = window_s, start_wait_s, speak_wait_s
        self.clock, self.threads = clock, threads
        self.current: dict | None = None
        self._thinking = False
        self._heard_wake = False  # the utterance now arriving had the wake word in it
        self._lock = threading.RLock()
        bus.on(self.on_event)

    # ---- hooks ---------------------------------------------------------------------------------------------------
    def on_pending(self, view: dict | None) -> None:
        if not view:
            return
        with self._lock:
            self.current = {"id": view["id"], "text": view.get("text", ""), "tier": view.get("tier", 2),
                            "set_at": self.clock(), "phase": "asking", "spoke": False, "until": None,
                            "expires": self._expires(view["id"]), "toasted": False}
        if self.threads:
            threading.Thread(target=self._run, args=(view["id"],), name="confirm-window", daemon=True).start()

    def on_event(self, event: dict) -> None:
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "thinking":
            self._thinking = bool(data.get("active"))
        elif kind == "heard":
            self._heard_wake = bool(data.get("wake"))

    def tap(self, text: str, owner: str = "unknown"):
        """A voice command on its way to the assistant. While the window is open and nobody said the name: a yes or
        no from your voice goes through (as "yes" / "no"), anything else is dropped. None: not ours to judge."""
        with self._lock:
            cur = self.current
            if cur is None or cur["phase"] != "listening" or self._heard_wake or self._waiting_id() != cur["id"]:
                return None
            said = answer(text)
            if owner == "mismatch":
                self.bus.publish("heard", {"text": text, "ignored": "not your voice (waiting for your yes or no)"})
                return True
            if said is None:
                self.bus.publish("heard", {"text": text, "ignored": "waiting for a yes or no"})
                return True
            cur["phase"], cur["toasted"] = "waiting", True  # the answer is on its way; expiry still clears the card
            self._publish("answered", answer=said)
            return said

    # ---- the state machine -----------------------------------------------------------------------------------------
    def tick(self) -> str:
        """Advance; returns the phase: asking | listening | waiting | done."""
        with self._lock:
            cur = self.current
            if cur is None:
                return "done"
            now = self.clock()
            if now >= cur["expires"]:
                self.assistant.cancel(cur["id"])  # quietly: the card goes (unless it was replaced), nothing is said
                self._publish("expired")
                return self._done()
            if self._waiting_id() != cur["id"]:  # answered (voice, HUD, toast) or replaced
                return self._done()
            speaking = self.speaker.speaking.is_set()
            if cur["phase"] == "asking":
                if speaking:
                    cur["spoke"] = True
                elif self._thinking and now - cur["set_at"] < self.speak_wait_s:
                    pass  # the model is still writing the question
                elif cur["spoke"] or now - cur["set_at"] >= self.speak_wait_s:
                    self._open(now)
                elif now - cur["set_at"] >= self.start_wait_s:  # nothing was said (typed, or speech is off)
                    self._wait()
            elif cur["phase"] == "listening" and now >= cur["until"]:
                self._wait()
            return cur["phase"]

    def _open(self, now: float) -> None:
        cur = self.current
        listener = self._listener()
        if listener is None or not hasattr(listener, "arm") or getattr(listener, "muted", False) \
                or getattr(listener, "state", "") in ("off", "unavailable", "error", "disabled"):
            return self._wait()
        cur["phase"], cur["until"] = "listening", now + self.window_s
        try:
            listener.arm(self.window_s, source="confirm")  # no wake word; the speaker check still applies
        except Exception:
            log.exception("could not open the yes/no window")
            return self._wait()
        self._publish("listening", until=cur["until"])

    def _wait(self) -> None:
        """No answer (or no voice): say nothing more; the HUD card and the toast hold it until it expires."""
        cur = self.current
        cur["phase"] = "waiting"
        self._publish("waiting")
        if self.toast is not None and not cur["toasted"]:
            cur["toasted"] = True
            view = self.assistant.pending_view()
            if view and view["id"] == cur["id"]:
                try:
                    self.toast(view)
                except Exception:
                    log.exception("toast failed")

    def _done(self) -> str:
        self.current = None
        return "done"

    def _run(self, pid: str) -> None:
        while True:
            with self._lock:
                if self.current is None or self.current["id"] != pid:
                    return
            if self.tick() == "done":
                return
            time.sleep(0.1 if self.current and self.current["phase"] != "waiting" else 0.5)

    # ---- helpers ---------------------------------------------------------------------------------------------------
    def _waiting_id(self) -> str | None:
        view = self.assistant.pending_view()
        return view["id"] if view else None

    def _expires(self, pid: str) -> float:
        pending = getattr(self.assistant, "pending", None) or {}
        if pending.get("id") == pid and pending.get("expires"):
            return float(pending["expires"])
        return self.clock() + 120

    def _publish(self, state: str, **extra) -> None:
        cur = self.current or {}
        self.bus.publish("confirm_window", {"id": cur.get("id"), "state": state, "expires": cur.get("expires"), **extra})

    def status(self) -> dict | None:
        cur = self.current
        return None if cur is None else {"id": cur["id"], "state": cur["phase"], "until": cur["until"], "expires": cur["expires"]}
