"""YouTube Live: the youtube box added to the Google sign-in (incremental consent), the live broadcast and its chat into
the Twitch feed (co-pilot questions, moments' chat signal), a quota budget that lasts the stream, titles on Twitch and
YouTube asked once then remembered (and behind the usual yes), and Twitch-only when YouTube isn't connected.
Google, YouTube and Twitch are faked: nothing reaches the network."""

import json
import time
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from assistant.brain.assistant import Assistant
from assistant.brain.router import RouterContext, route
from assistant.bus import EventBus
from assistant.integrations.eventsub import TwitchFeed
from assistant.integrations.google import SCOPES, YOUTUBE_SCOPE, GoogleAccounts
from assistant.skills import Skills
from assistant.skills import stream_copilot as cop_mod
from assistant.skills import stream_moments as moments_mod
from assistant.skills import stream_youtube as yt_mod

EMAIL = "j@example.com"
NOW = datetime(2026, 11, 4, 20, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class FakeYouTube:
    """liveBroadcasts, liveChat/messages, videos (read + update) and channels, as YouTube answers them."""

    def __init__(self, clock):
        self.clock, self.calls, self.updates = clock, [], []
        self.active = [{"id": "vid1", "snippet": {"title": "Old title", "liveChatId": "chat1"}}]
        self.upcoming = []
        self.pages = []  # what each chat read returns, in order
        self.error = None  # (status, reason) for the next call
        self.snippet = {"title": "Old title", "description": "Old desc", "categoryId": "20", "tags": ["elden ring"],
                        "defaultLanguage": "en", "channelTitle": "J"}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, q = req.url.path.split("/youtube/v3/")[-1], dict(req.url.params)
        self.calls.append((req.method, path, q))
        assert req.headers["authorization"] == "Bearer tok"
        if self.error:
            status, reason = self.error
            self.error = None
            return httpx.Response(status, json={"error": {"code": status, "message": reason, "errors": [{"reason": reason}]}})
        if path == "liveBroadcasts":
            return httpx.Response(200, json={"items": self.active if q["broadcastStatus"] == "active" else self.upcoming})
        if path == "liveChat/messages":
            page = self.pages.pop(0) if self.pages else {"items": []}
            return httpx.Response(200, json={"nextPageToken": f"p{len(self.calls)}", "pollingIntervalMillis": 5000, **page})
        if path == "videos" and req.method == "GET":
            return httpx.Response(200, json={"items": [{"id": q["id"], "snippet": dict(self.snippet)}]})
        if path == "videos" and req.method == "PUT":
            self.updates.append(json.loads(req.content))
            return httpx.Response(200, json=self.updates[-1])
        if path == "channels":
            return httpx.Response(200, json={"items": [{"id": "UCj"}]})
        return httpx.Response(404, json={"error": {"code": 404, "message": "nope"}})


def msg(name, text, cid="UCv", ago=0.0, owner=False, kind="textMessageEvent", now=NOW):
    published = datetime.fromtimestamp(now - ago, ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
    return {"snippet": {"type": kind, "displayMessage": text, "publishedAt": published},
            "authorDetails": {"displayName": name, "channelId": cid, "isChatOwner": owner}}


class FakeTwitch:
    configured, login_name = True, "jstreams"

    def __init__(self):
        self.titles = []
        self.auth = SimpleNamespace(connected=True)

    def set_channel(self, title=None, category=None):
        self.titles.append(title)
        return {"ok": True, "title": title, "category": None}


def google(fake, scopes=("openid", "email", YOUTUBE_SCOPE), clock=time.time):
    g = GoogleAccounts("cid", "secret", None, http=httpx.Client(transport=httpx.MockTransport(fake)), clock=clock)
    g._tokens = {EMAIL: {"access_token": "tok", "expires_at": 4e9, "refresh_token": "r", "scope": " ".join(scopes)}}
    return g


@pytest.fixture
def env(svc, monkeypatch):
    monkeypatch.setattr("assistant.config.save_setting", lambda cfg, key, value: None)
    clock, state = Clock(), {"live": True}
    fake = FakeYouTube(clock)
    svc.bus = EventBus()
    svc.live = lambda: state["live"]
    svc.google = google(fake)
    svc.twitch = FakeTwitch()
    svc.twitch_feed = TwitchFeed(clock=clock)
    opened = []
    yt = yt_mod.StreamYouTube(svc, clock=clock, opener=opened.append)
    return yt, fake, clock, state, opened


def reads(fake):
    return [c for c in fake.calls if c[1] == "liveChat/messages"]


# ---- sign-in: one more box, asked on its own --------------------------------------------------------------------

def test_connect_asks_google_only_for_the_youtube_box(env):
    yt, fake, clock, state, opened = env
    out = yt.connect()
    q = parse_qs(urlparse(out["url"]).query)
    assert opened == [out["url"]] and q["scope"] == [f"openid email {YOUTUBE_SCOPE}"]
    assert q["include_granted_scopes"] == ["true"] and q["redirect_uri"][0].endswith("/api/google/callback")
    assert YOUTUBE_SCOPE not in SCOPES  # connecting Gmail and Calendar never asks for YouTube
    yt.svc.google.client_id = ""
    assert "sign-in key" in yt.connect()["error"]


def test_an_account_without_the_box_isnt_connected_and_nothing_runs(env):
    yt, fake, clock, state, opened = env
    yt.svc.google = google(fake, scopes=SCOPES)
    assert yt.svc.google.accounts()[0]["missing"] == []  # YouTube is never "missing" from a Gmail/Calendar account
    yt.tick()
    assert not yt.connected() and fake.calls == [] and yt.status()["connected"] is False
    assert yt.selftest()[0]["status"] == "skip"


# ---- the live broadcast and its chat ----------------------------------------------------------------------------

def test_finds_the_broadcast_and_reads_chat_into_the_feed(env):
    yt, fake, clock, state, opened = env
    fake.pages = [{"items": [msg("Old", "from before", ago=600), msg("Ana", "what rank are you?", cid="UCana"),
                             msg("J", "my own message", owner=True), msg("Bo", "", kind="messageDeletedEvent")]},
                  {"items": [msg("Ana", "PogChamp", cid="UCana")]}]
    yt.tick()
    assert yt.broadcast == {"id": "vid1", "title": "Old title", "chat_id": "chat1"}
    chat = yt.svc.twitch_feed.recent_chat()
    assert [(m["user"], m["text"], m["platform"], m["login"]) for m in chat] == \
        [("Ana", "what rank are you?", "youtube", "yt:UCana")]  # not the backlog, not J's own, not a deleted one
    assert yt.svc.twitch_feed.names == {}  # a YouTube name is never offered for a Twitch shoutout or raid
    assert yt.interval == pytest.approx(10.6, abs=0.1)  # 8,500 units / 5 per read over 5 hours, slower than YouTube's 5 s
    clock.t += 5
    yt.tick()
    assert len(reads(fake)) == 1  # not yet
    clock.t += 6
    yt.tick()
    assert len(reads(fake)) == 2 and reads(fake)[1][2]["pageToken"] == "p2"
    assert [m["text"] for m in yt.svc.twitch_feed.recent_chat()][-1] == "PogChamp" and yt.read == 2


def test_youtube_chat_reaches_the_copilot_and_the_moments_signal(env):
    yt, fake, clock, state, opened = env
    svc = yt.svc
    svc.highlights = lambda: []
    cop = cop_mod.StreamCopilot(svc, clock=clock, fullscreen=lambda: "")
    svc.bus.publish("twitch", {"enabled": True, "live": True, "title": "t", "game": "ELDEN RING"}, sticky=True)
    cop.tick()  # the session opens
    mom = moments_mod.StreamMoments(svc, clock=clock)
    fake.pages = [{"items": [msg("Ana", "what boss is next?", cid="UCana"), msg("Bo", "LUL LUL", cid="UCbo")]}]
    clock.t += 1
    yt.tick()
    clock.t += 1
    cop.tick()
    qs = cop.open_questions()
    assert [(q["user"], q["platform"]) for q in qs] == [("Ana", "youtube")]
    assert cop.session["messages"] == 2 and cop.session["yt_messages"] == 2
    assert [g["user"] for g in cop.greet] == ["Bo", "Ana"]  # first-time chatters, by their YouTube channel
    mom._read_chat()
    assert len(mom.spike.times) == 2


def test_chat_waits_for_the_daily_budget_and_counts_across_restarts(env, monkeypatch):
    yt, fake, clock, state, opened = env
    monkeypatch.setitem(yt.cfg, "reserve", 9985)  # leaves 15 units: the lookup and two reads
    clock.t = datetime(2026, 11, 4, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()  # a morning stream
    yt.tick()
    assert yt.spent() == 10 and yt.chat_note is None  # the lookup (5) and one read (5)
    assert yt.interval == 5 * 3600  # one read left: spread over the rest of the stream
    clock.t += 60
    yt.tick()
    assert len(reads(fake)) == 1
    clock.t += yt.interval
    yt.tick()
    assert yt.spent() == 15 and "quota budget is spent" in yt.chat_note
    clock.t += 600
    yt.tick()
    assert len(reads(fake)) == 2  # stopped reading
    again = yt_mod.StreamYouTube(yt.svc, clock=clock)
    assert again.spent() == 15  # a restart doesn't start the count over
    clock.t = datetime(2026, 11, 5, 0, 1, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    assert again.spent() == 0  # midnight Pacific: a new quota day


def test_quota_exceeded_stops_everything_for_the_day(env):
    yt, fake, clock, state, opened = env
    fake.error = (403, "quotaExceeded")
    yt.tick()
    assert "quota is used up" in yt.error and yt.spent() == yt.daily()
    clock.t += 300
    n = len(fake.calls)
    yt.tick()
    assert len(fake.calls) == n  # no call while it's spent
    assert "quota is used up" in yt.set_youtube("New")["error"]


def test_not_live_on_youtube_looks_again_less_and_less(env):
    yt, fake, clock, state, opened = env
    fake.active = []
    times = []
    for _ in range(400):
        n = len(fake.calls)
        yt.tick()
        if len(fake.calls) > n:
            times.append(clock.t)
        clock.t += 5
    gaps = [round(b - a) for a, b in zip(times, times[1:])]
    assert gaps[:4] == [120, 240, 480, 900] and "not live on YouTube" in yt.status()["note"]


def test_chat_ended_or_stream_offline_drops_the_broadcast(env):
    yt, fake, clock, state, opened = env
    fake.pages = [{"items": [], "offlineAt": "2026-11-05T05:00:00Z"}]
    yt.tick()
    assert yt.broadcast is None and "ended" in yt.chat_note
    yt.next_find = 0
    fake.error = None
    yt.tick()  # found again
    clock.t += 30
    fake.error = (403, "liveChatEnded")
    yt.tick()
    assert yt.broadcast is None and yt.chat_note == "The YouTube chat ended."


def test_going_offline_resets_and_offline_nothing_is_called(env):
    yt, fake, clock, state, opened = env
    yt.tick()
    assert yt.broadcast
    state["live"] = False
    yt.tick()
    clock.t += 100  # past the 90 s grace: a blip isn't the end of the stream
    yt.tick()
    assert yt.broadcast is None and yt.read == 0
    n = len(fake.calls)
    clock.t += 3600
    yt.tick()
    assert len(fake.calls) == n


def test_network_trouble_is_a_hud_note_not_a_crash(env):
    yt, fake, clock, state, opened = env

    def down(req):
        raise httpx.ConnectError("offline")
    yt.svc.google.http = httpx.Client(transport=httpx.MockTransport(down))
    yt.tick()
    assert "didn't answer" in yt.status()["error"]


# ---- titles: Twitch and YouTube, asked once ---------------------------------------------------------------------

@pytest.fixture
def rig(env):
    yt, fake, clock, state, opened = env
    svc = yt.svc
    yt = svc.skills["stream_youtube"]  # the one the assistant routes to
    yt.cfg.pop("title_on", None)
    a = Assistant(svc)
    yt.start(SimpleNamespace(assistant=a))
    return a, yt, fake, svc


def test_set_the_title_asks_twitch_and_youtube_once_then_the_yes(rig):
    a, yt, fake, svc = rig
    out = a.handle("set the title to Ranked grind + Q&A", source="text")
    assert out["reply"] == "Twitch and YouTube, or just Twitch?" and a.pending_view() is None
    assert yt.status()["asking"] == {"title": "Ranked grind + Q&A"}
    out = a.handle("both", source="text")
    assert a.pending_view()["text"] == "set the stream title on Twitch and YouTube to “Ranked grind + Q&A”"
    assert yt.setting("title_on") == "both"  # remembered from the answer
    assert svc.twitch.titles == [] and fake.updates == []  # nothing changed before the yes
    out = a.handle("yes", source="text")
    assert out["reply"] == "Title updated on Twitch and YouTube."
    assert svc.twitch.titles == ["Ranked grind + Q&A"]
    snip = fake.updates[-1]["snippet"]
    assert snip["title"] == "Ranked grind + Q&A" and snip["tags"] == ["elden ring"] and snip["categoryId"] == "20"
    assert snip["description"] == "Old desc" and "channelTitle" not in snip
    # the next time: no question, straight to the yes for both
    a.handle("change the stream title to Boss rush", source="text")
    assert a.pending_view()["text"] == "set the stream title on Twitch and YouTube to “Boss rush”"


def test_just_twitch_is_remembered_and_then_its_the_twitch_path(rig):
    a, yt, fake, svc = rig
    a.handle("set the title to chill night", source="text")
    a.handle("just twitch", source="text")
    assert a.pending_view()["text"] == "set the stream title on Twitch to “chill night”"
    a.handle("yes", source="text")
    assert svc.twitch.titles == ["chill night"] and fake.updates == []
    assert yt.setting("title_on") == "twitch"
    intent = route("set the title to Late night", a.router_context())
    assert intent.tool == "twitch_set_channel"  # Twitch-only, as before 2.3


def test_the_hud_buttons_answer_and_change_it_at_once(rig):
    a, yt, fake, svc = rig
    a.handle("set the title to Tonight", source="text")
    out = yt.answer_where("both", from_hud=True)  # the click on "Twitch + YouTube" under the question
    assert out["ok"] and svc.twitch.titles == ["Tonight"] and fake.updates[-1]["snippet"]["title"] == "Tonight"
    assert out["reply"] == "Title updated on Twitch and YouTube."


def test_youtube_only_title_and_description(rig):
    a, yt, fake, svc = rig
    a.handle("set the youtube description to Elden Ring blind run, day 3", source="text")
    assert a.pending_view()["text"].startswith("set the YouTube description to “Elden Ring blind run, day 3”")
    assert a.handle("yes", source="text")["reply"] == "Description updated on YouTube."
    assert fake.updates[-1]["snippet"]["description"] == "Elden Ring blind run, day 3" and svc.twitch.titles == []
    assert fake.updates[-1]["snippet"]["title"] == "Old title"


def test_one_side_failing_is_said_plainly(rig):
    a, yt, fake, svc = rig
    yt.cfg["title_on"] = "both"
    fake.active, fake.upcoming = [], []
    a.handle("set the title to Ranked", source="text")
    reply = a.handle("yes", source="text")["reply"]
    assert reply.startswith("Title updated on Twitch. YouTube didn't take it: There's no YouTube stream to rename yet")


def test_upcoming_broadcast_is_renamed_before_going_live(env):
    yt, fake, clock, state, opened = env
    fake.active, fake.upcoming = [], [{"id": "vid9", "snippet": {"title": "Scheduled", "liveChatId": "c9"}}]
    assert yt.set_youtube("Starting soon <3")["ok"]
    assert fake.updates[-1]["id"] == "vid9" and fake.updates[-1]["snippet"]["title"] == "Starting soon 3"  # no < or >


def test_without_youtube_everything_stays_twitch_only(svc, monkeypatch):
    monkeypatch.setattr("assistant.config.save_setting", lambda cfg, key, value: None)
    ctx = RouterContext(skill_route=Skills(svc, ["stream_youtube"]).route)
    assert route("set the title to Ranked grind", ctx).tool == "twitch_set_channel"
    assert route("set the youtube title to Ranked", ctx).tool == "youtube_live"  # says how to connect, no yes asked
    yt = svc.skills["stream_youtube"]
    assert yt.say("youtube_live", {}, yt.live_status()) == yt_mod.NOT_CONNECTED
    assert yt.title_tool() == "twitch_set_channel"


def test_routes_when_connected(env):
    yt, fake, clock, state, opened = env
    ctx = RouterContext(skill_route=yt.route)
    yt.cfg["title_on"] = "both"
    assert route("titles on twitch only", ctx).args == {"where": "twitch"}
    assert route("put titles on both from now on", ctx).args == {"where": "both"}
    assert route("how's youtube chat", ctx).tool == "youtube_live"
    assert route("set the description to new run", ctx).args == {"description": "new run", "where": "youtube"}
    assert route("both", ctx) is None  # only an answer while the question is open
    assert yt.title_tool() == "stream_title"
