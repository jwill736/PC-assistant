"""Live captions: what J says, as captions on stream. Local only: the speech-to-text Vesper already runs, no cloud.

The tap is the listener's ``heard`` event: its voice detector and speech-to-text already run on every sentence the mic
catches (wake word or not), and it says what became of each one. In acoustic wake mode it skips sentences the name
model didn't fire on; while captions are showing it hears them all out (``listener.hear_all``), which costs about
0.05 CPU-core seconds per second of speech (Parakeet 110M; docs/VESPER-2.3-streaming.md has the numbers).

A sentence becomes a caption unless:
- it is Vesper's own voice coming back through the mic (the echo guard said "own voice"), or was heard over Vesper's
  reply;
- it was for Vesper: said with the name ("Vesper, …"), or in a window Vesper opened (an answer to its question, a
  conversation turn, push-to-talk);
- J's mic is muted in OBS (a call, someone in the room): viewers can't hear him, so it isn't captioned either, and
  what's up comes down. The mic is ``obs.mic_source``, else "Mic/Aux", the one stream health watches;
- the speaker check turned it away (someone else's voice), or it is a noise ("hmm");
- off the record is on: nothing is captioned until it ends.
Secrets in it become [secret] (privacy.redact, the same rule the log uses), and J's own words to hide
(``skills.stream_captions.hide``) become ***.

Where they show: the OBS overlay's Captions panel (a Browser source, ``/overlay?panels=captions&align=center``), and,
if ``skills.stream_captions.obs_source`` names one, an OBS Text source set over obs-websocket. Nothing is kept: the last
few lines live in memory for the overlay, never in a log or the database.

Off by default. "Vesper, captions on/off" or the switch on the Stream tab. Captions show while live, and for
``preview_s`` (3 minutes) after turning them on, so J can line them up in OBS first.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
import time
from collections import deque

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from ..integrations.stream_health import pick_mic
from ..privacy import redact
from . import Skill
from ._kit import save_setting

log = logging.getLogger(__name__)

PREVIEW_S = 180
KEEP_S = 7.0  # a line stays up this long after it was said
NOISE = {"hmm", "hm", "mm", "mmm", "mhm", "uh", "um", "uhm", "er", "ah", "oh", "huh"}
_ids = itertools.count(1)


def caption_text(heard: dict, wake_words: list[str], hide: list[str] | None = None) -> str | None:
    """What one ``heard`` event shows on stream, or None (see the module doc for what is left out)."""
    from ..voice.listener import split_wake

    text = re.sub(r"\s+", " ", str(heard.get("text") or "")).strip()
    if not text or heard.get("ignored") or heard.get("wake") or heard.get("armed") or heard.get("interrupted"):
        return None
    if wake_words and split_wake(text, wake_words)[0]:
        return None  # "Vesper, …" is a command, even when acoustic mode didn't take it as one
    if re.sub(r"[^a-z ]", "", text.lower()).strip() in NOISE:
        return None
    text = redact(text)
    for word in hide or []:
        if str(word).strip():
            text = re.sub(rf"(?i)\b{re.escape(str(word).strip())}\b", "***", text)
    return text[:200]


class StreamCaptions(Skill):
    name, title = "stream_captions", "Live captions"
    poll_s = 2  # takes an old line down from the OBS text source
    game_essential = True  # captions are for the stream
    delay_s = 5

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        self.lines: deque = deque(maxlen=4)
        self.preview_until = 0.0
        self.listener = None  # the runtime's VoiceListener, set at start
        self.off_record = lambda: False
        self.obs_error: str | None = None
        self._shown = ""  # what the OBS text source shows now
        self._want = ""
        self._sending = False
        self._olock = threading.Lock()  # the OBS text: what's wanted, what's shown, one sender at a time
        self._llock = threading.Lock()  # the lines: added on the mic thread, aged out on the tick
        self.threads = True  # tests send to OBS inline
        svc.bus.on(self._on_event)

    def start(self, runtime) -> None:
        a = getattr(runtime, "assistant", None)
        if a is not None and hasattr(a, "off_record"):
            self.off_record = a.off_record
        self.listener = getattr(runtime, "listener", None)
        if self.listener is not None:
            self.listener.hear_all = self.showing

    # ---- state ------------------------------------------------------------------------------------------------
    @property
    def on(self) -> bool:
        return bool(self.setting("on", False))

    def live(self) -> bool:
        try:
            return bool(self.svc.live and self.svc.live())
        except Exception:
            return False

    def showing(self) -> bool:
        """Captions are being made right now: on, and live (or just turned on, to check them in OBS)."""
        return self.on and (self.live() or self.clock() < self.preview_until)

    def _private(self) -> bool:
        try:
            return bool(self.off_record())
        except Exception:
            return True  # can't tell: caption nothing

    def _mic_muted(self) -> bool:
        """J's mic input is muted in OBS. Read from what OBS last reported (its mute events, or the 15 s re-read):
        nothing is asked of OBS here, on the mic thread."""
        read = getattr(self.svc.obs, "cached_inputs", None) if getattr(self.svc, "obs", None) is not None else None
        if read is None:
            return False
        try:
            inputs = [i for i in read() or [] if isinstance(i, dict)]
        except Exception:
            return False
        source = str((self.svc.cfg.get("obs") or {}).get("mic_source") or "")
        name = pick_mic([str(i.get("name") or "") for i in inputs], source)
        return any(i.get("name") == name and i.get("muted") for i in inputs)

    def current(self) -> list[dict]:
        now = self.clock()
        return [dict(x) for x in list(self.lines) if now - x["ts"] < float(self.setting("keep_s", KEEP_S))]

    # ---- the tap ----------------------------------------------------------------------------------------------
    def _on_event(self, ev: dict) -> None:
        if ev.get("type") == "heard" and isinstance(ev.get("data"), dict):
            self.hear(ev["data"])

    def hear(self, heard: dict) -> str | None:
        if not self.showing() or self._private() or self._mic_muted():
            return None
        text = caption_text(heard, self.svc.cfg["assistant"].get("wake_words") or [], self.setting("hide") or [])
        if not text:
            return None
        with self._llock:
            self.lines.append({"id": next(_ids), "text": text, "ts": self.clock()})
        self._to_obs()
        self.publish()
        return text

    # ---- the OBS text source ----------------------------------------------------------------------------------
    def _to_obs(self) -> None:
        source = str(self.setting("obs_source") or "").strip()
        if not source:
            return
        with self._olock:
            self._want = "\n".join(x["text"] for x in self.current()[-2:])
            if self._sending or self._want == self._shown:
                return
            self._sending = True
        if self.threads:  # off the mic thread: OBS can take a few seconds to answer when it's busy
            threading.Thread(target=self._send_loop, args=(source,), name="captions-obs", daemon=True).start()
        else:
            self._send_loop(source)

    def _send_loop(self, source: str) -> None:
        try:
            while True:
                with self._olock:
                    text = self._want
                    if text == self._shown:
                        return
                out = self.svc.obs.set_text(source, text)
                with self._olock:
                    self.obs_error = None if out.get("ok") else out.get("error")
                    self._shown = text if out.get("ok") else self._shown
                    if not out.get("ok"):
                        return
        finally:
            with self._olock:
                self._sending = False

    def tick(self) -> None:
        with self._llock:
            keep = self.current() if self.showing() and not self._mic_muted() else []
            old = len(self.lines) - len(keep)  # a line got old, captions stopped showing (the stream ended), or muted
            for _ in range(old):
                self.lines.popleft()
        if old:
            self._to_obs()
            self.publish()
        elif self.clock() - (self.last_status or {}).get("ts", 0) >= 30:
            self.publish()

    # ---- surface ----------------------------------------------------------------------------------------------
    def set_on(self, on: bool) -> dict:
        save_setting(self, "on", bool(on))
        self.preview_until = self.clock() + float(self.setting("preview_s", PREVIEW_S)) if on else 0.0
        if not on:
            with self._llock:
                self.lines.clear()
        self._to_obs()
        self.publish()
        return {"ok": True, "on": bool(on), "live": self.live(), "voice": self._voice_note(),
                "obs_source": str(self.setting("obs_source") or "")}

    def _voice_note(self) -> str | None:
        lst = self.listener
        if lst is None:
            return "Captions need the microphone, and voice is off (Setup → Voice)."
        if getattr(lst, "error", ""):
            return f"The microphone isn't working: {lst.error}"
        return None

    def set_source(self, name: str) -> dict:
        name = (name or "").strip()[:100]
        save_setting(self, "obs_source", name)
        self._shown, self.obs_error = "", None
        self._to_obs()
        self.publish()
        return {"ok": True, "obs_source": name}

    def overlay_link(self) -> dict:
        from ..overlay import load_overlay_token

        port = int(((self.svc.cfg.get("server") or {}).get("port")) or 8765)
        key = load_overlay_token(self.svc.cfg.data_dir)
        return {"ok": True, "url": f"http://127.0.0.1:{port}/overlay?panels=captions&theme=transparent&align=center&key={key}"}

    def status(self) -> dict:
        now = self.clock()
        return {"on": self.on, "showing": self.showing(), "live": self.live(),
                "preview_s": max(0, round(self.preview_until - now)) if self.on and not self.live() else 0,
                "lines": self.current() if self.showing() else [], "keep_s": float(self.setting("keep_s", KEEP_S)),
                "off_record": self._private(), "obs_source": str(self.setting("obs_source") or ""),
                "obs_error": self.obs_error, "voice": self._voice_note(), "ts": now}

    def actions(self):
        return {"toggle": lambda b: self.set_on(bool(b.get("on", not self.on))),
                "source": lambda b: self.set_source(str(b.get("name") or "")),
                "link": lambda b: self.overlay_link()}

    def tools(self):
        return [Tool("stream_captions", "Turn live captions on or off: what the user says shows as captions on stream "
                                        "(OBS overlay / text source), never Vesper's voice or commands to Vesper.",
                     obj({"on": {"type": "boolean"}}, ["on"]), lambda on=True: self.set_on(bool(on)), tier=1)]

    def route(self, t: str):
        m = re.fullmatch(r"(?:turn |switch |put )?(on|off) (?:the |my )?(?:live |stream )?(?:captions|subtitles|closed captions)"
                         r"|(?:turn |switch |put )?(?:the |my )?(?:live |stream )?(?:captions|subtitles|closed captions) (on|off)"
                         r"|(start|stop|enable|disable|show|hide) (?:the |my )?(?:live |stream )?(?:captions|subtitles|closed captions)", t)
        if m:
            word = m.group(1) or m.group(2) or m.group(3)
            return Intent("tool", "stream_captions", {"on": word in ("on", "start", "enable", "show")})
        return None

    def say(self, tool, args, result):
        if result.get("ok") is False:
            return result.get("error")
        if not result.get("on"):
            return "Captions off."
        if result.get("voice"):
            return f"Captions are on, but {result['voice'][0].lower()}{result['voice'][1:]}"
        where = f" and the OBS text “{result['obs_source']}”" if result.get("obs_source") else ""
        if result.get("live"):
            return f"Captions on: they go to the captions overlay{where}."
        return "Captions on. They show while you're live, and for the next 3 minutes so you can check them in OBS."
