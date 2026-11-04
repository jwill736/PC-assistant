"""Vocabulary: names as Parakeet hotwords (with a fallback), and corrections that are learned only on J's yes or
click ("you misheard X as Y", retyping what he said), never for words that steer Vesper."""

import sys
import time
from types import SimpleNamespace

import pytest
import yaml

from assistant.brain.assistant import Assistant
from assistant.brain.router import RouterContext, route
from assistant.skills import hearing as hmod
from assistant.voice import stt, vocab


# ---- hotwords -----------------------------------------------------------------------------------------------------

class FakeRecognizer:
    made: list = []

    def __init__(self, kw):
        self.kw, self.streams = kw, []

    @classmethod
    def from_transducer(cls, **kw):
        if kw.get("decoding_method") == "modified_beam_search" and FakeRecognizer.refuse_beam:
            raise RuntimeError("hotwords need a newer sherpa-onnx")
        rec = cls(kw)
        cls.made.append(rec)
        return rec

    def create_stream(self, hotwords=None):
        s = SimpleNamespace(hotwords=hotwords, accept_waveform=lambda *_a: None,
                            result=SimpleNamespace(text=" vesper set the category to balatro "))
        self.streams.append(s)
        return s

    def decode_stream(self, _s):
        pass


@pytest.fixture
def parakeet_dir(tmp_path, monkeypatch):
    (tmp_path / "tokens.txt").write_text("<blk> 0\n▁bal 1\nat 2\nro 3\n▁the 4\n", encoding="utf-8")
    FakeRecognizer.made, FakeRecognizer.refuse_beam = [], False
    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(OfflineRecognizer=FakeRecognizer))
    return tmp_path


def test_parakeet_hears_names_as_hotwords(parakeet_dir):
    eng = stt.SherpaSTT("parakeet", parakeet_dir, hotwords_score=2.0)
    kw = FakeRecognizer.made[0].kw
    assert kw["decoding_method"] == "modified_beam_search" and kw["hotwords_score"] == 2.0 and kw["modeling_unit"] == "bpe"
    vocab_lines = (parakeet_dir / "bpe.vocab").read_text(encoding="utf-8").splitlines()
    assert vocab_lines == ["▁bal\t-1", "at\t-1", "ro\t-1", "▁the\t-1"]  # fewest pieces per word; no <blk>
    text = eng.transcribe([0.0] * 1600, ["Vesper", "Balatro", "Hollow Knight: Silksong", "BRB", "x", "Balatro"])
    assert FakeRecognizer.made[0].streams[-1].hotwords == "vesper/balatro/hollow knight silksong/brb"
    assert text == "Vesper set the category to Balatro"  # the hint's own capitals back
    assert eng.hotwords_status() == {"on": True, "words": 4, "error": ""}


def test_no_hints_no_hotwords(parakeet_dir):
    eng = stt.SherpaSTT("parakeet", parakeet_dir, hotwords_score=2.0)
    assert eng.transcribe([0.0] * 1600) == "vesper set the category to balatro"
    assert FakeRecognizer.made[0].streams[-1].hotwords is None


def test_hotwords_off_or_unsupported_falls_back_to_plain_decoding(parakeet_dir):
    off = stt.SherpaSTT("parakeet", parakeet_dir, hotwords_score=0)
    assert "decoding_method" not in FakeRecognizer.made[-1].kw and off.hotwords_status()["on"] is False
    FakeRecognizer.refuse_beam = True
    old = stt.SherpaSTT("parakeet", parakeet_dir, hotwords_score=2.0)
    assert old.hotwords is False and "newer sherpa-onnx" in old.hotwords_error
    assert old.transcribe([0.0] * 1600, ["Balatro"]) == "vesper set the category to balatro"  # still hears you
    assert FakeRecognizer.made[-1].streams[-1].hotwords is None


def test_hotword_list_cleans_and_caps():
    words = stt.hotword_list([f"Player {chr(97 + i % 26)}{chr(97 + i // 26)}" for i in range(100)] + ["ok", "Chat!"])
    assert len(words) == stt.MAX_HOTWORDS and all(w.isalpha() or " " in w for w in words) and "ok" not in words


# ---- learning a correction from what J says ---------------------------------------------------------------------

@pytest.mark.parametrize("said,heard,meant", [
    ("you misheard balatro as belatro", "belatro", "balatro"),
    ("misheard niamh as niam", "niam", "niamh"),
    ("you heard shivon but i said siobhan", "shivon", "siobhan"),
    ("i said kaelthas not chaos", "chaos", "kaelthas"),
    ("it's helldivers not heldivers", "heldivers", "helldivers"),
])
def test_correction_phrases(said, heard, meant):
    got = vocab.parse_correction(said)
    assert got[:2] == (heard, meant)


def test_ordinary_sentences_are_not_corrections():
    assert vocab.parse_correction("open discord") is None
    assert vocab.parse_correction("what did you hear") is None


@pytest.mark.parametrize("heard,meant,why", [
    ("no", "yes", "steers me"), ("nope", "do it", "steers me"), ("stop", "start", "steers me"),
    ("vespa", "friday", "steers me"), ("to", "two", "too common"), ("niam", "niam", "both of those"),
    ("a b c d e", "x", "more than a word"),
])
def test_words_that_steer_vesper_are_never_rewritten(heard, meant, why):
    assert why in vocab.refuse(heard, meant, ["friday", "hey friday"])


def test_retyping_with_a_word_fixed_is_a_correction():
    assert vocab.retype_fixes("set the category to Belatro", "set the category to Balatro") == [("belatro", "Balatro")]
    assert vocab.retype_fixes("raid zen tria after the stream", "raid Zentreya after the stream") == [("zen tria", "Zentreya")]


@pytest.mark.parametrize("said,typed", [
    ("open spotify", "open discord"),                      # changed his mind (not a near-spelling)
    ("set the category to belatro", "what's the weather"),  # something else entirely
    ("mute the mic", "mute the mic"),                       # the same
    ("mute the mic", "please mute the mic now"),           # words added, nothing fixed
])
def test_other_typing_is_not_a_correction(said, typed):
    assert vocab.retype_fixes(said, typed) == []


def test_corrections_save_to_settings_live_and_remove(cfg):
    cfg["voice"]["corrections"] = {"vrb": "BRB"}  # from config.yaml: kept, not removable here
    store = vocab.Corrections(cfg)
    assert store.add("Belatro", "Balatro") == {"ok": True, "heard": "belatro", "meant": "Balatro"}
    assert cfg["voice"]["corrections"] == {"vrb": "BRB", "belatro": "Balatro"}  # the listener reads this dict
    layer = yaml.safe_load((cfg.data_dir / "settings.yaml").read_text(encoding="utf-8"))
    assert layer["voice"]["corrections"] == {"belatro": "Balatro"}  # only the learned one is written
    assert {(i["heard"], i["learned"]) for i in store.items()} == {("vrb", False), ("belatro", True)}
    assert store.remove("vrb")["ok"] is False
    assert store.remove("belatro")["ok"] is True and cfg["voice"]["corrections"] == {"vrb": "BRB"}
    assert store.add("no", "yes")["ok"] is False and "no" not in cfg["voice"]["corrections"]


def test_the_listener_applies_learned_fixes_longest_first():
    from assistant.voice.listener import VoiceListener

    lst = VoiceListener.__new__(VoiceListener)
    lst.cfg = {"corrections": {"zen": "Zen", "zen tria": "Zentreya"}}
    assert lst.correct("raid zen tria now") == "raid Zentreya now"


# ---- the skill: proposed, saved only on a yes or a click ---------------------------------------------------------

def hearing(svc):
    return svc.skills.get("hearing")


def test_saying_you_misheard_asks_first_and_learns_on_yes(svc):
    a = Assistant(svc)
    reply = a.handle("You misheard Balatro as Belatro")["reply"]
    assert reply == "Always write “Balatro” when I hear “belatro”? Say yes or no."
    assert "belatro" not in (svc.cfg["voice"].get("corrections") or {})  # nothing saved yet
    assert a.pending_view() is not None
    out = a.handle("yes")["reply"]
    assert svc.cfg["voice"]["corrections"]["belatro"] == "Balatro"  # J's capitals, from what he typed
    assert out == "Got it. When I hear belatro, I'll write Balatro."


def test_a_refused_correction_needs_no_yes(svc):
    a = Assistant(svc)
    reply = a.handle("you misheard yes as no")["reply"]
    assert a.pending_view() is None and "steers me" in reply
    assert not (svc.cfg["voice"].get("corrections") or {})


def test_its_y_not_x_only_counts_for_a_word_just_misheard(svc):
    sk = hearing(svc)
    ctx = RouterContext(skill_route=svc.skills.route)
    assert route("it's Tuesday, not Monday", ctx) is None or route("it's Tuesday, not Monday", ctx).tool != "learn_word"
    svc.bus.publish("user_said", {"text": "add Niam to the meeting", "source": "voice"})
    intent = route("it's Niamh, not Niam", ctx)
    assert intent.tool == "learn_word" and intent.args["heard"] == "niam"
    sk._heard = ("add Niam to the meeting", time.time() - hmod.RECENT_S - 1)
    assert sk.route("it's niamh not niam") is None  # too long ago


def test_retyping_in_the_hud_offers_a_fix_and_a_click_saves_it(svc):
    sk = hearing(svc)
    shown = []
    svc.bus.on(lambda ev: shown.append(ev["data"]) if ev["type"] == "hearing_suggestion" else None)
    svc.bus.publish("user_said", {"text": "set the category to Belatro", "source": "voice"})
    svc.bus.publish("user_said", {"text": "set the category to Balatro", "source": "text"})
    assert shown and shown[0]["items"][0]["heard"] == "belatro"
    assert not (svc.cfg["voice"].get("corrections") or {})  # offered, not saved
    assert sk.status()["suggestions"][0]["meant"] == "Balatro"
    sk.actions()["add"]({"heard": "belatro", "meant": "Balatro"})  # the click
    assert svc.cfg["voice"]["corrections"] == {"belatro": "Balatro"} and sk.status()["suggestions"] == []
    svc.bus.publish("user_said", {"text": "set the category to Balatro please", "source": "text"})
    assert len(shown) == 1  # one retype per sentence said


def test_retyping_long_after_is_just_typing(svc):
    sk = hearing(svc)
    sk._heard = ("set the category to Belatro", time.time() - hmod.RETYPE_S - 5)
    svc.bus.publish("user_said", {"text": "set the category to Balatro", "source": "text"})
    assert sk.status()["suggestions"] == []


def test_hud_lists_and_removes_learned_words(svc):
    sk = hearing(svc)
    sk.actions()["add"]({"heard": "heldivers", "meant": "Helldivers"})
    assert sk.status()["words"] == [{"heard": "heldivers", "meant": "Helldivers", "learned": True}]
    assert sk.actions()["remove"]({"heard": "heldivers"})["ok"] and sk.status()["words"] == []


# ---- names to expect -----------------------------------------------------------------------------------------------

def test_the_game_and_the_category_are_names_to_expect(svc):
    # Chatters' and email senders' names are not (anyone picks those): test_voice_sweep2.py
    svc.bus.publish("twitch", {"game": "Balatro"}, sticky=True)
    svc.activity = SimpleNamespace(current={"title": "Hollow Knight Silksong", "category": "other"})
    assert vocab.playing(svc, svc.bus, in_game=True) == ["Hollow Knight Silksong", "Balatro"]
    assert vocab.playing(svc, svc.bus, in_game=False) == ["Balatro"]
