import numpy as np
import pytest

from assistant.voice import speaker_id
from assistant.voice.speaker_id import SpeakerVerifier, VoiceProfile, build_profile, cosine


def voice(seed, n=8, spread=0.25, dim=256):
    """n fingerprints of one synthetic 'speaker': a fixed direction plus per-take jitter."""
    rng = np.random.default_rng(seed)
    base = rng.normal(size=dim)
    out = []
    for _ in range(n):
        v = base + rng.normal(size=dim) * spread * np.linalg.norm(base) / np.sqrt(dim)
        out.append((v / np.linalg.norm(v)).tolist())
    return out


class FakeEmbedder:
    """Clips are tagged by their first sample so tests pick whose 'voice' they are."""

    name = "fake"

    def __init__(self, voices):
        self.voices = voices
        self.calls = 0

    def embed(self, samples):
        self.calls += 1
        who = int(round(float(samples[0]) * 10))
        return self.voices[who].pop(0)


def clip(who, seconds=2.0):
    x = np.zeros(int(16000 * seconds), dtype=np.float32)
    x[0] = who / 10
    return x


def test_cosine():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)


def test_profile_threshold_is_tuned_inside_the_band():
    owner = voice(1)
    profile = build_profile(owner)
    assert speaker_id.MIN_THRESHOLD <= profile.threshold <= speaker_id.MAX_THRESHOLD
    assert len(profile.owner_scores) == len(owner)
    assert profile.threshold < min(profile.owner_scores)  # every enrollment take would pass
    # a fresh take from the owner passes, a stranger does not
    assert profile.score(voice(1, n=9)[-1]) >= profile.threshold
    assert profile.score(voice(2)[0]) < profile.threshold


def test_profile_needs_three_samples():
    with pytest.raises(ValueError):
        build_profile(voice(1, n=2))


def test_profile_roundtrip_and_delete(tmp_path):
    profile = build_profile(voice(1))
    speaker_id.save_profile(profile, tmp_path)
    raw = (tmp_path / speaker_id.PROFILE_FILE).read_text()
    assert "embeddings" not in raw  # wrapped (and DPAPI-encrypted on Windows), not plain JSON fields
    loaded = speaker_id.load_profile(tmp_path)
    assert isinstance(loaded, VoiceProfile)
    assert loaded.threshold == profile.threshold and len(loaded.embeddings) == len(profile.embeddings)
    assert speaker_id.delete_profile(tmp_path) is True
    assert speaker_id.load_profile(tmp_path) is None
    assert speaker_id.delete_profile(tmp_path) is False


def test_corrupt_profile_is_ignored(tmp_path):
    (tmp_path / speaker_id.PROFILE_FILE).write_text("{not json")
    assert speaker_id.load_profile(tmp_path) is None


def enrolled(tmp_path, mode):
    owner = voice(1, n=12)
    speaker_id.save_profile(build_profile(owner[:8]), tmp_path)
    emb = FakeEmbedder({1: owner[8:], 2: voice(2)})
    return SpeakerVerifier(tmp_path, mode, embedder=emb), emb


def test_strict_mode_ignores_strangers(tmp_path):
    v, _ = enrolled(tmp_path, "strict")
    assert v.active
    ok, score = v.check(clip(1))
    assert ok and score >= v.profile.threshold
    ok, score = v.check(clip(2))
    assert not ok and score < v.profile.threshold
    assert v.status()["last_score"] == score and v.status()["enrolled"]


def test_log_mode_scores_but_never_blocks(tmp_path):
    v, _ = enrolled(tmp_path, "log")
    ok, score = v.check(clip(2))
    assert ok and score is not None


def test_off_mode_and_short_clips_skip_the_model(tmp_path):
    v, emb = enrolled(tmp_path, "off")
    assert v.check(clip(2)) == (True, None)
    v.mode = "strict"
    assert v.check(clip(2, seconds=0.5)) == (True, None)  # too short to judge: never lock the owner out
    assert emb.calls == 0


def test_no_profile_means_no_check(tmp_path):
    v = SpeakerVerifier(tmp_path, "strict", embedder=FakeEmbedder({}))
    assert not v.active
    assert v.check(clip(2)) == (True, None)
    assert v.status() == {"enrolled": False, "mode": "strict", "last_score": None, "error": ""}


def test_unknown_mode_falls_back_to_strict(tmp_path):
    assert SpeakerVerifier(tmp_path, "paranoid").mode == "strict"
