"""Selected-text actions: read the selection with the clipboard put back, ask the model (the text is untrusted),
then replace the selection (a long result waits for a yes) or show it on the HUD. Keyboard, clipboard, windows faked."""

from types import SimpleNamespace

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import selection as sel


class Board:
    def __init__(self):
        self.text, self.n = "J's clipboard", 0

    def sequence(self):
        return self.n

    def read(self):
        return self.text

    def write(self, text):
        self.text, self.n = text, self.n + 1
        return True


class Keys:
    """The app in front: Ctrl+C copies its selection, Ctrl+V pastes over it."""

    def __init__(self, board, selection="teh meeting is moved to thursday"):
        self.board, self.selection, self.pasted = board, selection, []

    def chord(self, *names):
        if names == ("ctrl", "c") and self.selection:
            self.board.write(self.selection)
        if names == ("ctrl", "v"):
            self.pasted.append(self.board.read())
            self.selection = self.board.read()
        return True

    def modifiers_down(self):
        return False


@pytest.fixture
def env(svc):
    board = Board()
    keys = Keys(board)
    win = SimpleNamespace(app="outlook", title="Re: Thursday - Outlook", pid=4, handle=40)
    front = {"win": win}
    focused = []

    def focus(w):
        focused.append(w)
        front["win"] = w
        return True
    asked = []

    def think(prompt, **kw):
        asked.append(prompt)
        return think.answer
    think.answer = "The meeting is moved to Thursday."
    svc.think = think
    s = sel.Selection(svc, keys=keys, board=board, window=lambda: front["win"], password=lambda: False,
                      focus=focus, sleep=lambda s: None)
    return SimpleNamespace(s=s, board=board, keys=keys, front=front, win=win, think=think, asked=asked, focused=focused)


@pytest.mark.parametrize("said,action,lang", [
    ("rewrite this", "rewrite", ""), ("make this shorter", "shorter", ""), ("shorten that", "shorter", ""),
    ("fix this", "fix", ""), ("proofread this", "fix", ""), ("translate this to spanish", "translate", "Spanish"),
    ("translate this", "translate", ""), ("make this more professional", "formal", ""),
    ("make it friendlier", "casual", ""), ("explain this", "explain", ""), ("summarise this", "summarize", ""),
    ("summarize the selected text", "summarize", ""), ("reply to this", "reply", ""), ("draft a reply", "reply", ""),
    ("what does this mean", "explain", ""),
])
def test_phrases(svc, said, action, lang):
    ctx = RouterContext(skill_route=Skills(svc, ["selection", "screen", "clipboard"]).route)
    intent = route(said, ctx)
    assert intent and intent.tool == "selection_action" and intent.args["action"] == action
    assert intent.args.get("language", "") == lang


@pytest.mark.parametrize("said", ["summarize this page", "explain this error", "rewrite my clipboard", "fix my wifi"])
def test_other_skills_keep_their_phrases(svc, said):
    ctx = RouterContext(skill_route=Skills(svc, ["selection", "screen", "clipboard"]).route)
    intent = route(said, ctx)
    assert intent is None or intent.tool != "selection_action"


def test_rewrite_replaces_the_selection_and_restores_the_clipboard(env):
    out = env.s.act("fix")
    assert out["ok"] and out["replaced"] and env.keys.pasted == ["The meeting is moved to Thursday."]
    assert env.board.text == "J's clipboard"  # put back after the copy and after the paste
    assert "teh meeting" in env.asked[0] and sel.NOTE in env.asked[0]  # untrusted, fenced
    assert env.s.say("selection_action", {}, out) == "Fixed."


def test_a_long_replacement_waits_for_a_yes_after_a_spoken_preview(env, svc):
    from assistant.brain.assistant import Assistant

    a = Assistant(svc)
    svc.think = env.think  # the assistant installs its own; keep the fake model
    env.s.runtime = SimpleNamespace(assistant=a)
    env.think.answer = "Here is a much longer version. " + "It keeps going with detail. " * 20
    svc.skills.all["selection"] = env.s
    svc.skills._tool_owner = {}
    a.tools.refresh_skill_tools()
    out = a.handle("rewrite this", "voice")
    assert env.keys.pasted == []  # not yet
    assert out["reply"].startswith("It's ") and "here's how it starts: “Here is a much longer version.”" in out["reply"]
    assert out["reply"].endswith("Replace your selection with it?") and a.pending_view()["text"].startswith("replace your selection")
    reply = a.handle("yes", "voice")["reply"]
    assert reply == "Done, it's in." and env.keys.pasted == [env.think.answer.strip()]


def test_explain_summarise_and_reply_show_on_the_hud_without_touching_the_app(env):
    env.think.answer = "It says the meeting moved to Thursday."
    out = env.s.act("explain")
    assert out["ok"] and "replaced" not in out and env.keys.pasted == []
    assert env.s.say("selection_action", {}, out) == "It says the meeting moved to Thursday."
    assert env.s.status()["result"]["text"] == "It says the meeting moved to Thursday."
    env.think.answer = "Thanks, Thursday works for me."
    out = env.s.act("reply")
    assert "drafted a reply" in env.s.say("selection_action", {}, out)
    assert env.s.copy()["ok"] and env.board.text == "Thanks, Thursday works for me."


def test_on_air_the_result_stays_on_the_hud(env, svc):
    svc.live = lambda: True
    env.think.answer = "A private summary of a private email."
    out = env.s.act("summarize")
    assert env.s.say("selection_action", {}, out) == "Summary; it's on the HUD."


def test_nothing_selected(env):
    env.keys.selection = None
    out = env.s.act("rewrite")
    assert out["ok"] is False and "Nothing's selected" in out["error"] and env.board.text == "J's clipboard"


def test_no_model(env):
    env.think.answer = None
    out = env.s.act("rewrite")
    assert out["ok"] is False and "needs a model" in out["error"] and env.keys.pasted == []


def test_password_places_and_the_hud_are_refused(env):
    env.front["win"] = SimpleNamespace(app="keepassxc", title="KeePassXC", pid=1, handle=1)
    assert "don't read" in env.s.act("rewrite")["error"]
    env.s.password = lambda: True
    env.front["win"] = env.win
    assert "password field" in env.s.act("rewrite")["error"]
    env.s.password = lambda: False
    env.front["win"] = SimpleNamespace(app="chrome", title="Friday HUD - Google Chrome", pid=2, handle=2)
    assert "another app" in env.s.act("explain")["error"]
    assert env.asked == []


def test_replace_from_the_hud_goes_back_to_the_app_first(env):
    env.think.answer = "x" * 600
    out = env.s.act("rewrite")
    assert out["preview"] and env.keys.pasted == []
    env.front["win"] = SimpleNamespace(app="chrome", title="Friday HUD - Google Chrome", pid=2, handle=2)  # clicked the HUD
    done = env.s.actions()["replace"]({"id": out["id"]})
    assert done["ok"] and env.focused == [env.win] and env.keys.pasted == ["x" * 600]
    assert env.s.replace(out["id"])["ok"]  # still there, still valid


def test_translate_uses_the_language_and_the_tool_tiers(env, svc):
    env.think.answer = "La reunión se movió al jueves."
    out = env.s.act("translate", "spanish")
    assert out["language"] == "Spanish" and "into Spanish" in env.asked[0]
    tools = {t.name: t for t in env.s.tools()}
    assert tools["selection_action"].untrusted
    assert tools["selection_action"].tier_for({"action": "explain"}) == 0
    assert tools["selection_action"].tier_for({"action": "rewrite"}) == 1
    assert env.s.act("dance")["ok"] is False


def test_copy_that_after_a_reply(env, svc):
    env.think.answer = "Sounds good!"
    env.s.act("reply")
    svc.skills.all["selection"] = env.s
    intent = env.s.route("copy that")
    assert intent and intent.tool == "selection_copy"
    assert env.s.route("put it in") is None  # a reply isn't for replacing the selection
