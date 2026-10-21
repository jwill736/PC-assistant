"""Clip-worthy moments: chat + J's voice + Twitch events scored together, a marker and a "Clip that?" card above the
threshold, dedupe within 60 s and a rate limit, the clip on J's tap (audited as confirmed on the HUD), and the
post-stream highlights JSON for the promo-video skill. Fake Twitch, fake mic meter, fake clock: nothing real."""

import json
from datetime import datetime

import pytest

from assistant.brain.policy import AuditLog
from assistant.brain.router import RouterContext, route
from assistant.brain.tools import ToolBox
from assistant.bus import EventBus
from assistant.integrations.eventsub import TwitchFeed
from assistant.integrations.highlights import ChatSpike
from assistant.skills import Skills
from assistant.skills import stream_moments as sm


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Runtime:
    def __init__(self, svc):
        self.audit = AuditLog(None)
        self.assistant = type("A", (), {})()
        self.assistant.tools = ToolBox(svc, audit=self.audit)
        self.mic_watch = None


@pytest.fixture
def env(svc):
    clock, state = Clock(), {"live": False}
    svc.bus = EventBus()
    svc.live = lambda: state["live"]
    svc.twitch_feed = TwitchFeed(clock=clock)
    svc.highlights = lambda: []
    peaks, calls = [], {"markers": [], "clips": []}
    tw = svc.twitch
    tw.auth.token = {"access_token": "t", "refresh_token": "r", "expires_at": clock.t + 9999}
    tw.create_marker = lambda description="": calls["markers"].append(description) or {
        "ok": True, "id": f"mk{len(calls['markers'])}", "position_s": 600 + len(calls["markers"]), "description": description}
    tw.create_clip = lambda title=None, duration=None: calls["clips"].append((title, duration)) or {
        "ok": True, "id": "FunnyClip1", "url": "https://clips.twitch.tv/FunnyClip1", "title": title}
    mo = sm.StreamMoments(svc, clock=clock, loudness=lambda: list(peaks))
    rt = Runtime(svc)
    mo.start(rt)
    seen = []
    svc.bus.on(lambda ev: seen.append(ev))
    return mo, clock, state, peaks, calls, rt, seen


def go_live(svc, mo, clock, state, viewers=50):
    state["live"] = True
    svc.bus.publish("twitch", {"enabled": True, "live": True, "title": "Ranked grind", "game": "VALORANT", "viewers": viewers},
                    sticky=True)
    mo.tick()


def calm(svc, mo, clock, peaks, seconds=120):
    """Normal chat (one message every 10 s) and J talking normally (peaks around -20 dB)."""
    for i in range(int(seconds * 2)):
        clock.t += 0.5
        peaks.append((clock.t, -20.0 + (i % 3)))
        if i % 20 == 0:
            svc.twitch_feed.add({"kind": "chat", "user": f"v{i % 5}", "text": "hello"})
    mo.tick()


def burst(svc, clock, peaks, loud_db=-4.0, chat=True):
    """Chat goes off (20 messages from 8 people in 8 s) and/or J shouts."""
    for i in range(16):
        clock.t += 0.5
        if chat and i < 20:
            svc.twitch_feed.add({"kind": "chat", "user": f"fan{i % 8}", "text": "KEKW"})
            svc.twitch_feed.add({"kind": "chat", "user": f"fan{(i + 3) % 8}", "text": "LUL"})
        peaks.append((clock.t, loud_db if i > 10 else -20.0))


# ---- the pure parts --------------------------------------------------------------------------------------------

def test_chat_signal_against_the_channel_pace():
    clock = Clock()
    sp = ChatSpike(clock=clock, cooldown_s=0)
    for i in range(30):
        sp.add(f"u{i % 4}", "hi", clock.t + i * 10)
    now = clock.t + 300
    assert sm.chat_signal(sp, now)[0] == 0.0  # normal pace
    for i in range(20):
        sp.add(f"f{i % 6}", "LUL", now + i * 0.4)
    score, det = sm.chat_signal(sp, now + 8)
    assert score == 1.0 and det["messages"] == 20 and det["chatters"] == 6
    one = ChatSpike(clock=clock, cooldown_s=0)
    for i in range(20):
        one.add("spammer", "LUL", now + i * 0.4)
    assert sm.chat_signal(one, now + 8)[0] == 0.0  # one person spamming isn't chat going off


def test_voice_signal_is_relative_to_how_you_talk():
    t0 = 1000.0
    talk = [(t0 + i * 0.5, -20.0) for i in range(180)]
    now = t0 + 92
    assert sm.voice_signal(talk + [(now, -19.0)], now)[0] == 0.0
    assert sm.voice_signal(talk + [(now, -4.0)], now)[0] == 1.0
    assert 0.3 < sm.voice_signal(talk + [(now, -9.0)], now)[0] < 0.7
    quiet = [(t0 + i * 0.5, -45.0) for i in range(180)]
    assert sm.voice_signal(quiet + [(now, -30.0)], now)[0] == 0.0  # louder than the room, but not a raised voice
    assert sm.voice_signal([(now, -2.0)], now)[0] == 0.0  # no baseline yet


def test_event_weights_and_combining():
    assert sm.event_weight("raid", 40)[0] == 1.0 and sm.event_weight("hype_train", 1)[0] == 1.0
    assert sm.event_weight("gift", 10)[0] == 0.8 and sm.event_weight("gift", 1)[0] < 0.3
    assert sm.event_weight("cheer", 50)[0] == 0.0 and sm.event_weight("follow", 0)[0] == 0.0
    assert sm.combine(1, 0, 0) == 0.5 and sm.combine(0, 1, 0) == 0.45 and sm.combine(0, 0, 1) == 0.6
    assert sm.combine(1, 1, 1) == 1.0 and sm.combine(1, 0.4, 0, {"voice": 1.0}) == 0.9
    assert sm.reasons_of({"chat": 1, "voice": 0.8, "event": 0.9}, "raid (40) from Tenz") == \
        ["raid (40) from Tenz", "chat going off", "you got loud"]


def test_fallback_titles():
    assert sm.fallback_title({"reason": "Clip-worthy: raid (40) from Tenz", "reasons": ["raid (40) from Tenz"]}) == "The Tenz raid lands"
    assert sm.fallback_title({"reasons": ["chat going off", "you got loud"]}, "Hades II") == "Hades II: chat and I lost it together"
    assert sm.fallback_title({"reasons": ["you got loud"]}) == "I did NOT see that coming"


# ---- live --------------------------------------------------------------------------------------------------------

def test_chat_and_voice_make_a_moment_with_a_marker_and_a_clip_card(env, svc):
    mo, clock, state, peaks, calls, rt, seen = env
    go_live(svc, mo, clock, state)
    calm(svc, mo, clock, peaks)
    assert mo.session["moments"] == [] and mo.meter["score"] < 0.3
    burst(svc, clock, peaks)
    mo.tick()
    [m] = mo.session["moments"]
    assert m["score"] >= 0.9 and m["reasons"] == ["chat going off", "you got loud"] and m["signals"]["voice"] > 0.85
    assert m["marker"] and m["marker_id"] == "mk1" and calls["markers"] == ["Clip-worthy: chat going off, you got loud"]
    assert rt.audit.recent[-1]["tool"] == "twitch_marker" and rt.audit.recent[-1]["source"] == "stream_moments"
    [card] = mo.status()["cards"]
    assert card["kind"] == "clip" and card["text"] == "Clip that?" and card["data"]["moment"] == m["id"]
    hl = [e["data"] for e in seen if e["type"] == "highlight"]
    assert hl[-1]["kind"] == "moment" and hl[-1]["marker"] is True


def test_chat_alone_or_voice_alone_is_not_enough(env, svc):
    mo, clock, state, peaks, calls, rt, seen = env
    go_live(svc, mo, clock, state)
    calm(svc, mo, clock, peaks)
    burst(svc, clock, peaks, loud_db=-20.0)  # chat goes off, J doesn't react
    mo.tick()
    assert mo.session["moments"] == [] and mo.meter["chat"] == 1.0
    clock.t += 120
    calm(svc, mo, clock, peaks)
    burst(svc, clock, peaks, chat=False)  # J shouts at the game, chat is calm
    mo.tick()
    assert mo.session["moments"] == [] and mo.meter["voice"] > 0.85
    assert calls["markers"] == []


def test_a_raid_alone_is_a_moment_and_events_fade(env, svc):
    mo, clock, state, peaks, calls, rt, seen = env
    go_live(svc, mo, clock, state)
    svc.bus.publish("twitch_event", {"kind": "sub", "user": "Cy"})
    mo.tick()
    assert mo.session["moments"] == []
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Tenz", "amount": 40})
    mo.tick()
    [m] = mo.session["moments"]
    assert m["reasons"] == ["raid (40) from Tenz"] and m["signals"]["event"] == 1.0
    clock.t += 31
    mo.tick()
    assert mo.meter["event"] == 0.0


def test_dedupe_within_a_minute_and_rate_limit(env, svc):
    mo, clock, state, peaks, calls, rt, seen = env
    mo.cfg["max_per_10min"] = 2
    go_live(svc, mo, clock, state)
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Tenz", "amount": 40})
    mo.tick()
    clock.t += 10
    svc.bus.publish("twitch_event", {"kind": "hype_train", "amount": 1})
    mo.tick()
    [m] = mo.session["moments"]  # the same moment, still going
    assert m["reasons"] == ["raid (40) from Tenz", "hype train"] and len(calls["markers"]) == 1 and len(mo.cards.live()) == 1
    clock.t += 61
    svc.bus.publish("twitch_event", {"kind": "gift", "user": "Di", "amount": 10})
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Kai", "amount": 12})
    mo.tick()
    assert len(mo.session["moments"]) == 2
    clock.t += 61
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Lu", "amount": 9})
    mo.tick()
    assert len(mo.session["moments"]) == 2 and mo.skipped == 1 and len(calls["markers"]) == 2  # over the limit
    clock.t += 600
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Mo", "amount": 9})
    mo.tick()
    assert len(mo.session["moments"]) == 3


def test_reuses_the_runtimes_marker_and_works_without_twitch_login(env, svc):
    mo, clock, state, peaks, calls, rt, seen = env
    go_live(svc, mo, clock, state)
    svc.bus.publish("highlight", {"kind": "chat_spike", "reason": "Chat spike (LUL)", "ts": clock.t, "uptime_s": 30, "marker": True})
    svc.bus.publish("twitch_event", {"kind": "gift", "user": "Di", "amount": 5})
    mo.tick()
    [m] = mo.session["moments"]
    assert m["marker"] and calls["markers"] == []  # the runtime already put one in for the spike
    svc.twitch.auth.token = {}
    clock.t += 61
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Tenz", "amount": 40})
    mo.tick()
    assert len(mo.session["moments"]) == 2 and mo.session["moments"][1]["marker"] is False and calls["markers"] == []


def test_clip_on_yes_reaches_back_to_the_moment(env, svc):
    mo, clock, state, peaks, calls, rt, seen = env
    go_live(svc, mo, clock, state)
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Tenz", "amount": 40})
    mo.tick()
    [card] = mo.cards.live()
    clock.t += 20
    out = mo.actions()["card"]({"id": card["id"], "answer": "yes"})
    assert out["ok"] and out["url"] == "https://clips.twitch.tv/FunnyClip1"
    assert calls["clips"] == [("raid (40) from Tenz", 45)]  # 20 s ago + 25 s of lead-in
    entry = rt.audit.recent[-1]
    assert entry["tool"] == "twitch_clip" and entry["confirmed_by"] == "hud"
    m = mo.session["moments"][0]
    assert m["clip_url"] == "https://clips.twitch.tv/FunnyClip1" and m["state"] == "clipped" and mo.cards.live() == []
    assert any(e["type"] == "tool" and e["data"]["name"] == "twitch_clip" for e in seen)


def test_clip_card_expires_and_not_now(env, svc):
    mo, clock, state, peaks, calls, rt, seen = env
    go_live(svc, mo, clock, state)
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Tenz", "amount": 40})
    mo.tick()
    [card] = mo.cards.live()
    clock.t += 51
    assert mo.cards.live() == []
    assert mo.actions()["card"]({"id": card["id"], "answer": "yes"})["ok"] is False
    clock.t += 10
    assert "Too late" in mo.actions()["clip"]({"id": mo.session["moments"][0]["id"]})["error"] and calls["clips"] == []
    clock.t += 61
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Kai", "amount": 40})
    mo.tick()
    [card] = mo.cards.live()
    assert mo.actions()["card"]({"id": card["id"], "answer": "no"}) == {"ok": True, "answer": "no"}
    assert mo.session["moments"][1]["state"] == "skipped" and calls["clips"] == []


def test_nothing_happens_offline(env, svc):
    mo, clock, state, peaks, calls, rt, seen = env
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Tenz", "amount": 40})
    mo.tick()
    assert mo.session is None and mo.cards.live() == [] and mo.status()["live"] is False


# ---- after the stream ------------------------------------------------------------------------------------------

def stream_with_moments(svc, mo, clock, state, peaks):
    go_live(svc, mo, clock, state)
    start = mo.session["start"]
    clock.t += 600
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Tenz", "amount": 40})
    mo.tick()
    [card] = mo.cards.live()
    mo.actions()["card"]({"id": card["id"], "answer": "yes"})
    clock.t += 1200
    calm(svc, mo, clock, peaks)
    burst(svc, clock, peaks)
    mo.tick()
    return start


def test_highlights_json_after_the_stream(env, svc):
    mo, clock, state, peaks, calls, rt, seen = env
    svc.think = lambda prompt, schema=None, **kw: {"titles": ["Tenz brings the army", "Chat and J lose it"]}
    start = stream_with_moments(svc, mo, clock, state, peaks)
    manual_ts = clock.t - 300  # J said "mark that" between the two
    svc.highlights = lambda: [{"kind": "manual", "reason": "Insane ace", "ts": manual_ts, "uptime_s": manual_ts - start, "marker": True},
                              {"kind": "manual", "reason": "Yesterday", "ts": start - 86400}]
    state["live"] = False
    mo.tick()
    clock.t += 30
    mo.tick()
    assert mo.session is not None  # a short drop doesn't end it
    clock.t += 61
    mo.tick()
    assert mo.session is None
    last = mo.last
    date = datetime.fromtimestamp(start, svc.tz).strftime("%Y-%m-%d")
    assert last["path"].endswith(f"highlights/{date}-session.json".replace("/", __import__("os").sep))
    saved = json.loads(open(last["path"], encoding="utf-8").read())
    assert saved["schema"] == "vesper.highlights/1" and "path" not in saved
    s = saved["stream"]
    assert s["date"] == date and s["game"] == "VALORANT" and s["title"] == "Ranked grind" and s["duration_s"] > 1800
    hl = saved["highlights"]
    assert [h["rank"] for h in hl] == [1, 2, 3] and hl[0]["score"] >= hl[1]["score"] >= hl[2]["score"]
    raid = next(h for h in hl if "raid" in h["reason"])
    assert raid["at"] == "00:10:00" and raid["uptime_s"] == 600 and raid["clip_url"] == "https://clips.twitch.tv/FunnyClip1"
    assert raid["marker"] and raid["marker_id"] == "mk1" and raid["marker_position_s"] == 601
    assert raid["title"] == "Tenz brings the army" and raid["window"] == {"start_s": 580, "end_s": 610}
    manual = next(h for h in hl if h["kind"] == "manual")
    assert manual["reason"] == "Insane ace" and manual["score"] == 0.9 and manual["title"] == "Chat and J lose it"
    assert all(h["reason"] != "Yesterday" for h in hl)
    assert "nuhh-promo-video" in saved["note"]
    st = mo.status()
    assert st["promo"].startswith("Use the nuhh-promo-video skill") and '"clip_url": "https://clips.twitch.tv/FunnyClip1"' in st["promo"]
    note = [e["data"] for e in seen if e["type"] == "notice"][-1]
    assert note["text"].startswith("Highlights list ready: 3 moments")
    again = sm.StreamMoments(svc, clock=clock)  # survives a restart
    assert again.moments("last")["highlights"] == hl and again.briefing()["line"].startswith("3 highlights")


def test_titles_without_a_model_and_a_second_stream_the_same_day(env, svc):
    mo, clock, state, peaks, calls, rt, seen = env
    svc.think = lambda prompt, schema=None, **kw: None
    stream_with_moments(svc, mo, clock, state, peaks)
    state["live"] = False
    mo.tick()
    clock.t += 91
    mo.tick()
    first = mo.last
    assert [h["title"] for h in first["highlights"]] == ["VALORANT: chat and I lost it together", "The Tenz raid lands"]
    clock.t += 3600
    stream_with_moments(svc, mo, clock, state, peaks)
    state["live"] = False
    mo.tick()
    clock.t += 91
    mo.tick()
    assert mo.last["path"].endswith("-session-2.json") and first["path"] != mo.last["path"]


def test_restart_mid_stream_keeps_the_moments(env, svc):
    mo, clock, state, peaks, calls, rt, seen = env
    go_live(svc, mo, clock, state)
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Tenz", "amount": 40})
    mo.tick()
    other = sm.StreamMoments(svc, clock=clock, loudness=lambda: [])
    clock.t += 10
    other.tick()
    assert [m["reasons"] for m in other.session["moments"]] == [["raid (40) from Tenz"]]


# ---- routes and speech --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,args", [
    ("best moments so far", {"session": "now"}),
    ("clip worthy moments", {"session": "now"}),
    ("what were the best moments of the last stream", {"session": "last"}),
    ("highlights list", {"session": "now"}),
])
def test_routes(svc, text, args):
    intent = route(text, RouterContext(skill_route=Skills(svc, ["stream_moments"]).route))
    assert (intent.tool, intent.args) == ("stream_moments", args)


def test_show_highlights_still_goes_to_twitch(svc):
    ctx = RouterContext(skill_route=Skills(svc, ["stream_moments"]).route)
    assert route("show highlights", ctx).tool == "twitch_highlights"


def test_say(env, svc):
    mo, clock, state, peaks, calls, rt, seen = env
    assert mo.say("stream_moments", {}, mo.moments()).startswith("No highlights yet")
    go_live(svc, mo, clock, state)
    assert mo.say("stream_moments", {}, mo.moments()) == "Nothing clip-worthy yet. I'm watching chat and your mic."
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Tenz", "amount": 40})
    mo.tick()
    assert mo.say("stream_moments", {}, mo.moments()) == "1 clip-worthy moment so far. Each has a marker on the VOD."
    tool = ToolBox(svc).tools["stream_moments"]
    assert tool.tier_for({}) == 0
