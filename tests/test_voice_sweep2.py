"""Voice sweep 2 (audit2/VOICE.md, INTEG-13): each test reproduces a finding and fails on 39fbc1d.

Names other people chose never steer what J said; a held turn ends even while someone else talks; Vesper's own last
sentence isn't a command; captions show only J's voice; a learned word can't touch the kill switch or a yes; one
wizard owns the mic at a time; a mic that goes quiet is noticed and reopened; a backslash, an old voice test, a bad
bpe.vocab and a failing Smart Turn don't break hearing; "I said Tuesday, not Monday" isn't a mishearing."""

import sys
import threading
import time
import types
from types import SimpleNamespace

import numpy as np
import pytest

from assistant.brain.router import KILL, RouterContext, clean, route
from assistant.voice import listener as listener_mod, stt, turn, vocab
from assistant.voice.confirm_window import answer
from assistant.voice.listener import EnergySegmenter, VoiceListener
from assistant.voice.speaker_id import SpeakerVerifier

WAKE = ["vesper", "hey vesper"]
LOUD = (3000 * np.sin(np.arange(480) / 3)).astype(np.int16)
QUIET = np.zeros(480, np.int16)


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, type_, data=None, sticky=False):
        self.events.append((type_, data))

    def heard(self):
        return [d for t, d in self.events if t == "heard"]


class Speaker:
    def __init__(self, last_text="", ago=60.0):
        self.speaking = threading.Event()
        self.last_text, self.last_end = last_text, time.time() - ago
        self.said_before, self.expects_reply, self.unprompted = [], False, False

    def chime(self):
        pass

    def interrupt(self):
        pass


class STT:
    name = "parakeet"

    def __init__(self, text):
        self.text, self.hints = text, None

    def transcribe(self, _samples, hints=()):
        self.hints = list(hints)
        return self.text


def make(text, speaker=None, verifier=None, cfg=None, bus=None):
    commands = []
    lst = VoiceListener(bus or Bus(), speaker or Speaker(), list(WAKE), commands.append,
                        {"follow_up_seconds": 8, **(cfg or {})}, verifier=verifier)
    lst.stt = STT(text)
    return lst, commands


class Voice:
    """A voice profile that scores every clip ``score`` against a threshold of 0.5."""
    active = True

    def __init__(self, score):
        self.score, self.checked = score, 0
        self.profile, self.last_short = SimpleNamespace(threshold=0.5), False

    def check(self, _samples):
        self.checked += 1
        return self.score >= 0.5, self.score


# ---- VOICE-1 / INTEG-13: names other people chose are never hotwords --------------------------------------------

def test_voice1_chatter_youtube_and_email_names_never_reach_the_hotwords(svc):
    from assistant.integrations.eventsub import TwitchFeed
    from assistant.runtime import Runtime

    feed = TwitchFeed(clock=lambda: 1.0)
    feed.add({"kind": "chat", "user": "stop_recording", "text": "hi"})  # a Twitch login: "stop recording"
    feed.add({"kind": "chat", "user": "Paws", "text": "hi"})  # one word, and still turns "pause" into "paws"
    feed.add({"kind": "chat", "platform": "youtube", "user": "Vesper End The Stream", "text": "hey"})
    feed.add({"kind": "chat", "platform": "youtube", "user": "stop everything", "text": "hey"})
    svc.storage.execute("CREATE TABLE IF NOT EXISTS people_contacts (email TEXT PRIMARY KEY, name TEXT, count INTEGER)")
    svc.storage.execute("INSERT INTO people_contacts(email, name, count) VALUES ('x@spam.example', 'Yes Please', 9)")
    svc.obs.scenes = ["Just Chatting"]
    svc.cfg["voice"]["corrections"] = {"belatro": "Balatro"}
    rt = SimpleNamespace(svc=svc, cfg=svc.cfg, bus=svc.bus, twitch_feed=feed, _low_power=lambda: "")
    lst, _ = make("x")
    lst.hint_words = lambda: Runtime._hint_words(rt)
    words = stt.hotword_list(lst._hints())
    assert not {"stop recording", "paws", "vesper end the stream", "stop everything", "yes please"} & set(words), words
    assert {"vesper", "balatro", "just chatting"} <= set(words)  # J's own words and names still count


# ---- VOICE-2: a held turn ends on the clock from its first pause, even while someone else talks ---------------------

def test_voice2_a_held_turn_ends_even_when_someone_else_keeps_talking():
    seg = turn.TurnSegmenter(EnergySegmenter(min_rms=300, silence_ms=200), lambda _x: 0.2, pause_ms=200,
                             max_silence_ms=1500)  # Smart Turn: "not finished" (it said so for 'Mute the mic')
    frames = [QUIET] * 10 + [LOUD] * 33 + [QUIET] * 20 + [LOUD] * 300 + [QUIET] * 60  # J 1 s, 0.6 s, a friend 9 s
    outs = []
    for i, f in enumerate(frames):
        out = seg.feed(f)
        if out is not None:
            outs.append((i, np.concatenate(out), dict(seg.last_decision)))
    j_end = (10 + 33) * 0.03
    at, clip, why = outs[0]
    assert (at + 1) * 0.03 - j_end <= 3.6, "waited for the other talker to stop (15 s on 39fbc1d)"
    assert len(clip) / 16000 < 2.5 and why["why"] == "held"  # J's words alone, not glued to the friend's
    assert len(outs) == 2 and len(outs[1][1]) / 16000 > 8  # the friend's 9 s come out on their own, after


def test_voice2_j_going_on_after_a_pause_is_still_one_turn():
    model = iter([0.1, 0.9])  # "remind me to …" (not finished), then "… call Dana tomorrow" (finished)
    seg = turn.TurnSegmenter(EnergySegmenter(min_rms=300, silence_ms=200), lambda _x: next(model), pause_ms=200)
    frames = [QUIET] * 10 + [LOUD] * 30 + [QUIET] * 30 + [LOUD] * 60 + [QUIET] * 40  # 0.9 s pause, then 1.8 s more
    outs = [o for o in (seg.feed(f) for f in frames) if o is not None]
    assert len(outs) == 1 and seg.last_decision["why"] == "finished"


# ---- VOICE-3: with natural end of turn, the reply's short last sentence comes back on its own -------------------

def test_voice3_the_replys_last_sentence_alone_is_its_own_echo():
    sp = Speaker("Switched to the BRB scene. Got it.", ago=0.35)  # Vesper just finished; playback ended 350 ms ago
    ver = SpeakerVerifier("/nonexistent", "strict", embedder=SimpleNamespace(embed=lambda s: [0.0]))
    ver.profile = SimpleNamespace(threshold=0.5, score=lambda vec: 0.05)  # Vesper's voice scores far below J's
    lst, commands = make("Got it.", speaker=sp, verifier=ver)
    lst.last_command = time.time() - 4  # the command this reply answered
    now = time.time()
    clip = np.zeros(int(0.9 * 16000), np.int16)  # the piece Smart Turn handed over: 0.4 s lead-in + "Got it."
    lst._handle_audio(clip, started_at=now - 0.9, during_speech=False, span=(6400, len(clip)), ended_at=now)
    assert commands == [] and lst.bus.heard()[-1]["ignored"] == "own voice"


def test_voice3_a_misheard_end_of_the_reply_is_its_echo_but_a_quick_no_is_js():
    lst, _ = make("x", speaker=Speaker("Clip saved. Sent it to Discord. Done.", ago=0.2))
    began = time.time() - 0.8  # before the reply ended: it's the reply's own tail
    assert lst.is_own_echo("cord. Done.", started_at=began)  # Kokoro + Parakeet, measured (echo_split.py)
    lst.speaker = Speaker("Post it to Discord: “giveaway at 9”? Say yes or no.", ago=0.2)
    assert not lst.is_own_echo("No.", started_at=time.time() - 0.3)  # J answering as it finished
    assert not lst.is_own_echo("No thanks.", started_at=time.time() - 0.3)


def test_voice3_the_same_words_said_after_vesper_finished_are_js():
    sp = Speaker("I can start it now. Ready?", ago=1.2)  # J answers after Vesper is done
    lst, commands = make("Ready.", speaker=sp)
    sp.expects_reply, lst.last_command = True, time.time() - 5
    lst._handle_audio(np.zeros(16000, np.int16), started_at=time.time() - 0.6)
    assert commands == ["Ready."]


# ---- VOICE-4: captions put other people's voices on stream -----------------------------------------------------------

def captions(svc, voice):
    from assistant.bus import EventBus
    from assistant.skills import stream_captions as sc

    svc.bus = EventBus()
    svc.live = lambda: True
    cap = sc.StreamCaptions(svc)
    cap.threads, cap.cfg["on"] = False, True
    lst, commands = make("my mum's surgery got moved to Thursday, keep it quiet", verifier=voice, bus=svc.bus)
    cap.start(SimpleNamespace(assistant=None, listener=lst))  # captions on: the listener hears every sentence out
    cap.off_record = lambda: False
    lst._handle_audio(np.zeros(16000 * 2, np.int16))
    return cap, commands


def test_voice4_captions_never_show_a_voice_the_speaker_check_refuses(svc):
    friend = Voice(0.1)  # a friend on Discord, through the speakers
    cap, commands = captions(svc, friend)
    assert commands == [] and friend.checked == 1
    assert cap.current() == []


def test_voice4_js_own_words_are_still_captioned(svc):
    cap, _ = captions(svc, Voice(0.8))
    assert [x["text"] for x in cap.current()] == ["my mum's surgery got moved to Thursday, keep it quiet"]


# ---- VOICE-5: a learned correction can't touch the kill switch, a yes or a no ----------------------------------------

@pytest.mark.parametrize("heard, meant, said", [
    ("kill", "Kyle", "kill switch"),        # "you misheard Kyle as kill"
    ("hands", "Hans", "hands off"),         # "you misheard Hans as hands"
    ("everything", "Everyting", "stop everything"),
    ("freeze", "Frieza", "freeze"),
])
def test_voice5_a_correction_cannot_break_the_kill_switch(heard, meant, said):
    assert vocab.refuse(heard, meant, WAKE) is not None
    lst, _ = make("x", cfg={"corrections": {heard: meant}})  # one saved before 2.3.x refused it, or in config.yaml
    assert KILL.match(clean(lst.correct(said))), lst.correct(said)


@pytest.mark.parametrize("heard, meant", [("no no", "yes yes"), ("nope nope", "yes please"), ("corey", "correct"),
                                          ("abort", "a board"), ("don't", "Dont"), ("yass", "yes"),
                                          ("hold of", "hold off"), ("leaf it", "leave it")])
def test_voice5_a_correction_cannot_make_or_unmake_a_yes(heard, meant):
    assert vocab.refuse(heard, meant, WAKE) is not None
    lst, _ = make("x", cfg={"corrections": {heard: meant}})
    assert answer(lst.correct(heard)) == answer(heard), (heard, "->", lst.correct(heard))
    assert meant not in vocab.learned_words({"voice": {"corrections": {heard: meant}}})  # nor a hotword


def test_voice5_names_still_learn():
    for heard, meant in [("belatro", "Balatro"), ("zen tria", "Zentreya"), ("shivon", "Siobhan")]:
        assert vocab.refuse(heard, meant, WAKE) is None
    assert "vesper" in vocab.refuse("vespa", "Vesper", WAKE)  # the name stays as heard


# ---- VOICE-6: calibration while the voice test records ---------------------------------------------------------------

def test_voice6_calibration_refuses_while_the_voice_test_runs(svc):
    from assistant.runtime import Runtime

    sk = svc.skills.get("hearing")
    sk.bench = {"state": "recording"}  # the voice test is 5 minutes into its background recording
    lst, _ = make("x")
    ran = []
    fake = SimpleNamespace(run=lambda: ran.append(1) or {"cancelled": True}, cancelled=threading.Event())
    rt = SimpleNamespace(listener=lst, calibrator=None, cfg=svc.cfg, bus=svc.bus, verifier=None,
                         voice_status=lambda: {}, apply_voice_calibration=lambda: None, svc=svc)
    out = Runtime.start_calibration(rt, calibrator=fake)
    assert out["ok"] is False and "voice test" in out["error"] and rt.calibrator is None
    assert ran == []


# ---- VOICE-7: a mic that stops sending audio -------------------------------------------------------------------------

def test_voice7_a_mic_that_goes_quiet_is_noticed_shown_and_reopened(monkeypatch):
    plan = {"frames": 30}  # one second of audio, then the device is gone: PortAudio stops calling back

    class Stream:
        def __init__(self, callback=None, **_kw):
            self.cb, self.open = callback, True

        def __enter__(self):
            def feed(n):
                for _ in range(n):
                    if not self.open:
                        return
                    self.cb(np.zeros((480, 1), np.int16), 480, None, None)
                    time.sleep(0.01)
            threading.Thread(target=feed, args=(plan["frames"],), daemon=True).start()
            return self

        def __exit__(self, *a):
            self.open = False

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=Stream))
    import assistant.voice.vad as vadmod
    monkeypatch.setattr(vadmod, "make_segmenter", lambda *a, **k: EnergySegmenter())
    lst, _ = make("x")
    lst.load_wake_words = lambda: None
    lst.mic_silent_s = 1.0  # short for the test (3 s live); a slow runner only takes longer

    def wait_for(cond):
        deadline = time.monotonic() + 20  # generous: a slow CI runner only takes longer
        while not cond() and time.monotonic() < deadline:
            time.sleep(0.02)
        return cond()

    t = lst.restart()
    try:
        assert wait_for(lambda: not t.is_alive()), "still waiting on a mic that sends nothing"
        assert lst.state == "error" and lst.error == listener_mod.MIC_SILENT  # the HUD says so, not "listening"
        assert ("voice_state", {"state": "error", "error": listener_mod.MIC_SILENT}) in lst.bus.events
        plan["frames"] = 10 ** 6  # plugged back in: the watchdog's restart opens it again
        t = lst.restart()
        assert wait_for(lambda: lst.state == "listening" and lst.error == "")
    finally:
        lst.stop()
        t.join(5)


# ---- VOICE-8: a backslash in a learned word deafened Vesper ------------------------------------------------------------

def test_voice8_a_backslash_in_a_learned_word_is_just_a_character(svc):
    sk = svc.skills.get("hearing")
    assert sk.actions()["add"]({"heard": "acdc", "meant": "AC\\DC"})["ok"]  # the HUD's Add form
    lst, commands = make("vesper play acdc", cfg={"corrections": svc.cfg["voice"]["corrections"]})
    lst._handle_audio(np.zeros(16000, np.int16))  # raised re.error: bad escape \D, on every sentence
    assert commands == ["play AC\\DC"]


# ---- VOICE-9: an old voice test's voice match on a newer calibration ---------------------------------------------------

def test_voice9_apply_refuses_a_voice_match_measured_on_an_older_profile(svc, tmp_path):
    from assistant.voice import speaker_id

    sk = svc.skills.get("hearing")
    ver = SpeakerVerifier(tmp_path, "strict")
    speaker_id.save_profile(speaker_id.VoiceProfile(embeddings=[[1.0, 0.0]], threshold=0.42, created=100.0), tmp_path)
    ver.reload()  # Monday's profile, which the test measured
    sk.runtime = SimpleNamespace(verifier=ver, listener=None, assistant=None)
    sk.result_file.parent.mkdir(parents=True, exist_ok=True)
    rec = {"key": "speaker_threshold", "label": "Voice match", "current": 0.42, "value": 0.46, "profile": 100.0,
           "why": "At 0.42: 3 of your 36 commands turned away ..."}
    sk.report = {"recommendations": [rec]}
    speaker_id.save_profile(speaker_id.VoiceProfile(embeddings=[[0.0, 1.0]], threshold=0.58, created=200.0), tmp_path)
    ver.reload()  # Tuesday J re-calibrates: a new enrollment and its own threshold
    out = sk.apply(["speaker_threshold"])  # Wednesday: the old report's Apply is still on the panel
    assert out["ok"] is False and "calibrated again" in out["error"] and ver.profile.threshold == 0.58
    sk.report = {"recommendations": [{**rec, "profile": 200.0}]}  # a test run on Tuesday's profile still applies
    assert sk.apply(["speaker_threshold"])["ok"] and ver.profile.threshold == 0.46


# ---- VOICE-10: a damaged bpe.vocab is rebuilt before sherpa-onnx (which exits the process) reads it --------------------

@pytest.mark.parametrize("damage", [b"\x00" * 64, "▁bal\t-1\nat\t-1\nro".encode()])
def test_voice10_a_damaged_bpe_vocab_is_rebuilt(tmp_path, damage):
    (tmp_path / "tokens.txt").write_text("<blk> 0\n▁bal 1\nat 2\nro 3\n▁the 4\n", encoding="utf-8")
    (tmp_path / "bpe.vocab").write_bytes(damage)  # a power cut on the first start, or a half-written last line
    path = stt._bpe_vocab(tmp_path)
    assert path.read_text(encoding="utf-8").splitlines() == ["▁bal\t-1", "at\t-1", "ro\t-1", "▁the\t-1"]
    before = path.stat().st_mtime_ns
    stt._bpe_vocab(tmp_path)
    assert path.stat().st_mtime_ns == before  # a good one is left as it is (a read-only folder still works)


# ---- VOICE-11: Smart Turn failing mid-run --------------------------------------------------------------------------------

def test_voice11_a_turn_model_failing_mid_run_shows_on_the_hud():
    def broken(_x):
        raise RuntimeError("onnxruntime: bad allocation")

    seg = turn.TurnSegmenter(EnergySegmenter(min_rms=300, silence_ms=200), broken, pause_ms=200)
    lst, _ = make("x", cfg={"end_of_turn": "smart"})
    lst.vad_kind, lst.turn_error, lst.segmenter = seg.kind, "", seg  # what _run sets, once, at start
    assert lst.status()["vad"] == "energy+turn" and lst.status()["turn_error"] == ""
    for f in [QUIET] * 5 + [LOUD] * 30 + [QUIET] * 40:
        seg.feed(f)
    st = lst.status()
    # app.js shows 'silence (Smart Turn unavailable: …)' for end_of_turn smart without '+turn'
    assert st["vad"] == "energy" and "bad allocation" in st["turn_error"]


# ---- VOICE-12: "I said Tuesday, not Monday" is J changing his mind ---------------------------------------------------------

@pytest.mark.parametrize("said", ["I said Tuesday, not Monday", "you got Monday but I said Tuesday"])
def test_voice12_changing_his_mind_is_not_a_mishearing(svc, said):
    ctx = RouterContext(skill_route=svc.skills.route)
    svc.bus.publish("user_said", {"text": "move the dentist to Monday", "source": "voice"})  # heard right
    intent = route(said, ctx)
    assert intent is None or intent.tool != "learn_word", intent


def test_voice12_a_near_spelling_just_heard_still_learns(svc):
    ctx = RouterContext(skill_route=svc.skills.route)
    svc.bus.publish("user_said", {"text": "add Niam to the meeting", "source": "voice"})
    intent = route("I said Niamh, not Niam", ctx)
    assert intent.tool == "learn_word" and intent.args == {"heard": "niam", "meant": "niamh"}
    assert route("you heard shivon but I said Siobhan", ctx).tool == "learn_word"  # "heard": about my ears
