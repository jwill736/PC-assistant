import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant.bus import EventBus
from assistant.config import DEFAULTS, Config, deep_merge
from assistant.services import build_services
from assistant.storage import Storage


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(deep_merge(DEFAULTS, {
        "data_dir": str(tmp_path / "data"),
        "assistant": {"name": "Friday", "wake_words": ["friday", "hey friday"], "user_name": "J", "timezone": "America/New_York"},
        "voice": {"enabled": False},
        "tracking": {"enabled": False},
        "obs": {"enabled": False},
        "claude": {"enabled": False},
        "pc_control": {"toast_confirm": False},  # no real Windows notifications from the test run
        "profiles": {
            "work": {"label": "Work", "apps": ["code", "slack"], "title_keywords": ["github", "jira"],
                     "launch": {"apps": ["slack"], "urls": ["gmail"], "obs_scene": None}, "close_apps": []},
            "stream": {"label": "Stream", "apps": ["obs64"], "title_keywords": ["twitch.tv"],
                       "launch": {"apps": [], "urls": [], "obs_scene": None}, "close_apps": []},
        },
        "news": {"feeds": [{"name": "Local", "url": "http://127.0.0.1:9/none", "topic": "tech"}]},
    }))
    c.root = tmp_path
    return c


@pytest.fixture
def svc(cfg):
    return build_services(cfg, EventBus(), Storage(":memory:"))


# ---- fake Anthropic client -------------------------------------------------

def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(name, args, id_="toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs, default=lambda o: getattr(o, "__dict__", str(o)))))
        content, stop = self.script.pop(0)
        return SimpleNamespace(content=content, stop_reason=stop)


class FakeClient:
    def __init__(self, script):
        self.messages = FakeMessages(script)
        self.beta = SimpleNamespace(messages=self.messages)


@pytest.fixture
def fake_client():
    return FakeClient
