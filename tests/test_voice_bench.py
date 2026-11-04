"""The voice test (voice/bench.py RealBench, skills/hearing.py): J's commands and background replayed through the
capture chain, numbers out, settings suggested, nothing changed until Apply. Every model is a fake: amplitudes stand
for words (so the fake recogniser can tell which part of a command a piece holds), and time is samples."""

import math
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from assistant.voice import bench, turn
from assistant.voice.listener import FRAME_SAMPLES, EnergySegmenter

WORDS = {2500: "Friday, open Discord", 2700: "mute the mic", 2900: "Friday, clip that", 3100: "start stream mode",
         3300: "Friday, good morning", 4000: "Friday, remind me to", 4400: "call the dentist",
         1200: "friday is so good at this game", 1400: "nice shot"}


def tone(amplitude, ms):
    n = FRAME_SAMPLES * (ms // 30)
    return np.array([int(amplitude * math.sin(i / 3)) for i in range(n)], dtype=np.int16)


def gap(ms):
    return np.zeros(FRAME_SAMPLES * (ms // 30), dtype=np.int16)


def amplitudes(samples):
    """The 'words' in a clip, in order: each 30 ms frame's peak, to the nearest 100."""
    x = np.asarray(samples, dtype=np.float32) * 32768
    out = []
    for i in range(0, len(x) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        a = int(round(np.abs(x[i:i + FRAME_SAMPLES]).max() / 100) * 100)
        if a in WORDS and (not out or out[-1] != a):
            out.append(a)
    return out


def transcribe(samples, hints=()):
    return " ".join(WORDS[a] for a in amplitudes(samples))


def score(voice):
    a = amplitudes(voice)
    return 0.30 if a == [1200] else 0.62 if a == [1400] else 0.84


def fake_turn(samples):  # "Friday, remind me to" alone doesn't sound finished
    return 0.1 if amplitudes(samples)[-1:] == [4000] else 0.9


def make_seg(cfg):
    inner_ms = int(cfg.get("turn_pause_ms", 210)) if cfg.get("end_of_turn") == "smart" else int(cfg.get("silence_ms", 400))
    inner = EnergySegmenter(min_rms=300, silence_ms=inner_ms, max_utterance_s=15)
    if cfg.get("end_of_turn") == "smart":
        return turn.TurnSegmenter(inner, fake_turn, pause_ms=210, max_silence_ms=1500,
                                  fallback_ms=int(cfg.get("silence_ms", 400)))
    return inner


COMMANDS = [("Friday, open Discord", tone(2500, 900)), ("Mute the mic", tone(2700, 900)),
            ("Friday, clip that", tone(2900, 900)), ("Start stream mode", tone(3100, 900)),
            ("Friday, good morning", tone(3300, 900)),
            ("Friday, remind me to call the dentist", np.concatenate([tone(4000, 600), gap(700), tone(4400, 600)]))]
BACKGROUND = np.concatenate([gap(900), tone(1200, 1200), gap(1500), tone(1400, 900), gap(1500)])


def chain(**kw):
    args = dict(wake_words=["friday", "hey friday"], transcribe=transcribe, segmenter=make_seg, hints=["Discord"],
                score=score, threshold=0.6, turn_model=fake_turn)
    return bench.Chain(cfg={"vad": "energy", "silence_ms": 400, **kw.pop("cfg", {})}, **{**args, **kw})


@pytest.fixture(scope="module")
def report():
    return bench.RealBench(chain(), clock=lambda: 1000.0).run(COMMANDS, BACKGROUND)


def test_numbers_from_your_commands(report):
    assert report["commands"] == 6 and report["wer"] == 0.0 and report["wer_without_names"] == 0.0
    assert report["named"] == 4 and report["missed_wakes"] == 0
    assert report["cut_off"] == 1  # the pause in "remind me to … call the dentist" ended it at 400 ms
    cut = next(r for r in report["rows"] if r["cut"])
    assert cut["pieces"] == 2 and cut["heard"] == "Friday, remind me to call the dentist"
    lat = report["latency_ms"]
    assert 380 <= lat["endpoint_p50"] <= 450 and lat["p50"] >= lat["endpoint_p50"]


def test_numbers_from_your_background(report):
    assert report["background_s"] == 6
    assert report["false_wakes"] == 1 and report["false_heard"] == ["friday is so good at this game"]
    assert report["would_run"] == 0  # the voice check turned it away
    assert report["false_wakes_per_hour"] == 600.0
    assert sorted(report["other_scores"]) == [0.3, 0.62] and set(report["owner_scores"]) == {0.84}


def test_settings_tried_on_the_same_recordings(report):
    ends = report["tried"]["endpoint"]
    assert ends[400]["cut"] == 1 and ends[800]["cut"] == 0 and ends[800]["delay_ms"] > ends[400]["delay_ms"]
    assert report["tried"]["turn"]["smart"]["cut"] == 0
    assert report["tried"]["turn"]["smart"]["delay_ms"] < ends[400]["delay_ms"]  # and it answers sooner


def test_suggestions_come_from_the_numbers(report):
    recs = {r["key"]: r for r in report["recommendations"]}
    assert recs["voice.end_of_turn"]["value"] == "smart" and "0 of 6 commands cut off" in recs["voice.end_of_turn"]["why"]
    assert "voice.silence_ms" not in recs  # one choice for how a sentence ends, not two that fight
    assert recs["speaker_threshold"]["current"] == 0.6 and recs["speaker_threshold"]["value"] == 0.72
    assert "1 of 2 other voices let in" in recs["speaker_threshold"]["why"]
    assert "voice.hotwords_score" not in recs  # names didn't hurt


def test_silence_wins_when_the_model_cant_hold_a_pause():
    rep = bench.RealBench(chain(turn_model=None)).run(COMMANDS, None)
    recs = {r["key"]: r for r in rep["recommendations"]}
    assert recs["voice.silence_ms"]["value"] == 800 and "voice.end_of_turn" not in recs
    assert rep["background_s"] == 0 and rep["false_wakes_per_hour"] is None


def test_nothing_to_suggest_when_settings_fit():
    commands = COMMANDS[:5]
    rep = bench.RealBench(chain(turn_model=None, threshold=None, cfg={"silence_ms": 300})).run(commands, None)
    assert rep["recommendations"] == []


def test_names_that_hurt_are_turned_off():
    def mangled(samples, hints=()):
        return transcribe(samples).replace("Discord", "Discard") if hints else transcribe(samples)

    rep = bench.RealBench(chain(transcribe=mangled, turn_model=None)).run(COMMANDS[:5], None)
    assert rep["wer"] > rep["wer_without_names"]
    assert {r["key"]: r["value"] for r in rep["recommendations"]}.get("voice.hotwords_score") == 0


def test_the_name_model_is_scored_at_the_start_of_each_utterance():
    class Wake:
        threshold, thresholds, last_scores = 0.5, {"friday": 0.6}, {}

        def reset(self):
            self.n = 0

        def feed(self, frame):
            self.n += 1
            self.last_scores = {"friday": 0.9 if 20 <= self.n <= 30 else 0.05}

    utts = bench.replay(make_seg({"silence_ms": 400}), np.concatenate([gap(600), tone(2500, 900), gap(900)]), Wake(), "friday")
    assert len(utts) == 1 and utts[0]["wake_peak"] == 0.9
    c = chain(wake=Wake(), wake_name="friday", wake_mode="acoustic")
    assert c.name_threshold() == 0.6 and bench.hear(c, utts[0])["woke"] is True


@pytest.mark.parametrize("good,bad,want", [
    ([0.6, 0.62, 0.65, 0.7, 0.68], [0.3, 0.4], 0.5),    # a gap: sit in the middle
    ([0.6, 0.62, 0.65, 0.7, 0.68], [], 0.57),           # nobody else heard: just under your weakest
    ([0.9, 0.95, 0.92, 0.91, 0.93], [0.7], 0.72),       # never above the model's sane band
    ([0.8, 0.82], [0.4], None),                         # too few of yours to say
])
def test_split_point(good, bad, want):
    assert bench.split_point(good, bad, 0.35, 0.72) == want


def test_split_point_with_overlap_lets_fewer_strangers_in():
    t = bench.split_point([0.55, 0.6, 0.7, 0.75, 0.8, 0.85], [0.5, 0.58, 0.62], 0.35, 0.72)
    assert 0.62 < t <= 0.7


def test_apply_saves_only_what_was_ticked(cfg):
    recs = [{"key": "voice.end_of_turn", "current": "silence", "value": "smart"},
            {"key": "voice.endpoint_ms", "current": 400, "value": 600},
            {"key": "speaker_threshold", "current": 0.6, "value": 0.66}]
    verifier = SimpleNamespace(set=[], set_threshold=lambda t: verifier.set.append(t) or {"threshold": t})
    done = bench.apply(cfg, recs, ["voice.end_of_turn", "speaker_threshold"], verifier)
    assert done == ["voice.end_of_turn", "speaker_threshold"] and verifier.set == [0.66]
    layer = yaml.safe_load((cfg.data_dir / "settings.yaml").read_text(encoding="utf-8"))
    assert layer == {"voice": {"end_of_turn": "smart"}} and cfg["voice"]["end_of_turn"] == "smart"


def test_set_threshold_replaces_the_calibrated_one(tmp_path):
    from assistant.voice import speaker_id

    profile = speaker_id.build_profile([[1.0, 0.0], [0.9, 0.1], [0.95, 0.05]])
    speaker_id.save_profile(profile, tmp_path)
    v = speaker_id.SpeakerVerifier(tmp_path, embedder=object())
    assert v.set_threshold(0.99)["threshold"] == speaker_id.MAX_THRESHOLD  # inside the sane band
    again = speaker_id.load_profile(tmp_path)
    assert again.threshold == again.enrolled_threshold == speaker_id.MAX_THRESHOLD
    assert speaker_id.SpeakerVerifier(tmp_path / "none", embedder=object()).set_threshold(0.5) is None


# ---- what to read, and the recording ---------------------------------------------------------------------------

def test_phrases_start_with_your_own_commands():
    history = ["open Discord", "switch to the gameplay scene", "set a timer for 10 minutes", "open discord",
               "what's the weather", "x"]
    lines = bench.phrases("Friday", history, count=20)
    assert len(lines) == 18 and sum(bench.PAUSE in x for x in lines) == 4  # 3 of yours, 11 more everyday, 4 pauses
    mine = [x for x in lines if bench.PAUSE not in x][:3]
    assert mine == ["Friday, what's the weather", "open discord", "Friday, switch to the gameplay scene"]
    assert not any("10" in x for x in lines)  # digits read back differently than they're written
    assert sum(x.lower().endswith("open discord") for x in lines) == 1  # not twice
    assert bench.reference("Friday, remind me to … call the dentist tomorrow") == "Friday, remind me to call the dentist tomorrow"
    assert 2.2 < bench.record_seconds("Mute the mic") < bench.record_seconds("Friday, remind me to … call the dentist") <= 10


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, type_, data, sticky=False):
        self.events.append((type_, data))


def test_recorder_reads_the_lines_then_records_background_with_vesper_talking(cfg):
    bus, said, asked = Bus(), [], []

    def record(seconds, on_level):
        asked.append(seconds)
        on_level(500.0)
        return np.full(int(seconds * 100), 0.1, dtype=np.float32)

    rec = bench.Recorder(cfg, bus, record, ["Mute the mic", "Friday, remind me to … call Dana"], background_s=70,
                         speak=said.append, chunk_s=30, prompt_pause=0)
    got = rec.run()
    assert [r for r, _ in got["commands"]] == ["Mute the mic", "Friday, remind me to call Dana"]
    assert got["commands"][0][1].dtype == np.int16 and len(asked) == 2 + 3 and asked[2:] == [30, 30, 10]
    assert said == bench.VESPER_LINES[:3] and not any("Friday" in s for s in said)  # never its own name
    steps = {d.get("step") for t, d in bus.events if t == "voice_bench"}
    assert steps >= {"commands", "background"} and not any(t == "calibration" for t, _ in bus.events)


def test_recorder_cancel(cfg):
    bus = Bus()
    rec = bench.Recorder(cfg, bus, lambda s, cb: np.zeros(10, np.float32), ["a b", "c d"], prompt_pause=0)
    rec.cancelled.set()
    assert rec.run() == {"cancelled": True}
    assert ("voice_bench", {"step": "cancelled"}) in bus.events


def test_recordings_round_trip(tmp_path):
    bench.save_recordings(tmp_path / "t", COMMANDS[:2], BACKGROUND)
    commands, background = bench.load_recordings(tmp_path / "t")
    assert [r for r, _ in commands] == [r for r, _ in COMMANDS[:2]] and np.array_equal(commands[1][1], COMMANDS[1][1])
    assert np.array_equal(background, BACKGROUND)
    assert bench.load_recordings(tmp_path / "missing") is None


# ---- the skill: the whole flow on a fake runtime ---------------------------------------------------------------

class Listener:
    def __init__(self, cfg):
        self.cfg, self.models_dir, self.wake_words = cfg, None, ["friday", "hey friday"]
        self.paused, self.log, self.restarts = set(), [], 0

    def pause(self, why):
        self.paused.add(why)
        self.log.append(("pause", why))

    def resume(self, why):
        self.paused.discard(why)
        self.log.append(("resume", why))

    def ensure_stt(self):
        return SimpleNamespace(transcribe=transcribe)

    def _hints(self):
        return ["Friday"]

    def correct(self, text):
        return text

    def wake_mode(self):
        return "transcript"

    def trigger_kind(self, _n):
        return "name"

    def restart(self):
        self.restarts += 1


@pytest.fixture
def flow(svc, monkeypatch):
    from assistant.voice import vad, wakeword

    svc.cfg["voice"].update({"vad": "energy", "silence_ms": 400})
    lst = Listener(svc.cfg["voice"])
    sk = svc.skills.get("hearing")
    sk.threads = False
    seen = {}

    def recorder(cfg, bus, lines, background_s, speak):
        seen.update(lines=lines, background_s=background_s)
        return SimpleNamespace(run=lambda: (seen.update(paused=set(lst.paused)) or
                                            {"commands": COMMANDS, "background": BACKGROUND}), cancelled=SimpleNamespace(set=lambda: None))

    sk._recorder = recorder

    def no_model(*_a, **_k):
        raise RuntimeError("not downloaded")

    monkeypatch.setattr(turn, "load", no_model)
    monkeypatch.setattr(wakeword, "load", lambda *_a, **_k: None)
    monkeypatch.setattr(vad, "make_segmenter", lambda c, *_a, **_k: make_seg(c))
    sk.start(SimpleNamespace(listener=lst, verifier=None, speaker=None, calibrator=None, assistant=None))
    return sk, lst, seen


def test_the_voice_test_end_to_end(flow, svc):
    sk, lst, seen = flow
    assert sk.actions()["start"]({"phrases": [r for r, _ in COMMANDS], "background_s": 5})["ok"]
    assert seen["background_s"] == 30 and seen["paused"] == {"voice test"}  # the mic was the test's, not commands'
    assert not lst.paused and lst.log[-1] == ("resume", "voice test")
    st = sk.status()
    assert st["bench"]["state"] == "done" and st["recordings"] and st["report"]["cut_off"] == 1
    assert "Natural end of turn couldn't be tried" in st["report"]["notes"][0]
    assert (svc.cfg.data_dir / "bench" / "voice-test.json").exists()
    recs = {r["key"]: r for r in sk.report["recommendations"]}
    assert recs["voice.silence_ms"]["value"] == 800
    assert "silence_ms" not in yaml.safe_load((svc.cfg.data_dir / "settings.yaml").read_text()).get("voice", {}) \
        if (svc.cfg.data_dir / "settings.yaml").exists() else True  # suggested, not applied
    out = sk.actions()["apply"]({"keys": ["voice.silence_ms"]})
    assert out == {"ok": True, "applied": ["voice.silence_ms"]} and svc.cfg["voice"]["silence_ms"] == 800
    assert lst.restarts == 1 and sk.report["applied"] == ["voice.silence_ms"]
    assert sk.actions()["apply"]({"keys": []})["ok"] is False


def test_rerun_uses_the_saved_recordings_and_delete_removes_them(flow):
    sk, lst, _seen = flow
    assert sk.rerun()["ok"] is False  # nothing recorded yet
    sk.actions()["start"]({"phrases": [r for r, _ in COMMANDS], "background_s": 60})
    sk.report = None
    assert sk.actions()["rerun"]({})["ok"] and sk.report["commands"] == 6
    assert sk.actions()["delete"]({})["ok"] and not sk.status()["recordings"]
    assert sk.report is not None  # the result stays


def test_the_test_refuses_to_start_when_it_cant(flow):
    sk, lst, _seen = flow
    assert "at least 5" in sk.start_test(["one", "two"], 60)["error"]
    sk.runtime.calibrator = object()
    assert "Calibration" in sk.start_test(["a b"] * 6, 60)["error"]
    sk.runtime.calibrator, sk.runtime.listener = None, None
    assert "Voice is off" in sk.start_test(["a b"] * 6, 60)["error"]


def test_the_end_of_turn_switch_saves_and_restarts_the_listener(flow, svc):
    sk, lst, _seen = flow
    assert sk.actions()["end_of_turn"]({"mode": "smart"}) == {"ok": True, "end_of_turn": "smart"}
    assert svc.cfg["voice"]["end_of_turn"] == "smart" and lst.restarts == 1 and sk.status()["end_of_turn"] == "smart"
    assert yaml.safe_load((svc.cfg.data_dir / "settings.yaml").read_text())["voice"]["end_of_turn"] == "smart"
    assert sk.actions()["end_of_turn"]({"mode": "always"})["ok"] is False


def test_cancel_and_failure_give_the_mic_back(flow):
    sk, lst, _seen = flow
    sk._recorder = lambda *a: SimpleNamespace(run=lambda: {"cancelled": True}, cancelled=None)
    sk.start_test(["a b"] * 6, 60)
    assert sk.bench["state"] == "cancelled" and not lst.paused

    def boom():
        raise OSError("no microphone")

    sk._recorder = lambda *a: SimpleNamespace(run=boom, cancelled=None)
    sk.start_test(["a b"] * 6, 60)
    assert sk.bench["state"] == "error" and "no microphone" in sk.bench["error"] and not lst.paused
