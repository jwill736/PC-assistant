import os
from pathlib import Path

from assistant.config import deep_merge, load_config, load_env_file


def test_deep_merge_keeps_defaults_and_overrides():
    base = {"a": {"b": 1, "c": 2}, "d": [1]}
    out = deep_merge(base, {"a": {"c": 3}, "d": [2]})
    assert out == {"a": {"b": 1, "c": 3}, "d": [2]}
    assert base["a"]["c"] == 2  # not mutated


def test_wake_words_always_include_name(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("assistant:\n  name: Friday\n  wake_words: [hey friday]\n")
    cfg = load_config(p)
    assert cfg["assistant"]["wake_words"][0] == "friday"
    assert "hey friday" in cfg["assistant"]["wake_words"]
    assert cfg["obs"]["port"] == 4455  # default survives


def test_env_file_does_not_override_real_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nFOO_TEST_KEY='from-file'\nexport BAR_TEST_KEY=bar\n")
    monkeypatch.setenv("FOO_TEST_KEY", "from-env")
    monkeypatch.delenv("BAR_TEST_KEY", raising=False)
    load_env_file(env)
    assert os.environ["FOO_TEST_KEY"] == "from-env"
    assert os.environ["BAR_TEST_KEY"] == "bar"


def test_the_example_config_loads(tmp_path):
    """setup.bat copies config.example.yaml to config.yaml: it must parse and load for every new install."""
    import shutil

    src = Path(__file__).resolve().parent.parent / "config.example.yaml"
    shutil.copy(src, tmp_path / "config.yaml")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg["assistant"]["name"] == "Vesper"
    assert cfg["obs"]["scene_aliases"]["game"] == "Gameplay" and cfg["obs"]["prestream_minutes"] == 15
    assert cfg["pc_control"]["kill_hotkey"] == "ctrl+alt+k" and cfg["voice"]["tts"]["engine"] == "auto"
