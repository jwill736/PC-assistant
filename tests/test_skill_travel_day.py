"""Travel day: leave-by from the airport buffer and the drive (setting, OSRM route, or a guess), check-in links, the
weather where you're going, a packing list that's kept, the "leave by" nudge, voice, and "what's the weather in Denver".
Every network call is faked."""

from datetime import datetime

import httpx
import pytest
from test_google import CLIENT_ID, SECRET, connect
from test_skill_travel import NY, inbox

from assistant.brain.router import RouterContext, route
from assistant.integrations.gmail import Gmail
from assistant.integrations.google import GoogleAccounts
from assistant.skills import Skills
from assistant.skills import travel as tv

EARLY = datetime(2026, 10, 16, 3, 0, tzinfo=NY)  # Friday 3 AM: DL 1234 ATL→AUS leaves at 7:05
HOME = {"name": "Atlanta, GA", "lat": 33.749, "lon": -84.388}


def forecast(start_day=16, lows=(45, 52, 55), rain=(70, 10, 0), codes=(61, 2, 0), highs=(70, 74, 76)):
    days = [f"2026-10-{start_day + i:02d}" for i in range(7)]
    pad = lambda xs, v: list(xs) + [v] * (7 - len(xs))  # noqa: E731
    return {"current": {"time": f"{days[0]}T09:00", "temperature_2m": 58, "apparent_temperature": 56,
                        "relative_humidity_2m": 60, "weather_code": codes[0], "wind_speed_10m": 5, "is_day": 1},
            "hourly": {"time": [f"{days[0]}T{h:02d}:00" for h in range(24)], "temperature_2m": [58] * 24,
                       "precipitation_probability": [0] * 24, "weather_code": [2] * 24},
            "daily": {"time": days, "weather_code": pad(codes, 0), "temperature_2m_max": pad(highs, 75),
                      "temperature_2m_min": pad(lows, 55), "precipitation_probability_max": pad(rain, 0),
                      "sunrise": [f"{days[0]}T07:30"] * 7, "sunset": [f"{days[0]}T19:00"] * 7, "uv_index_max": [4] * 7}}


class Net:
    """The outside world: Open-Meteo forecast + geocoding and the OSRM router."""

    def __init__(self):
        self.calls, self.route_down, self.weather_down = [], False, False
        self.fc = forecast()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        host = req.url.host
        self.calls.append(host)
        if "osrm" in host:
            if self.route_down:
                raise httpx.ConnectError("blocked")
            return httpx.Response(200, json={"code": "Ok", "routes": [{"duration": 1500, "distance": 21000}]})
        if "geocoding" in host:
            name = req.url.params["name"].lower()
            if name == "boulder":
                return httpx.Response(200, json={"results": [{"name": "Boulder", "admin1": "Colorado", "country_code": "US",
                                                              "latitude": 40.015, "longitude": -105.27}]})
            return httpx.Response(200, json={"generationtime_ms": 0.1})
        if "api.open-meteo.com" in host:
            if self.weather_down:
                return httpx.Response(503)
            return httpx.Response(200, json=self.fc)
        return httpx.Response(404)


class Clock:
    t = EARLY.timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def net():
    return Net()


@pytest.fixture
def travel(svc, tmp_path, net):
    from test_gmail import FakeGmail

    fake = FakeGmail()
    fake.mail = inbox()
    acc = GoogleAccounts(CLIENT_ID, SECRET, tmp_path / "t.json", http=httpx.Client(transport=httpx.MockTransport(fake)))
    connect(acc, fake, "code-work")
    connect(acc, fake, "code-home")
    svc.gmail = Gmail(acc)
    svc.cfg["skills"]["weather"] = {"place": HOME}
    t = tv.Travel(svc, clock=Clock(), http=httpx.Client(transport=httpx.MockTransport(net)))
    t.nudged = []
    t.nudge = lambda text, **kw: t.nudged.append((text, kw.get("priority")))
    t.scan()
    return t


def at(day, h, m=0):
    return datetime(2026, 10, day, h, m, tzinfo=NY).timestamp()


# ---- leave-by --------------------------------------------------------------------------------------------------

def test_travel_day_card(travel, net):
    d = travel.travel_day()
    assert (d["flight"], d["from"], d["to"], d["dest"], d["today"], d["when"]) == ("DL 1234", "Atlanta", "Austin", "Austin", True, "today")
    assert (d["buffer_min"], d["drive_min"], d["drive_from"], d["international"]) == (120, 30, "route", False)
    assert d["leave_by"] == "2026-10-16T04:35:00-04:00" and d["leave_clock"] == "4:35 AM" and d["leave_in_s"] == 95 * 60
    assert d["checkin_url"] == "https://www.delta.com/mytrips/" and d["checkin_open"] and d["nights"] == 2
    w = d["weather"]
    assert w["place"] == "Austin" and [x["date"] for x in w["days"]] == ["2026-10-16", "2026-10-17", "2026-10-18"]
    items = [p["item"] for p in d["packing"]]
    assert items[:2] == ["ID & wallet", "Phone charger"] and "Clothes for 3 days" in items
    assert "Umbrella" in items and "Jacket" in items and "Sunscreen" not in items and "Passport" not in items
    assert "template" not in d
    osrm = net.calls.count("router.project-osrm.org")
    travel.travel_day()  # the route is kept (30 days) and the forecast for an hour: no new calls
    assert net.calls.count("router.project-osrm.org") == osrm == 1
    assert net.calls.count("api.open-meteo.com") == 1


def test_no_card_until_the_day_before(travel):
    travel.clock.t = at(13, 9)
    assert travel.travel_day() is None and travel.status()["travel_day"] is None
    travel.clock.t = at(15, 6)
    d = travel.travel_day()
    assert d and not d["today"] and d["when"] == "tomorrow" and not d["checkin_open"]


def test_drive_from_setting_guess_and_blocked_router(travel, net):
    travel.cfg["drive_min"] = 50
    assert travel.drive(travel.resolve("ATL")) == (50, "setting")
    del travel.cfg["drive_min"]
    net.route_down = True
    assert travel.drive(travel.resolve("ATL")) == (45, "guess")
    travel.drive(travel.resolve("ATL"))
    assert net.calls.count("router.project-osrm.org") == 1  # blocked: not asked again for an hour
    travel.clock.t += 3700
    net.route_down = False
    assert travel.drive(travel.resolve("ATL")) == (30, "route")
    travel.svc.cfg["skills"]["weather"] = {}
    assert travel.drive(travel.resolve("ATL")) == (45, "guess")  # no home known


def test_flying_home_from_away_and_international(travel):
    back = next(f for f in travel.trips()["flights"] if f["flight"] == "DL 2290")
    p = travel.plan(back)
    assert (p["drive_min"], p["drive_from"], p["dest"]) == (30, "away", "Atlanta")
    f = {**back, "key": "flight:BA226:2026-10-22", "flight": "BA 226", "airline": "British Airways", "from": "ATL",
         "to": "LHR", "date": "2026-10-22", "time": "18:00", "depart": "2026-10-22T18:00:00-04:00", "clock": "6:00 PM"}
    p = travel.plan(f, flights=[f])
    assert p["international"] and p["buffer_min"] == 180 and p["leave_clock"] == "2:30 PM" and p["nights"] is None
    assert p["checkin_url"].startswith("https://www.britishairways.com/")
    assert "Passport" in p["template"] and "Power adapter" in p["template"] and "ID & wallet" not in p["template"]
    travel.cfg["buffer_international_min"] = 150
    assert travel.plan(f, flights=[f])["buffer_min"] == 150


def test_weather_down_still_plans(travel, net):
    net.weather_down = True
    d = travel.travel_day()
    assert d["weather"] is None and d["leave_clock"] == "4:35 AM" and "Umbrella" not in [p["item"] for p in d["packing"]]


def test_packing_template_rules():
    hot = {"unit": "°F", "days": [{"high": 92, "low": 75, "rain": 0, "icon": "sun"}]}
    snow = {"unit": "°C", "days": [{"high": 1, "low": -6, "rain": 20, "icon": "snow"}]}
    assert {"Sunscreen", "Sunglasses"} <= set(tv.packing_template(1, weather=hot)) and "Jacket" not in tv.packing_template(1, weather=hot)
    s = tv.packing_template(6, weather=snow, extra=["Switch", " "], laptop=False)
    assert {"Jacket", "Gloves & hat", "Warm boots", "Laundry bag", "Switch"} <= set(s) and "Laptop + charger" not in s
    assert "Clothes for 7 days" in s and "Underwear & socks × 7" in s
    assert "Clothes for a week" in tv.packing_template(9)


# ---- the packing list is kept ------------------------------------------------------------------------------

def test_packing_tick_add_remove_persist(travel):
    key = travel.travel_day()["key"]
    act = travel.actions()
    assert act["pack_toggle"]({"trip": key, "item": "Umbrella"})["ok"]
    assert act["pack_remove"]({"trip": key, "item": "Headphones"})["ok"]
    out = act["pack_add"]({"trip": key, "text": "  Switch +   dock "})
    assert out["ok"] and out["packing"][-1] == {"item": "Switch + dock", "done": False, "auto": False}
    assert act["pack_add"]({"trip": key, "text": ""}) == {"ok": False, "error": "Which item?"}
    d = travel.travel_day()  # rebuilt: the removed item stays removed, the tick and the added item stay
    items = {p["item"]: p for p in d["packing"]}
    assert "Headphones" not in items and items["Umbrella"]["done"] and "Switch + dock" in items
    act["pack_add"]({"trip": key, "item": "Headphones"})  # added back by hand
    assert "Headphones" in [p["item"] for p in travel.travel_day()["packing"]]


# ---- nudges, status, voice ---------------------------------------------------------------------------------

def test_leave_by_nudge_30_minutes_before(travel):
    travel.tick()
    assert not any(p == "urgent" for _, p in travel.nudged)
    st = travel.status()["travel_day"]
    assert st["leave_clock"] == "4:35 AM" and st["packing"]
    travel.clock.t = at(16, 4, 10)
    travel.tick()
    assert ("Leave by 4:35 AM for DL 1234.", "urgent") in travel.nudged
    travel.clock.t = at(16, 4, 40)  # already past: no new nudge
    travel.nudged.clear()
    travel.tick()
    assert not any(p == "urgent" for _, p in travel.nudged)


def test_say_leave(travel, svc):
    r = travel._leave()
    assert travel.say("leave_for_flight", {}, r) == ("Leave by 4:35 AM for DL 1234 at 7:05 AM: 2 hours at the airport and "
                                                     "about 30 minutes' drive. That's in 1 hour 35 minutes.")
    travel.clock.t = at(15, 8)
    said = travel.say("leave_for_flight", {}, travel._leave())
    assert said.startswith("Tomorrow, leave by 4:35 AM for DL 1234") and "That's in" not in said
    travel.clock.t = at(16, 5)
    assert travel.say("leave_for_flight", {}, travel._leave()).startswith("You should have left at 4:35 AM")
    svc.live = lambda: True
    assert travel.say("leave_for_flight", {}, travel._leave()) == "Your travel times are on the HUD."
    svc.live = None
    travel.clock.t = at(25, 9)
    assert travel.say("leave_for_flight", {}, travel._leave()) == "I don't see a flight coming up in your email."
    net_guess = {"plan": {**r["plan"], "drive_from": "guess", "drive_min": 45}}
    assert "a 45-minute drive I guessed" in travel.say("leave_for_flight", {}, net_guess)


def test_weather_in_a_city(travel, net):
    r = travel.weather_in("denver")
    assert r["ok"] and r["place"] == "Denver" and "geocoding-api.open-meteo.com" not in net.calls  # a known airport city
    assert travel.say("weather_in", {}, r).startswith("58°F and light rain in Denver. Today 45 to 70")
    b = travel.weather_in("Boulder")
    assert b["ok"] and b["place"].startswith("Boulder")
    travel.weather_in("Boulder")
    assert net.calls.count("geocoding-api.open-meteo.com") == 1  # remembered
    assert travel.weather_in("Xyzzy") == {"ok": False, "error": "I can't find a place called Xyzzy."}


@pytest.mark.parametrize("said,tool,args", [
    ("when do I need to leave for my flight", "leave_for_flight", {}),
    ("what time should I leave for the airport", "leave_for_flight", {}),
    ("when should I head to the airport", "leave_for_flight", {}),
    ("when do I have to leave for the airport", "leave_for_flight", {}),
    ("what's the weather in Denver", "weather_in", {"place": "denver"}),
    ("what is the weather like in New York tomorrow", "weather_in", {"place": "new york"}),
    ("is it raining in Seattle", "weather_in", {"place": "seattle"}),
    ("what's the weather", "get_weather", {"day": "today"}),
    ("what's the weather tomorrow", "get_weather", {"day": "tomorrow"}),
    ("what's the weather for the weekend", "get_weather", {"day": "weekend"}),
])
def test_routes(svc, said, tool, args):
    i = route(said, RouterContext(skill_route=Skills(svc, ["weather", "travel"]).route))
    assert i is not None and (i.tool, i.args) == (tool, args)
