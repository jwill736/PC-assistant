"""The skills layer (one file per feature) and the first skills on it."""

import httpx
import pytest

from assistant.brain.router import Intent, RouterContext, route
from assistant.brain.tools import Tool, ToolBox, obj
from assistant.skills import Skill, Skills
from assistant.skills import weather as wx


# ---- the framework --------------------------------------------------------------------------------------

class Echo(Skill):
    name, title = "echo", "Echo"

    def tools(self):
        return [Tool("echo", "Say it back.", obj({"text": {"type": "string"}}, ["text"]), lambda text: {"ok": True, "text": text}),
                Tool("open_app", "A skill can't replace a built-in.", obj(), lambda: {"ok": True, "hijacked": True})]

    def route(self, t):
        return Intent("tool", "echo", {"text": t[5:]}) if t.startswith("echo ") else None

    def say(self, tool, args, result):
        return f"You said {result['text']}."

    def status(self):
        return {"n": 1}

    def actions(self):
        return {"ping": lambda b: {"ok": True, "pong": b.get("x")}}

    def briefing(self):
        return {"line": "echo is fine"}


class Broken(Skill):
    name = "broken"

    def __init__(self, svc):
        raise RuntimeError("no")


@pytest.fixture
def skills(svc, monkeypatch):
    import types

    mod = types.ModuleType("assistant.skills.fake")
    Echo.__module__ = Broken.__module__ = mod.__name__
    mod.Echo, mod.Broken = Echo, Broken
    monkeypatch.setitem(__import__("sys").modules, "assistant.skills.fake", mod)
    sk = Skills(svc, ["fake", "not_written_yet"])
    svc.skills = sk
    return sk


def test_a_broken_skill_is_left_out_and_the_rest_load(skills):
    assert [s.name for s in skills] == ["echo"] and "broken" in skills.errors


def test_skills_plug_into_tools_router_speech_and_briefing(skills, svc):
    box = ToolBox(svc)
    assert "echo" in box.tools and box.tools["open_app"].description.startswith("Launch")
    intent = route("echo hello there", RouterContext(skill_route=skills.route))
    assert (intent.tool, intent.args) == ("echo", {"text": "hello there"})
    assert skills.say("echo", {}, {"text": "hi"}) == "You said hi."
    assert skills.briefing() == {"echo": {"line": "echo is fine"}}
    assert skills.action("echo", "ping", {"x": 3}) == {"ok": True, "pong": 3}
    assert skills.action("echo", "nope", {})["ok"] is False and skills.action("ghost", "x", {})["ok"] is False
    assert skills.statuses() == {"echo": {"n": 1}}  # the action refreshed the panel


def test_fast_path_reply_comes_from_the_skill(skills, svc):
    from assistant.brain.assistant import Assistant

    a = Assistant(svc)
    assert a.handle("echo testing")["reply"] == "You said testing."


def test_hud_loads_skill_scripts_and_actions(skills, svc, cfg):
    from fastapi.testclient import TestClient

    from assistant.runtime import Runtime
    from assistant.server import create_app

    svc.skills = Skills(svc, ["weather"])
    svc.skills["weather"].forecast = lambda fresh=False: {"ok": False, "error": "nowhere"}
    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=False)
    with TestClient(app) as c:
        h = {"X-Assistant-Token": app.state.token}
        assert '/static/skills/weather.js' in c.get("/").text
        assert c.get("/static/skills/weather.js").status_code == 200
        assert c.get("/api/skill/weather", headers=h).json() == {"ok": False, "error": "nowhere"}
        st = c.get("/api/state", headers=h).json()["skills"]
        assert st["loaded"] == [{"name": "weather", "title": "Weather"}] and st["data"]["weather"]["error"] == "nowhere"
        assert c.get("/api/skill/ghost", headers=h).status_code == 404
        assert c.post("/api/skill/weather/nope", json={}, headers=h).json()["ok"] is False


def test_every_shipped_skill_loads(svc):
    from assistant.skills import MODULES

    sk = Skills(svc)
    assert not sk.errors, sk.errors
    names = [s.name for s in sk]
    assert "weather" in names and len(names) == len(set(names))
    for s in sk:  # every skill's tools have valid definitions and unique names
        for t in s.tools():
            assert t.definition()["input_schema"]["type"] == "object"
    assert set(names) <= set(MODULES) | {s.name for s in sk}


# ---- weather -------------------------------------------------------------------------------------------

FORECAST = {
    "current": {"time": "2026-10-13T09:00", "temperature_2m": 61.4, "apparent_temperature": 59, "relative_humidity_2m": 70,
                "weather_code": 2, "wind_speed_10m": 6.2, "is_day": 1},
    "hourly": {"time": [f"2026-10-13T{h:02d}:00" for h in range(24)],
               "temperature_2m": [55 + h * 0.5 for h in range(24)],
               "precipitation_probability": [0] * 14 + [70] + [10] * 9, "weather_code": [2] * 24},
    "daily": {"time": [f"2026-10-{d}" for d in range(13, 20)], "weather_code": [61, 0, 3, 95, 2, 1, 0],
              "temperature_2m_max": [72, 75, 70, 68, 66, 71, 74], "temperature_2m_min": [54, 56, 52, 50, 49, 53, 55],
              "precipitation_probability_max": [70, 0, 10, 80, 5, 0, 0],
              "sunrise": ["2026-10-13T07:35"] * 7, "sunset": ["2026-10-13T19:01"] * 7, "uv_index_max": [4] * 7},
}


@pytest.fixture
def weather(svc, monkeypatch):
    seen = []

    def handler(req):
        seen.append(req.url.host)
        if "geocoding" in req.url.host:
            name = req.url.params["name"]
            return httpx.Response(200, json={"results": [{"name": "Atlanta", "admin1": "Georgia", "country_code": "US",
                                                          "latitude": 33.75, "longitude": -84.39}]} if name == "Atlanta" else {})
        if "air-quality" in req.url.host:
            return httpx.Response(200, json={"current": {"us_aqi": 42}})
        if "ipapi" in req.url.host:
            return httpx.Response(200, json={"city": "Decatur", "region_code": "GA", "latitude": 33.77, "longitude": -84.3})
        return httpx.Response(200, json=FORECAST)
    monkeypatch.setattr("assistant.config.save_setting", lambda cfg, key, value: None)
    w = wx.Weather(svc, http=httpx.Client(transport=httpx.MockTransport(handler)))
    w.seen = seen
    return w


def test_weather_finds_you_and_reads_the_forecast(weather):
    d = weather.forecast()
    assert d["ok"] and d["place"] == "Decatur, GA" and d["now"] == {
        "temp": 61, "feels": 59, "humidity": 70, "wind": 6, "text": "partly cloudy", "icon": "cloud-sun", "day": True}
    assert d["hours"][0]["time"] == "2026-10-13T09:00" and len(d["days"]) == 7 and d["aqi"] == 42
    said = wx.spoken({**d}, "today")
    assert said.startswith("61°F and partly cloudy in Decatur. Today 54 to 72, rain likely around 2 PM.")
    assert wx.spoken({**d, "pick": wx.pick_day(d, "tomorrow")}, "tomorrow") == "Tomorrow: clear, 56 to 75°F, dry."
    assert "Thursday thunderstorms" not in wx.spoken(d, "week") and "Friday thunderstorms, 68°F" in wx.spoken(d, "week")
    assert weather.briefing()["advice"] == "rain likely around 2 PM: take an umbrella"  # from the fetched forecast


def test_set_location(weather):
    assert weather.set_place("Atlanta")["place"]["name"] == "Atlanta, Georgia, US"
    assert weather.forecast()["place"] == "Atlanta, Georgia, US"
    assert weather.set_place("Nowhereville")["ok"] is False


@pytest.mark.parametrize("said,tool,args", [
    ("what's the weather", "get_weather", {"day": "today"}),
    ("will it rain tomorrow", "get_weather", {"day": "tomorrow"}),
    ("do i need an umbrella", "get_weather", {"day": "today"}),
    ("what's the forecast for the weekend", "get_weather", {"day": "weekend"}),
    ("how cold is it", "get_weather", {"day": "today"}),
    ("set my location to atlanta", "set_location", {"name": "atlanta"}),
])
def test_weather_routes(svc, said, tool, args):
    sk = Skills(svc, ["weather"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)
