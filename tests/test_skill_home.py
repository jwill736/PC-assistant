"""Lights & home skill: Elgato Key Lights, Home Assistant, scenes, routes, and the ON AIR light."""

import json

import httpx
import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import home as hm

HA = "http://ha.local:8123"
TOKEN = "t" * 40
STATES = [
    {"entity_id": "light.living_room", "state": "on",
     "attributes": {"friendly_name": "Living Room", "brightness": 128, "color_temp_kelvin": 3000,
                    "supported_color_modes": ["color_temp"]}},
    {"entity_id": "switch.gaming_pc_plug", "state": "on", "attributes": {"friendly_name": "Gaming PC Plug"}},
    {"entity_id": "light.on_air", "state": "off", "attributes": {"friendly_name": "On Air Sign", "supported_color_modes": ["onoff"]}},
    {"entity_id": "scene.movie", "state": "scening", "attributes": {"friendly_name": "Movie"}},
    {"entity_id": "cover.garage_door", "state": "closed", "attributes": {"friendly_name": "Garage Door"}},
    {"entity_id": "sensor.office_temp", "state": "71", "attributes": {"friendly_name": "Office Temperature",
                                                                      "unit_of_measurement": "°F"}},
]


class Fake:
    def __init__(self):
        self.puts, self.services = [], []
        self.light = {"on": 0, "brightness": 20, "temperature": 213}
        self.down = set()  # hosts that don't answer
        self.token_ok = True

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.host in self.down:
            raise httpx.ConnectError("no route to host")
        if req.url.port == 9123:
            if req.method == "PUT":
                body = json.loads(req.content)
                self.puts.append(body["lights"][0])
                self.light.update(body["lights"][0])
            return httpx.Response(200, json={"numberOfLights": 1, "lights": [dict(self.light)]})
        if req.headers.get("authorization") != f"Bearer {TOKEN}" or not self.token_ok:
            return httpx.Response(401, text="401: Unauthorized")
        if req.url.path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if req.url.path == "/api/states":
            return httpx.Response(200, json=STATES)
        if req.url.path.startswith("/api/services/"):
            self.services.append((req.url.path.removeprefix("/api/services/"), json.loads(req.content)))
            return httpx.Response(200, json=[])
        return httpx.Response(404)


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture
def home(svc, fake, monkeypatch):
    monkeypatch.setenv("HA_URL", "")
    monkeypatch.setenv("HA_TOKEN", "")
    monkeypatch.setattr("assistant.config.save_setting", lambda cfg, key, value: None)
    now = [1000.0]
    h = hm.Home(svc, http=httpx.Client(transport=httpx.MockTransport(fake)), clock=lambda: now[0])
    h.now_ = now
    return h


def test_nothing_set_up_explains_the_next_step(home):
    st = home.status()
    assert st["next_step"].startswith("Add your Key Light") and st["lights"] == [] and not st["ha"]["configured"]
    assert "No lights set up yet" in home.set_lights("", on=True)["error"]
    assert home.discover()["ok"] is False  # zeroconf isn't installed here: type the IP instead
    assert home.state("garage")["ok"] is False


def test_key_light_on_brightness_and_warmth(home, fake):
    assert home.add_light("bad ip!")["ok"] is False
    assert home.add_light("10.0.0.5") == {"ok": True, "reachable": True}
    (kl,) = home.lights()
    assert (kl["name"], kl["on"], kl["brightness"], kl["kelvin"]) == ("Key Light", False, 20, 4700)
    out = home.set_lights("key light", on=True)
    assert fake.puts[-1] == {"on": 1} and home.say("lights_on", {}, out) == "Key Light on."
    out = home.set_lights("", brightness=50)
    assert fake.puts[-1] == {"on": 1, "brightness": 50} and home.say("lights_brightness", {}, out) == "Key Light at 50 percent."
    out = home.set_lights("", warmer=True)
    assert fake.puts[-1] == {"on": 1, "temperature": hm.k_to_mired(4100)}
    assert home.say("lights_temperature", {"warmer": True}, out) == "Warmer: Key Light at 4100 K."
    assert home.set_lights("", change=-80)["brightness"] == 1  # never below 1
    assert home.set_lights("bedroom")["error"] == "I don't know a light called bedroom."


def test_home_assistant_connect_and_control(home, fake, cfg):
    assert home.set_ha("homeassistant", TOKEN)["ok"] is False
    fake.token_ok = False
    assert "refused" in home.set_ha(HA, TOKEN)["error"]
    fake.token_ok = True
    assert home.set_ha(HA + "/", TOKEN) == {"ok": True, "reachable": True}
    env = (cfg.root / ".env").read_text()
    assert f"HA_URL={HA}\n" in env and f"HA_TOKEN={TOKEN}" in env
    ids = {x["id"]: x for x in home.lights()}
    assert ids["light.living_room"]["brightness"] == 50 and ids["light.living_room"]["dimmable"]
    assert not ids["light.on_air"]["dimmable"] and "cover.garage_door" not in ids
    out = home.set_lights("living room lights", on=False)
    assert fake.services[-1] == ("homeassistant/turn_off", {"entity_id": "light.living_room"})
    assert home.say("lights_off", {}, out) == "Living Room off."
    home.set_lights("living room", brightness=30, kelvin=2700)
    assert fake.services[-1] == ("light/turn_on", {"entity_id": "light.living_room", "brightness_pct": 30,
                                                   "color_temp_kelvin": 2700})
    out = home.scene("movie scene")
    assert fake.services[-1] == ("scene/turn_on", {"entity_id": "scene.movie"})
    assert home.say("home_scene", {}, out) == "Movie scene on."
    assert home.scene("disco")["ok"] is False
    st = home.state("office temperature")
    assert home.say("home_state", {}, st) == "Office Temperature is 71 °F."
    assert home.state("garage door")["state"] == "closed"


def test_presets_work_without_home_assistant(home, fake):
    home.add_light("10.0.0.5")
    home.cfg["presets"] = {"chill": {"brightness": 15, "kelvin": 3000}}
    out = home.scene("chill")
    assert out["ok"] and fake.puts[-1] == {"on": 1, "brightness": 15, "temperature": hm.k_to_mired(3000)}


def test_services_down_never_crash(home, fake):
    home.add_light("10.0.0.5")
    home.set_ha(HA, TOKEN)
    fake.down |= {"10.0.0.5", "ha.local"}
    home.now_[0] += 60
    home.tick()
    st = home.status()
    assert "isn't answering" in st["ha"]["error"] and any("10.0.0.5" in e for e in st["errors"])
    out = home.set_lights("key light", on=True)
    assert out["ok"] is False and "isn't answering" in out["error"]


def test_on_air_light_follows_the_stream(home, fake, svc):
    live = [False]
    svc.live = lambda: live[0]
    home.add_light("10.0.0.5")
    home.set_ha(HA, TOKEN)
    assert home.set_on_air_config({"entity": "light.on_air", "brightness": "40", "kelvin": "5600"})["ok"]
    home.tick()
    assert not fake.services and not home.on_air
    live[0] = True
    home.tick()
    assert home.on_air and fake.puts[-1] == {"on": 1, "brightness": 40, "temperature": hm.k_to_mired(5600)}
    assert fake.services[-1] == ("homeassistant/turn_on", {"entity_id": "light.on_air"})
    assert home.status()["on_air"]["on"] and home.status()["on_air"]["entity"] == "light.on_air"
    home.tick()
    assert len([s for s in fake.services if s[0] == "homeassistant/turn_on"]) == 1  # only on the change
    live[0] = False
    home.tick()
    assert not home.on_air and fake.services[-1] == ("homeassistant/turn_off", {"entity_id": "light.on_air"})
    assert fake.puts[-1] == {"on": 0, "brightness": 20, "temperature": hm.k_to_mired(hm.mired_to_k(213))}  # as it was


@pytest.mark.parametrize("said,tool,args", [
    ("turn on my key light", "lights_on", {"target": "key light"}),
    ("turn off the living room lights", "lights_off", {"target": "living room lights"}),
    ("key light off", "lights_off", {"target": "key light"}),
    ("lights to 50 percent", "lights_brightness", {"target": "lights", "level": 50}),
    ("set the key light to forty", "lights_brightness", {"target": "key light", "level": 40}),
    ("dim the lights", "lights_brightness", {"target": "lights", "change": -20}),
    ("warmer lights", "lights_temperature", {"target": "lights", "warmer": True}),
    ("make the lights cooler", "lights_temperature", {"target": "lights", "warmer": False}),
    ("lights to 3200k", "lights_temperature", {"target": "lights", "kelvin": 3200}),
    ("movie scene", "home_scene", {"name": "movie"}),
    ("turn on the on air sign", "lights_on", {"target": "on air sign"}),
    ("is the garage door open", "home_state", {"entity": "garage door"}),
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["home"])
    sk["home"]._states = STATES
    sk["home"].ha_states = lambda fresh=False: STATES
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


@pytest.mark.parametrize("said", ["turn off the pc", "dim the screen", "switch to the gaming scene", "turn on dark mode",
                                  "start work mode"])
def test_routes_leave_other_things_alone(svc, said):
    sk = Skills(svc, ["home"])
    sk["home"]._states = STATES
    sk["home"].ha_states = lambda fresh=False: STATES
    intent = route(said, RouterContext(skill_route=sk.route))
    assert intent is None or not intent.tool or not intent.tool.startswith(("lights_", "home_"))
