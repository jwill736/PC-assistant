"""Raid planner: ranking (size ±50%, language, friends, raided-before, not this week), the card 10 minutes before the
calendar's stream block ends, raiding from the HUD (the tap is the yes: audited confirmed_by=hud) and by voice (asks
first), raid history both ways. A fake Twitch client: nothing real."""

from datetime import datetime, timedelta

import pytest

from assistant.brain.policy import AuditLog
from assistant.brain.router import RouterContext, route
from assistant.brain.tools import ToolBox
from assistant.bus import EventBus
from assistant.skills import Skills
from assistant.skills import stream_raid as sr

DAY = 86400


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


def stream(login, viewers, lang="en", title="playing"):
    return {"user_login": login, "user_name": login.title(), "viewer_count": viewers, "language": lang, "title": title,
            "game_name": "Hades II", "is_mature": False}


class FakeTwitch:
    configured = enabled = True
    login_name = "jstreams"

    def __init__(self):
        self.streams = [stream("jstreams", 50), stream("ana", 45), stream("bigguy", 500), stream("klaus", 40, "de"),
                        stream("pal", 200), stream("mia", 50), stream("raider", 60)]
        self.raids, self.fail = [], None
        self.auth = type("Auth", (), {"connected": True, "token": {}})()

    def find_category(self, name):
        return {"id": "123", "name": "Hades II"} if name.lower().startswith("hades") else None

    def live_streams(self, game_id, language=None, first=100):
        if self.fail:
            raise self.fail
        assert game_id == "123"
        return list(self.streams)

    def channel_info(self):
        return {"enabled": True, "title": "", "game": ""}

    def start_raid(self, user):
        self.raids.append(user)
        return {"ok": True, "user": user}


@pytest.fixture
def env(svc):
    clock, state = Clock(), {"live": True}
    svc.bus = EventBus()
    svc.live = lambda: state["live"]
    svc.twitch = FakeTwitch()
    svc.bus.publish("twitch", {"enabled": True, "live": True, "game": "Hades II", "viewers": 50, "title": "x"}, sticky=True)
    rp = sr.RaidPlanner(svc, clock=clock)
    svc.skills.all["stream_raid"] = rp  # the ToolBox's twitch_raid is this one's
    audit = AuditLog(None)
    rt = type("RT", (), {})()
    rt.assistant = type("A", (), {})()
    rt.assistant.tools = ToolBox(svc, audit=audit)
    rp.start(rt)
    return rp, clock, state, audit


def test_rank_puts_similar_size_friends_and_reciprocity_first():
    now = 1_000_000.0
    out = sr.rank([stream("jstreams", 50), stream("ana", 45), stream("bigguy", 500), stream("klaus", 40, "de"),
                   stream("pal", 200), stream("recent", 50), stream("raider", 60)],
                  me="jstreams", my_viewers=50, language="en", friends={"Pal"},
                  history={"recent": {"out": now - 2 * DAY}, "raider": {"in": now - 30 * DAY}, "ana": {"out": now - 20 * DAY}},
                  now=now)
    assert [c["login"] for c in out] == ["raider", "ana", "pal", "klaus", "bigguy", "recent"]
    by = {c["login"]: c for c in out}
    assert by["raider"]["why"] == ["about your size", "raided you before"]
    assert by["ana"]["why"] == ["about your size", "you raided them before"]
    assert by["klaus"]["why"] == ["about your size", "streams in de"] and by["pal"]["friend"]
    assert by["recent"]["recent"] and by["recent"]["why"][-1] == "raided 2 days ago"
    assert by["bigguy"]["why"] == ["much bigger"]
    assert sr.rank([stream("a", 0), stream("b", 10)], my_viewers=None, now=now)[0]["login"] == "b"  # nobody watching: last
    assert len(sr.rank([stream(f"c{i}", i) for i in range(20)], now=now, limit=8)) == 8


def test_who_should_i_raid(env, svc):
    rp, clock, state, audit = env
    out = rp.suggest()
    assert out["ok"] and out["game"] == "Hades II" and [c["login"] for c in out["channels"]][:3] == ["mia", "ana", "raider"]
    assert "jstreams" not in [c["login"] for c in out["channels"]]
    line = rp.say("raid_suggestions", {}, out)
    assert line == "Top pick: Mia, 50 watching (about your size). 5 more on the HUD."
    svc.twitch.streams = [stream("jstreams", 50)]
    assert rp.say("raid_suggestions", {}, rp.suggest()) == "Nobody else is live in Hades II right now."


def test_raid_from_the_hud_is_confirmed_by_the_tap_and_remembered(env, svc):
    rp, clock, state, audit = env
    rp.suggest()
    seen = []
    svc.bus.on(lambda ev: seen.append(ev))
    out = rp.actions()["raid"]({"login": "ana"})
    assert out["ok"] and out["user"] == "Ana" and svc.twitch.raids == ["ana"]
    entry = audit.recent[-1]
    assert entry["tool"] == "twitch_raid" and entry["tier"] == 2 and entry["confirmed_by"] == "hud" and entry["outcome"] == "ok"
    assert any(e["type"] == "tool" and e["data"]["name"] == "twitch_raid" for e in seen)
    assert rp.status()["history"][0]["login"] == "ana" and rp.status()["history"][0]["direction"] == "out"
    clock.t += 3 * DAY
    by = {c["login"]: c for c in rp.suggest()["channels"]}
    assert by["ana"]["recent"] and by["ana"]["why"][-1] == "raided 3 days ago"
    assert rp.suggest()["channels"][-1]["login"] == "ana"  # last, however good otherwise
    clock.t += 5 * DAY
    by = {c["login"]: c for c in rp.suggest()["channels"]}
    assert not by["ana"]["recent"] and "you raided them before" in by["ana"]["why"]


def test_raid_by_voice_needs_a_yes(env, svc):
    rp, clock, state, audit = env
    ctx = RouterContext(skill_route=Skills(svc, ["stream_raid"]).route)
    intent = route("raid ana", ctx)
    assert (intent.tool, intent.args) == ("twitch_raid", {"user": "ana"})
    tools = ToolBox(svc, audit=AuditLog(None))
    blocked = tools.run("twitch_raid", {"user": "ana"})
    assert blocked["blocked"] and svc.twitch.raids == []
    assert tools.tools["twitch_raid"].describe({"user": "ana"}) == "raid ana"
    done = tools.run("twitch_raid", {"user": "ana"}, confirmed_by="voice")
    assert done["ok"] and rp.say("twitch_raid", {}, done) == "Raiding ana. Twitch counts down 90 seconds."


@pytest.mark.parametrize("text", ["who should i raid", "who should i raid tonight", "find someone to raid", "raid suggestions",
                                  "show me channels to raid"])
def test_suggestion_routes(svc, text):
    intent = route(text, RouterContext(skill_route=Skills(svc, ["stream_raid"]).route))
    assert intent.tool == "raid_suggestions"


def test_raid_someone_is_not_a_channel(svc):
    assert Skills(svc, ["stream_raid"]).route("raid someone") is None


def test_card_ten_minutes_before_the_calendar_block_ends(env, svc):
    rp, clock, state, audit = env
    now = datetime.fromtimestamp(clock.t, svc.tz)
    ev = {"title": "Stream: Hades II", "start": (now - timedelta(hours=2)).isoformat(), "end": (now + timedelta(minutes=15)).isoformat(),
          "all_day": False, "profile": "stream"}
    svc.calendars.agenda = lambda days, start=None: [ev]
    notices = []
    svc.bus.on(lambda e: notices.append(e["data"]) if e["type"] == "notice" else None)
    rp.tick()
    assert rp.cards.live() == []  # 15 minutes left
    clock.t += 6 * 60
    rp.tick()
    [card] = rp.status()["cards"]
    assert card["kind"] == "raid" and card["text"] == "Stream ends in 9 min. Raid someone?"
    assert [c["login"] for c in card["data"]["channels"]][:2] == ["mia", "ana"]
    assert notices[-1]["text"] == "Stream ends in 9 minutes: raid picks are on the HUD (top: Mia, 50 watching)."
    clock.t += 60
    rp.tick()
    assert len(rp.cards.live()) == 1 and len(notices) == 1  # once
    assert rp.actions()["card"]({"id": card["id"], "answer": "no"})["ok"] and rp.cards.live() == []


def test_no_card_offline_or_without_a_stream_block(env, svc):
    rp, clock, state, audit = env
    now = datetime.fromtimestamp(clock.t, svc.tz)
    ev = {"title": "Stream: Hades II", "start": (now - timedelta(hours=2)).isoformat(), "end": (now + timedelta(minutes=5)).isoformat(),
          "all_day": False, "profile": "stream"}
    svc.calendars.agenda = lambda days, start=None: []
    rp.tick()
    assert rp.cards.live() == []
    svc.calendars.agenda = lambda days, start=None: [ev]
    state["live"] = False
    rp.tick()
    assert rp.cards.live() == [] and rp.list == []


def test_incoming_raids_and_friends_count(env, svc):
    rp, clock, state, audit = env
    svc.bus.publish("twitch_event", {"kind": "raid", "user": "Klaus", "amount": 30, "detail": {"login": "klaus"}})
    assert rp.history()["klaus"]["in"] == clock.t
    assert rp.actions()["friend"]({"login": "BigGuy", "on": True}) == {"ok": True, "login": "bigguy", "friend": True}
    by = {c["login"]: c for c in rp.suggest()["channels"]}
    assert "raided you before" in by["klaus"]["why"] and by["bigguy"]["friend"]
    rp.cfg["friends"] = ["@Pal"]
    assert "pal" in rp.friends()
    rp.actions()["friend"]({"login": "bigguy", "on": False})
    assert "bigguy" not in rp.friends()


def test_explains_what_is_missing(env, svc):
    rp, clock, state, audit = env
    svc.twitch.fail = RuntimeError("503")
    assert "Couldn't ask Twitch" in rp.suggest()["error"] and "503" in rp.status()["error"]
    svc.bus.publish("twitch", {"enabled": True, "live": True, "game": "Mystery Game", "viewers": 5}, sticky=True)
    assert rp.suggest()["error"] == "Twitch has no category called Mystery Game."
    svc.bus.publish("twitch", {"enabled": True, "live": False}, sticky=True)
    assert "category isn't set" in rp.suggest()["error"]
    svc.twitch.enabled = False
    assert rp.suggest()["error"].startswith("Connect Twitch first")
    assert rp.actions()["raid"]({"login": ""})["error"] == "Which channel?"
