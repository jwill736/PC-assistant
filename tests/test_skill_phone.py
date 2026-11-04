"""Phone bridge: pairing, strangers ignored, commands both ways, confirmations by reply, pushes when away."""

import json
import time
import types
from types import SimpleNamespace

import httpx
import pytest

from assistant.brain.router import Intent
from assistant.brain.tools import Tool, obj
from assistant.skills import Skill, Skills
from assistant.skills import phone as ph

TOKEN = "123456789:AAH" + "x" * 32
ME, STRANGER = 111, 222


class FakeTelegram:
    def __init__(self):
        self.updates, self.sent, self.ntfy, self.calls, self.uid = [], [], [], [], 0
        self.down, self.files, self.claimed = False, {}, {}

    def queue(self, **u):
        self.uid += 1
        self.updates.append({"update_id": self.uid, **u})

    def msg(self, text, chat=ME, age=0, **chat_extra):
        self.queue(message={"message_id": self.uid, "date": int(time.time() - age), "text": text,
                            "chat": {"id": chat, "type": "private", "first_name": "J", **chat_extra}})

    def button(self, data, chat=ME):
        self.queue(callback_query={"id": f"cb{self.uid}", "data": data, "message": {"chat": {"id": chat}}})

    def voice(self, file_id, data=b"OggS-fake", duration=3, chat=ME, date=None, size=None):
        self.files[file_id], self.claimed[file_id] = data, len(data) if size is None else size
        self.queue(message={"message_id": self.uid, "date": int(time.time() if date is None else date),
                            "voice": {"file_id": file_id, "duration": duration, "mime_type": "audio/ogg",
                                      "file_size": self.claimed[file_id]},
                            "chat": {"id": chat, "type": "private", "first_name": "J"}})

    def __call__(self, req: httpx.Request):
        if self.down:
            raise httpx.ConnectError("no route", request=req)
        if req.url.path.startswith("/file/"):  # a voice message's audio
            self.calls.append("download")
            if req.url.path.split("/")[2][3:] != TOKEN:
                return httpx.Response(404)
            return httpx.Response(200, content=self.files.get(req.url.path.rsplit("/", 1)[-1].split(".")[0], b""))
        if req.url.host == "ntfy.sh":
            self.ntfy.append((req.url.path, req.content.decode(), req.headers.get("Priority")))
            return httpx.Response(200, json={"id": "x"})
        token, method = req.url.path.split("/")[1][3:], req.url.path.split("/")[2]
        if token != TOKEN:
            return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
        body = json.loads(req.content or b"{}")
        self.calls.append(method)
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "is_bot": True, "username": "jvesper_bot"}})
        if method == "getUpdates":
            out = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
            return httpx.Response(200, json={"ok": True, "result": out})
        if method == "sendMessage":
            self.sent.append(body)
        if method == "getFile":
            data = self.files.get(body.get("file_id"))
            if data is None:
                return httpx.Response(400, json={"ok": False, "description": "Bad Request: invalid file_id"})
            size = self.claimed.get(body["file_id"], len(data))  # what Telegram says, not always what it sends
            return httpx.Response(200, json={"ok": True, "result": {"file_id": body["file_id"], "file_size": size,
                                                                    "file_path": f"voice/{body['file_id']}.oga"}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def texts(self, chat=ME):
        return [m["text"] for m in self.sent if m["chat_id"] == chat]


@pytest.fixture
def tg(svc, monkeypatch):
    monkeypatch.setenv(ph.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ph.NTFY_ENV, "")
    # the Phone that build_services made listens on the bus too, with the real idle time: only ours may push
    svc.bus._listeners = [f for f in svc.bus._listeners if not isinstance(getattr(f, "__self__", None), ph.Phone)]
    fake = FakeTelegram()
    idle = {"s": 0}
    p = ph.Phone(svc, http=httpx.Client(transport=httpx.MockTransport(fake)), idle=lambda: idle["s"])
    p.spawn = lambda fn, *a: fn(*a)  # pushes run inline in tests
    ran = []
    p.run_command = lambda text: ran.append(text) or {"reply": f"Did: {text}."}
    fake.p, fake.ran, fake.idle = p, ran, idle
    return fake


def pair(tg):
    tg.msg(f"/start {tg.p.state['code']}")
    tg.p.poll_once(timeout=0)


def test_pairing_needs_the_code_from_the_hud(tg, cfg):
    p = tg.p
    code = p.status()["code"]
    assert len(code) == 6 and not p.status()["paired"]
    tg.msg("/start")
    tg.msg("/start 99999x")
    tg.msg("hello")  # not /start: ignored
    tg.msg(f"/start {code}", chat=-5, type="group")  # a group can never pair
    p.poll_once(timeout=0)
    assert not p.status()["paired"] and len(tg.texts()) == 2 and "code" in tg.texts()[0]
    assert p.status()["pair_link"] == f"https://t.me/jvesper_bot?start={code}"
    pair(tg)
    st = p.status()
    assert st["paired"] and st["chat"] == "J" and st["code"] is None and "Paired" in tg.texts()[-1]
    assert json.loads((cfg.data_dir / "phone.json").read_text())["chat_id"] == ME
    assert ph.Phone(tg.p.svc, idle=lambda: 0).chat_id == ME  # kept across restarts
    assert tg.ran == []


def test_strangers_are_ignored_and_cannot_pair_later(tg):
    pair(tg)
    n = len(tg.sent)
    tg.msg("/start 123456", chat=STRANGER)
    tg.msg("shut down the pc", chat=STRANGER)
    tg.button("yes", chat=STRANGER)
    tg.p.poll_once(timeout=0)
    assert len(tg.sent) == n and tg.ran == []


def test_guessing_the_code_locks_pairing(tg):
    first = tg.p.state["code"]
    for i in range(5):
        tg.msg(f"/start {i:06d}", chat=STRANGER)
    tg.msg(f"/start {first}", chat=STRANGER)  # even the old right code: locked, and the code changed
    tg.p.poll_once(timeout=0)
    assert not tg.p.status()["paired"] and tg.p.status()["locked"] and tg.p.state["code"] != first


def test_messages_run_as_commands_and_replies_come_back(tg):
    pair(tg)
    tg.msg("what's on my calendar")
    tg.msg("old one", age=3600)  # sent while Vesper was off: not run
    tg.p.poll_once(timeout=0)
    assert tg.ran == ["what's on my calendar"] and tg.texts()[-1] == "Did: what's on my calendar."
    assert "sendChatAction" in tg.calls
    tg.p.poll_once(timeout=0)  # the offset moved on: nothing runs twice
    assert tg.ran == ["what's on my calendar"]


def test_phone_commands_status_mute_and_no_screenshots(tg, svc):
    svc.live = lambda: True
    pair(tg)
    for t in ("/status", "/mute 30", "/screenshot", "take a screenshot", "/help", "/nope"):
        tg.msg(t)
    tg.p.poll_once(timeout=0)
    status, mute, shot1, shot2, helps, nope = tg.texts()[-6:]
    assert "Live on stream now." in status and "PC" in status
    assert mute == "Quiet for 30 minutes." and svc.skills["nudges"]._muted_until > time.time() + 1700
    assert "don't go to the phone" in shot1 and shot1 == shot2 and "/status" in helps and "/help" in nope
    assert tg.ran == []


class Rocket(Skill):
    name, title = "rocket", "Rocket"

    def tools(self):
        return [Tool("launch_rocket", "Launch it.", obj(), self.launch, tier=2, confirm_text=lambda a: "launch the rocket")]

    def launch(self):
        self.launched = getattr(self, "launched", 0) + 1
        return {"ok": True}

    def route(self, t):
        return Intent("tool", "launch_rocket", {}) if t == "launch the rocket" else None

    def say(self, tool, args, result):
        return "Rocket away."


def test_actions_that_need_a_yes_ask_on_the_phone(tg, svc, monkeypatch):
    from assistant.brain.assistant import Assistant

    mod = types.ModuleType("assistant.skills.rocketfake")
    Rocket.__module__ = mod.__name__
    mod.Rocket = Rocket
    monkeypatch.setitem(__import__("sys").modules, mod.__name__, mod)
    svc.skills = Skills(svc, ["rocketfake", "nudges"])
    a = Assistant(svc)
    tg.p.run_command = lambda text, pending_id=None: a.handle(text, "phone", pending_id=pending_id)
    tg.p.pending = a.pending_view
    rocket = svc.skills["rocket"]
    pair(tg)
    tg.msg("launch the rocket")
    tg.p.poll_once(timeout=0)
    ask = tg.sent[-1]
    assert ask["text"] == "Confirm: launch the rocket? Reply yes or no."
    pid = a.pending_view()["id"]  # the buttons name the question they answer
    assert [b["callback_data"] for b in ask["reply_markup"]["inline_keyboard"][0]] == [f"yes:{pid}", f"no:{pid}"]
    assert not getattr(rocket, "launched", 0)
    tg.button(f"yes:{pid}")
    tg.p.poll_once(timeout=0)
    assert rocket.launched == 1 and tg.texts()[-1] == "Rocket away."
    tg.msg("launch the rocket")
    tg.msg("no")
    tg.p.poll_once(timeout=0)
    assert rocket.launched == 1 and tg.texts()[-1] == "Cancelled."
    tg.msg("yes")
    tg.p.poll_once(timeout=0)
    assert rocket.launched == 1 and tg.texts()[-1] == "Nothing waiting on a yes."


def test_urgent_things_go_to_the_phone_only_when_away(tg, svc, monkeypatch):
    monkeypatch.setenv(ph.NTFY_ENV, "vesper-test-topic-123")
    pair(tg)
    n = len(tg.sent)
    svc.bus.publish("notice", {"text": "Reminder: call Dana.", "level": "urgent", "key": "r1"})
    assert len(tg.sent) == n and not tg.ntfy  # at the PC: the HUD and voice have it
    tg.idle["s"] = 900
    svc.bus.publish("notice", {"text": "Just so you know.", "level": "normal", "key": "n1"})
    svc.bus.publish("notice", {"text": "Reminder: call Dana.", "level": "urgent", "key": "r1"})
    svc.bus.publish("notice", {"text": "Reminder: call Dana.", "level": "urgent", "key": "r1"})  # once only
    assert tg.texts()[n:] == ["Vesper: Reminder: call Dana."]
    assert tg.ntfy == [("/vesper-test-topic-123", "Reminder: call Dana.", "high")]
    # reminders held in the nudges queue while away go out on the next tick
    nud = svc.skills["nudges"]
    nud.idle, nud.fullscreen = (lambda: 900), (lambda: "")
    nud.add("Reminder: stretch.", priority="urgent", key="r2", source="automations")
    assert nud.queue and nud.queue[0].held == "away from the PC"
    tg.p.tick()
    assert tg.texts()[-1] == "Vesper: Reminder: stretch."
    tg.p._set_push(False)
    svc.bus.publish("notice", {"text": "Meeting in 5.", "level": "urgent", "key": "m1"})
    assert tg.texts()[-1] == "Vesper: Reminder: stretch."


def test_token_is_checked_and_saved_to_env_only(tg, cfg, monkeypatch):
    p = tg.p
    assert p.set_token("not a token")["ok"] is False
    bad = p.set_token("987654321:" + "B" * 35)
    assert bad["ok"] is False and "Unauthorized" in bad["error"] and "B" * 35 not in bad["error"]
    monkeypatch.setenv(ph.TOKEN_ENV, "")
    out = p.set_token(TOKEN)
    assert out == {"ok": True, "bot": "jvesper_bot"} and "deleteWebhook" in tg.calls
    assert f"TELEGRAM_BOT_TOKEN={TOKEN}" in (cfg.root / ".env").read_text()
    pair(tg)
    assert TOKEN not in (cfg.data_dir / "phone.json").read_text() and TOKEN not in json.dumps(p.status())
    assert p.set_ntfy("short")["ok"] is False and p.set_ntfy("vesper-abcdefghijkl")["ok"]
    monkeypatch.setenv(ph.NTFY_ENV, "vesper-abcdefghijkl")
    assert p.status()["ntfy_hint"] == "vesper…"
    assert p.test()["sent"] == ["telegram", "ntfy"]
    assert p.unpair()["ok"] and not p.status()["paired"] and p.status()["code"]


def test_telegram_down_is_an_error_not_a_crash_and_never_shows_the_token(tg):
    tg.down = True
    with pytest.raises(ph.PhoneError) as exc:
        tg.p.poll_once(timeout=0)
    assert TOKEN not in str(exc.value) and "ConnectError" in str(exc.value)
    assert tg.p.send("hi") is False
    assert tg.p.push("hi")["ok"] is False


def test_loop_keeps_going_and_start_wires_the_runtime(svc, monkeypatch):
    monkeypatch.delenv(ph.TOKEN_ENV, raising=False)
    p = ph.Phone(svc, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))), idle=lambda: 0)
    calls = []
    rt = SimpleNamespace(command=lambda text, source, pending_id=None: calls.append((text, source)) or {"reply": "ok"},
                         assistant=SimpleNamespace(pending_view=lambda: None))
    p.start(rt)
    try:
        assert p.status()["polling"] and not p.status()["token"]
        p.run_command("hi")
        assert calls == [("hi", "phone")]
    finally:
        p.stop()
        p._thread.join(10)
    assert not p._thread.is_alive()


def test_send_to_phone_tool(tg):
    pair(tg)
    tool = tg.p.tools()[0]
    assert tool.name == "send_to_phone" and tool.tier_for({}) == 1
    assert tool.handler(text="123 Main St")["sent"] == ["telegram"] and tg.texts()[-1] == "Vesper: 123 Main St"
    assert tg.p.say("send_to_phone", {}, {}) == "Sent to your phone."


# ---- voice messages ---------------------------------------------------------------------------------------------

@pytest.fixture
def heard(tg):
    """The phone with a fake decoder (no ffmpeg run) and a fake speech-to-text that says what the test wants."""
    import numpy as np

    said = {"text": "what's on my calendar", "decoded": []}

    def decode(path, ff, runner=None):
        said["decoded"].append((path.suffix, path.read_bytes(), ff, runner))
        yield np.zeros(480, dtype=np.int16)

    def stt(samples):
        if isinstance(said["text"], Exception):
            raise said["text"]
        return said["text"]
    tg.p.ffmpeg, tg.p.decode, tg.p._transcribe_fn = "ffmpeg", decode, stt
    return said


def test_voice_messages_run_like_typed_ones_and_say_what_was_heard(tg, heard, cfg):
    pair(tg)
    tg.voice("v1", b"OggS-hello")
    tg.p.poll_once(timeout=0)
    assert tg.ran == ["what's on my calendar"]
    assert tg.texts()[-1] == "Heard: “what's on my calendar”\n\nDid: what's on my calendar."
    assert heard["decoded"] == [(".ogg", b"OggS-hello", "ffmpeg", ph._popen)]
    assert not list((cfg.data_dir / "phone_voice").iterdir())  # the audio is gone once it's read
    heard["text"] = "  Take a   screenshot. "  # by voice it's the same rule as typed
    tg.voice("v2")
    tg.p.poll_once(timeout=0)
    assert tg.texts()[-1] == "Heard: “Take a screenshot.”\n\nScreenshots and what's on the screen don't go to the phone."
    assert tg.ran == ["what's on my calendar"]


def test_voice_from_strangers_or_while_off_is_never_fetched(tg, heard):
    tg.voice("v0", chat=STRANGER)  # not paired yet: a voice message pairs nothing
    tg.p.poll_once(timeout=0)
    pair(tg)
    n = len(tg.sent)
    tg.voice("v1", chat=STRANGER)
    tg.voice("v2", date=time.time() - 3600)  # sent while Vesper was off
    tg.p.poll_once(timeout=0)
    assert "getFile" not in tg.calls and "download" not in tg.calls and tg.ran == [] and len(tg.sent) == n
    assert tg.p.chat_id == ME


def test_voice_without_ffmpeg_says_how_to_fix_it(tg, heard, svc):
    pair(tg)
    tg.p.ffmpeg = ""
    tg.voice("v1")
    tg.p.poll_once(timeout=0)
    assert tg.texts()[-1] == ph.NO_FFMPEG and "winget install Gyan.FFmpeg" in ph.NO_FFMPEG
    assert "getFile" not in tg.calls and tg.ran == []
    checks = {c["name"]: c for c in tg.p.selftest()}
    assert checks["Phone voice messages"]["status"] == "skip" and "winget" in checks["Phone voice messages"]["fix"]


def test_voice_size_and_rate_limits(tg, heard, cfg, monkeypatch):
    now = {"t": 5_000_000.0}
    tg.p.clock = lambda: now["t"]
    pair_at = now["t"]
    tg.msg(f"/start {tg.p.state['code']}")
    tg.updates[-1]["message"]["date"] = int(pair_at)
    tg.p.poll_once(timeout=0)
    tg.voice("long", duration=600, date=now["t"])  # too long: not even fetched
    tg.voice("fat", size=5_000_000, date=now["t"])  # too big by what Telegram says
    tg.p.poll_once(timeout=0)
    assert "getFile" not in tg.calls and all("too long" in t for t in tg.texts()[-2:])
    monkeypatch.setattr(ph, "VOICE_MAX_BYTES", 100)
    tg.voice("liar", data=b"x" * 50, date=now["t"])
    tg.files["liar"] = b"x" * 500  # bigger than it said: the download stops at the limit
    tg.p.poll_once(timeout=0)
    assert "too big" in tg.texts()[-1] and not list((cfg.data_dir / "phone_voice").iterdir())
    monkeypatch.setattr(ph, "VOICE_MAX_BYTES", 2_000_000)
    for i in range(ph.VOICE_RATE[0]):  # the "liar" download counted too
        tg.voice(f"r{i}", date=now["t"])
    tg.p.poll_once(timeout=0)
    assert "a lot of voice messages" in tg.texts()[-1] and len(tg.ran) == ph.VOICE_RATE[0] - 1
    now["t"] += ph.VOICE_RATE[1] + 1
    tg.voice("later", date=now["t"])
    tg.p.poll_once(timeout=0)
    assert len(tg.ran) == ph.VOICE_RATE[0] and tg.texts()[-1].startswith("Heard:")


def test_voice_silence_no_engine_and_telegram_errors(tg, heard):
    pair(tg)
    heard["text"] = "Thank you."  # what speech models "hear" in a pocket
    tg.voice("v1")
    tg.p.poll_once(timeout=0)
    assert "couldn't make out any words" in tg.texts()[-1]
    heard["text"] = RuntimeError("No speech-to-text engine installed")
    tg.voice("v2")
    tg.p.poll_once(timeout=0)
    assert "Speech-to-text isn't working" in tg.texts()[-1] and "Update Vesper" in tg.texts()[-1]
    tg.queue(message={"message_id": 99, "date": int(time.time()), "chat": {"id": ME, "type": "private"},
                      "voice": {"file_id": "gone", "duration": 2}})  # Telegram no longer has it
    tg.p.poll_once(timeout=0)
    assert tg.texts()[-1].startswith("I couldn't fetch that voice message") and TOKEN not in tg.texts()[-1]
    assert tg.ran == []


def test_voice_yes_and_no_go_through_the_same_confirmation(tg, heard, svc, monkeypatch):
    from assistant.brain.assistant import Assistant

    mod = types.ModuleType("assistant.skills.rocketfake")
    Rocket.__module__ = mod.__name__
    mod.Rocket = Rocket
    monkeypatch.setitem(__import__("sys").modules, mod.__name__, mod)
    svc.skills = Skills(svc, ["rocketfake", "nudges"])
    a = Assistant(svc)
    tg.p.run_command = lambda text: a.handle(text, "phone")
    tg.p.pending = a.pending_view
    rocket = svc.skills["rocket"]
    pair(tg)
    heard["text"] = "Launch the rocket."
    tg.voice("v1")
    tg.p.poll_once(timeout=0)
    ask = tg.sent[-1]
    assert ask["text"] == "Heard: “Launch the rocket.”\n\nConfirm: launch the rocket? Reply yes or no."
    pid = a.pending_view()["id"]
    assert [b["callback_data"] for b in ask["reply_markup"]["inline_keyboard"][0]] == [f"yes:{pid}", f"no:{pid}"]
    assert not getattr(rocket, "launched", 0)  # a tier-2 action waits for a yes, by voice too
    heard["text"] = "No."
    tg.voice("v2")
    tg.p.poll_once(timeout=0)
    assert not getattr(rocket, "launched", 0) and tg.texts()[-1].endswith("Cancelled.")
    heard["text"] = "Launch the rocket."
    tg.voice("v3")
    tg.p.poll_once(timeout=0)
    heard["text"] = "Yes."
    tg.voice("v4")
    tg.p.poll_once(timeout=0)
    assert rocket.launched == 1 and tg.texts()[-1] == "Heard: “Yes.”\n\nRocket away."


# ---- long work started from the phone answers there --------------------------------------------------------------

def _job(state, **kw):
    return {"id": "ab12cd", "title": "Research: capture cards", "state": state, "source": "phone", "deliver": [],
            "result": None, "error": "", "batch": "", **kw}


def test_a_job_started_from_the_phone_answers_there_when_done(tg, svc):
    pair(tg)
    n = len(tg.sent)
    tg.idle["s"] = 900
    pub = lambda *jobs: svc.bus.publish("tasks_agent", {"ok": True, "jobs": list(jobs)}, sticky=True)  # noqa: E731
    pub(_job("done", result={"summary": "Old news."}))  # finished before Vesper started: never seen running
    pub(_job("running", id="vv", source="voice"), _job("running"))
    pub(_job("done", id="vv", source="voice", result={"summary": "Said at the PC."}),
        _job("done", result={"summary": "The Elgato 4K X is the pick.", "note": "x.md"}))
    pub(_job("done", result={"summary": "The Elgato 4K X is the pick."}))  # published again: told once
    assert tg.texts()[n:] == ["Research: capture cards is done. The Elgato 4K X is the pick. The full write-up is in "
                              "Jobs on the PC."]
    pub(_job("running", id="w1", title="Inbox triage"))
    pub(_job("waiting", id="w1", title="Inbox triage", batch="save 3 reply drafts in Gmail (never sent)"))
    assert tg.texts()[-1] == ("Inbox triage is ready. It waits on your OK to save 3 reply drafts in Gmail (never sent): "
                              "reply “approve the inbox triage job”.")
    pub(_job("running", id="f1", title="Week wrap"))
    pub(_job("failed", id="f1", title="Week wrap", error="every step failed"))
    assert tg.texts()[-1] == "Week wrap didn't work: every step failed"
    m = len(tg.sent)
    pub(_job("running", id="d1", deliver=["phone"]))
    pub(_job("done", id="d1", deliver=["phone"], result={"summary": "x"}))  # "…and text it to me" sent it already
    tg.idle["s"] = 0
    pub(_job("running", id="h1"))
    pub(_job("done", id="h1", result={"summary": "Back at the PC: the nudge has it."}))
    assert len(tg.sent) == m


def test_notes_from_a_recording_stopped_from_the_phone(tg, svc):
    pair(tg)
    tg.idle["s"] = 900
    ctx = {"source": "voice"}
    tg.p._ctx = lambda: ctx
    svc.bus.publish("tool", {"name": "recording_stop", "args": {}, "result": {"ok": True, "id": "rec-pc"}})
    ctx["source"] = "phone"
    svc.bus.publish("tool", {"name": "recording_stop", "args": {}, "result": {"ok": True, "id": "rec-1"}})
    svc.bus.publish("tool", {"name": "recording_mark", "args": {}, "result": {"ok": True, "id": "rec-2"}})
    n = len(tg.sent)
    rec = lambda rid, state, **kw: {"id": rid, "title": "Standup", "state": state, "summary": "We ship Friday.",  # noqa: E731
                                     "tasks": 2, "tasks_state": "ask", "error": "", **kw}
    svc.bus.publish("recorder", {"state": "idle", "recent": [rec("rec-1", "notes"), rec("rec-pc", "done")]}, sticky=True)
    assert len(tg.sent) == n  # still writing; the PC one isn't the phone's
    svc.bus.publish("recorder", {"state": "idle", "recent": [rec("rec-1", "done"), rec("rec-2", "done")]}, sticky=True)
    svc.bus.publish("recorder", {"state": "idle", "recent": [rec("rec-1", "done")]}, sticky=True)
    assert tg.texts()[n:] == ["Notes ready: Standup. We ship Friday. 2 action items: reply “add the recording tasks” "
                              "to add them."]
    svc.bus.publish("tool", {"name": "notes_from_file", "args": {}, "result": {"ok": True, "id": "file-1"}})
    svc.bus.publish("recorder", {"recent": [rec("file-1", "error", error="ffmpeg couldn't read call.m4a")]}, sticky=True)
    assert tg.texts()[-1] == "The notes from Standup didn't work: ffmpeg couldn't read call.m4a"


def test_a_model_test_that_ends_while_away_is_pushed(tg, svc):
    pair(tg)
    n = len(tg.sent)
    svc.bus.publish("brain_bench", {"running": False, "summary": "qwen3.6:35b-a3b is the best."}, sticky=True)
    tg.idle["s"] = 900
    svc.bus.publish("brain_bench", {"running": True, "done": 3, "total": 9})
    svc.bus.publish("brain_bench", {"running": False, "summary": "qwen3.6:35b-a3b is the best."}, sticky=True)
    assert tg.texts()[n:] == ["Vesper: model test done. qwen3.6:35b-a3b is the best."]
    tg.p._set_push(False)
    svc.bus.publish("brain_bench", {"running": False, "summary": "again"}, sticky=True)
    assert len(tg.sent) == n + 1


def test_start_wires_the_listener_and_the_command_source(svc, monkeypatch):
    monkeypatch.delenv(ph.TOKEN_ENV, raising=False)
    p = ph.Phone(svc, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))), idle=lambda: 0)
    lst = SimpleNamespace(transcribe=lambda s: "vrb in five", correct=lambda t: t.replace("vrb", "BRB"))
    a = SimpleNamespace(pending_view=lambda: None, _ctx={"source": "phone"})
    p.start(SimpleNamespace(command=lambda text, source: {}, assistant=a, listener=lst))
    try:
        assert p._ctx() == {"source": "phone"} and p._transcribe([0.0]) == "BRB in five"
    finally:
        p.stop()
        p._thread.join(10)
