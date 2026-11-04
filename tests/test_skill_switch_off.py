"""Switched off (Setup → Skills, or the crash guard after five errors in a row), a skill stops acting: its bus
listeners come off, its hooks are put back, its own poller ends. Switched on again, all of it comes back, once.
With two real skills made the way Vesper makes them: Phone (pushes to Telegram and ntfy, a long-poll thread) and
Captions (J's words on stream, the listener's hear_all)."""

import queue
import threading
from types import SimpleNamespace

import httpx
import pytest

from assistant.bus import EventBus
from assistant.skills import CRASHED, Skills
from assistant.skills import phone as ph
from tests.test_skill_phone import TOKEN, FakeTelegram
from tests.test_skill_stream_captions import FakeOBS


@pytest.fixture
def world(svc, monkeypatch):
    monkeypatch.setattr("assistant.config.save_setting", lambda cfg, key, value: None)
    monkeypatch.setenv(ph.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ph.NTFY_ENV, "vesper-test-topic-123")
    svc.bus, svc.live, svc.obs = EventBus(), (lambda: True), FakeOBS()
    sk = Skills(svc, ["nudges", "phone", "stream_captions"])
    svc.skills = sk
    phone, cap = sk["phone"], sk["stream_captions"]
    tg = FakeTelegram()
    phone.http = httpx.Client(transport=httpx.MockTransport(tg))
    phone.idle, phone.spawn = (lambda: 900), (lambda fn, *a: fn(*a))  # J is away from the PC; pushes run inline
    tg.msg(f"/start {phone.state['code']}")
    phone.poll_once(timeout=0)
    assert phone.chat_id
    cap.threads = False
    monkeypatch.setitem(cap.cfg, "obs_source", "Captions")
    cap.set_on(True)
    own = lambda: False  # noqa: E731  (the listener's own: hear only what's said to Vesper)
    listener = SimpleNamespace(hear_all=own)
    runtime = SimpleNamespace(command=lambda text, source: {"reply": "ok"}, listener=listener,
                              assistant=SimpleNamespace(pending_view=lambda: None, _ctx={}, off_record=lambda: False))
    cap.start(runtime)
    started = []
    sk.starter = lambda s: started.append(s.name)  # the runtime's start (the phone's own poller: the test below)
    return SimpleNamespace(sk=sk, phone=phone, cap=cap, tg=tg, listener=listener, own=own, started=started)


def urgent(svc, text):
    svc.bus.publish("notice", {"text": text, "level": "urgent", "key": text})


def said(svc, text):
    svc.bus.publish("heard", {"text": text, "wake": False, "armed": False})


def owned(svc, skill):
    return [cb for cb in svc.bus.listeners() if getattr(cb, "__self__", None) is skill]


def test_switched_off_it_stops_acting_and_on_again_it_acts_once(world, svc):
    w = world
    urgent(svc, "Email from your bank: statement ready.")
    said(svc, "that boss fight was brutal")
    assert w.tg.texts()[-1] == "Vesper: Email from your bank: statement ready." and len(w.tg.ntfy) == 1
    assert "boss fight" in svc.obs.sent[-1][1] and w.listener.hear_all == w.cap.showing

    assert w.sk.set_enabled("phone", False)["ok"]  # Setup → Skills → Phone off
    for _ in range(5):  # Captions: five ticks in a row fail, and the crash guard switches it off
        w.sk._failed(w.cap, "tick", RuntimeError("obs went away"))
    assert not w.sk.active("phone") and w.sk.off["stream_captions"] == CRASHED
    assert svc.obs.sent[-1] == ("Captions", "")  # the last caption doesn't stay on stream
    n, m, o = len(w.tg.sent), len(w.tg.ntfy), len(svc.obs.sent)
    urgent(svc, "Your card was charged $80.")
    said(svc, "ugh my landlord called again about the rent")
    assert w.tg.sent[n:] == [] and w.tg.ntfy[m:] == [], "a switched-off Phone still pushed to the phone"
    assert svc.obs.sent[o:] == [] and w.cap.current() == [], "a switched-off Captions still put J's words on stream"
    assert w.listener.hear_all is w.own  # and the listener no longer transcribes every sentence for it
    assert owned(svc, w.phone) == [] and owned(svc, w.cap) == []

    assert w.sk.set_enabled("phone", True)["ok"] and w.sk.switch_on("stream_captions", persist=False)["ok"]
    assert w.started == ["phone", "stream_captions"]
    urgent(svc, "Dana: running 10 min late.")
    said(svc, "okay chat back to it")
    assert w.tg.texts()[n:] == ["Vesper: Dana: running 10 min late."] and len(w.tg.ntfy) == m + 1  # once, not twice
    assert len(svc.obs.sent) == o + 1 and "back to it" in svc.obs.sent[-1][1]
    assert w.listener.hear_all == w.cap.showing
    assert len(owned(svc, w.phone)) == 1 and len(owned(svc, w.cap)) == 1


def test_hooks_go_back_to_what_was_there(svc):
    svc.memory, svc.nudge = None, None  # as at startup, before any skill plugged in
    sk = Skills(svc, ["nudges", "memory", "focus"])
    svc.skills = sk
    mem, nud, focus = sk["memory"], sk["nudges"], sk["focus"]
    own_gate = nud.focus
    focus.start(SimpleNamespace())
    focus.session = {"id": 1}  # J is mid-session: non-urgent nudges are held
    assert svc.memory is mem.memory and nud.focus() is True
    sk.switch_off("memory")
    sk.switch_off("focus")
    assert svc.memory is None  # answers stop getting the profile and recalled facts
    assert nud.focus is own_gate and nud.focus() is False  # nudges no longer held by a session nothing will end
    sk.switch_off("nudges")
    assert svc.nudge is None  # other skills' nudges become HUD cards, not a queue nobody reads
    for name in ("nudges", "memory", "focus"):
        sk.switch_on(name, persist=False)
    assert svc.memory is mem.memory and nud.focus() is True and svc.nudge == nud.add


def pollers(skip=()):
    return [t for t in threading.enumerate() if t.name == "phone" and t.is_alive() and t not in skip]


class LongPoll:
    """Stands in for one getUpdates long poll: each call waits until the test lets it answer."""

    def __init__(self):
        self.calls = queue.Queue()
        self.earlier = set(pollers())  # an earlier test's loop that's still winding down isn't this test's

    def __call__(self, timeout=25):
        me, answer = threading.current_thread(), threading.Event()
        self.calls.put((me, pollers({me, *self.earlier}), answer))
        answer.wait(10)
        return 0

    def next(self):
        return self.calls.get(timeout=10)


def test_phone_off_and_on_again_within_a_long_poll_keeps_one_poller(world):
    w = world
    poll = LongPoll()
    w.phone.poll_once = poll
    runtime = SimpleNamespace(command=lambda text, source: {"reply": "ok"}, listener=None,
                              assistant=SimpleNamespace(pending_view=lambda: None, _ctx={}))
    w.sk.starter = lambda s: s.start(runtime)
    w.phone.start(runtime)
    first, _, answer1 = poll.next()  # its long poll (25 s) is out
    pending = [answer1]
    try:
        w.sk.set_enabled("phone", False)
        w.sk.set_enabled("phone", True)  # a moment later
        answer1.set()  # Telegram answers the first poll
        me, others, answer2 = poll.next()
        pending.append(answer2)
        assert me is not first and others == [], "two loops polled Telegram with one bot token at once"
        first.join(10)
        assert not first.is_alive() and pollers(poll.earlier) == [me]
    finally:
        w.phone.stop()
        for a in pending:
            a.set()


def test_a_poll_that_comes_back_after_the_switch_off_runs_nothing(world):
    w = world
    ran, once = [], []
    runtime = SimpleNamespace(command=lambda text, source: ran.append(text) or {"reply": "ok"}, listener=None,
                              assistant=SimpleNamespace(pending_view=lambda: None, _ctx={}))
    w.phone._loop = lambda *a: None  # no loop thread: the test polls by hand
    w.sk.starter = lambda s: s.start(runtime)
    w.phone.start(runtime)

    def telegram(req):  # J switches Phone off while this poll is out, and a message arrives meanwhile
        if req.url.path.endswith("/getUpdates") and not once:
            once.append(1)
            w.sk.set_enabled("phone", False)
            w.tg.msg("turn off the lights")
        return w.tg(req)
    w.phone.http = httpx.Client(transport=httpx.MockTransport(telegram))
    assert w.phone.poll_once(timeout=0) == 0 and ran == []
    w.sk.set_enabled("phone", True)
    w.phone.poll_once(timeout=0)
    assert ran == ["turn off the lights"]  # left for the next poll, not lost
