"""Phone: talk to Vesper from your phone through a Telegram bot, and get urgent things there when you're away.

Setup (HUD → Setup → Phone): make a bot with @BotFather in Telegram, paste its token (saved to .env as
TELEGRAM_BOT_TOKEN), then send the bot ``/start <code>`` with the code the panel shows. The first chat to send the
right code is paired (data/phone.json); every other chat is ignored.

From the paired chat, any message runs as if you'd typed it on the PC and the reply comes back; a forwarded message
(someone else's words) never runs, Vesper asks what to do with it instead. Anything that needs
a yes on the PC needs one here too: Vesper answers "Confirm: …? Reply yes or no" with Yes/No buttons, and the answer
goes through the assistant's normal confirmation. /status, /mute [minutes], /unmute, /help; no screenshots.

Voice messages: downloaded (at most 2 minutes long, 2 MB, 6 in 10 minutes), decoded by ffmpeg (as the recorder does
for files), turned into text by the PC's own speech-to-text (nothing goes to a cloud) and then run exactly like a
typed message from the paired chat; the reply starts with "Heard: …" so a mishearing shows.

Push: while you're away from the PC (idle 5+ minutes), urgent notices and reminders also go to the phone, by
Telegram and/or an ntfy.sh topic (NTFY_TOPIC in .env). Long work started from the phone (a job, a recording's notes)
answers there when it's done if you're still away; a model test that finishes while you're away does too.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import httpx

from ..brain.tools import Tool, obj
from ..voice import recording as rec_mod
from . import Skill, check, lazy_client

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
TOKEN_ENV, NTFY_ENV = "TELEGRAM_BOT_TOKEN", "NTFY_TOPIC"
MAX_AGE_S = 600  # a message older than this (sent while Vesper was off) is not run
HELP = ("Send me anything you'd say to Vesper: “what's on my calendar”, “remind me at 6 to call Dana”. Voice "
        "messages work too.\n"
        "/status: what's going on at the PC\n/mute [minutes]: hold Vesper's spoken updates (default 60)\n"
        "/unmute: speak up again\n/unpair: forget this phone\nThings that need a yes ask first: reply yes or no.")
VOICE_MAX_S, VOICE_MAX_BYTES = 120, 2_000_000  # Telegram's voice notes are ~30 kbit/s Opus: 2 min is ~0.5 MB
VOICE_RATE = (6, 600)  # at most 6 voice messages fetched in 10 minutes
NO_FFMPEG = ("I got your voice message, but the PC needs ffmpeg to play it. At the PC: Win+R, type cmd, run "
             "“winget install Gyan.FFmpeg”, then run Update Vesper from the Start menu. Typing works meanwhile.")
FORWARDED = ("That's a forwarded message, so I didn't act on it: someone else's words aren't a command from you. "
             "Tell me what to do with it in your own message (copy in the part that matters).")
JOB_ACTIVE = ("queued", "planning", "running", "paused")
SCREEN = re.compile(r"\bscreen ?shots?\b|\bscreen ?(?:capture|grab)\b|\bwhat'?s on (?:my|the) screen\b")


class PhoneError(Exception):
    pass


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


class Phone(Skill):
    name, title = "phone", "Phone"
    poll_s = 20
    game_poll_s = 60  # you're at the PC: nothing waits to be pushed
    delay_s = 20

    def __init__(self, svc, http: httpx.Client | None = None, idle=None, clock=time.time, transcribe=None,
                 ffmpeg: str | None = None):
        super().__init__(svc)
        self.http = http or lazy_client(timeout=40, headers={"User-Agent": "Vesper/1.0"})
        self.clock = clock
        if idle is None:
            from ..integrations import desktop

            idle = desktop.idle_seconds
        self.idle = idle
        self.state = self._load()
        self.state.setdefault("code", new_code())
        self.state.setdefault("push_away", True)
        self.run_command = None  # set at start: runtime.command(text, source="phone")
        self.pending = lambda: None  # set at start: what's waiting on a yes
        self.spawn = lambda fn, *a: threading.Thread(target=fn, args=a, daemon=True, name="phone push").start()
        self.bot: str | None = None
        self.error = ""
        self.last_poll = self.last_message = 0.0
        self._pushed: dict[str, float] = {}
        self._bad, self._locked_until = int(self.state.get("bad") or 0), float(self.state.get("locked_until") or 0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ntfy_suggest = "vesper-" + secrets.token_urlsafe(12).replace("_", "x").replace("-", "y").lower()
        # voice messages: float32 16 kHz -> text (tests), else the listener's engine; ffmpeg None: look on PATH
        self._transcribe_fn, self.ffmpeg, self.decode = transcribe, ffmpeg, rec_mod.decode_file
        self.listener = self._stt = None
        self._fetched: deque[float] = deque()
        # long work started from the phone, answered there when it's done: job id -> state, recording ids
        self._jobs: dict[str, str] = {}
        self._recs: set[str] = set()
        self._ctx = lambda: {}  # set at start: the command being run (its source says it came from the phone)
        svc.bus.on(self._on_event)

    # ---- saved state -----------------------------------------------------------------------------------
    def _path(self):
        try:
            return self.svc.cfg.data_dir / "phone.json"
        except (AttributeError, KeyError):
            return None

    def _load(self) -> dict:
        p = self._path()
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p and p.exists() else {}
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        p = self._path()
        if p:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
            tmp.replace(p)

    def _save_env(self, key: str, value: str) -> None:
        from ..firstrun import set_env_value

        env = self.svc.cfg.root / ".env"
        text = env.read_text(encoding="utf-8") if env.exists() else ""
        text = set_env_value(text, key, value)
        env.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        os.environ[key] = value

    def token(self) -> str:
        return self.svc.cfg.secret(TOKEN_ENV)

    @property
    def chat_id(self):
        return self.state.get("chat_id")

    # ---- Telegram ----------------------------------------------------------------------------------------
    def api(self, method: str, payload: dict | None = None, *, timeout: float = 15, token: str | None = None):
        tok = token or self.token()
        if not tok:
            raise PhoneError("No Telegram bot token yet.")
        try:
            r = self.http.post(f"{API}/bot{tok}/{method}", json=payload or {}, timeout=timeout)
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:  # the message would carry the URL, and the URL the token
            raise PhoneError(f"Telegram didn't answer ({type(exc).__name__}).") from None
        if not data.get("ok"):
            raise PhoneError(str(data.get("description") or f"Telegram said {r.status_code}").replace(tok, "…"))
        return data.get("result")

    def send(self, text: str, *, buttons: list[tuple[str, str]] | None = None, chat_id=None) -> bool:
        cid = chat_id or self.chat_id
        if not cid or not self.token():
            return False
        payload = {"chat_id": cid, "text": text[:4000] or "…"}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": a, "callback_data": b} for a, b in buttons]]}
        try:
            self.api("sendMessage", payload)
            return True
        except PhoneError as exc:
            self.error = str(exc)
            return False

    def poll_once(self, timeout: int = 25) -> int:
        """One long poll: wait up to ``timeout`` seconds for messages and handle them."""
        if not self.bot:
            self.bot = (self.api("getMe") or {}).get("username")
        updates = self.api("getUpdates", {"offset": self.state.get("offset", 0), "timeout": timeout,
                                          "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 10)
        if self._stop.is_set():  # switched off while it waited: they're left for the next poll after it's back on
            return 0
        for u in updates or []:
            self.state["offset"] = int(u.get("update_id", 0)) + 1
            self._save()  # before running it: a crash must never run the same message twice
            try:
                self.handle(u)
            except Exception:
                log.exception("phone: a message failed")
        self.last_poll, self.error = self.clock(), ""
        return len(updates or [])

    def _loop(self, stop: threading.Event | None = None, before: threading.Thread | None = None) -> None:
        """``stop``: this run's own (a loop from before a stop() never runs on). ``before``: that loop, when the phone
        was switched off and on again within its long poll: it ends first, since one bot token takes one poll at a
        time (Telegram answers a second with "409 Conflict")."""
        stop = stop or self._stop
        if before is not None and before is not threading.current_thread():
            before.join(60)
        backoff = 5.0
        while not stop.is_set():
            if not self.token():
                stop.wait(5)
                continue
            try:
                self.poll_once()
                backoff = 5.0
            except Exception as exc:  # Telegram down, no internet, a bad token: wait and try again, never crash
                if stop.is_set():
                    break
                self.error = str(exc) if isinstance(exc, PhoneError) else f"Polling failed ({type(exc).__name__})."
                self.publish()
                stop.wait(backoff)
                backoff = min(backoff * 2, 300)

    # ---- messages in -------------------------------------------------------------------------------------
    def handle(self, u: dict) -> None:
        if "callback_query" in u:  # a Yes/No button
            cq = u["callback_query"]
            cid = ((cq.get("message") or {}).get("chat") or {}).get("id")
            if not self.chat_id or cid != self.chat_id:
                return
            try:
                self.api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            except PhoneError:
                pass
            m = re.fullmatch(r"(yes|no)(?::(\w{1,40}))?", str(cq.get("data") or ""))
            if m:  # the card's own question, never one that replaced it (a card from before ids: out of date)
                self._command(m.group(1), pending_id=m.group(2) or "(none)")
            return
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        cid, text = chat.get("id"), (msg.get("text") or "").strip()
        if cid is None or (msg.get("date") and self.clock() - float(msg["date"]) > MAX_AGE_S):
            return
        if not self.chat_id:
            self._pairing(cid, chat, text)
            return
        if cid != self.chat_id:
            return  # a stranger found the bot: say nothing
        self.last_message = self.clock()
        if msg.get("forward_origin") or msg.get("forward_date") or msg.get("forward_from"):
            self.send(FORWARDED)  # someone else's words (or J's from elsewhere): never run, nor learned, as his command
            return
        if not text and msg.get("voice"):
            heard, why = self.hear(msg["voice"])
            if heard:
                self.reply_to(heard, heard=True)
            else:
                self.send(why)
            return
        if not text:
            self.send("I can read text and voice messages.")
            return
        self.reply_to(text)

    def _pairing(self, cid, chat: dict, text: str) -> None:
        if not text.startswith("/start") or self.clock() < self._locked_until or chat.get("type", "private") != "private":
            return  # only a one-to-one chat can pair: never a group where others could talk to Vesper
        m = re.fullmatch(r"/start(?:@\w+)?\s+(\S+)", text)
        if m and hmac.compare_digest(m.group(1).encode(), str(self.state["code"]).encode()):
            who = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or "your phone"
            self.state.update(chat_id=cid, chat_name=who, paired_at=self.clock(), code=None, bad=0, lockouts=0)
            self._save()
            self._bad = 0
            self.send(f"Paired with Vesper. Send me anything you'd say to it.\n\n{HELP}")
            self.notify(f"Phone paired: {who}.")
            self.publish()
            return
        self._bad += 1
        self.state["bad"] = self._bad  # kept across restarts: a restart is no fresh set of guesses
        if self._bad >= 5:
            # Someone guessing: a new code, and no tries at all for 10 minutes, doubling each time (at most a day), so
            # a million codes at 5 guesses a lockout stays out of reach. The lockout survives a restart too.
            strikes = int(self.state.get("lockouts") or 0) + 1
            self._bad, self._locked_until = 0, self.clock() + min(600 * 2 ** (strikes - 1), 86400)
            self.state.update(code=new_code(), bad=0, lockouts=strikes, locked_until=self._locked_until)
            self._save()
            self.notify("Someone sent your Telegram bot wrong pairing codes. Pairing is locked for a while and the "
                        "code changed.", level="warning", key="phone:guessing")
            self.publish()
            return
        self._save()
        self.send("That code doesn't match. Check Vesper's HUD (Setup → Phone)." if m else
                  "Send /start followed by the 6-digit code on Vesper's HUD (Setup → Phone).", chat_id=cid)

    def reply_to(self, text: str, heard: bool = False) -> None:
        """``heard``: it was a voice message (never a /command): the reply says what was heard first."""
        low = text.lower().strip()
        cmd = low.split()[0].split("@")[0] if low.startswith("/") else ""
        if cmd == "/start":
            self.send("Already paired. /help shows what I can do.")
        elif cmd == "/help":
            self.send(HELP)
        elif cmd == "/status":
            self.send(self.status_text())
        elif cmd in ("/mute", "/unmute"):
            m = re.search(r"\d+", low)
            self.send(self.mute(0 if cmd == "/unmute" else float(m.group()) if m else 60))
        elif cmd == "/unpair":
            self.send("Unpaired. Pair again from Vesper's HUD.")
            self.unpair()
        elif cmd == "/screenshot" or SCREEN.search(low):
            self.send(self._heard(text, heard) + "Screenshots and what's on the screen don't go to the phone.")
        elif cmd:
            self.send("I don't know that command. /help")
        else:
            self._command(text, self._heard(text, heard))

    @staticmethod
    def _heard(text: str, heard: bool) -> str:
        return f"Heard: “{text}”\n\n" if heard else ""

    def _command(self, text: str, prefix: str = "", pending_id: str | None = None) -> None:
        """``pending_id``: a Yes/No button's question (it answers that one only)."""
        if self.run_command is None:
            self.send(prefix + "Vesper is still starting. Try again in a minute.")
            return
        try:
            self.api("sendChatAction", {"chat_id": self.chat_id, "action": "typing"})
        except PhoneError:
            pass
        try:
            out = (self.run_command(text, pending_id=pending_id) if pending_id else self.run_command(text)) or {}
        except Exception as exc:
            self.send(f"{prefix}That failed ({type(exc).__name__}).")
            return
        reply = str(out.get("reply") or "Done.")
        waiting = self.pending()
        if not waiting:
            self.send(prefix + reply)
            return
        reply = re.sub(r"\s*Say yes or (?:cancel|no)\.?\s*$", "", reply).strip()
        asked = str(waiting["text"]).lower()
        if reply.rstrip("?").lower() == asked:  # the spoken question: "Launch the rocket?"
            reply = ""
        if "confirm" not in reply.lower() and not reply.rstrip("?").lower().endswith(asked):
            reply = f"{reply}\n\nConfirm: {waiting['text']}?".strip()
        pid = waiting["id"]  # the buttons answer this question, and only this one
        self.send(f"{prefix}{reply} Reply yes or no.", buttons=[("Yes", f"yes:{pid}"), ("No", f"no:{pid}")])

    # ---- voice messages ------------------------------------------------------------------------------------
    def hear(self, voice: dict) -> tuple[str, str]:
        """A voice message -> (what was said, "") or ("", what to tell J). Size and rate are checked before anything
        is fetched; the audio file is deleted as soon as it's read."""
        if float(voice.get("duration") or 0) > VOICE_MAX_S or int(voice.get("file_size") or 0) > VOICE_MAX_BYTES:
            return "", f"That voice message is too long for me: keep it under {VOICE_MAX_S // 60} minutes, or type it."
        ff = self.ffmpeg if self.ffmpeg is not None else (shutil.which("ffmpeg") or "")
        if not ff:
            return "", NO_FFMPEG
        now = self.clock()
        while self._fetched and now - self._fetched[0] > VOICE_RATE[1]:
            self._fetched.popleft()
        if len(self._fetched) >= VOICE_RATE[0]:
            return "", "That's a lot of voice messages at once. Give me a few minutes, or type it."
        self._fetched.append(now)
        try:
            self.api("sendChatAction", {"chat_id": self.chat_id, "action": "typing"})
        except PhoneError:
            pass
        try:
            path = self._fetch(str(voice.get("file_id") or ""))
        except PhoneError as exc:
            return "", f"I couldn't fetch that voice message: {exc}"
        try:
            return self._listen(path, ff)
        finally:
            path.unlink(missing_ok=True)

    def _fetch(self, file_id: str) -> Path:
        info = self.api("getFile", {"file_id": file_id}) or {}
        where = str(info.get("file_path") or "")
        if not re.fullmatch(r"[\w./-]{1,200}", where) or ".." in where:
            raise PhoneError("Telegram didn't say where it is.")
        if int(info.get("file_size") or 0) > VOICE_MAX_BYTES:
            raise PhoneError("it's too big.")
        folder = Path(self.svc.cfg.data_dir) / "phone_voice"
        folder.mkdir(parents=True, exist_ok=True)
        path, got = folder / f"{secrets.token_hex(8)}.ogg", 0
        try:
            with self.http.stream("GET", f"{API}/file/bot{self.token()}/{where}", timeout=30) as r, \
                    open(path, "wb") as out:
                if r.status_code != 200:
                    raise PhoneError(f"Telegram said {r.status_code}.")
                for chunk in r.iter_bytes():
                    got += len(chunk)
                    if got > VOICE_MAX_BYTES:  # the size Telegram gave can be missing or wrong: count it here too
                        raise PhoneError("it's too big.")
                    out.write(chunk)
        except (PhoneError, httpx.HTTPError, OSError) as exc:
            path.unlink(missing_ok=True)
            raise exc if isinstance(exc, PhoneError) else PhoneError(f"{type(exc).__name__}.") from None  # no token
        return path

    def _listen(self, path: Path, ff: str) -> tuple[str, str]:
        import numpy as np

        try:
            frames = list(self.decode(path, ff, runner=_popen))
        except rec_mod.DecodeError as exc:
            return "", f"I couldn't play that voice message: {exc}"
        if not frames:
            return "", "That voice message was empty."
        samples = np.concatenate(frames).astype(np.float32) / 32768.0
        try:
            text = self._transcribe(samples)
        except Exception as exc:  # no speech engine installed, its model missing
            log.warning("phone: voice message not transcribed: %s", exc)
            return "", ("Speech-to-text isn't working on the PC, so I can't read voice messages right now "
                        f"({str(exc)[:120] or type(exc).__name__}). Run Update Vesper on the PC. Typing works meanwhile.")
        text = re.sub(r"\s+", " ", text or "").strip()
        if text.lower().strip(" .,!?") in rec_mod.HALLUCINATIONS:  # silence, wind, a pocket
            return "", "I couldn't make out any words in that voice message."
        return text, ""

    def _transcribe(self, samples) -> str:
        if self._transcribe_fn is not None:
            return self._transcribe_fn(samples)
        lst = self.listener
        if lst is not None and hasattr(lst, "transcribe"):  # the listener's engine, with its wake word and app names
            text = lst.transcribe(samples)
            return lst.correct(text) if hasattr(lst, "correct") else text
        if self._stt is None:  # voice listening is off: load the engine ourselves (as the recorder does)
            from ..voice import stt

            self._stt = stt.load(self.svc.cfg.get("voice") or {}, Path(self.svc.cfg.data_dir) / "models")
        return self._stt.transcribe(samples)

    def status_text(self) -> str:
        svc, lines = self.svc, []
        try:
            lines.append("Live on stream now." if svc.live and svc.live() else "Not streaming.")
        except Exception:
            pass
        try:
            now = datetime.now(svc.tz)
            nxt = next((e for e in svc.calendars.agenda(1)
                        if not e.get("all_day") and datetime.fromisoformat(e["start"]) > now), None)
            if nxt:
                at = datetime.fromisoformat(nxt["start"]).strftime("%I:%M %p").lstrip("0")
                lines.append(f"Next meeting: {nxt['title']} at {at}.")
            else:
                lines.append("No more meetings today.")
        except Exception:
            pass
        try:
            gm = svc.gmail
            if gm is not None and gm.accounts():
                n = sum(a.get("waiting", 0) for a in gm.needs_you().get("accounts", []))
                lines.append(f"{n} email{'s' if n != 1 else ''} waiting on you." if n else "No emails waiting on you.")
        except Exception:
            pass
        idle = self._idle()
        lines.append(f"PC idle {int(idle // 60)} min." if idle >= 120 else "Someone's at the PC.")
        return "\n".join(lines)

    def mute(self, minutes: float) -> str:
        nud = self.svc.skills.get("nudges") if self.svc.skills is not None else None
        if nud is None:
            return "Nudges are off, so there's nothing to mute."
        nud.mute(minutes)
        return "Vesper will speak up again." if not minutes else f"Quiet for {int(minutes)} minutes."

    # ---- pushes out ----------------------------------------------------------------------------------------
    def _idle(self) -> float:
        try:
            return float(self.idle() or 0)
        except Exception:
            return 0.0

    def away(self) -> bool:
        return self._idle() > float(self.setting("away_after_s", 300))

    def _on_event(self, ev: dict) -> None:
        t, d = ev.get("type"), ev.get("data") or {}
        if t == "notice":
            if d.get("level") == "urgent":
                self._maybe_push(str(d.get("text") or ""), d.get("key"))
        elif t == "tasks_agent":
            self._jobs_changed(d.get("jobs") or [])
        elif t == "tool" and self._ctx().get("source") == "phone":
            if d.get("name") in ("recording_start", "recording_stop", "notes_from_file"):
                rid = (d.get("result") or {}).get("id")
                if rid:
                    self._recs = set(list(self._recs)[-20:]) | {str(rid)}
        elif t == "recorder":
            self._recs_changed(d.get("recent") or [])
        elif t == "brain_bench" and not d.get("running") and d.get("summary"):
            if self.state.get("push_away", True) and self.away():  # started at the PC or on its own: a push
                self.spawn(self.send, f"Vesper: model test done. {d['summary']}")

    def _answer_later(self, text: str) -> None:
        """The answer to something asked from the phone, when it's ready: there, if J is still away from the PC
        (at the PC the nudge and the HUD have it). Not a push, so "push when away" doesn't hold it back."""
        if self.chat_id and text and self.away():
            self.spawn(self.send, text[:3500])

    def _jobs_changed(self, jobs: list[dict]) -> None:
        for j in jobs:
            jid, state = str(j.get("id") or ""), j.get("state")
            if j.get("source") != "phone" or not jid:
                continue
            if state in JOB_ACTIVE:
                self._jobs[jid] = state
                continue
            if self._jobs.pop(jid, None) is None:
                continue  # never seen running: it finished before this run of Vesper
            title, r = j.get("title") or "The job", j.get("result") or {}
            if state == "failed":
                self._answer_later(f"{title} didn't work: {j.get('error') or 'every step failed'}")
            elif state == "waiting" and j.get("batch"):
                ref = " ".join(re.sub(r"[^a-z0-9 ]+", " ", title.lower().split(":")[0]).split()[:3]) or "last"
                self._answer_later(f"{title} is ready. It waits on your OK to {j['batch']}: reply “approve the {ref} "
                                   "job”.")
            elif state == "done" and "phone" not in (j.get("deliver") or []):  # "…and text it to me" already did
                self._answer_later(f"{title} is done. {r.get('summary') or ''}".strip()
                                   + (" The full write-up is in Jobs on the PC." if r.get("note") else ""))

    def _recs_changed(self, recent: list[dict]) -> None:
        for r in recent:
            rid = str(r.get("id") or "")
            if rid not in self._recs or r.get("state") not in ("done", "error"):
                continue
            self._recs.discard(rid)
            title = r.get("title") or "the recording"
            if r["state"] == "error":
                self._answer_later(f"The notes from {title} didn't work: {r.get('error') or 'unknown error'}")
                continue
            n = int(r.get("tasks") or 0)
            self._answer_later(f"Notes ready: {title}. {r.get('summary') or ''}".strip()
                               + (f" {n} action item{'s' if n != 1 else ''}: reply “add the recording tasks” to add "
                                  f"{'them' if n != 1 else 'it'}." if n and r.get("tasks_state") == "ask" else ""))

    def _maybe_push(self, text: str, key: str | None) -> None:
        if not text or not self.state.get("push_away", True) or not self.away():
            return
        key = key or text[:60]
        now = self.clock()
        self._pushed = {k: t for k, t in self._pushed.items() if now - t < 6 * 3600}
        if key in self._pushed:
            return
        self._pushed[key] = now
        self.spawn(self.push, text)

    def push(self, text: str) -> dict:
        """To the phone by every channel that's set up: Telegram (paired) and ntfy."""
        sent = []
        if self.chat_id and self.token() and self.send(f"Vesper: {text}"):
            sent.append("telegram")
        topic = self.svc.cfg.secret(NTFY_ENV)
        if topic:
            server = str(self.setting("ntfy_server", "https://ntfy.sh")).rstrip("/")
            try:
                r = self.http.post(f"{server}/{topic}", content=text.encode("utf-8"), timeout=15,
                                   headers={"Title": "Vesper", "Priority": "high", "Tags": "bell"})
                if r.status_code < 300:
                    sent.append("ntfy")
                else:
                    self.error = f"ntfy said {r.status_code}."
            except httpx.HTTPError as exc:
                self.error = f"ntfy didn't answer ({type(exc).__name__})."
        return {"ok": bool(sent), "sent": sent, **({} if sent else {"error": self.error or "No phone is set up."})}

    def tick(self) -> None:
        """Reminders and urgent nudges wait in the nudges queue while you're away: send those to the phone."""
        nud = self.svc.skills.get("nudges") if self.svc.skills is not None else None
        if nud is not None and self.away():
            for n in list(getattr(nud, "queue", [])):
                if n.priority == "urgent":
                    self._maybe_push(n.text, n.key)
        self.publish()

    # ---- setup from the HUD ---------------------------------------------------------------------------------
    def set_token(self, token: str) -> dict:
        token = (token or "").strip()
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token):
            return {"ok": False, "error": "That doesn't look like a bot token. It looks like 123456789:AA… (BotFather "
                                          "sends it after /newbot)."}
        try:
            me = self.api("getMe", token=token) or {}
            self.api("deleteWebhook", token=token)  # polling only works without one
        except PhoneError as exc:
            return {"ok": False, "error": f"Telegram didn't accept it: {exc}"}
        self._save_env(TOKEN_ENV, token)
        self.bot, self.error = me.get("username"), ""
        return {"ok": True, "bot": self.bot}

    def unpair(self) -> dict:
        self.state.update(chat_id=None, chat_name=None, paired_at=None, code=new_code())
        self._save()
        self.publish()
        return {"ok": True}

    def set_ntfy(self, topic: str) -> dict:
        topic = (topic or "").strip()
        if topic and not re.fullmatch(r"[A-Za-z0-9_-]{12,64}", topic):
            return {"ok": False, "error": "Use 12+ letters, digits, - or _ (anyone who knows the topic can read it)."}
        self._save_env(NTFY_ENV, topic)
        return {"ok": True}

    def test(self) -> dict:
        if not self.chat_id and not self.svc.cfg.secret(NTFY_ENV):
            return {"ok": False, "error": "Pair your phone or set an ntfy topic first."}
        return self.push("Test from your PC. Replies work too: try /status.")

    def selftest(self) -> list[dict]:
        if not self.token():
            return [check("Telegram", "skip", "not set up", "Optional: Setup → Phone")]
        try:
            me = self.api("getMe", timeout=8) or {}
        except PhoneError as exc:
            return [check("Telegram", "fail", f"the bot token doesn't work: {exc}", "Setup → Phone → Change token")]
        paired = bool(self.chat_id)
        ff = self.ffmpeg if self.ffmpeg is not None else shutil.which("ffmpeg")
        return [check("Telegram", "pass" if paired else "warn",
                      f"bot @{me.get('username')} answers" + ("; phone paired" if paired else "; no phone paired yet"),
                      "" if paired else "Setup → Phone: send the bot /start with the code shown"),
                check("Phone voice messages", "pass" if ff else "skip", "ffmpeg found" if ff else "need ffmpeg",
                      "" if ff else "Win+R, cmd: winget install Gyan.FFmpeg, then Update Vesper")]

    def status(self) -> dict:
        topic = self.svc.cfg.secret(NTFY_ENV)
        return {"token": bool(self.token()), "bot": self.bot, "paired": bool(self.chat_id),
                "chat": self.state.get("chat_name"), "paired_at": self.state.get("paired_at"),
                "code": None if self.chat_id else self.state.get("code"),
                "pair_link": f"https://t.me/{self.bot}?start={self.state.get('code')}" if self.bot and not self.chat_id else None,
                "bot_link": f"https://t.me/{self.bot}" if self.bot else None,
                "locked": self.clock() < self._locked_until, "push_away": bool(self.state.get("push_away", True)),
                "ntfy": bool(topic), "ntfy_hint": (topic[:6] + "…") if topic else None, "ntfy_suggest": self.ntfy_suggest,
                "polling": bool(self._thread and self._thread.is_alive()), "last_poll": self.last_poll or None,
                "last_message": self.last_message or None, "away": self.away(), "error": self.error or None}

    def actions(self):
        return {"token": lambda b: self.set_token(str(b.get("token") or "")),
                "unpair": lambda b: self.unpair(),
                "new_code": lambda b: self.unpair(),
                "test": lambda b: self.test(),
                "push_away": lambda b: self._set_push(bool(b.get("on"))),
                "ntfy": lambda b: self.set_ntfy(str(b.get("topic") or ""))}

    def _set_push(self, on: bool) -> dict:
        self.state["push_away"] = on
        self._save()
        return {"ok": True}

    def tools(self):
        return [Tool("send_to_phone", "Send a short text to the user's phone (Telegram/ntfy), e.g. an address or a "
                                      "link they asked for.", obj({"text": {"type": "string"}}, ["text"]),
                     lambda text: self.push(text), tier=1)]

    def say(self, tool, args, result):
        return "Sent to your phone." if tool == "send_to_phone" else None

    def start(self, runtime) -> None:
        self.run_command = lambda text, pending_id=None: (
            runtime.command(text, source="phone", pending_id=pending_id) if pending_id
            else runtime.command(text, source="phone"))
        self._stop.set()  # turned back on in Setup → Skills: the last loop ends after its poll, a new one takes over
        before, self._stop = self._thread, threading.Event()
        self.pending = runtime.assistant.pending_view
        self._ctx = lambda: getattr(runtime.assistant, "_ctx", None) or {}
        self.listener = getattr(runtime, "listener", None)
        self._thread = threading.Thread(target=self._loop, args=(self._stop, before), name="phone", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def _popen(args, **kw):
    """ffmpeg without a console window flashing up on Windows."""
    return subprocess.Popen(args, creationflags=0x08000000 if sys.platform == "win32" else 0, **kw)
