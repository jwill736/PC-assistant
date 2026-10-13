"""Lights & home: Elgato Key Lights on your network, Home Assistant (lights, switches, scenes), and an ON AIR light.

- Key Lights: their own little web server (http://<ip>:9123/elgato/lights). Add each one's IP on the HUD, or click
  Find (only if the optional ``zeroconf`` package is installed). Kept in skills.home.key_lights.
- Home Assistant: its address and a long-lived token (HA → your profile → Security) in .env as HA_URL / HA_TOKEN,
  pasted on the HUD. Lights, switches and scenes come from /api/states; changes go to /api/services/<domain>/<svc>.
- ON AIR: when the stream starts, turn on skills.home.on_air.entity (any HA entity) and/or put the Key Lights on a
  preset (on_air.key_light: {brightness, kelvin}); when it ends, turn it off and put the lights back how they were.

"Turn on my key light", "lights to 50 percent", "warmer lights", "movie scene", "turn off the living room lights".
"""

from __future__ import annotations

import re
import threading
import time

import httpx

from ..brain.router import NUMBER, Intent, number
from ..brain.tools import Tool, obj
from . import Skill
from ._kit import LiveEdge, save_secret, save_setting

PORT = 9123
DOMAINS = ("light", "switch", "input_boolean", "fan")
STOP = {"the", "my", "a", "all", "light", "lights", "lamp", "lamps", "please"}
LIGHTISH = r"\b(?:lights?|lamps?|key ?lights?)"


def k_to_mired(k: float) -> int:
    return max(143, min(344, round(1_000_000 / max(1, k))))


def mired_to_k(m: float) -> int:
    return int(round(1_000_000 / max(1, m) / 50) * 50)


def _words(text: str) -> set[str]:
    return {w.rstrip("s") if len(w) > 3 else w for w in re.findall(r"[a-z0-9]+", text.lower().replace("_", " "))} - STOP


class HomeError(Exception):
    pass


class Home(Skill):
    name, title = "home", "Lights & home"
    poll_s = 10
    delay_s = 8

    def __init__(self, svc, http: httpx.Client | None = None, clock=time.time):
        super().__init__(svc)
        self.http = http or httpx.Client(timeout=4, headers={"Connection": "close"})  # Key Light firmware is fragile
        self.clock = clock
        self._lock = threading.RLock()
        self._states: list[dict] = []  # Home Assistant, every entity
        self._at = 0.0
        self._elgato: dict[str, dict] = {}  # ip -> last state
        self._restore: dict[str, dict] = {}  # ip -> state before ON AIR
        self.errors: dict[str, str] = {}
        self.on_air = False
        self._live = LiveEdge(svc)

    # ---- setup ---------------------------------------------------------------------------------------
    @property
    def ha_url(self) -> str:
        return self.svc.cfg.secret("HA_URL").strip().rstrip("/")

    @property
    def ha_token(self) -> str:
        return self.svc.cfg.secret("HA_TOKEN").strip()

    def key_lights(self) -> list[dict]:
        out = []
        for i, x in enumerate(self.setting("key_lights", []) or []):
            x = x if isinstance(x, dict) else {"ip": str(x)}
            if x.get("ip"):
                out.append({"ip": str(x["ip"]).strip(), "name": x.get("name") or ("Key Light" + (f" {i + 1}" if i else ""))})
        return out

    def set_ha(self, url: str, token: str) -> dict:
        url, token = (url or "").strip().rstrip("/"), (token or "").strip()
        if not re.match(r"^https?://[^\s/]+", url) or len(token) < 20:
            return {"ok": False, "error": "Paste Home Assistant's address (like http://homeassistant.local:8123) and a "
                                          "long-lived token (HA → your profile → Security → Create token)."}
        try:
            r = self.http.get(f"{url}/api/", headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401:
                return {"ok": False, "error": "Home Assistant refused that token: make a new long-lived one."}
            reachable = r.status_code == 200
        except httpx.HTTPError:
            reachable = False
        save_secret(self.svc, "HA_URL", url)
        save_secret(self.svc, "HA_TOKEN", token)
        self._at = 0
        return {"ok": True, "reachable": reachable}

    def add_light(self, ip: str, name: str = "") -> dict:
        ip = (ip or "").strip()
        if not re.fullmatch(r"[\w.-]+", ip):
            return {"ok": False, "error": "Type the light's IP address, like 192.168.1.40."}
        kept = [x for x in self.setting("key_lights", []) or [] if (x.get("ip") if isinstance(x, dict) else x) != ip]
        save_setting(self, "key_lights", kept + [{"ip": ip, "name": name.strip()} if name.strip() else ip])
        st = self._elg_get(ip)
        return {"ok": True, "reachable": st is not None}

    def remove_light(self, ip: str) -> dict:
        save_setting(self, "key_lights", [x for x in self.setting("key_lights", []) or []
                                          if (x.get("ip") if isinstance(x, dict) else x) != ip])
        self._elgato.pop(ip, None)
        return {"ok": True}

    def discover(self, wait: float = 3.0) -> dict:
        try:
            from zeroconf import ServiceBrowser, Zeroconf
        except ImportError:
            return {"ok": False, "error": "Type the light's IP address instead: Elgato Control Center → the light's "
                                          "gear icon → its IP address."}
        found: dict[str, str] = {}  # pragma: no cover - needs a real network

        class Seen:  # pragma: no cover
            def add_service(self, zc, kind, name):
                info = zc.get_service_info(kind, name, 2000)
                for ip in (info.parsed_addresses() if info else []):
                    found[ip] = name.split(".")[0]
            update_service = remove_service = lambda *a: None  # noqa: E731

        zc = Zeroconf()  # pragma: no cover
        try:
            ServiceBrowser(zc, "_elg._tcp.local.", Seen())
            time.sleep(wait)
        finally:
            zc.close()
        known = {x["ip"] for x in self.key_lights()}
        for ip, name in found.items():
            if ip not in known:
                self.add_light(ip, name)
        return {"ok": True, "found": sorted(found)}

    # ---- Elgato --------------------------------------------------------------------------------------
    def _elg_get(self, ip: str) -> dict | None:
        try:
            st = self.http.get(f"http://{ip}:{PORT}/elgato/lights").json()["lights"][0]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            self.errors[ip] = f"Key Light at {ip} isn't answering ({type(exc).__name__})."
            return None
        self.errors.pop(ip, None)
        self._elgato[ip] = st
        return st

    def _elg_put(self, ip: str, on: bool | None = None, brightness: int | None = None, kelvin: int | None = None) -> bool:
        light = {k: v for k, v in (("on", None if on is None else int(on)), ("brightness", brightness),
                                   ("temperature", None if kelvin is None else k_to_mired(kelvin))) if v is not None}
        try:
            r = self.http.put(f"http://{ip}:{PORT}/elgato/lights", json={"numberOfLights": 1, "lights": [light]})
            r.raise_for_status()
            self._elgato[ip] = {**self._elgato.get(ip, {}), **((r.json().get("lights") or [light])[0])}
        except (httpx.HTTPError, ValueError) as exc:
            self.errors[ip] = f"Key Light at {ip} isn't answering ({type(exc).__name__})."
            return False
        self.errors.pop(ip, None)
        return True

    # ---- Home Assistant ------------------------------------------------------------------------------
    def _ha(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        if not (self.ha_url and self.ha_token):
            raise HomeError("Home Assistant isn't set up.")
        try:
            r = self.http.request(method, self.ha_url + path, json=body, headers={"Authorization": f"Bearer {self.ha_token}"})
        except httpx.HTTPError as exc:
            raise HomeError(f"Home Assistant isn't answering at {self.ha_url} ({type(exc).__name__}).") from exc
        if r.status_code == 401:
            raise HomeError("Home Assistant refused the token: paste a new long-lived token.")
        if r.status_code >= 400:
            raise HomeError(f"Home Assistant said {r.status_code}: {r.text[:120]}")
        return r

    def ha_states(self, fresh: bool = False) -> list[dict]:
        if not (self.ha_url and self.ha_token):
            return []
        if fresh or self.clock() - self._at > 15:
            try:
                self._states = [s for s in self._ha("GET", "/api/states").json() if isinstance(s, dict)]
                self.errors.pop("ha", None)
            except (HomeError, ValueError) as exc:
                self.errors["ha"] = str(exc)
            self._at = self.clock()
        return self._states

    def _service(self, domain: str, service: str, data: dict) -> bool:
        try:
            self._ha("POST", f"/api/services/{domain}/{service}", data)
        except HomeError as exc:
            self.errors["ha"] = str(exc)
            return False
        self._at = 0
        return True

    # ---- everything as one list ----------------------------------------------------------------------
    def lights(self, fresh: bool = False) -> list[dict]:
        out = []
        for x in self.key_lights():
            st = self._elg_get(x["ip"]) if fresh or x["ip"] not in self._elgato else self._elgato[x["ip"]]
            st = st or {}
            out.append({"id": f"elgato:{x['ip']}", "name": x["name"], "kind": "elgato", "on": bool(st.get("on")),
                        "brightness": st.get("brightness"), "dimmable": True, "ok": bool(st),
                        "kelvin": mired_to_k(st["temperature"]) if st.get("temperature") else None})
        for s in self.ha_states(fresh):
            domain, a = s.get("entity_id", "").split(".")[0], s.get("attributes") or {}
            if domain in DOMAINS:
                out.append({"id": s["entity_id"], "name": a.get("friendly_name") or s["entity_id"], "kind": domain,
                            "on": s.get("state") == "on", "ok": s.get("state") != "unavailable",
                            "brightness": round(a["brightness"] / 2.55) if a.get("brightness") is not None else None,
                            "kelvin": a.get("color_temp_kelvin"), "dimmable": domain == "light" and bool(
                                set(a.get("supported_color_modes") or []) - {"onoff"})})
        return out

    def scenes(self) -> list[dict]:
        out = [{"id": s["entity_id"], "name": (s.get("attributes") or {}).get("friendly_name") or s["entity_id"]}
               for s in self.ha_states() if s.get("entity_id", "").startswith("scene.")]
        return out + [{"id": f"preset:{k}", "name": k.title()} for k in (self.setting("presets", {}) or {})]

    def match(self, target: str = "") -> list[dict]:
        lights, words = self.lights(), _words(target or "")
        if "all" in (target or "").split() and not words:
            return lights
        if not words:  # "the lights": the Key Lights, else every HA light
            return [x for x in lights if x["kind"] == "elgato"] or [x for x in lights if x["kind"] == "light"]
        if words == {"key"}:
            return [x for x in lights if x["kind"] == "elgato" or "key" in _words(x["name"])]
        return [x for x in lights if words <= _words(x["name"]) | _words(x["id"].split(".")[-1])]

    def apply(self, hits: list[dict], on: bool | None = None, brightness: int | None = None, change: int | None = None,
              kelvin: int | None = None, warmer: bool | None = None) -> dict:
        done, failed = [], []
        for x in hits:
            b = brightness if change is None else max(1, min(100, (x.get("brightness") or 50) + int(change)))
            k = kelvin if warmer is None else max(2900, min(7000, (x.get("kelvin") or 4500) + (-600 if warmer else 600)))
            b = max(1, min(100, int(b))) if b is not None else None
            turn = on if on is not None else (True if b is not None or k is not None else None)
            if x["kind"] == "elgato":
                ok = self._elg_put(x["id"].split(":", 1)[1], turn, b, k)
            elif turn is False or (turn is None and x["on"]):
                ok = self._service("homeassistant", "turn_off", {"entity_id": x["id"]})
            else:
                data = {"entity_id": x["id"]}
                if x["kind"] == "light":
                    data.update({k_: v for k_, v in (("brightness_pct", b), ("color_temp_kelvin", k)) if v is not None})
                ok = self._service("light" if x["kind"] == "light" else "homeassistant", "turn_on", data)
            (done if ok else failed).append({**x, "brightness": b or x.get("brightness"), "kelvin": k or x.get("kelvin")})
        names = " and ".join(x["name"] for x in done)
        if not done:
            err = "; ".join(self.errors.values()) or "Nothing changed."
            return {"ok": False, "error": err if hits else "I don't know that light."}
        return {"ok": True, "names": names, "lights": [x["id"] for x in done], "failed": [x["name"] for x in failed],
                "brightness": done[0]["brightness"], "kelvin": done[0]["kelvin"]}

    def set_lights(self, target: str = "", **kw) -> dict:
        hits = self.match(target)
        if not hits and not self.lights():
            return {"ok": False, "error": "No lights set up yet: add a Key Light or Home Assistant on the HUD "
                                          "(Setup → Lights & home)."}
        if not hits:
            return {"ok": False, "error": f"I don't know a light called {target}."}
        return self.apply(hits, **kw)

    def by_id(self, id: str, **kw) -> dict:
        return self.apply([x for x in self.lights() if x["id"] == id], **kw)

    def scene(self, name: str) -> dict:
        words = _words(re.sub(r"\b(?:scene|mode)\b", "", name or ""))
        hit = next((s for s in self.scenes() if s["id"] == name or (words and words <= _words(s["name"]))), None)
        if hit is None:
            return {"ok": False, "error": f"I don't know a scene called {name}."}
        if hit["id"].startswith("preset:"):
            p = (self.setting("presets", {}) or {})[hit["id"][7:]] or {}
            out = self.set_lights(p.get("target", ""), on=p.get("on", True), brightness=p.get("brightness"), kelvin=p.get("kelvin"))
        else:
            out = {"ok": self._service("scene", "turn_on", {"entity_id": hit["id"]})}
            if not out["ok"]:
                out["error"] = self.errors.get("ha")
        return {**out, "scene": hit["name"]}

    def state(self, entity: str = "") -> dict:
        words = _words(entity)
        for s in self.ha_states(fresh=True):
            a = s.get("attributes") or {}
            if s.get("entity_id") == entity or (words and words <= _words(a.get("friendly_name") or s["entity_id"])):
                return {"ok": True, "entity": s["entity_id"], "name": a.get("friendly_name") or s["entity_id"],
                        "state": s.get("state"), "unit": a.get("unit_of_measurement") or ""}
        hits = self.match(entity)
        if hits:
            return {"ok": True, "entity": hits[0]["id"], "name": hits[0]["name"], "state": "on" if hits[0]["on"] else "off", "unit": ""}
        return {"ok": False, "error": f"I can't find {entity or 'that'} in your home."}

    # ---- ON AIR --------------------------------------------------------------------------------------
    def set_on_air(self, on: bool) -> dict:
        conf = self.setting("on_air", {}) or {}
        preset, entity = conf.get("key_light") or {}, conf.get("entity")
        for x in self.key_lights() if preset else []:
            ip = x["ip"]
            if on:
                before = self._elg_get(ip)
                if before is not None:
                    self._restore[ip] = before
                self._elg_put(ip, True, preset.get("brightness"), preset.get("kelvin"))
            elif ip in self._restore:
                was = self._restore.pop(ip)
                self._elg_put(ip, bool(was.get("on")), was.get("brightness"),
                              mired_to_k(was["temperature"]) if was.get("temperature") else None)
            else:
                self._elg_put(ip, False)
        if entity:
            self._service("homeassistant", "turn_on" if on else "turn_off", {"entity_id": entity})
        self.on_air = on
        if preset or entity:
            self.notify("On air light on." if on else "On air light off.", level="quiet", key=f"home:on_air:{on}")
        return {"ok": True, "on": on, "entity": entity, "preset": bool(preset)}

    def set_on_air_config(self, body: dict) -> dict:
        conf = {"entity": str(body.get("entity") or "").strip() or None}
        if body.get("brightness") or body.get("kelvin"):
            conf["key_light"] = {k: int(body[k]) for k in ("brightness", "kelvin") if body.get(k)}
        save_setting(self, "on_air", conf)
        return {"ok": True}

    # ---- skill surface -------------------------------------------------------------------------------
    def tick(self) -> None:
        edge = self._live()
        if edge:
            self.set_on_air(edge == "start")
        self.lights(fresh=True)
        self.publish()

    def status(self) -> dict:
        lights = self.lights()
        ha = bool(self.ha_url and self.ha_token)
        step = None if lights or ha else "Add your Key Light's IP address, or connect Home Assistant."
        try:
            import zeroconf  # noqa: F401
            can_find = True
        except ImportError:
            can_find = False
        return {"lights": lights, "scenes": self.scenes(), "key_lights": self.key_lights(), "can_find": can_find,
                "ha": {"configured": ha, "url": self.ha_url, "error": self.errors.get("ha")},
                "errors": [v for k, v in self.errors.items() if k != "ha"], "next_step": step,
                "on_air": {"on": self.on_air, "live": self._live.was, **(self.setting("on_air", {}) or {})}}

    def actions(self):
        num = lambda b, k: int(b[k]) if b.get(k) is not None else None  # noqa: E731
        return {"toggle": lambda b: self.by_id(str(b.get("id")), on=bool(b.get("on"))),
                "brightness": lambda b: self.by_id(str(b.get("id")), brightness=num(b, "level")),
                "kelvin": lambda b: self.by_id(str(b.get("id")), kelvin=num(b, "kelvin")),
                "scene": lambda b: self.scene(str(b.get("id") or b.get("name") or "")),
                "ha": lambda b: self.set_ha(str(b.get("url") or ""), str(b.get("token") or "")),
                "add_light": lambda b: self.add_light(str(b.get("ip") or ""), str(b.get("name") or "")),
                "remove_light": lambda b: self.remove_light(str(b.get("ip") or "")),
                "discover": lambda b: self.discover(),
                "on_air": lambda b: self.set_on_air(bool(b.get("on"))),
                "on_air_setup": self.set_on_air_config,
                "refresh": lambda b: {"ok": True, "n": len(self.lights(fresh=True))}}

    def tools(self):
        target = {"target": {"type": "string", "description": "which light, e.g. 'key light', 'living room'; "
                                                              "empty = the key lights (or every light)"}}
        return [
            Tool("lights_on", "Turn lights on (Elgato Key Lights, Home Assistant lights and switches).", obj(target),
                 lambda target="": self.set_lights(target, on=True), tier=1),
            Tool("lights_off", "Turn lights off.", obj(target), lambda target="": self.set_lights(target, on=False), tier=1),
            Tool("lights_brightness", "Set light brightness: level 1-100, or change (+/-).",
                 obj({**target, "level": {"type": "integer"}, "change": {"type": "integer"}}),
                 lambda target="", level=None, change=None: self.set_lights(target, brightness=level, change=change), tier=1),
            Tool("lights_temperature", "Light colour temperature: kelvin (2900-7000), or warmer true/false for a step.",
                 obj({**target, "kelvin": {"type": "integer"}, "warmer": {"type": "boolean"}}),
                 lambda target="", kelvin=None, warmer=None: self.set_lights(target, kelvin=kelvin, warmer=warmer), tier=1),
            Tool("home_scene", "Turn on a Home Assistant scene (or a saved light preset) by name, e.g. 'movie'.",
                 obj({"name": {"type": "string"}}, ["name"]), self.scene, tier=1),
            Tool("home_state", "Whether something in the home is on/off/open, or a sensor's value (Home Assistant).",
                 obj({"entity": {"type": "string"}}, ["entity"]), self.state),
        ]

    def _known(self, name: str, kind: str = "exact") -> bool:
        """A name Home Assistant or a Key Light already has (no network). ``exact`` for changes, so "turn off the pc"
        never hits a "Gaming PC plug"; ``scene`` / ``any`` match on some of the words."""
        w = _words(name)
        pool = self.scenes() if kind == "scene" else [
            {"name": (s.get("attributes") or {}).get("friendly_name") or s.get("entity_id", "")} for s in self._states
        ] + [{"name": x["name"]} for x in self.key_lights()]
        return bool(w) and any(w == _words(x["name"]) if kind == "exact" else w <= _words(x["name"]) for x in pool)

    def route(self, t: str):
        m = re.fullmatch(rf"(?:turn|switch) (on|off) (?:the |my |all (?:the |my )?)?(.*{LIGHTISH})|(?:turn|switch) "
                         rf"(?:the |my )?(.*{LIGHTISH}) (on|off)|(?:the |my )?(.*{LIGHTISH}) (on|off)", t)
        if m:
            on, target = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3)) if m.group(4) else (m.group(6), m.group(5))
            return Intent("tool", "lights_on" if on == "on" else "lights_off", {"target": target})
        m = re.fullmatch(r"(?:turn|switch) (on|off) (?:the |my )?(.+)", t)
        if m and self._known(m.group(2)):
            return Intent("tool", f"lights_{m.group(1)}", {"target": m.group(2)})
        m = re.fullmatch(rf"(?:set |turn |dim |put )?(?:the |my )?(.*{LIGHTISH})(?: brightness)? (?:to|at) {NUMBER}(?: percent| %|%)?", t)
        if m and number(m.group(2)) is not None:
            return Intent("tool", "lights_brightness", {"target": m.group(1), "level": min(100, number(m.group(2)))})
        m = re.fullmatch(rf"(dim|brighten) (?:the |my )?(.*{LIGHTISH})|(?:turn )?(?:the |my )?(.*{LIGHTISH}) (up|down)|"
                         rf"turn (up|down) (?:the |my )?(.*{LIGHTISH})|(brighter|dimmer) {LIGHTISH}", t)
        if m:
            word = m.group(1) or m.group(4) or m.group(5) or m.group(7)
            target = m.group(2) or m.group(3) or m.group(6) or ""
            return Intent("tool", "lights_brightness", {"target": target, "change": 20 if word in ("brighten", "up", "brighter") else -20})
        m = re.fullmatch(rf"(warmer|cooler|colder) (?:the |my )?(.*{LIGHTISH})|(?:make |set |turn )?(?:the |my )?(.*{LIGHTISH}) "
                         rf"(warmer|cooler|colder)", t)
        if m:
            word, target = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            return Intent("tool", "lights_temperature", {"target": target, "warmer": word == "warmer"})
        m = re.fullmatch(rf"(?:set )?(?:the |my )?(.*{LIGHTISH}) to (\d{{4}}) ?k(?:elvin)?", t)
        if m:
            return Intent("tool", "lights_temperature", {"target": m.group(1), "kelvin": int(m.group(2))})
        m = re.fullmatch(r"(?:activate |start |set |turn on |switch to )?(?:the |my )?(.+?) (?:scene|mode)|scene (.+)", t)
        if m and self._known(m.group(1) or m.group(2), "scene"):
            return Intent("tool", "home_scene", {"name": m.group(1) or m.group(2)})
        m = re.fullmatch(r"is (?:the |my )?(.+?) (?:on|off|open|closed|locked|unlocked|running)", t)
        if m and self._known(m.group(1), "any"):
            return Intent("tool", "home_state", {"entity": m.group(1)})
        return None

    def say(self, tool, args, result):
        if tool in ("lights_on", "lights_off"):
            return f"{result['names']} {'on' if tool == 'lights_on' else 'off'}."
        if tool == "lights_brightness":
            return f"{result['names']} at {result.get('brightness')} percent."
        if tool == "lights_temperature":
            how = "Warmer" if args.get("warmer") is True else "Cooler" if args.get("warmer") is False else "Set"
            return f"{how}: {result['names']} at {result.get('kelvin')} K."
        if tool == "home_scene":
            return f"{result['scene']}" + (" on." if "scene" in result["scene"].lower() else " scene on.")
        if tool == "home_state":
            return f"{result['name']} is {result['state']}{(' ' + result['unit']) if result.get('unit') else ''}."
        return None
