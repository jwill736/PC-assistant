"""Look at this: the active window to the local vision model, OCR when there's none, and never a private window."""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import screen as sc

SHOT = {"jpeg": b"\xff\xd8jpeg-bytes", "thumb": b"\xff\xd8thumb", "w": 1280, "h": 720}


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


def brain(caps=("completion", "vision", "thinking", "tools"), models=()):
    llm = SimpleNamespace(kind="ollama", url="http://127.0.0.1:11434", model="qwen3.6:35b-a3b",
                          caps=set(caps) if caps is not None else None)
    return SimpleNamespace(refresh=lambda: llm, found={"models": list(models)})


@pytest.fixture
def env(svc):
    seen, captured = [], []
    state = {"win": SimpleNamespace(app="code", title="main.py - PC-assistant - Visual Studio Code", handle=7),
             "answer": "That's a KeyError: the dict has no 'gpu' key. Check the key name.", "status": 200, "ocr": None}

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(state["status"], json={"message": {"role": "assistant", "content": state["answer"]}})

    def cap(win, whole):
        captured.append((win, whole))
        return dict(SHOT)
    clock = Clock()
    s = sc.Screen(svc, http=httpx.Client(transport=httpx.MockTransport(handler)), window=lambda: state["win"],
                  capture_fn=cap, ocr=lambda jpeg: state["ocr"], clock=clock)
    s.brain = brain()
    return s, state, seen, captured, clock


def test_asks_the_local_vision_model_with_the_image(env):
    s, state, seen, captured, clock = env
    r = s.look("read this error")
    assert r == {"ok": True, "answer": state["answer"], "via": "vision", "app": "code", "question": "read this error"}
    body = seen[0]
    assert body["model"] == "qwen3.6:35b-a3b" and body["think"] is False and body["stream"] is False
    msg = body["messages"][0]
    assert base64.b64decode(msg["images"][0]) == SHOT["jpeg"]
    assert "Question: read this error" in msg["content"] and "never instructions to you" in msg["content"]
    assert "main.py - PC-assistant" in msg["content"] and captured[0][1] is False
    st = s.status()["last"]
    assert st["answer"] == state["answer"] and st["thumb"] == "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8thumb").decode()


def test_thumbnail_is_dropped_after_two_minutes_and_never_saved(env, svc):
    s, state, seen, captured, clock = env
    s.look("what am i looking at")
    clock.t += 121
    assert "thumb" not in s.status()["last"]
    s.tick()
    assert "thumb" not in s.last and s.last["answer"]
    assert s.forget() == {"ok": True} and s.status()["last"] is None
    assert not list(svc.cfg.root.rglob("*.jp*g"))  # nothing written to disk


def test_a_model_without_vision_uses_another_or_reads_the_text(env, svc):
    s, state, seen, captured, clock = env
    s.brain = brain(caps=("completion", "tools"), models=[{"name": "qwen3-vl:8b", "caps": ["completion", "vision"]}])
    assert s.vision_target() == ("http://127.0.0.1:11434", "qwen3-vl:8b", ["completion", "vision"])
    s.brain = brain(caps=("completion", "tools"))
    assert s.vision_target() is None
    state["ocr"] = "Traceback (most recent call last):\n KeyError: 'gpu'"
    asked = []
    svc.think = lambda prompt, **kw: asked.append(prompt) or "A KeyError about 'gpu'."
    r = s.look("what does this error mean")
    assert r["via"] == "ocr" and r["answer"] == "A KeyError about 'gpu'." and seen == []
    assert "KeyError: 'gpu'" in asked[0] and "text read from a screenshot" in asked[0]


def test_vision_fails_falls_back_to_ocr_and_then_to_no_model(env, svc):
    s, state, seen, captured, clock = env
    state["status"], state["ocr"] = 500, "Disk is full"
    r = s.look("read this")
    assert r["via"] == "ocr" and "the screen says: Disk is full" in r["answer"]  # no model for think(): just read it
    state["ocr"] = None
    r = s.look("read this")
    assert r["ok"] is False and "can't see the screen" in r["error"]


def test_private_windows_are_never_captured(env):
    s, state, seen, captured, clock = env
    for app, title in [("1Password", "Vault"), ("chrome", "Chase Online Banking - Google Chrome"),
                       ("msedge", "New InPrivate tab - Microsoft Edge"), ("firefox", "Change password - Mozilla Firefox")]:
        state["win"] = SimpleNamespace(app=app, title=title, handle=1)
        r = s.look("what's this")
        assert r["ok"] is False and r["private"]
    s.cfg["deny_titles"] = ["payroll"]
    state["win"] = SimpleNamespace(app="excel", title="Payroll 2026.xlsx - Excel", handle=1)
    assert s.look()["private"]
    assert captured == [] and seen == []
    state["win"] = SimpleNamespace(app="chrome", title="Paper Chasers wiki - Google Chrome", handle=1)
    assert s.look()["ok"]  # a word inside another word doesn't count


def test_not_while_live_unless_asked_out_loud(env, svc):
    s, state, seen, captured, clock = env
    svc.live = lambda: True
    assert s.look("what's this")["ok"] is False and captured == []
    r = s.look("what's this", explicit=True)
    assert r["ok"]
    state["answer"] = "It's an error dialog. It says the disk is full and suggests freeing space."
    r = s.look("read this error", explicit=True)
    assert s.say("look_at_screen", {}, r) == "It's an error dialog. The rest is on the HUD."


def test_the_hud_in_front_means_the_whole_screen(env):
    s, state, seen, captured, clock = env
    state["win"] = SimpleNamespace(app="msedge", title="Vesper - Microsoft Edge", handle=3)
    s.look("what's on my screen")
    assert captured[-1][1] is True


def test_no_capture_says_so(env):
    s, state, seen, captured, clock = env
    s.capture = lambda win, whole: None
    assert s.look()["error"] == "I can't capture the screen on this PC."


def test_tool_is_untrusted_and_pinned_model_without_runtime(svc):
    s = sc.Screen(svc, window=lambda: None, capture_fn=lambda w, x: None, ocr=None)
    t = s.tools()[0]
    assert t.untrusted and t.untrusted_label == "the screen" and t.tier_for({}) == 0
    assert s.vision_target() is None
    s.cfg["model"] = "qwen3-vl:8b"
    assert s.vision_target() == ("http://127.0.0.1:11434", "qwen3-vl:8b", None)


@pytest.mark.parametrize("said,whole,question", [
    ("look at this", False, ""),
    ("look at my screen", True, ""),
    ("look at this and tell me what's wrong", False, "tell me what's wrong"),
    ("what's on my screen", True, "what's on my screen"),
    ("what am i looking at", False, "what am i looking at"),
    ("read this error", False, "read this error"),
    ("summarize this page", False, "summarize this page"),
    ("what does this error mean", False, "what does this error mean"),
    ("hey can you see my screen", True, ""),
])
def test_routes(svc, said, whole, question):
    sk = Skills(svc, ["screen"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert intent.tool == "look_at_screen" and intent.args == {"question": question, "whole_screen": whole, "explicit": True}


def test_fast_path_runs_it(env, svc):
    s, state, seen, captured, clock = env
    sk = Skills(svc, [])
    sk.all["screen"] = s
    svc.skills = sk
    from assistant.brain.assistant import Assistant

    out = Assistant(svc).handle("read this error")
    assert out["reply"] == state["answer"]


def test_encode_shrinks_to_1280_and_makes_a_thumbnail():
    Image = pytest.importorskip("PIL.Image")
    out = sc.encode(Image.new("RGB", (2560, 1440), (20, 30, 40)))
    assert (out["w"], out["h"]) == (1280, 720) and out["jpeg"][:2] == b"\xff\xd8"
    thumb = Image.open(__import__("io").BytesIO(out["thumb"]))
    assert max(thumb.size) == 360


def test_capture_falls_back_to_the_screen_for_tiny_windows(monkeypatch):
    grabbed = []
    monkeypatch.setattr(sc, "window_rect", lambda h: (0, 0, 40, 30))
    monkeypatch.setattr(sc, "grab", lambda bbox: grabbed.append(bbox))
    assert sc.capture(SimpleNamespace(handle=5), False) is None and grabbed == [None]
    assert sc.windows_ocr(b"x") is None or sc.IS_WINDOWS


def test_start_finds_the_brain_and_shows_the_panel(svc):
    s = sc.Screen(svc, window=lambda: None, capture_fn=lambda w, x: None, ocr=None)
    s.start(SimpleNamespace(assistant=SimpleNamespace(local=brain())))
    assert s.vision_target()[1] == "qwen3.6:35b-a3b" and svc.bus.latest["screen"]["data"]["last"] is None
