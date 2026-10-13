"""YouTube skill: channel stats with a key, latest videos, daily snapshots for growth, quota-friendly caching."""

import httpx
import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import youtube as yt

KEY = "AIza" + "x" * 35
CHANNEL = {"items": [{"id": "UC" + "a" * 22, "snippet": {"title": "J Plays", "customUrl": "@jplays",
                                                         "thumbnails": {"default": {"url": "thumb"}}},
                      "statistics": {"subscriberCount": "1200", "viewCount": "48210", "videoCount": "31",
                                     "hiddenSubscriberCount": False},
                      "contentDetails": {"relatedPlaylists": {"uploads": "UU" + "a" * 22}}}]}
VIDEOS = {"items": [{"id": "v1", "snippet": {"title": "Clutch 1v4", "publishedAt": "2026-10-12T18:00:00Z",
                                             "thumbnails": {"medium": {"url": "m1"}}},
                     "statistics": {"viewCount": "1203", "likeCount": "87", "commentCount": "12"}},
                    {"id": "v2", "snippet": {"title": "Older"}, "statistics": {"viewCount": "500"}}]}


class Fake:
    def __init__(self):
        self.calls = []
        self.subs = 1200
        self.error = None

    def __call__(self, req):
        self.calls.append((req.url.path.rsplit("/", 1)[-1], dict(req.url.params)))
        if self.error:
            return httpx.Response(403, json={"error": {"code": 403, "message": "quota", "errors": [{"reason": self.error}]}})
        p = req.url.path
        if p.endswith("/channels"):
            if req.url.params.get("forHandle") == "@nobody":
                return httpx.Response(200, json={"items": []})
            ch = {**CHANNEL["items"][0], "statistics": {**CHANNEL["items"][0]["statistics"], "subscriberCount": str(self.subs)}}
            return httpx.Response(200, json={"items": [ch]})
        if p.endswith("/playlistItems"):
            return httpx.Response(200, json={"items": [{"contentDetails": {"videoId": "v1"}}, {"contentDetails": {"videoId": "v2"}}]})
        return httpx.Response(200, json=VIDEOS)


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture
def ytb(svc, fake, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "")
    monkeypatch.setattr("assistant.config.save_setting", lambda cfg, key, value: None)
    now = [1_760_000_000.0]
    y = yt.YouTube(svc, http=httpx.Client(transport=httpx.MockTransport(fake)), clock=lambda: now[0])
    y.now_ = now
    return y


def test_parse_channel():
    assert yt.parse_channel("@jplays") == ("handle", "@jplays")
    assert yt.parse_channel("jplays") == ("handle", "@jplays")
    assert yt.parse_channel("https://www.youtube.com/@jplays/videos") == ("handle", "@jplays")
    assert yt.parse_channel("https://youtube.com/channel/UC" + "b" * 22) == ("id", "UC" + "b" * 22)
    assert yt.parse_channel("not a channel!") is None


def test_setup_steps_and_secrets(ytb, cfg, fake):
    assert ytb.stats()["error"].startswith("Make an API key") and not fake.calls
    assert ytb.status()["next_step"].startswith("Make an API key")
    assert ytb.setup(key="nope")["ok"] is False
    out = ytb.setup(key=KEY)
    assert out["ok"] is False and out["error"].startswith("Paste your channel")
    assert f"YOUTUBE_API_KEY={KEY}" in (cfg.root / ".env").read_text()
    assert ytb.setup(channel="???")["ok"] is False
    d = ytb.setup(channel="@jplays")
    assert d["ok"] and d["channel"] == "J Plays" and d["subs"] == 1200 and d["views"] == 48210
    assert fake.calls[0] == ("channels", {"part": "snippet,statistics,contentDetails", "key": KEY, "forHandle": "@jplays"})
    assert d["latest"][0] == {"id": "v1", "title": "Clutch 1v4", "published": "2026-10-12T18:00:00Z", "thumb": "m1",
                              "views": 1203, "likes": 87, "comments": 12, "url": "https://youtu.be/v1"}
    assert d["latest"][1]["likes"] is None


def test_cache_respects_quota(ytb, fake):
    ytb.setup(key=KEY, channel="@jplays")
    n = len(fake.calls)
    assert n == 3  # channels + uploads + videos: 3 units
    ytb.stats()
    ytb.stats(fresh=True)  # a click within a minute: still the cache
    assert len(fake.calls) == n
    ytb.now_[0] += 120
    ytb.stats()
    assert len(fake.calls) == n
    ytb.now_[0] += yt.CACHE_S
    ytb.stats()
    assert len(fake.calls) == 2 * n


def test_daily_snapshots_give_growth(ytb, fake):
    ytb.setup(key=KEY, channel="@jplays")
    d = ytb.stats()
    assert d["week"] is None and len(d["history"]) == 1
    for _ in range(8):  # a week passes, +10 a day
        ytb.now_[0] += 86400
        fake.subs += 10
        d = ytb.stats()
    assert d["subs"] == 1280 and d["week"]["subs"] == 70 and d["day"]["subs"] == 10 and len(d["history"]) == 9
    assert ytb.say("youtube_stats", {}, d) == "1,280 subscribers on YouTube, up 70 this week. 48,210 views in all."
    assert ytb.briefing() == {"subs": 1280, "week": 70}


def test_growth_since_the_first_snapshot_when_younger_than_a_week(ytb, fake):
    ytb.setup(key=KEY, channel="@jplays")
    ytb.now_[0] += 2 * 86400
    fake.subs = 1190
    d = ytb.stats()
    assert d["week"]["subs"] == -10
    assert "down 10 this week" in ytb.say("youtube_stats", {}, d)


def test_latest_and_errors(ytb, fake):
    ytb.setup(key=KEY, channel="@jplays")
    out = ytb.latest(1)
    assert len(out["videos"]) == 1 and ytb.say("youtube_latest", {}, out) == "Your latest, “Clutch 1v4”, has 1,203 views and 87 likes."
    fake.error = "quotaExceeded"
    ytb.now_[0] += yt.CACHE_S + 1
    out = ytb.stats()
    assert out["ok"] is False and "quota is used up" in out["error"]
    st = ytb.status()
    assert st["stale"] and st["subs"] == 1200  # the last numbers stay on the HUD
    fake.error = None
    assert "no channel @nobody" in ytb.setup(channel="@nobody")["error"]


def test_service_down(svc, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)

    def down(req):
        raise httpx.ConnectTimeout("slow")
    y = yt.YouTube(svc, http=httpx.Client(transport=httpx.MockTransport(down)))
    y.cfg["channel"] = "@jplays"
    assert "didn't answer" in y.stats()["error"]
    y.tick()
    assert y.status()["ok"] is False and y.briefing() is None


def test_hidden_subscriber_count(ytb, fake, monkeypatch):
    ytb.setup(key=KEY, channel="UC" + "a" * 22)
    assert fake.calls[0][1]["id"] == "UC" + "a" * 22
    d = {**ytb.stats(), "subs": None}
    assert ytb.say("youtube_stats", {}, d).startswith("Your subscriber count is hidden")


@pytest.mark.parametrize("said,tool", [
    ("how's my youtube", "youtube_stats"),
    ("how is my youtube channel doing", "youtube_stats"),
    ("how many subscribers do i have on youtube", "youtube_stats"),
    ("youtube stats", "youtube_stats"),
    ("how's my latest video doing", "youtube_latest"),
    ("my latest youtube videos", "youtube_latest"),
])
def test_routes(svc, said, tool):
    sk = Skills(svc, ["youtube"])
    assert route(said, RouterContext(skill_route=sk.route)).tool == tool


def test_other_youtube_phrases_stay_generic(svc):
    sk = Skills(svc, ["youtube"])
    assert route("youtube lofi beats", RouterContext(skill_route=sk.route)).tool == "web_search"
    assert sk.route("how many subscribers do i have") is None  # could be Twitch
