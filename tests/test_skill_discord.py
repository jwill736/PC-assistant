"""Discord skill: webhook setup, posting with rate limits and dedupe, the go-live post, clips, and the yes for posts."""

import json

import httpx
import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import discord as dm

HOOK = "https://discord.com/api/webhooks/123456/abcDEF-token_x"


class Fake:
    def __init__(self):
        self.posts, self.status, self.known = [], 200, True

    def __call__(self, req):
        if req.method == "GET":
            return httpx.Response(200, json={"name": "Vesper", "channel_id": "9", "guild_id": "8"}) if self.known \
                else httpx.Response(404, json={"message": "Unknown Webhook"})
        self.posts.append({"params": dict(req.url.params), **json.loads(req.content)})
        if self.status == 429:
            return httpx.Response(429, json={"message": "You are being rate limited.", "retry_after": 12.4})
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "Cannot send an empty message"})
        return httpx.Response(200, json={"id": f"m{len(self.posts)}"})


@pytest.fixture
def fake():
    return Fake()


def make(svc, fake, now):
    return dm.Discord(svc, http=httpx.Client(transport=httpx.MockTransport(fake)), clock=lambda: now[0])


@pytest.fixture
def dc(svc, fake, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr("assistant.config.save_setting", lambda cfg, key, value: None)
    now = [1_760_000_000.0]
    d = make(svc, fake, now)
    d.now_ = now
    return d


def test_setup(dc, fake, cfg):
    assert dc.status()["next_step"].startswith("In Discord: Server Settings") and not dc.status()["configured"]
    assert "isn't set up" in dc.post("hi")["error"] and not fake.posts
    assert dc.set_webhook("https://example.com/hook")["ok"] is False
    fake.known = False
    assert "deleted" in dc.set_webhook(HOOK)["error"]
    fake.known = True
    assert dc.set_webhook(HOOK + "?wait=true") == {"ok": True, "name": "Vesper"}
    assert f"DISCORD_WEBHOOK_URL={HOOK}" in (cfg.root / ".env").read_text()
    assert dc.status()["hook"]["name"] == "Vesper" and not fake.posts  # checking the link posts nothing


def test_posting_rate_limit_and_dedupe(dc, fake):
    dc.set_webhook(HOOK)
    out = dc.post("Stream at 8 tonight")
    assert out["ok"] and fake.posts[-1] == {"params": {"wait": "true"}, "content": "Stream at 8 tonight",
                                            "allowed_mentions": {"parse": []}}
    assert "seconds ago" in dc.post("Another")["error"]
    dc.now_[0] += 60
    assert dc.post("Stream at 8 tonight")["duplicate"]
    dc.now_[0] += dm.DEDUPE_S
    assert dc.post("Stream at 8 tonight")["ok"]
    dc.now_[0] += 60
    fake.status = 429
    assert "try again in 12 seconds" in dc.post("x")["error"]
    fake.status = 400
    assert "refused" in dc.post("y")["error"]
    assert len(dc.status()["posts"]) == 2


def test_go_live_post_once_per_stream(dc, fake, svc, monkeypatch):
    live = [False]
    svc.live = lambda: live[0]
    dc.set_webhook(HOOK)
    dc.cfg["mention"] = "@everyone"
    svc.bus.publish("twitch", {"live": True, "channel": "jplays", "title": "Ranked grind", "game": "Valorant", "uptime_s": 30},
                    sticky=True)
    assert dc.status()["preview"] == "**jplays is live!** Ranked grind · Valorant\nhttps://twitch.tv/jplays"
    dc.tick()
    assert not fake.posts
    live[0] = True
    dc.tick()
    p = fake.posts[-1]
    assert p["content"] == "@everyone **jplays is live!** Ranked grind · Valorant\nhttps://twitch.tv/jplays"
    assert p["allowed_mentions"] == {"parse": ["everyone"]}
    dc.tick()
    assert len(fake.posts) == 1
    # Vesper restarts mid-stream: no second post
    live[0] = False
    dc.tick()
    again = make(svc, fake, dc.now_)
    again.now_ = dc.now_
    dc.now_[0] += 3600
    live[0] = True
    again.tick()
    assert len(fake.posts) == 1
    # the next stream, a day later
    live[0] = False
    again.tick()
    dc.now_[0] += 86400
    live[0] = True
    again.tick()
    assert len(fake.posts) == 2


def test_go_live_respects_the_switch_and_a_late_start(dc, fake, svc):
    live = [False]
    svc.live = lambda: live[0]
    dc.set_webhook(HOOK)
    dc.cfg["auto_announce"] = False
    live[0] = True
    dc.tick()
    assert not fake.posts
    dc.cfg["auto_announce"] = True
    live[0] = False
    dc.tick()
    svc.bus.publish("twitch", {"live": True, "channel": "jplays", "title": "t", "uptime_s": 3600}, sticky=True)
    live[0] = True
    dc.tick()
    assert not fake.posts  # started Vesper an hour into the stream: too late for "going live"


def test_custom_template_and_mentions(dc, fake, svc):
    dc.set_webhook(HOOK)
    dc.cfg["template"] = "{name} → {title} {link} {unknown}"
    dc.cfg["name"] = "J"
    svc.bus.publish("twitch", {"live": True, "channel": "jplays", "title": "Chill"}, sticky=True)
    assert dc.golive_message() == "J → Chill https://twitch.tv/jplays"
    assert dc.remember("mention", "everyone!")["ok"] is False
    assert dm._mentions("<@&42>") == {"parse": [], "roles": ["42"]}


def test_post_latest_clip(dc, fake, svc):
    dc.set_webhook(HOOK)
    svc.highlights = lambda: []
    svc.twitch = type("T", (), {"enabled": False})()
    assert "make a clip" in dc.post_clip()["error"]

    class Twitch:
        enabled = True
        login_name = "jplays"

        def broadcaster_id(self):
            return "77"

        def _get(self, path, params):
            assert path == "clips" and params["broadcaster_id"] == "77"
            return [{"url": "https://clips.twitch.tv/Old", "title": "old", "created_at": "2026-10-01T00:00:00Z"},
                    {"url": "https://clips.twitch.tv/New", "title": "huge play", "created_at": "2026-10-12T00:00:00Z"}]
    svc.twitch = Twitch()
    out = dc.post_clip()
    assert out["ok"] and fake.posts[-1]["content"] == "huge play\nhttps://clips.twitch.tv/New"
    dc.now_[0] += 60
    svc.highlights = lambda: [{"kind": "clip", "reason": "Twitch clip: the ace", "detail": {"url": "https://clips.twitch.tv/Ace"}}]
    assert dc.post_clip()["clip"] == {"url": "https://clips.twitch.tv/Ace", "title": "the ace"}


def test_discord_down_never_crashes(svc, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)

    def down(req):
        raise httpx.ConnectError("offline")
    d = dm.Discord(svc, http=httpx.Client(transport=httpx.MockTransport(down)))
    assert "Couldn't reach Discord" in d.post("hi")["error"]
    d.tick()
    assert "Couldn't reach Discord" in d.status()["error"]


def test_announcing_by_voice_waits_for_a_yes(dc, fake, svc):
    from assistant.brain.assistant import Assistant

    dc.set_webhook(HOOK)
    sk = Skills(svc, [])
    sk.all["discord"] = dc
    svc.skills = sk
    a = Assistant(svc)
    r = a.handle("announce stream at 8 tonight")
    assert r["kind"] == "pending" and "post to Discord: “Stream at 8 tonight”" in r["reply"] and not fake.posts
    assert a.handle("yes")["reply"] == "Posted to Discord."
    assert fake.posts[-1]["content"] == "Stream at 8 tonight"


@pytest.mark.parametrize("said,tool,args", [
    ("announce stream at 8 tonight", "discord_announce", {"text": "Stream at 8 tonight"}),
    ("post on discord we hit 1000 followers", "discord_announce", {"text": "We hit 1000 followers"}),
    ("say good morning in discord", "discord_announce", {"text": "Good morning"}),
    ("post my clip", "discord_post_clip", {}),
    ("share my latest clip on discord", "discord_post_clip", {}),
])
def test_routes(svc, monkeypatch, said, tool, args):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    sk = Skills(svc, ["discord"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


def test_bare_announce_is_left_alone_without_a_webhook(svc, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    assert Skills(svc, ["discord"]).route("announce dinner is ready") is None
