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
        self.down = False

    def queue(self, **u):
        self.uid += 1
        self.updates.append({"update_id": self.uid, **u})

    def msg(self, text, chat=ME, age=0, **chat_extra):
        self.queue(message={"message_id": self.uid, "date": int(time.time() - age), "text": text,
                            "chat": {"id": chat, "type": "private", "first_name": "J", **chat_extra}})

    def button(self, data, chat=ME):
        self.queue(callback_query={"id": f"cb{self.uid}", "data": data, "message": {"chat": {"id": chat}}})

    def __call__(self, req: httpx.Request):
        if self.down:
            raise httpx.ConnectError("no route", request=req)
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
    tg.p.run_command = lambda text: a.handle(text, "phone")
    tg.p.pending = a.pending_view
    rocket = svc.skills["rocket"]
    pair(tg)
    tg.msg("launch the rocket")
    tg.p.poll_once(timeout=0)
    ask = tg.sent[-1]
    assert ask["text"] == "Confirm: launch the rocket? Reply yes or no."
    assert [b["callback_data"] for b in ask["reply_markup"]["inline_keyboard"][0]] == ["yes", "no"]
    assert not getattr(rocket, "launched", 0)
    tg.button("yes")
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
    rt = SimpleNamespace(command=lambda text, source: calls.append((text, source)) or {"reply": "ok"},
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
