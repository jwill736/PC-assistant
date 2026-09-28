"""Phase 3: local neural voices, sentence streaming, turns, interrupts, settings that stick."""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from assistant.config import load_config, save_setting
from assistant.voice import neural, tts
from assistant.voice.neural import AudioOut, SentenceStream, speakable, split_sentences


# ---- text -> sentences ---------------------------------------------------------

def test_split_sentences_keeps_abbreviations_numbers_and_lists_together():
    assert split_sentences("Good morning. You have three meetings, the first at 10. Dr. Lee moved to 3:30 PM! OK?") == [
        "Good morning.", "You have three meetings, the first at 10.", "Dr. Lee moved to 3:30 PM!", "OK?"]
    assert split_sentences("1. Open Discord\n2. Switch to BRB") == ["1. Open Discord", "2. Switch to BRB"]
    assert split_sentences("  ") == []


def test_run_on_sentence_is_cut_at_a_comma_so_speech_can_start():
    text = ("This keeps going and going with clause after clause about your schedule and the stream, "
            "and the thumbnail and the sponsor read and the raid target, until it finally ends")
    parts = split_sentences(text)
    assert len(parts) == 2 and parts[0].endswith(",")


def test_stream_hands_out_each_sentence_when_the_next_one_starts_and_marks_the_last():
    out = []
    s = SentenceStream(lambda text, final: out.append((text, final)))
    for tok in "Sure. Opening Discord now. Want me to start the stream?".split(" "):
        s.feed(tok + " ")
    assert out == [("Sure.", False), ("Opening Discord now.", False)]  # the question is held back
    s.flush(final=True)
    assert out[-1] == ("Want me to start the stream?", True)


def test_speakable_spells_out_initialisms():
    assert speakable("Switched to BRB. CPU at 91% and OBS is OK.") == \
        "Switched to B R B. C P U at 91 percent and O B S is okay."
    assert speakable("I think so") == "I think so"


# ---- playback --------------------------------------------------------------------

class FakeStream:
    def __init__(self, callback):
        self.callback, self.started, self.closed = callback, False, False

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        self.closed = True

    def pull(self, frames=480):
        buf = np.full((frames, 1), 9.0, np.float32)
        self.callback(buf, frames)
        return buf[:, 0]


def audio_out(sr=1000, **kw):
    streams = []
    out = AudioOut(sr, stream_factory=lambda cb: streams.append(FakeStream(cb)) or streams[-1], **kw)
    return out, streams


def test_audio_out_plays_chunks_in_order_then_goes_idle():
    out, streams = audio_out()
    assert not out.playing and not streams  # the device opens on first use
    out.play(np.ones(300, np.float32))
    out.play(np.full(300, 2.0, np.float32))
    assert out.playing and streams[0].started
    buf = streams[0].pull(480)
    assert list(buf[:300]) == [1.0] * 300 and list(buf[300:]) == [2.0] * 180
    buf = streams[0].pull(480)
    assert list(buf[:120]) == [2.0] * 120 and not buf[120:].any()  # silence, not stale data
    assert not out.playing and out.wait(timeout=0.1)


def test_abort_fades_out_within_ten_ms_instead_of_clicking():
    out, streams = audio_out(sr=1000)
    out.play(np.ones(5000, np.float32))
    streams[0].pull(100)
    out.abort()
    buf = streams[0].pull(100)
    tail = buf[:10]
    assert tail[0] == 1.0 and tail[-1] == 0.0 and np.all(np.diff(tail) <= 0)  # a 10 ms ramp down
    assert not buf[10:].any() and not out.playing


def test_idle_stream_is_closed_and_reopened():
    out, streams = audio_out(idle_close_s=0)
    out.play(np.ones(10, np.float32))
    streams[0].pull()
    out.maybe_close()
    assert streams[0].closed
    out.play(np.ones(10, np.float32))
    assert len(streams) == 2 and streams[1].started


# ---- speaker ---------------------------------------------------------------------

class Bus:
    client_count = 0

    def __init__(self):
        self.events = []

    def publish(self, type_, data, sticky=False):
        self.events.append((type_, data))

    def of(self, kind):
        return [d for t, d in self.events if t == kind]


class FakeVoice:
    sample_rate = 1000

    def __init__(self, engine="supertonic", voice_id=None, speed=1.0, delay=0.0, fail_on=None):
        self.engine, self.voice, self.speed = engine, neural.voice(engine, voice_id), speed
        self.said, self.delay, self.fail_on = [], delay, fail_on

    def synth(self, text):
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("bad text")
        time.sleep(self.delay)
        self.said.append(text)
        return np.ones(200, np.float32)  # 0.2 s at 1 kHz

    def warm_up(self):
        return 0.0


class Card:
    """Plays audio in real time on a thread, like a sound card pulling from the callback."""

    def __init__(self, callback):
        self.callback, self.run = callback, False

    def start(self):
        self.run = True

        def loop():
            buf = np.zeros((20, 1), np.float32)
            while self.run:
                self.callback(buf, 20)
                time.sleep(0.02)
        threading.Thread(target=loop, daemon=True).start()

    def stop(self):
        self.run = False

    def close(self):
        self.run = False


@pytest.fixture
def neural_speaker(monkeypatch):
    monkeypatch.setattr(neural, "available", lambda: True)
    made = []

    def make(engine="supertonic", **kw):
        def loader(eng, _dir, voice_id, speed, _threads, on_progress=None):
            v = FakeVoice(eng, voice_id, speed, **kw)
            made.append(v)
            return v
        bus = Bus()
        sp = tts.Speaker(bus, engine, voice="m2", loader=loader,
                         audio_factory=lambda sr: AudioOut(sr, stream_factory=Card))
        sp.start()
        wait_for(lambda: sp.state == "ready")
        return sp, bus, made
    return make


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    raise AssertionError("timed out")


def test_auto_prefers_the_local_neural_voice(monkeypatch):
    monkeypatch.setattr(neural, "available", lambda: True)
    assert tts.resolve_engine("auto") == "supertonic"
    assert tts.resolve_engine("kokoro") == "kokoro"
    monkeypatch.setattr(neural, "available", lambda: False)
    monkeypatch.setattr(tts, "_offline_tts_available", lambda: False)
    assert tts.resolve_engine("auto") == tts.resolve_engine("kokoro") == "browser"
    assert tts.resolve_engine("none") == "none"


def test_reply_is_spoken_sentence_by_sentence_and_the_flag_clears_after_playback(neural_speaker):
    sp, bus, made = neural_speaker()
    assert made[0].voice.id == "m2"
    sp.say("Good morning. Stream at eight.")
    assert sp.speaking.is_set()
    wait_for(lambda: not sp.speaking.is_set())
    assert made[0].said == ["Good morning.", "Stream at eight."]
    assert sp.first_audio_ms and sp.status()["first_audio_ms"] is not None
    assert [d["active"] for d in bus.of("speaking")][-1] is False


def test_interrupt_silences_the_rest_of_the_turn_even_what_claude_is_still_streaming(neural_speaker):
    sp, _bus, made = neural_speaker()
    turn = sp.new_turn()
    sp.say("Here's the whole rundown of your afternoon.", turn=turn)
    wait_for(lambda: sp.out is not None and sp.out.playing)
    assert sp.interrupt() is True
    sp.say("And another thing.", turn=turn)  # arrives from the stream after the interrupt
    wait_for(lambda: not sp.speaking.is_set())
    assert "And another thing." not in made[0].said
    sp.say("Cancelled.")  # the next thing said on its own starts a fresh turn
    wait_for(lambda: "Cancelled." in made[0].said)


def test_a_new_command_drops_the_unspoken_rest_of_the_old_reply(neural_speaker):
    sp, _bus, made = neural_speaker(delay=0.05)
    old = sp.new_turn()
    for s in ("One.", "Two.", "Three.", "Four."):
        sp.say(s, expects_reply=False, turn=old)
    new = sp.new_turn()
    sp.say("Switched to BRB.", turn=new)
    wait_for(lambda: not sp.speaking.is_set())
    assert made[0].said[-1] == "Switched to BRB." and "Four." not in made[0].said
    assert sp.last_text == "Switched to BRB."


def test_streamed_sentences_share_the_echo_text_and_the_last_one_sets_the_follow_up(neural_speaker):
    sp, _bus, _made = neural_speaker()
    turn = sp.new_turn()
    sp.say("Opening Discord.", expects_reply=False, turn=turn)
    sp.say("Want the stream too?", expects_reply=None, turn=turn)
    assert sp.last_text == "Opening Discord. Want the stream too?" and sp.expects_reply is True


def test_a_sentence_that_fails_to_synthesise_is_skipped_not_fatal(neural_speaker):
    sp, _bus, made = neural_speaker(fail_on="bad")
    sp.say("Fine one. A bad one. Last one.")
    wait_for(lambda: not sp.speaking.is_set())
    assert made[0].said == ["Fine one.", "Last one."]


def test_voice_switch_on_the_same_model_is_instant_and_engine_switch_reloads(neural_speaker):
    sp, _bus, made = neural_speaker()
    first = sp._thread
    sp.configure(voice="f3", speed=1.2)
    assert sp._thread is first and sp.neural.voice.id == "f3" and sp.neural.speed == 1.2
    sp.configure(engine="kokoro", voice="bm_george")
    wait_for(lambda: sp.neural is not None and sp.neural.engine == "kokoro")
    assert made[-1].voice.label.startswith("George") and not first.is_alive()
    assert sp.restart() is sp._thread  # the watchdog finding the old thread dead must not start a second worker


def test_voice_picked_while_the_model_downloads_is_used_once_it_loads(monkeypatch):
    monkeypatch.setattr(neural, "available", lambda: True)
    gate = threading.Event()
    loads = []

    def slow_loader(eng, _dir, voice_id, speed, _threads, on_progress=None):
        loads.append(voice_id)
        gate.wait(2)
        return FakeVoice(eng, voice_id, speed)
    sp = tts.Speaker(Bus(), "kokoro", voice="bm_george", loader=slow_loader,
                     audio_factory=lambda sr: AudioOut(sr, stream_factory=Card))
    sp.start()
    wait_for(lambda: sp.state == "loading")
    sp.configure(voice="bm_lewis", speed=1.1)  # no second download
    gate.set()
    wait_for(lambda: sp.state == "ready")
    assert loads == ["bm_george"] and sp.neural.voice.id == "bm_lewis" and sp.neural.speed == 1.1


def test_failed_model_load_falls_back_and_says_why(monkeypatch):
    monkeypatch.setattr(neural, "available", lambda: True)
    monkeypatch.setattr(tts, "_offline_tts_available", lambda: False)

    def broken(*_a, **_k):
        raise OSError("download blocked")
    sp = tts.Speaker(Bus(), "supertonic", loader=broken)
    sp.start()
    wait_for(lambda: sp.state == "fallback")
    assert sp.engine_name == "browser" and "download blocked" in sp.status()["error"]


def test_preview_interrupts_and_speaks_a_sample(neural_speaker):
    sp, _bus, made = neural_speaker()
    sp.preview()
    wait_for(lambda: len(made[0].said) >= 1)
    assert made[0].said[0] == "Good evening."


# ---- settings chosen in the HUD stick, over config.yaml -------------------------

def test_hud_settings_win_over_config_yaml_and_survive_a_reload(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "data_dir: data\nvoice:\n  stt_engine: auto\n  speaker_check: strict\n  tts:\n    engine: pyttsx3\n")
    cfg = load_config(tmp_path / "config.yaml")
    (cfg.data_dir / "calibration.yaml").write_text("voice:\n  stt_engine: moonshine\n  min_rms: 700\n")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg["voice"]["stt_engine"] == "auto" and cfg["voice"]["min_rms"] == 700  # measured values stay under
    save_setting(cfg, "voice.speaker_check", "log")
    save_setting(cfg, "voice.tts.engine", "kokoro")
    save_setting(cfg, "voice.stt_engine", "moonshine")
    assert cfg["voice"]["speaker_check"] == "log"  # applied in memory at once
    again = load_config(tmp_path / "config.yaml")
    assert again["voice"]["speaker_check"] == "log" and again["voice"]["tts"]["engine"] == "kokoro"
    assert again["voice"]["stt_engine"] == "moonshine" and again["voice"]["tts"]["rate"] == 190
    saved = yaml.safe_load((cfg.data_dir / "settings.yaml").read_text())
    assert saved == {"voice": {"speaker_check": "log", "stt_engine": "moonshine", "tts": {"engine": "kokoro"}}}


# ---- push-to-talk while it's talking -----------------------------------------------

def test_hotkey_while_speaking_interrupts_first():
    from assistant.voice.listener import VoiceListener

    calls = []
    speaker = SimpleNamespace(speaking=threading.Event(), chime=lambda: calls.append("chime"),
                              interrupt=lambda: calls.append("interrupt"))
    speaker.speaking.set()
    lst = VoiceListener(Bus(), speaker, ["friday"], lambda _t: None, {})
    lst.arm(source="hotkey")
    assert calls == ["interrupt", "chime"]
    calls.clear()
    lst.arm(source="wake")  # "Vesper?" said over a reply is handled by barge-in, not here
    assert calls == ["chime"]


# ---- health check ------------------------------------------------------------------

def test_doctor_reports_the_speaking_voice(cfg, monkeypatch):
    from assistant import doctor

    cfg["voice"]["tts"]["engine"] = "none"
    assert doctor.check_tts(cfg).status == doctor.SKIP
    cfg["voice"]["tts"]["engine"] = "auto"
    monkeypatch.setattr(neural, "available", lambda: False)
    monkeypatch.setattr(tts, "_offline_tts_available", lambda: True)
    c = doctor.check_tts(cfg)
    assert c.status == doctor.WARN and "Windows SAPI" in c.detail and "requirements-voice" in c.fix

    monkeypatch.setattr(neural, "available", lambda: True)
    slow = FakeVoice("kokoro", "bm_george", delay=1.0)
    monkeypatch.setattr(neural, "load", lambda *a, **k: slow)
    cfg["voice"]["tts"]["engine"] = "kokoro"
    c = doctor.check_tts(cfg)
    assert c.status == doctor.WARN and "George" in c.detail and "Supertonic" in c.fix
