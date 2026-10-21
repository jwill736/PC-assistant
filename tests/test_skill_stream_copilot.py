"""Stream co-pilot: sessions from live transitions, chat questions and mood, regulars, recaps and VOD chapters,
go-live prep from the calendar, the category hint, routes, and short spoken lines (viewers' words stay on the HUD)."""

import json
from datetime import datetime, timedelta

import pytest

from assistant.brain.router import RouterContext, route
from assistant.bus import EventBus
from assistant.integrations.eventsub import TwitchFeed
from assistant.skills import Skills
from assistant.skills import stream_copilot as sc


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(svc):
    clock, state, nudges = Clock(), {"live": False}, []
    svc.bus = EventBus()  # the skills build_services loaded listen on the old bus
    svc.live = lambda: state["live"]
    svc.nudge = lambda text, **kw: nudges.append((text, kw))
    svc.twitch_feed = TwitchFeed(clock=clock)
    svc.highlights = lambda: []
    cop = sc.StreamCopilot(svc, clock=clock, fullscreen=lambda: "")
    return cop, clock, state, nudges


def twitch(svc, **kw):
    svc.bus.publish("twitch", {"enabled": True, "live": True, "title": "Ranked grind", "game": "VALORANT", **kw}, sticky=True)


def chat(svc, clock, user, text):
    clock.t += 1
    svc.twitch_feed.add({"kind": "chat", "user": user, "text": text})


# ---- the pure parts ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,sign", [("PogChamp", 1), ("KEKW KEKW", 1), ("WWWW", 1), ("lets gooo", 1), ("OMEGALUL", 1),
                                       ("this is boring", -1), ("L", -1), ("LLLL lag", -1), ("hello there", 0)])
def test_message_mood(text, sign):
    m = sc.message_mood(text)
    assert (m is None) if sign == 0 else (m * sign > 0)


def test_mood_label():
    assert sc.mood_label(0.6, 10) == "hyped" and sc.mood_label(-0.6, 10) == "salty"
    assert sc.mood_label(0.0, 4) == "steady" and sc.mood_label(0.9, 0) == "quiet"


@pytest.mark.parametrize("text,yes", [
    ("what PSU are you using?", True), ("how long have you been streaming", True), ("is this the new patch", True),
    ("@jstreams what mouse is that", True), ("@kai what rank are you?", False), ("???", False), ("!discord", False),
    ("nice shot", False), ("lol?", False)])
def test_is_question(text, yes):
    assert sc.is_question(text, me="jstreams") is yes


def test_answered_by_name_or_topic():
    q = {"user": "Kai_99", "text": "what power supply are you using?"}
    assert sc.answered(q, "kai 99 it's a corsair")
    assert sc.answered(q, "my power supply is a corsair one thousand")
    assert not sc.answered(q, "let's go next round")


def test_parse_plan_and_game_detection():
    assert sc.parse_plan("Stream: Elden Ring | Boss rush") == {"game": "Elden Ring", "title": "Boss rush"}
    assert sc.parse_plan("Streaming Hades II") == {"game": "Hades II", "title": ""}
    assert sc.parse_plan("Stream") == {"game": "", "title": ""}
    assert sc.detect_game("VALORANT-Win64-Shipping.exe", "VALORANT") == "VALORANT"
    assert sc.detect_game("chrome.exe", "Twitch", fullscreen=lambda: "game") is None
    assert sc.detect_game("hades2.exe", "Hades II", fullscreen=lambda: "game") == "Hades II"
    assert sc.detect_game("mygame.exe", "x", {"mygame": "My Game"}, fullscreen=lambda: "") == "My Game"
    assert sc.same_game("Counter-Strike 2", "Counter-Strike") and not sc.same_game("VALORANT", "Just Chatting")


def test_vod_chapters_format_order_and_spacing():
    start = 1000.0
    hl = [{"kind": "chat_spike", "ts": start + 750, "uptime_s": None, "detail": {"reaction": "LUL", "messages": 20}},
          {"kind": "manual", "reason": "Clutch round", "ts": 0, "uptime_s": 4210},
          {"kind": "clip", "reason": "Clip that (replay saved)", "ts": start + 780, "uptime_s": 780},  # beats the spike
          {"kind": "raid", "reason": "Raid from Tenz (40)", "ts": 0, "uptime_s": 3600}]
    ch = sc.vod_chapters(hl, start)
    assert ch == ["00:00:00 Start", "00:13:00 Clipped moment", "01:00:00 Raid from Tenz (40)", "01:10:10 Clutch round"]
    assert sc.fmt_ts(3725) == "01:02:05" and sc.vod_chapters([], start) == ["00:00:00 Start"]


# ---- a whole stream ------------------------------------------------------------------------------------------

def test_session_from_live_to_recap(env, svc):
    cop, clock, state, nudges = env
    cop.tick()
    assert cop.session is None and cop.status()["live"] is False
    state["live"] = True
    twitch(svc, viewers=40)
    cop.tick()
    assert cop.session is not None
    start = cop.session["start"]
    for ev in [{"kind": "follow", "user": "Ana"}, {"kind": "follow", "user": "Bo"}, {"kind": "sub", "user": "Cy"},
               {"kind": "gift", "user": "Di", "amount": 5}, {"kind": "cheer", "user": "Ed", "amount": 500},
               {"kind": "raid", "user": "Tenz", "amount": 40}]:
        svc.bus.publish("twitch_event", ev)
    svc.bus.publish("stream_alert", {"kind": "network", "level": "warning", "text": "Dropped frames"})
    svc.bus.publish("stream_alert", {"kind": "recovered", "level": "good", "text": "fine"})
    svc.bus.publish("highlight", {"kind": "manual", "reason": "Clutch round", "ts": start + 750, "uptime_s": 750})
    chat(svc, clock, "Kai", "what PSU are you using?")
    chat(svc, clock, "Mo", "POGGERS")
    chat(svc, clock, "Nightbot", "Follow on X!")
    clock.t += 600
    twitch(svc, viewers=84)
    cop.tick()
    now = cop.stats_now()
    assert now["live"] and now["peak_viewers"] == 84 and now["avg_viewers"] == 62 and now["follows"] == 2
    assert now["subs"] == 6 and now["bits"] == 500 and now["raids"] == 1 and now["messages"] == 2 and now["incidents"] == 1
    assert [g["user"] for g in cop.greet] == ["Mo", "Kai"] and all(g["kind"] == "new" for g in cop.greet)
    # offline: a short drop doesn't end it; staying offline does
    state["live"] = False
    clock.t += 30
    cop.tick()
    assert cop.session is not None
    clock.t += 120
    cop.tick()
    r = cop.recap
    assert cop.session is None and r["peak_viewers"] == 84 and r["follows"] == 2 and r["raids"][0]["user"] == "Tenz"
    assert r["chapters"] == ["00:00:00 Start", "00:12:30 Clutch round"]
    assert r["health"]["count"] == 1 and "dropped frames" in r["health"]["text"]
    assert len(r["posts"]["x"]) <= 280 and "00:12:30 Clutch round" in r["posts"]["discord"]
    assert nudges[-1][0] == "Stream recap is ready on the HUD." and nudges[-1][1]["priority"] == "normal"
    row = svc.storage.query("SELECT end, data FROM stream_copilot_sessions")
    assert row[0]["end"] and json.loads(row[0]["data"])["recap"]["follows"] == 2
    again = sc.StreamCopilot(svc, clock=clock)  # the last recap survives a restart
    assert again.stream_recap()["chapters"] == r["chapters"]
    assert again.briefing()["line"].startswith("Last stream: 10 minutes")


def test_posts_from_the_model_and_titles(env, svc):
    cop, clock, state, nudges = env
    svc.think = lambda prompt, schema=None, **kw: ({"x": "GG all", "discord": "Recap!"} if "x" in schema["properties"]
                                                  else {"titles": ["Boss rush until dawn"]})
    s = sc.new_session(1, clock.t - 3600, {"title": "T", "game": "Elden Ring"})
    assert cop.build_recap(s, clock.t)["posts"] == {"x": "GG all", "discord": "Recap!", "from_model": True}
    assert cop.suggest_title("Elden Ring")["titles"] == ["Boss rush until dawn"]
    svc.think = lambda prompt, schema=None, **kw: None
    out = cop.suggest_title("Elden Ring", "Boss rush")
    assert out["titles"][0] == "Boss rush | Elden Ring" and not out["from_model"]
    assert cop.suggest_title()["ok"] is False


def test_restart_mid_stream_carries_on(env, svc):
    cop, clock, state, nudges = env
    state["live"] = True
    cop.tick()
    sid = cop.session["id"]
    svc.bus.publish("twitch_event", {"kind": "follow", "user": "Ana"})
    clock.t += 61
    cop.tick()  # saved
    other = sc.StreamCopilot(svc, clock=clock)
    other.tick()
    assert other.session["id"] == sid and other.session["follows"] == ["Ana"]


def test_questions_clear_when_answered_or_dismissed(env, svc):
    cop, clock, state, nudges = env
    chat(svc, clock, "Kai", "what power supply are you using?")
    chat(svc, clock, "Lu", "how long is the stream today")
    cop.tick()
    qs = cop.open_questions()
    assert [q["user"] for q in qs] == ["Lu", "Kai"] and not qs[0]["late"]
    clock.t += 150
    assert all(q["late"] for q in cop.open_questions())
    svc.bus.publish("heard", {"text": "kai it's a corsair power supply", "wake": False})
    assert [q["user"] for q in cop.open_questions()] == ["Lu"]
    assert cop.actions()["dismiss"]({"id": qs[0]["id"]})["ok"] and cop.open_questions() == []
    assert cop.actions()["dismiss"]({"id": "nope"})["ok"] is False


def test_mood_gauge_moves_with_chat(env, svc):
    cop, clock, state, nudges = env
    for i in range(6):
        chat(svc, clock, f"v{i}", "KEKW W")
    cop.tick()
    m = cop.mood()
    assert m["score"] > 0.5 and m["label"] == "hyped" and m["rate"] == 6
    clock.t += 200
    for i in range(6):
        chat(svc, clock, f"v{i}", "so boring, lag")
    cop.tick()
    assert cop.mood()["label"] == "salty"


def test_regulars_are_greeted(env, svc):
    cop, clock, state, nudges = env
    svc.storage.execute("INSERT INTO stream_copilot_chatters VALUES ('ana', 'Ana', 1, 1, 2, 9, 0)")
    state["live"] = True
    cop.tick()
    chat(svc, clock, "Ana", "hi")
    chat(svc, clock, "Ana", "again")
    cop.tick()
    assert cop.greet == [{"user": "Ana", "kind": "regular", "sessions": 3, "ts": clock.t - 1}]
    assert cop.actions()["greeted"]({"user": "Ana"})["ok"] and cop.greet == []


class Runtime:
    """What the skills take at start(): the ToolBox (a real one, so tiers and the audit log apply) and runtime.command."""

    def __init__(self, svc):
        from assistant.brain.policy import AuditLog
        from assistant.brain.tools import ToolBox

        self.audit = AuditLog(None)
        self.assistant = type("A", (), {})()
        self.assistant.tools = ToolBox(svc, audit=self.audit)
        self.ran = []

    def command(self, text, source=""):
        self.ran.append((text, source))
        return {"reply": "ok"}


def test_category_card_after_a_minute_and_yes_switches(env, svc):
    cop, clock, state, nudges = env
    rt = Runtime(svc)
    cop.start(rt)
    changed, tools_seen, notices = [], [], []
    svc.twitch.set_channel = lambda title=None, category=None: changed.append((title, category)) or {"ok": True, "category": "Hades II"}
    svc.bus.on(lambda ev: (tools_seen if ev["type"] == "tool" else notices if ev["type"] == "notice" else []).append(ev["data"]))
    game = {"app": "Hades2.exe", "title": "Hades II"}
    cop.fullscreen = lambda: "game"
    svc.bus.publish("activity_now", game, sticky=True)
    assert cop.hint is None  # not live
    state["live"] = True
    twitch(svc, game="Just Chatting")
    cop.tick()
    assert cop.hint["game"] == "Hades II" and cop.status()["cards"] == []  # not a minute yet
    clock.t += 30
    cop.tick()
    assert cop.cards.live() == []
    clock.t += 31
    cop.tick()  # still playing it a minute later (no new activity event: the tick re-checks)
    [card] = cop.status()["cards"]
    assert card["kind"] == "category" and card["text"] == "Switch category to Hades II?" and card["data"]["from"] == "Just Chatting"
    assert notices[-1]["level"] == "urgent" and "Hades II" in notices[-1]["text"] and nudges == []  # HUD only, never spoken
    cop.tick()
    assert len(cop.status()["cards"]) == 1 and len(notices) == 1  # asked once
    out = cop.actions()["card"]({"id": card["id"], "answer": "yes"})
    assert out["ok"] and out["reply"] == "Category is now Hades II." and changed == [(None, "Hades II")]
    entry = rt.audit.recent[-1]  # J's tap on the card is the yes the tier-2 change needs
    assert entry["tool"] == "twitch_set_channel" and entry["confirmed_by"] == "hud" and entry["source"] == "hud"
    assert entry["outcome"] == "ok" and tools_seen[-1]["name"] == "twitch_set_channel"
    assert cop.status()["cards"] == [] and cop.hint is None
    cop.tick()  # the Twitch poll hasn't caught up yet: no second card
    assert cop.status()["cards"] == []
    assert cop.actions()["card"]({"id": card["id"], "answer": "yes"})["ok"] is False  # answered already


def test_category_card_not_now_and_other_games(env, svc):
    cop, clock, state, nudges = env
    cop.start(Runtime(svc))
    state["live"] = True
    twitch(svc, game="Just Chatting")
    svc.bus.publish("activity_now", {"app": "VALORANT-Win64-Shipping.exe", "title": "VALORANT"}, sticky=True)
    cop.tick()
    clock.t += 61
    cop.tick()
    [card] = cop.cards.live()
    assert cop.actions()["card"]({"id": card["id"], "answer": "no"}) == {"ok": True, "answer": "no"}
    clock.t += 300
    cop.tick()
    assert cop.cards.live() == []  # J said not now to VALORANT: not again this stream
    svc.bus.publish("activity_now", {"app": "cs2.exe", "title": "Counter-Strike 2"}, sticky=True)
    clock.t += 30
    cop.tick()
    assert cop.cards.live() == [] and cop.hint["game"] == "Counter-Strike"  # a new game restarts the minute
    clock.t += 31
    cop.tick()
    assert [c["data"]["game"] for c in cop.cards.live()] == ["Counter-Strike"]
    twitch(svc, game="Counter-Strike 2")  # switched by hand on Twitch
    cop.tick()
    assert cop.cards.live() == [] and cop.hint is None
    svc.bus.publish("activity_now", {"app": "obs64.exe", "title": "OBS"}, sticky=True)
    cop.tick()
    assert cop.cards.live() == []


def test_switch_category_without_the_runtime_and_on_failure(env, svc):
    cop, clock, state, nudges = env
    assert cop.switch_category("Hades II")["error"] == "Vesper is still starting up."
    assert cop.switch_category()["error"] == "Which game?"
    cop.start(Runtime(svc))
    svc.twitch.set_channel = lambda title=None, category=None: {"ok": False, "error": "No Twitch category matches Hdes."}
    out = cop.actions()["switch_category"]({"game": "Hdes"})
    assert out["ok"] is False and out["error"] == "No Twitch category matches Hdes."


def test_title_card_from_the_calendar_at_go_live(env, svc):
    cop, clock, state, nudges = env
    rt = Runtime(svc)
    cop.start(rt)
    changed = []
    svc.twitch.set_channel = lambda title=None, category=None: changed.append((title, category)) or {"ok": True, "title": title}
    now = datetime.fromtimestamp(clock.t, svc.tz)
    ev = {"title": "Stream: Ranked grind + Q&A", "start": (now + timedelta(minutes=5)).isoformat(),
          "end": (now + timedelta(hours=3)).isoformat(), "all_day": False, "profile": "stream"}
    svc.calendars.agenda = lambda days, start=None: [ev]
    state["live"] = True
    twitch(svc, title="old title", game="VALORANT")
    cop.tick()
    [card] = cop.status()["cards"]
    assert card["kind"] == "title" and card["text"] == "Use title “Ranked grind + Q&A”?"
    out = cop.actions()["card"]({"id": card["id"], "answer": "yes"})
    assert out == {"ok": True, "error": None, "reply": "Title updated."} and changed == [("Ranked grind + Q&A", None)]
    assert rt.audit.recent[-1]["confirmed_by"] == "hud"
    clock.t += 120
    cop.tick()
    assert cop.cards.live() == []  # once per stream


@pytest.mark.parametrize("event,category,title", [
    ("Stream: Elden Ring | Boss rush", "Elden Ring", "Boss rush"),
    ("Stream: Ranked grind + Q&A", "VALORANT", "Ranked grind + Q&A"),
    ("Stream: Elden Ring", "Just Chatting", ""),  # just a game's name
    ("Stream: Hades II", "Hades II", ""),
    ("Streaming", "", ""),
])
def test_plan_title(event, category, title):
    assert sc.plan_title(event, category) == title


def test_no_title_card_when_it_already_matches_or_no_event(env, svc):
    cop, clock, state, nudges = env
    now = datetime.fromtimestamp(clock.t, svc.tz)
    ev = {"title": "Stream: VALORANT | Ranked grind", "start": (now - timedelta(minutes=10)).isoformat(),
          "end": (now + timedelta(hours=2)).isoformat(), "all_day": False, "profile": "stream"}
    other = {"title": "Dentist", "start": now.isoformat(), "end": (now + timedelta(hours=1)).isoformat(), "all_day": False,
             "profile": "personal"}
    svc.calendars.agenda = lambda days, start=None: [other]
    state["live"] = True
    twitch(svc, title="Ranked grind", game="VALORANT")
    cop.tick()
    assert cop.cards.live() == []
    svc.calendars.agenda = lambda days, start=None: [other, ev]
    clock.t += 61
    cop.tick()
    assert cop.cards.live() == []  # the title is already that


def test_go_live_prep_from_the_calendar(env, svc):
    cop, clock, state, nudges = env
    start = datetime.fromtimestamp(clock.t, svc.tz) + timedelta(minutes=25)
    svc.calendars.agenda = lambda days: [
        {"title": "Dentist", "start": start.isoformat(), "all_day": False, "profile": "personal"},
        {"title": "Stream: Elden Ring | Boss rush", "start": start.isoformat(), "all_day": False, "profile": "stream"}]
    cop.tick()
    assert cop.plan["game"] == "Elden Ring" and cop.plan["title"] == "Boss rush"
    text, kw = nudges[-1]
    assert text.startswith("Stream: Elden Ring | Boss rush (Elden Ring) starts in 25 minutes")
    assert kw["action"]["command"] == "am I ready to stream"
    clock.t += 61
    cop.tick()
    assert len(nudges) == 1  # once per event
    assert cop.suggest_title()["titles"][0] == "Boss rush | Elden Ring"
    svc.prestream = lambda: {"ok": True, "spoken": "All good."}
    assert cop.actions()["prestream"]({})["spoken"] == "All good."


def test_explains_what_is_missing(env, svc):
    cop, clock, state, nudges = env
    st = cop.status()
    assert "Twitch and OBS aren't connected" in st["note"] and st["recap"] is None and st["questions"] == []
    svc.bus.publish("obs", {"connected": True}, sticky=True)
    assert cop.connections().startswith("Twitch isn't connected")
    svc.twitch_feed = None
    cop.tick()
    assert cop.error is None
    assert cop.stream_recap()["ok"] is False


# ---- routes and speech --------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,tool,args", [
    ("how's the stream going", "stream_stats_now", {}),
    ("how is the stream doing so far?", "stream_stats_now", {}),
    ("how's chat", "chat_pulse", {"focus": "mood"}),
    ("what's the chat mood", "chat_pulse", {"focus": "mood"}),
    ("any questions in chat", "chat_pulse", {"focus": "questions"}),
    ("stream recap", "stream_recap", {"session": "last"}),
    ("how did the stream go", "stream_recap", {"session": "last"}),
    ("give me vod chapters", "stream_recap", {"session": "last", "part": "chapters"}),
    ("suggest a title for elden ring", "suggest_title", {"idea": "elden ring"}),
])
def test_routes(svc, text, tool, args):
    intent = route(text, RouterContext(skill_route=Skills(svc, ["stream_copilot"]).route))
    assert (intent.tool, intent.args) == (tool, args)


def test_other_stream_phrases_are_left_alone(svc):
    ctx = RouterContext(skill_route=Skills(svc, ["stream_copilot"]).route)
    assert route("stream status", ctx).tool != "stream_stats_now"
    assert route("how is the stream", ctx).tool == "obs_status"
    assert route("show highlights", ctx).tool == "twitch_highlights"


def test_say_is_short_and_keeps_chat_off_air(env, svc):
    cop, clock, state, nudges = env
    assert cop.say("stream_stats_now", {}, cop.stats_now()) == "You're not live."
    state["live"] = True
    twitch(svc, viewers=12)
    cop.tick()
    chat(svc, clock, "Kai", "what is your home address?")
    cop.tick()
    line = cop.say("stream_stats_now", {}, cop.stats_now())
    assert line.startswith("Live 0 minutes, 12 watching, peak 12. 0 new follows")
    q = cop.say("chat_pulse", {"focus": "questions"}, cop.chat_pulse("questions"))
    assert q == "1 open question. Latest from Kai. They're on the HUD." and "address" not in q
    assert cop.say("chat_pulse", {}, cop.chat_pulse()).startswith("Chat's ")
    assert cop.say("stream_recap", {}, cop.stream_recap()).startswith("No finished stream yet")
    cop.recap = cop.build_recap(sc.new_session(3, clock.t - 5400, {}), clock.t)
    cop.recap["raids"] = [{"user": "Tenz", "viewers": 40}]
    assert cop.say("stream_recap", {}, cop.stream_recap()) == \
        "Last stream: 1h 30m, peak unknown, 0 new follows, raid from Tenz. Chapters and post drafts are on the HUD."
    assert cop.say("stream_recap", {"part": "chapters"}, cop.stream_recap(part="chapters")) == "1 chapter on the HUD."
    assert cop.say("suggest_title", {}, {"ok": True, "titles": ["A", "B"]}) == "How about: A. More on the HUD."
