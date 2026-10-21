""""That was me": a sentence the speaker check wrongly turned away is learned into the voice profile and run, once,
from the HUD button only (never by saying it), within 2 minutes, and never while the kill switch is on."""

import threading
import time

import numpy as np
import pytest

from assistant.runtime import Runtime
from assistant.voice import speaker_id
from assistant.voice.listener import REJECT_KEEP_S, VoiceListener
from assistant.voice.speaker_id import SpeakerVerifier, adapt_profile, build_profile

AUDIO = np.zeros(16000 * 2, dtype=np.int16)


def voice(seed, n=8, spread=0.25, dim=256):
    """n fingerprints of one synthetic 'speaker': a fixed direction plus per-take jitter."""
    rng = np.random.default_rng(seed)
    base = rng.normal(size=dim)
    out = []
    for _ in range(n):
        v = base + rng.normal(size=dim) * spread * np.linalg.norm(base) / np.sqrt(dim)
        out.append((v / np.linalg.norm(v)).tolist())
    return out


def new_mic(seed=1, n=6, mix=0.75):
    """J through a different mic/room: his voice, pulled towards another direction (scores under the threshold)."""
    j, other = voice(seed, n=n), voice(seed + 100, n=n, spread=0.1)
    out = []
    for a, b in zip(j, other):
        v = (1 - mix) * np.asarray(a) + mix * np.asarray(b)
        out.append((v / np.linalg.norm(v)).tolist())
    return out


class QueueEmbedder:
    name = "fake"

    def __init__(self, takes):
        self.takes = list(takes)

    def embed(self, _samples):
        return self.takes.pop(0)


class FakeSTT:
    name = "parakeet"

    def __init__(self, text):
        self.text = text

    def transcribe(self, _samples, hints=()):
        return self.text


class ListBus:
    def __init__(self):
        self.events = []

    def publish(self, type_, data, sticky=False):
        self.events.append((type_, data))

    def heard(self):
        return [d for t, d in self.events if t == "heard"]


class FakeSpeaker:
    def __init__(self):
        self.speaking = threading.Event()
        self.last_text = ""
        self.last_end = time.time() - 60
        self.expects_reply = False

    def chime(self):
        pass

    def interrupt(self, **_k):
        pass


def enrol(data_dir, takes):
    data_dir.mkdir(parents=True, exist_ok=True)
    speaker_id.save_profile(build_profile(voice(1, n=8)), data_dir)
    return SpeakerVerifier(data_dir, "strict", embedder=QueueEmbedder(takes))


# ---- the listener remembers what it turned away ----------------------------------------------------------------------

def test_a_rejection_is_remembered_with_its_id_and_expires(tmp_path):
    takes = new_mic(n=3)
    verifier = enrol(tmp_path, takes[:])
    commands = []
    lst = VoiceListener(ListBus(), FakeSpeaker(), ["friday"], commands.append, {}, verifier=verifier)
    lst.stt = FakeSTT("Friday, what time is it?")
    lst._handle_audio(AUDIO)
    heard = lst.bus.heard()[-1]
    assert commands == [] and heard["ignored"] == "voice not recognised" and heard["rejection"]
    assert heard["rejections_recent"] == 1
    item = lst.take_rejection(heard["rejection"])
    assert item["command"] == "what time is it" and item["wake"] is True
    assert item["embedding"] == takes[0] and item["score"] == heard["score"]  # the check's own fingerprint, not a 2nd pass
    assert lst.take_rejection(heard["rejection"]) is None  # once per id

    lst._handle_audio(AUDIO)
    second = lst.bus.heard()[-1]
    assert second["rejections_recent"] == 2  # "I keep not recognising your voice"
    lst._rejected[second["rejection"]]["at"] -= REJECT_KEEP_S + 1
    assert lst.take_rejection(second["rejection"]) is None  # older than 2 minutes
    assert lst.take_rejection("") is None and lst.take_rejection("nope") is None


# ---- the profile learns, capped, and never gets stricter --------------------------------------------------------------

def test_the_profile_grows_is_capped_and_never_raises_the_bar(tmp_path):
    base = build_profile(voice(1, n=8))
    enrolled = base.threshold
    assert base.enrolled_threshold == enrolled
    p = base
    takes = new_mic(n=25)
    for t in takes:
        p = adapt_profile(p, t, cap=20)
        assert p.threshold <= enrolled and p.enrolled_threshold == enrolled
        assert speaker_id.MIN_THRESHOLD <= p.threshold
        assert p.threshold >= round(enrolled - speaker_id.MAX_LEARNED_DROP, 3)  # one odd take can't open the door
    assert len(p.adaptive) == 20 and p.adaptive[-1] == takes[-1] and p.adaptive[0] == takes[5]  # the newest 20
    assert len(p.embeddings) == 8  # calibration takes stay
    assert p.summary()["learned"] == 20 and p.summary()["clips"] == 8
    # already-strict profile: learning a take that fits well can't push the threshold above calibration
    tight = adapt_profile(base, voice(1, n=9)[-1])
    assert tight.threshold <= enrolled


def test_learning_lets_the_new_mic_through_and_survives_a_restart(tmp_path):
    takes = new_mic(n=4)
    v = enrol(tmp_path, takes[1:])
    enrolled = v.profile.threshold
    assert v.profile.score(takes[0]) < enrolled  # J on the new mic is turned away
    out = v.learn(takes[0])
    assert out["learned"] == 1 and out["threshold"] <= enrolled and out["threshold_before"] == enrolled
    ok, _ = v.check(AUDIO.astype(np.float32))  # the next sentence on the same mic
    assert ok
    loaded = speaker_id.load_profile(tmp_path)
    assert loaded.adaptive == v.profile.adaptive and loaded.enrolled_threshold == enrolled
    assert loaded.threshold == v.profile.threshold
    assert v.learn([0.1, 0.2]) is None  # a fingerprint from another model is never mixed in
    assert speaker_id.delete_profile(tmp_path) and speaker_id.load_profile(tmp_path) is None  # Delete takes it all


def test_an_older_profile_without_learned_takes_still_loads_and_learns(tmp_path):
    old = build_profile(voice(1, n=8))
    old.enrolled_threshold = None  # saved before this feature: no such fields
    speaker_id.save_profile(old, tmp_path)
    v = SpeakerVerifier(tmp_path, "strict")
    assert v.profile.adaptive == [] and v.status()["learned"] == 0
    assert v.learn(new_mic(n=1)[0])["enrolled_threshold"] == old.threshold


# ---- the endpoint ----------------------------------------------------------------------------------------------------

def wired(cfg, svc, text="Friday, what time is it?", takes=None):
    """A runtime with a real listener + speaker check (fake mic, speech-to-text and voice model)."""
    rt = Runtime(cfg, services=svc)
    takes = takes if takes is not None else new_mic(n=4)
    rt.verifier = enrol(cfg.data_dir, takes)
    rt.listener = VoiceListener(rt.bus, FakeSpeaker(), ["friday"], rt.voice_command, {}, verifier=rt.verifier)
    rt.listener.stt = FakeSTT(text)
    heard = []
    rt.bus.on(lambda ev: heard.append(ev["data"]) if ev["type"] == "heard" else None)
    ran = []
    real = rt.assistant.handle
    rt.assistant.handle = lambda text, source="text", turn=None, owner=None: (
        ran.append((text, source, owner)), real(text, source, turn=turn, owner=owner))[1]
    return rt, heard, ran


def flush(rt):
    rt._commands.submit(lambda: None).result(timeout=10)


@pytest.fixture
def client_for():
    from fastapi.testclient import TestClient

    from assistant.server import create_app

    opened = []

    def make(rt):
        app = create_app(rt, start_background=False)
        c = TestClient(app)
        c.__enter__()
        c.headers["X-Assistant-Token"] = app.state.token
        opened.append((c, rt))
        return c
    yield make
    for c, rt in opened:
        c.__exit__(None, None, None)
        rt._commands.shutdown()


def test_that_was_me_learns_the_take_and_runs_the_command_once(cfg, svc, client_for):
    rt, heard, ran = wired(cfg, svc)
    c = client_for(rt)
    rt.listener._handle_audio(AUDIO)
    rid = heard[-1]["rejection"]
    assert ran == [] and rt.verifier.profile.adaptive == []

    out = c.post("/api/voice/that_was_me", json={"id": rid}).json()
    flush(rt)
    assert out["ok"] and out["ran"] == "what time is it" and out["learned"]["learned"] == 1
    assert ran == [("what time is it", "voice", "pressed")]  # as if J had said it
    assert len(rt.verifier.profile.adaptive) == 1
    assert speaker_id.load_profile(cfg.data_dir).adaptive == rt.verifier.profile.adaptive  # persisted
    entry = rt.assistant.tools.audit.tail(5)
    assert any(e["tool"] == "voice_profile_learned" and e["source"] == "hud" for e in entry)
    assert c.get("/api/voice").json()["profile"]["learned"] == 1

    again = c.post("/api/voice/that_was_me", json={"id": rid}).json()
    flush(rt)
    assert again["ok"] is False and len(ran) == 1 and len(rt.verifier.profile.adaptive) == 1  # once per id


def test_that_was_me_does_nothing_when_expired_unknown_or_without_the_token(cfg, svc, client_for):
    rt, heard, ran = wired(cfg, svc)
    c = client_for(rt)
    rt.listener._handle_audio(AUDIO)
    rid = heard[-1]["rejection"]
    bad = c.post("/api/voice/that_was_me", json={"id": rid}, headers={"X-Assistant-Token": "nope"})
    assert bad.status_code == 401
    assert c.post("/api/voice/that_was_me", json={"id": "made-up"}).json()["ok"] is False
    assert c.post("/api/voice/that_was_me").json()["ok"] is False  # no body
    rt.listener._rejected[rid]["at"] -= REJECT_KEEP_S + 1
    assert c.post("/api/voice/that_was_me", json={"id": rid}).json()["ok"] is False
    flush(rt)
    assert ran == [] and rt.verifier.profile.adaptive == []
    assert not any(e.get("tool") == "voice_profile_learned" for e in rt.assistant.tools.audit.tail(20))


def test_that_was_me_does_nothing_while_the_kill_switch_is_on(cfg, svc, client_for):
    rt, heard, ran = wired(cfg, svc, text="Friday, resume control")
    c = client_for(rt)
    rt.assistant.stop_everything("hud")
    rt.listener._handle_audio(AUDIO)  # a stranger trying to undo the kill switch
    rid = heard[-1]["rejection"]
    out = c.post("/api/voice/that_was_me", json={"id": rid}).json()
    flush(rt)
    assert out["ok"] is False and "paused" in out["error"]
    assert ran == [] and rt.verifier.profile.adaptive == [] and rt.assistant.tools.guard.hands_off


def test_a_tier3_command_after_that_was_me_still_waits_for_a_yes(cfg, svc, client_for, monkeypatch):
    obs_calls = []
    monkeypatch.setattr(svc.obs, "control", lambda action: obs_calls.append(action) or {"ok": True})
    rt, heard, ran = wired(cfg, svc, text="Friday, end the stream")
    c = client_for(rt)
    rt.listener._handle_audio(AUDIO)
    out = c.post("/api/voice/that_was_me", json={"id": heard[-1]["rejection"]}).json()
    flush(rt)
    assert out["ok"] and ran == [("end the stream", "voice", "pressed")]
    pending = rt.assistant.pending_view()
    assert pending and pending["tier"] == 3 and obs_calls == []  # asked, not done


def test_a_bare_yes_is_learned_but_not_replayed(cfg, svc, client_for):
    rt, heard, ran = wired(cfg, svc, text="Friday, yes")
    c = client_for(rt)
    rt.listener._handle_audio(AUDIO)
    out = c.post("/api/voice/that_was_me", json={"id": heard[-1]["rejection"]}).json()
    flush(rt)
    assert out["ok"] and out["ran"] is None and "Confirm" in out["message"]
    assert ran == [] and len(rt.verifier.profile.adaptive) == 1


def test_saying_that_was_me_does_not_vouch_for_anything(cfg, svc, client_for):
    """Anyone who was just turned away could say it: only the HUD button counts."""
    takes = new_mic(n=1) + voice(1, n=12)[8:]  # turned away, then a take that passes
    rt, heard, ran = wired(cfg, svc, takes=takes)
    client_for(rt)
    rt.listener._handle_audio(AUDIO)
    rid = heard[-1]["rejection"]
    rt.listener.stt = FakeSTT("Friday, that was me")
    rt.listener._handle_audio(AUDIO)
    flush(rt)
    assert rt.verifier.profile.adaptive == []  # nothing learned
    assert ran == [("that was me", "voice", "match")]  # just an ordinary sentence for the brain
    assert rt.listener.take_rejection(rid) is not None  # and the rejected command didn't run
