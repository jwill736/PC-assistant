"""The CORE sweep's findings (audit2/CORE.md), each kept fixed: a yes names what it answers, "forget this
conversation" forgets, a stalled Claude request holds nothing up, and the rest."""

import json
import sqlite3
import sys
import threading
import time
import types
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from conftest import FakeClient, text_block

from assistant.brain import threads as threads_mod
from assistant.brain.assistant import Assistant
from assistant.brain.router import Intent
from assistant.brain.tools import Tool, obj
from assistant.memory import EXTRACT_SCHEMA
from assistant.skills import Skill, Skills
from assistant.storage import Storage


def answers(*texts):
    return [([text_block(t)], "end_turn") for t in texts]


class Think:
    def __init__(self, notes=None, facts=()):
        self.notes, self.facts, self.prompts = notes, list(facts), []

    def __call__(self, prompt, schema=None, **kw):
        self.prompts.append((prompt, schema, kw))
        if schema is EXTRACT_SCHEMA:
            return {"facts": self.facts}
        if schema is threads_mod.THREAD_SCHEMA:
            return self.notes
        return None


NOTES = {"title": "Wedding plans", "summary": "Talk about a wedding.", "key_points": [], "stances": [],
         "open_questions": [], "promises": [], "follow_up": ""}


# ---- CORE-1: a yes names what it answers --------------------------------------------------------------------------

class Two(Skill):
    name, title = "two", "Two"
    did: list = []

    def tools(self):
        return [Tool("launch_rocket", "x", obj(), lambda: self.did.append("rocket") or {"ok": True}, tier=2,
                     confirm_text=lambda a: "launch the rocket"),
                Tool("wipe_drive", "x", obj(), lambda: self.did.append("WIPE") or {"ok": True}, tier=3,
                     confirm_text=lambda a: "wipe the D drive")]

    def route(self, t):
        return {"launch the rocket": Intent("tool", "launch_rocket", {}),
                "wipe the d drive": Intent("tool", "wipe_drive", {})}.get(t)


@pytest.fixture
def two(svc, monkeypatch):
    mod = types.ModuleType("assistant.skills.twofake")
    Two.__module__ = mod.__name__
    mod.Two = Two
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    Two.did = []
    svc.skills = Skills(svc, ["twofake", "nudges"])
    return Two


@pytest.fixture
def phone(svc, two, monkeypatch):
    import test_skill_phone as tsp

    fake = tsp.FakeTelegram()
    monkeypatch.setenv(tsp.ph.TOKEN_ENV, tsp.TOKEN)
    monkeypatch.setenv(tsp.ph.NTFY_ENV, "")
    a = Assistant(svc)
    svc.bus._listeners = [f for f in svc.bus._listeners if not isinstance(getattr(f, "__self__", None), tsp.ph.Phone)]
    p = tsp.ph.Phone(svc, http=httpx.Client(transport=httpx.MockTransport(fake)), idle=lambda: 0)
    p.spawn = lambda fn, *x: fn(*x)
    p.run_command = lambda text, pending_id=None: a.handle(text, "phone", **({"pending_id": pending_id} if pending_id else {}))
    p.pending = a.pending_view
    fake.p = p
    tsp.pair(fake)
    return SimpleNamespace(fake=fake, p=p, a=a)


def buttons(msg):
    return [b["callback_data"] for b in msg["reply_markup"]["inline_keyboard"][0]]


def test_an_old_phone_yes_button_never_confirms_the_question_that_replaced_it(phone, two):
    fake, p, a = phone.fake, phone.p, phone.a
    fake.msg("launch the rocket")
    p.poll_once(timeout=0)
    card1 = fake.sent[-1]
    rocket = a.pending_view()
    assert buttons(card1) == [f"yes:{rocket['id']}", f"no:{rocket['id']}"]  # the buttons name their question
    a.handle("wipe the D drive", "text")  # later, at the PC: it replaces the rocket question
    fake.button(buttons(card1)[0])  # J taps Yes on the old rocket card
    p.poll_once(timeout=0)
    assert two.did == []  # neither the wipe nor the rocket ran
    assert fake.texts()[-1] == "That question has changed: Wipe the D drive? Reply yes or no."
    wipe = a.pending_view()
    assert buttons(fake.sent[-1]) == [f"yes:{wipe['id']}", f"no:{wipe['id']}"]
    fake.button(buttons(card1)[1])  # nor does the old card's No cancel the newer question
    p.poll_once(timeout=0)
    assert a.pending_view()["id"] == wipe["id"]
    fake.button("yes")  # a card from before the buttons carried an id: out of date too
    p.poll_once(timeout=0)
    assert two.did == []
    fake.button(f"yes:{wipe['id']}")  # the new card's own Yes is for the wipe
    p.poll_once(timeout=0)
    assert two.did == ["WIPE"]


def test_a_typed_yes_on_the_phone_answers_the_question_the_phone_was_shown(phone, two):
    fake, p, a = phone.fake, phone.p, phone.a
    fake.msg("launch the rocket")
    p.poll_once(timeout=0)
    a.handle("wipe the D drive", "text")  # at the PC: replaces it
    fake.msg("yes")  # meant for the rocket
    p.poll_once(timeout=0)
    assert two.did == [] and fake.texts()[-1] == "That question has changed: Wipe the D drive? Reply yes or no."
    fake.msg("yes")  # now the phone has seen the wipe question: this yes is for it
    p.poll_once(timeout=0)
    assert two.did == ["WIPE"]


def test_a_bare_yes_at_the_pc_answers_the_question_that_surface_heard(svc, two):
    a = Assistant(svc)
    a.handle("launch the rocket", "voice")  # asked aloud
    a.handle("wipe the D drive", "phone")  # from the phone, before J answers
    out = a.handle("yes", "voice")  # J's yes was for the rocket
    assert two.did == [] and out["reply"] == "That question has changed: Wipe the D drive? Say yes or no."
    a.handle("yes", "voice", owner="match")  # the question was said to him: a yes now is for it
    assert two.did == ["WIPE"]
    a.handle("launch the rocket", "text")
    assert a.handle("yes", "text")["reply"] == "Done." and two.did == ["WIPE", "rocket"]  # no change: no extra step


def test_a_voice_window_yes_carries_its_question_even_when_another_replaces_it_first(cfg, svc, two, monkeypatch):
    from assistant.runtime import Runtime

    rt = Runtime(cfg, services=svc)
    try:
        a, w = rt.assistant, rt.confirm_window
        w.threads = False  # the test moves the window along itself
        queued = []
        for ex in ("_commands", "_answers"):
            if hasattr(rt, ex):
                monkeypatch.setattr(getattr(rt, ex), "submit", lambda fn, *x: queued.append((fn, x)))
        a.handle("launch the rocket", "voice")
        w.current.update(phase="listening", until=time.time() + 60)  # the question was said; the window is open
        rt.voice_command("yes", "match")
        assert len(queued) == 1
        a.handle("wipe the D drive", "text")  # a typed command replaces it before the yes has run
        fn, x = queued.pop()
        fn(*x)
        assert two.did == []
        assert a.pending_view()["text"] == "wipe the D drive"
    finally:
        rt.stop()


def test_a_hud_or_toast_yes_must_name_its_card(cfg, svc, two):
    from assistant.runtime import Runtime

    rt = Runtime(cfg, services=svc)
    try:
        rt.assistant.handle("launch the rocket", "text")
        assert rt.confirm_pending(None, "hud").startswith("That question has changed")
        assert two.did == []
        assert rt.confirm_pending(rt.assistant.pending_view()["id"], "hud") == "Done." and two.did == ["rocket"]
    finally:
        rt.stop()


# ---- CORE-2: "forget this conversation" forgets --------------------------------------------------------------------

def test_a_forgotten_conversation_is_gone_from_recall_the_hud_and_the_next_talk(svc):
    svc.cfg["conversation"] = {**(svc.cfg.get("conversation") or {}), "save_wait": True}
    a = Assistant(svc, client=FakeClient(answers("Okay.", "I hear you.", "Sure.", "Hi again.", "Mm.")))
    svc.think = Think(NOTES)
    forgotten = []
    svc.bus.on(lambda e: forgotten.append(e["data"]) if e["type"] == "log_forgotten" else None)
    a.handle("let's talk")
    a.handle("I've been seeing a therapist about my panic attacks since March")
    a.handle("forget this conversation")
    a.handle("that's it")
    st = svc.storage
    assert not st.query("SELECT text FROM log WHERE text LIKE '%therapist%'")
    assert not st.query("SELECT kind, text FROM memory WHERE memory MATCH 'therapist'")  # the search index too
    assert not st.search_memory("what did I say about my therapist", before=time.time() + 60)
    assert not any("therapist" in e["text"] for e in a.convo.status()["transcript"])  # the HUD's panel
    assert forgotten  # and the HUD's log drops those minutes
    a.handle("let's talk")  # a few minutes later, within the history window
    a.handle("what were we just talking about")
    sent = json.dumps(a.client.messages.calls[-1]["messages"])
    assert "therapist" not in sent  # never sent to Claude again


def test_forgetting_while_talking_to_the_local_model_drops_its_history_too(svc):
    sent = []

    class LLM:
        model = label = "llama3.2:3b"
        kind, can_act = "ollama", False

        def chat(self, messages, **kw):
            sent.append(json.dumps(messages))
            return {"content": "Sure.", "tool_calls": []}

    a = Assistant(svc)
    a.local = SimpleNamespace(ready=True, streaming=False, refresh=lambda force=False: LLM(), in_game=lambda: LLM())
    svc.cfg["brain"] = {**(svc.cfg.get("brain") or {}), "provider": "local"}
    a.handle("I've been seeing a therapist about my panic attacks")
    a.convo.summary = "J talked about his therapist."
    a.convo.forget_current()
    a.handle("what should I cook tonight")
    assert "therapist" in sent[0] and "therapist" not in sent[-1] and a.convo.summary == ""


def test_an_undone_task_leaves_recall(svc):
    a = Assistant(svc)
    svc.skills["undo"].start(SimpleNamespace(assistant=a))
    a.handle("add a task to call the divorce lawyer")
    a.handle("undo that")
    assert not svc.storage.list_tasks()
    rows = svc.storage.query("SELECT kind, text FROM memory WHERE memory MATCH 'lawyer'")
    assert rows and not [r for r in rows if r["kind"] == "task"]  # (what was said stays in the conversation log)


def test_a_renamed_task_is_found_by_its_new_title(svc):
    t = svc.storage.add_task("call the plumber")
    svc.storage.update_task(t["id"], title="call the electrician")
    hits = svc.storage.search_memory("electrician", before=time.time() + 60)
    assert [h["text"] for h in hits] == ["call the electrician"]
    assert not svc.storage.search_memory("plumber", before=time.time() + 60)


def test_words_forgotten_before_the_fix_are_cleaned_out_of_the_index_once(tmp_path):
    db = tmp_path / "vesper.db"
    old = Storage(db)
    old.log("user", "the biopsy results are back")
    old.log("user", "keep this one")
    old.add_note("a note to keep")
    for name in ("memory_log_gone", "memory_note_gone", "memory_task_gone", "memory_task_title"):
        old.execute(f"DROP TRIGGER IF EXISTS {name}")  # as an install from before the delete triggers
    old.execute("DELETE FROM log WHERE text LIKE '%biopsy%'")  # an old "forget this conversation"
    assert old.query("SELECT 1 FROM memory WHERE memory MATCH 'biopsy'")  # the orphan the old version left
    old._db.close()
    new = Storage(db)
    assert not new.query("SELECT 1 FROM memory WHERE memory MATCH 'biopsy'")
    assert new.query("SELECT 1 FROM memory WHERE memory MATCH 'keep'") and len(new.query("SELECT * FROM memory")) == 2
    new._db.close()
    con = sqlite3.connect(db)
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name='memory_log_gone'").fetchone()
    con.close()


# ---- CORE-3: a HUD macro click works its pins out fresh; a yes runs its own question's ---------------------------

@pytest.fixture
def deep_work(svc, tmp_path):
    from fake_workspace import FakeWorkspace, accounts_for
    from test_google import HOME, WORK

    fake = FakeWorkspace()
    svc.google = accounts_for(tmp_path, fake)
    tz = svc.tz
    now = datetime(2026, 10, 21, 10, 7, tzinfo=tz)

    def at(h, m=0):
        return datetime(2026, 10, 21, h, m, tzinfo=tz).isoformat()
    events = [{"title": "Client call", "start": at(11), "end": at(12), "all_day": False},
              {"title": "Gym", "start": at(13), "end": at(17), "all_day": False}]
    svc.calendars.agenda = lambda days=1, profile=None, start=None: list(events)
    svc.calendars.refresh = lambda force=False: None
    svc.cfg["google"] = {**(svc.cfg.get("google") or {}), "calendars": [
        {"account": WORK, "id": WORK, "name": "Work", "profile": "work"},
        {"account": HOME, "id": HOME, "name": "J", "profile": "personal"}]}
    clock = {"t": now.timestamp()}
    svc.skills["focusblocks"].clock = lambda: clock["t"]
    svc.cfg["macros"] = {"deep work": {"say": ["deep work"], "steps": [{"protect_time": {"minutes": 30, "title": "Deep work"}}]}}
    return SimpleNamespace(fake=fake, clock=clock, now=now)


def test_a_hud_macro_click_books_the_slot_free_now_not_when_the_hud_loaded(svc, deep_work):
    a = Assistant(svc)
    a.macro_risks("deep work")  # 10:07: the HUD loads (the question's wording is worked out on a copy)
    deep_work.clock["t"] = (deep_work.now + timedelta(minutes=33)).timestamp()  # J clicks at 10:40
    a.run_macro("deep work", confirmed=True)
    [ev] = deep_work.fake.events
    assert ev["body"]["start"]["dateTime"].startswith("2026-10-21T12:00")
    assert a.macro_needs_yes("deep work")  # what /api/state asks for the HUD's button: nothing is pinned


def test_a_macro_yes_runs_the_slot_its_question_named(svc, deep_work):
    a = Assistant(svc)
    a.handle("deep work")  # 10:07: "deep work will put Deep work on your calendar 10:15 to 10:45…"
    deep_work.clock["t"] = (deep_work.now + timedelta(minutes=33)).timestamp()
    a.macro_risks("deep work")  # the HUD refreshes in between (what Runtime.voice_status asked before the fix)
    a.handle("yes")
    [ev] = deep_work.fake.events
    assert ev["body"]["start"]["dateTime"].startswith("2026-10-21T10:15")  # what J said yes to


# ---- CORE-4: "undo that" is J's own last change, never a background auto-marker -------------------------------

def test_undo_that_after_an_auto_marker_undoes_js_scene_switch(svc, monkeypatch):
    from test_undo import FakeOBS

    fake = FakeOBS()
    svc.obs.enabled, svc.obs.scenes, svc.obs._send = True, ["Gaming", "BRB"], fake
    a = Assistant(svc)
    svc.skills["undo"].start(SimpleNamespace(assistant=a))
    monkeypatch.setattr(svc.twitch, "create_marker", lambda description="": {"ok": True, "description": description})
    a.run_tool("obs_switch_scene", {"scene": "BRB"})  # J: "switch to BRB"
    a.tools.run("twitch_marker", {"description": "Raid from bigstreamer (120)"}, context={"source": "highlight"})
    out = a.handle("undo that")
    assert fake.current == "Gaming", out["reply"]
    assert any(i["tool"] == "twitch_marker" for i in a.tools.history.view())  # still listed on the HUD


# ---- CORE-6: calendar titles and the briefing aren't read out on air --------------------------------------------

def test_calendar_titles_are_not_spoken_on_air(svc):
    svc.live = lambda: True
    now = datetime.now(svc.tz)
    start = (now + timedelta(minutes=50)).replace(second=0, microsecond=0)
    ev = {"title": "Final interview - Acme (don't tell boss)", "start": start.isoformat(),
          "end": (start + timedelta(hours=1)).isoformat(), "all_day": False, "profile": "work", "calendar": "Work"}
    svc.calendars.agenda = lambda days=1, profile=None, start=None: [ev]
    svc.calendars.status = lambda: []
    a = Assistant(svc)
    out = a.handle("when's my next meeting", "voice")
    assert "Acme" not in out["reply"] and "HUD" in out["reply"] and "in " in out["reply"]
    result = a.tools.run("calendar", {"days": 1})
    assert result["live"] and "Acme" not in json.dumps(result)  # nor does the model get to read it out
    b = a.handle("good morning", "voice")
    assert "Acme" not in b["reply"] and "just the numbers" in b["reply"]  # (the count is 0 just before midnight)
    svc.live = lambda: False
    assert "Acme" in a.handle("when's my next meeting", "voice")["reply"]  # off air: as before


# ---- CORE-7: a journal entry's words stay out of the action log ---------------------------------------------------

def test_a_journal_entrys_words_stay_out_of_the_action_log(svc, cfg):
    a = Assistant(svc)
    svc.think = lambda *x, **k: None
    a.handle("journal", "voice")
    entry = "I had a panic attack before the client call and hid in the bathroom"
    a.handle(entry, "voice")
    hit = [e for e in a.control_status()["recent"] if e.get("tool") == "journal_add"]
    assert hit and "panic" not in json.dumps(hit)
    files = list((cfg.data_dir / "logs").glob("actions-*.jsonl"))
    assert files and "panic attack" not in files[0].read_text(encoding="utf-8")


# ---- CORE-8: a conversation thread never learns another person's voice as J's ----------------------------------

def test_thread_learning_skips_a_voice_the_speaker_check_said_wasnt_js(svc):
    svc.cfg["conversation"] = {**(svc.cfg.get("conversation") or {}), "save_wait": True}
    a = Assistant(svc, client=FakeClient(answers("Sure.", "Oh nice.", "Congrats.", "Mm.")))
    think = Think(NOTES, facts=[{"statement": "J's sister is getting married in Leeds next June", "kind": "event",
                                 "subject": "J", "subject_kind": "self", "context": "", "importance": 4}])
    svc.think = think
    a.handle("let's talk", "voice", owner="match")
    a.handle("so we were talking about weddings earlier", "voice", owner="match")
    a.handle("my sister is getting married in Leeds next June", "voice", owner="mismatch")  # a friend in the room
    a.handle("weddings cost a fortune these days", "voice", owner="match")
    a.handle("that's it", "voice", owner="match")
    extract = [p for p, s, _ in think.prompts if s is EXTRACT_SCHEMA]
    assert not any("Leeds" in p for p in extract)
    notes = [p for p, s, _ in think.prompts if s is threads_mod.THREAD_SCHEMA]
    assert notes and "Someone else (not J): my sister is getting married in Leeds" in notes[0]


# ---- CORE-10: the morning briefing obeys the game gate ------------------------------------------------------------

class LLM:
    def __init__(self, model):
        self.model, self.kind, self.can_act, self.calls, self.label = model, "ollama", True, 0, model

    def chat(self, messages, **kw):
        self.calls += 1
        return {"content": '{"spoken": "Morning.", "headline": "x"}', "tool_calls": []}


def test_the_morning_briefing_never_loads_the_big_model_mid_game(svc):
    big = LLM("qwen3.6:35b-a3b")

    class Brain:
        ready, streaming = True, False

        def refresh(self, force=False):
            return big

        def in_game(self):
            return None  # only the big one is left

    a = Assistant(svc)
    a.local = Brain()
    svc.cfg["brain"] = {**(svc.cfg.get("brain") or {}), "provider": "local"}
    a.low_power = lambda: "game"
    b = a.briefing("morning")
    assert big.calls == 0 and b["generated_by"] == "local" and b.get("model") is None  # the built-in summary


def test_the_morning_prebuild_waits_for_the_game_to_end(cfg, svc, monkeypatch):
    from assistant import runtime as rt_mod

    class Nine(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 7, 9, 0, tzinfo=tz)

    rt = rt_mod.Runtime(cfg, services=svc)
    try:
        built = threading.Event()
        monkeypatch.setattr(rt.assistant, "briefing", lambda kind="morning": built.set())
        monkeypatch.setattr(rt.assistant, "brain", lambda: "local")
        monkeypatch.setattr(rt_mod, "datetime", Nine)
        svc.activity.current = {"start": 1.0, "end": 1.0, "app": "game.exe", "title": "", "category": "gaming"}
        game = {"on": True}
        monkeypatch.setattr(rt, "_low_power", lambda: "game" if game["on"] else "")
        rt._morning_check()  # first activity after 5 am is a game
        assert rt._briefed_on is None and not built.is_set()
        game["on"] = False
        rt._morning_check()  # the game is over
        assert built.wait(5) and rt._briefed_on == "2026-11-07"
    finally:
        svc.activity.current = None
        rt.stop()


# ---- CORE-11: a stalled Claude request holds nothing up ------------------------------------------------------------

def test_the_claude_client_gives_up_on_a_stalled_connection_in_a_minute(svc, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    svc.cfg["claude"] = {**svc.cfg["claude"], "enabled": True, "api_key_env": "ANTHROPIC_API_KEY"}
    a = Assistant(svc)
    assert a.client.max_retries == 1
    assert a.client.timeout.read == 60 and a.client.timeout.connect == 5
    assert a._client_for(600).timeout.read == 600  # the briefing and research may take longer to write


class Stall:
    """A Claude whose connection stalls until released: create() blocks."""

    def __init__(self):
        self.release, self.entered = threading.Event(), threading.Event()
        self.messages = self
        self.beta = SimpleNamespace(messages=self)

    def create(self, **kw):
        self.entered.set()
        self.release.wait(10)
        return SimpleNamespace(content=[text_block("Late.")], stop_reason="end_turn")


def test_a_yes_and_the_kill_switch_never_wait_on_a_stalled_request(cfg, svc, two):
    from assistant.runtime import Runtime

    stall = Stall()
    rt = Runtime(cfg, services=svc, assistant=Assistant(svc, client=stall))
    a = rt.assistant
    try:
        a.handle("launch the rocket", "text")
        pid = a.pending_view()["id"]
        done = {}
        t = threading.Thread(target=lambda: done.setdefault("q", a.handle("plan my evening", "text")), daemon=True)
        t.start()
        assert stall.entered.wait(5)
        box = {}
        y = threading.Thread(target=lambda: box.setdefault("r", rt.confirm_pending(pid, "hud")), daemon=True)
        y.start()
        y.join(5)
        assert box.get("r") == "Done." and two.did == ["rocket"]  # the HUD's Confirm ran beside the request
        rt.kill_switch("hotkey")
        t.join(5)
        assert done.get("q", {}).get("reply") == "Stopped. PC control is paused."  # let go of at once
        assert any("Done." == m["content"] for m in a._late_notes)  # Claude hears about the yes next time
    finally:
        stall.release.set()
        rt.stop()


# ---- CORE-12: the diagnostics report leaves out what the mic overheard -------------------------------------------

def test_the_diagnostics_report_leaves_out_overheard_speech_and_journal_entries(cfg):
    from collections import deque

    from assistant import diagnostics

    heard = deque(maxlen=40)
    heard.append({"ts": time.time(), "text": "babe the clinic called, the biopsy results are back", "wake": False,
                  "armed": False})  # someone else in the room
    heard.append({"ts": time.time(), "text": "journal I feel like quitting my job", "wake": True, "armed": False,
                  "private": True})
    heard.append({"ts": time.time(), "text": "vesper what's the weather", "wake": True, "armed": False})
    lst = SimpleNamespace(recent_heard=heard, wake_words=["vesper"], status=lambda: {})
    rt = SimpleNamespace(listener=lst, speaker=None, verifier=None, timings={}, supervisor=None, started=None)
    rep = diagnostics.run(cfg, None, rt, timeout=2, write=False)
    md = diagnostics.to_markdown(rep)
    blob = md + json.dumps(rep, default=str)
    assert "biopsy" not in blob and "quitting" not in blob
    assert "(9 words)" in md and "(7 words)" in md and "what's the weather" in md


def test_a_private_voice_command_is_marked_for_the_report(cfg, svc, monkeypatch):
    from collections import deque

    from assistant.runtime import Runtime

    rt = Runtime(cfg, services=svc)
    try:
        heard = deque([{"ts": 1.0, "text": "journal I hid in the bathroom", "wake": True, "armed": False}])
        rt.listener = SimpleNamespace(recent_heard=heard, last_owner="match")
        rt._commands.submit = lambda fn, *x: fn(*x)
        monkeypatch.setattr(rt.assistant, "handle", lambda *x, **k: {"reply": "Saved.", "private": True})
        rt.voice_command("journal I hid in the bathroom", "match")
        assert heard[0]["private"] is True
    finally:
        rt.listener = None
        rt.stop()


# ---- CORE-13: two settings saved at once both stick ---------------------------------------------------------------

def test_two_settings_saved_at_once_both_stick(cfg, monkeypatch):
    from assistant import config as config_mod

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    real = config_mod._read_yaml
    other = {}

    def read_then_let_another_save_in(path):
        out = real(path)
        if not other:  # the first save has read the file; a second save starts now, on another thread
            other["t"] = threading.Thread(target=config_mod.save_setting, args=(cfg, "voice.speaker_check", "log"))
            other["t"].start()
            other["t"].join(0.3)  # (with the lock it waits for this save to finish)
        return out
    monkeypatch.setattr(config_mod, "_read_yaml", read_then_let_another_save_in)
    config_mod.save_setting(cfg, "skills.weather.enabled", False)
    other["t"].join(5)
    on_disk = real(cfg.data_dir / config_mod.SETTINGS_FILE)
    assert on_disk["skills"]["weather"]["enabled"] is False and on_disk["voice"]["speaker_check"] == "log"
    assert not list(cfg.data_dir.glob("*.tmp"))


# ---- CORE-14: undo with ids oldest first still puts back the first value -----------------------------------------

def test_undo_with_ids_oldest_first_puts_back_the_value_before_both(svc, monkeypatch):
    from test_undo import Endpoint

    from assistant.integrations import controls

    e = Endpoint(level=0.70, muted=0)
    monkeypatch.setattr(controls, "IS_WINDOWS", True)
    monkeypatch.setattr(controls, "_endpoint", lambda: e)
    a = Assistant(svc)
    svc.skills["undo"].start(SimpleNamespace(assistant=a))
    a.run_tool("set_volume", {"level": 30})
    a.run_tool("set_volume", {"level": 50})
    ids = sorted(i["id"] for i in a.tools.history.view())  # oldest first
    out = a.tools.run("undo", {"ids": ids})
    assert round(e.level * 100) == 70, out["message"]
    assert "so I left it" not in out["message"]
