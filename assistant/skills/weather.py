"""Weather where you are, from Open-Meteo (free, no key): now, the next hours, the week, air quality.

"What's the weather", "will it rain tomorrow", "do I need a jacket", "set my location to Atlanta". The place is found
once from your internet address unless you say one; it's kept in data/settings.yaml (skills.weather.place).
"""

from __future__ import annotations

import re
import time
from datetime import datetime

import httpx

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill, check, lazy_client

FORECAST = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
AIR = "https://air-quality-api.open-meteo.com/v1/air-quality"
IPLOC = "https://ipapi.co/json/"

# WMO weather codes -> words and an icon name the HUD draws
CODES = {0: ("clear", "sun"), 1: ("mostly clear", "sun"), 2: ("partly cloudy", "cloud-sun"), 3: ("overcast", "cloud"),
         45: ("foggy", "fog"), 48: ("freezing fog", "fog"), 51: ("light drizzle", "drizzle"), 53: ("drizzle", "drizzle"),
         55: ("heavy drizzle", "drizzle"), 56: ("freezing drizzle", "drizzle"), 57: ("freezing drizzle", "drizzle"),
         61: ("light rain", "rain"), 63: ("rain", "rain"), 65: ("heavy rain", "rain"), 66: ("freezing rain", "rain"),
         67: ("freezing rain", "rain"), 71: ("light snow", "snow"), 73: ("snow", "snow"), 75: ("heavy snow", "snow"),
         77: ("snow grains", "snow"), 80: ("rain showers", "rain"), 81: ("rain showers", "rain"),
         82: ("violent rain showers", "storm"), 85: ("snow showers", "snow"), 86: ("heavy snow showers", "snow"),
         95: ("thunderstorms", "storm"), 96: ("thunderstorms with hail", "storm"), 99: ("severe thunderstorms", "storm")}


def describe(code) -> tuple[str, str]:
    return CODES.get(int(code or 0), ("unknown", "cloud"))


class Weather(Skill):
    name, title = "weather", "Weather"
    poll_s = 900

    def __init__(self, svc, http: httpx.Client | None = None):
        super().__init__(svc)
        self.http = http or lazy_client(timeout=15, headers={"User-Agent": "Vesper/1.0"})
        self._data: dict | None = None
        self._at = 0.0
        self.error: str | None = None

    # ---- where -----------------------------------------------------------------------------------------
    @property
    def fahrenheit(self) -> bool:
        return str(self.setting("units", "fahrenheit")).lower().startswith("f")

    def place(self) -> dict | None:
        p = self.setting("place")
        if isinstance(p, dict) and "lat" in p:
            return p
        try:
            r = self.http.get(IPLOC).json()
            if r.get("latitude"):
                found = {"name": ", ".join(x for x in (r.get("city"), r.get("region_code") or r.get("region")) if x),
                         "lat": r["latitude"], "lon": r["longitude"], "from": "internet address"}
                self._save_place(found)
                return found
        except (httpx.HTTPError, ValueError):
            pass
        return None

    def set_place(self, name: str) -> dict:
        try:
            res = self.http.get(GEOCODE, params={"name": name.strip(), "count": 1, "language": "en"}).json().get("results")
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "error": f"Couldn't look that place up ({type(exc).__name__})."}
        if not res:
            return {"ok": False, "error": f"I can't find a place called {name}."}
        r = res[0]
        found = {"name": ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country_code")) if x),
                 "lat": r["latitude"], "lon": r["longitude"], "from": "you"}
        self._save_place(found)
        self._at = 0
        self.publish()
        return {"ok": True, "place": found}

    def _save_place(self, place: dict) -> None:
        from ..config import save_setting

        self.cfg["place"] = place
        try:
            save_setting(self.svc.cfg, "skills.weather.place", place)
        except Exception:  # read-only data folder: keep it for this run
            pass

    # ---- the forecast ----------------------------------------------------------------------------------
    def forecast(self, fresh: bool = False) -> dict:
        if self._data and not fresh and time.time() - self._at < 600:
            return self._data
        place = self.place()
        if not place:
            self.error = "Say “Vesper, set my location to <your city>” so I know where you are."
            return {"ok": False, "error": self.error}
        unit = "fahrenheit" if self.fahrenheit else "celsius"
        try:
            f = self.http.get(FORECAST, params={
                "latitude": place["lat"], "longitude": place["lon"], "timezone": "auto", "forecast_days": 7,
                "temperature_unit": unit, "wind_speed_unit": "mph" if self.fahrenheit else "kmh",
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day,precipitation",
                "hourly": "temperature_2m,precipitation_probability,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset,uv_index_max",
            }).json()
        except (httpx.HTTPError, ValueError) as exc:
            self.error = f"The weather service didn't answer ({type(exc).__name__})."
            return {"ok": False, "error": self.error}
        if "current" not in f:
            self.error = str(f.get("reason") or "No forecast came back.")
            return {"ok": False, "error": self.error}
        aqi = None
        try:
            a = self.http.get(AIR, params={"latitude": place["lat"], "longitude": place["lon"], "current": "us_aqi"}).json()
            aqi = (a.get("current") or {}).get("us_aqi")
        except (httpx.HTTPError, ValueError):
            pass
        self._data = shape(f, place, aqi, "°F" if self.fahrenheit else "°C")
        self._at, self.error = time.time(), None
        return self._data

    def selftest(self) -> list[dict]:
        p = self.setting("place")
        if not (isinstance(p, dict) and "lat" in p):
            return [check("Forecast", "warn", "no location yet", "Say “Vesper, set my location to <your city>”")]
        try:
            r = self.http.get(FORECAST, params={"latitude": p["lat"], "longitude": p["lon"], "current": "temperature_2m"},
                              timeout=8)
            ok = r.status_code == 200 and "current" in r.json()
        except (httpx.HTTPError, ValueError) as exc:
            return [check("Forecast", "warn", f"the weather service didn't answer ({type(exc).__name__})")]
        return [check("Forecast", "pass" if ok else "warn", f"works for {p.get('name') or 'your place'}" if ok else
                      f"the weather service said {r.status_code}")]

    def status(self) -> dict:
        data = self.forecast()
        return data if data.get("ok") else {"ok": False, "error": data.get("error")}

    def briefing(self):
        d = self._data or {}  # what the last refresh fetched: the briefing never waits on the network
        if not d.get("ok"):
            return None
        today = d["days"][0]
        return {"place": d["place"], "now": f"{d['now']['temp']}{d['unit']} {d['now']['text']}",
                "today": f"{today['text']}, {today['low']}–{today['high']}{d['unit']}, {today['rain']}% rain",
                "advice": advice(d)}

    def actions(self):
        return {"place": lambda b: self.set_place(str(b.get("name") or "")),
                "refresh": lambda b: self.forecast(fresh=True)}

    # ---- the brain -------------------------------------------------------------------------------------
    def tools(self):
        return [
            Tool("get_weather", "The weather where the user is: now, the next hours and the week (day: today, "
                                "tomorrow, or a weekday). Use for weather, rain, umbrella, jacket, temperature questions.",
                 obj({"day": {"type": "string"}}), self._get_weather),
            Tool("set_location", "Set the user's city for the weather.", obj({"name": {"type": "string"}}, ["name"]),
                 self.set_place, tier=1),
        ]

    def _get_weather(self, day: str = "today") -> dict:
        d = self.forecast()
        if not d.get("ok"):
            return d
        return {**d, "asked": day or "today", "pick": pick_day(d, day)}

    def route(self, t: str):
        m = re.search(r"^(?:set|change|update) my (?:location|city|town) to (.+)$|^i (?:live|am|'m) (?:in|at) (.+)$", t)
        if m:
            return Intent("tool", "set_location", {"name": m.group(1) or m.group(2)})
        if re.search(r"\b(?:weather|forecast|temperature)\b|\bwill it (?:rain|snow|storm)\b|\bis it (?:going to )?(?:rain|snow)"
                     r"|\bdo i need (?:an? )?(?:umbrella|jacket|coat|sunscreen)\b|^how (?:hot|cold|warm) is it", t) \
                and not re.search(r"\b(?:stream|twitch|obs|document|email)\b", t):
            day = next((w for w in ("tomorrow", "tonight", "monday", "tuesday", "wednesday", "thursday", "friday",
                                    "saturday", "sunday", "weekend", "week") if w in t), "today")
            return Intent("tool", "get_weather", {"day": day})
        return None

    def say(self, tool, args, result):
        if tool == "set_location":
            return f"Got it: {result['place']['name']}."
        if tool != "get_weather":
            return None
        return spoken(result, args.get("day") or "today")


# ---- shaping and speaking -------------------------------------------------------------------------------------

def shape(f: dict, place: dict, aqi, unit: str) -> dict:
    cur, hourly, daily = f["current"], f.get("hourly") or {}, f.get("daily") or {}
    text, icon = describe(cur.get("weather_code"))
    now_iso = cur.get("time", "")
    hours = []
    times = hourly.get("time") or []
    start = next((i for i, t in enumerate(times) if t >= now_iso[:13]), 0)
    for i in range(start, min(start + 24, len(times))):
        ht, hi = describe(hourly["weather_code"][i])
        hours.append({"time": times[i], "temp": round(hourly["temperature_2m"][i]),
                      "rain": hourly["precipitation_probability"][i] or 0, "icon": hi, "text": ht})
    days = []
    for i, day in enumerate(daily.get("time") or []):
        dt, di = describe(daily["weather_code"][i])
        days.append({"date": day, "name": datetime.fromisoformat(day).strftime("%A"), "text": dt, "icon": di,
                     "high": round(daily["temperature_2m_max"][i]), "low": round(daily["temperature_2m_min"][i]),
                     "rain": daily["precipitation_probability_max"][i] or 0,
                     "sunrise": (daily.get("sunrise") or [""] * 7)[i][-5:], "sunset": (daily.get("sunset") or [""] * 7)[i][-5:],
                     "uv": (daily.get("uv_index_max") or [None] * 7)[i]})
    return {"ok": True, "place": place.get("name") or "here", "unit": unit, "at": time.time(),
            "now": {"temp": round(cur["temperature_2m"]), "feels": round(cur.get("apparent_temperature", cur["temperature_2m"])),
                    "humidity": cur.get("relative_humidity_2m"), "wind": round(cur.get("wind_speed_10m") or 0),
                    "text": text, "icon": icon, "day": bool(cur.get("is_day", 1))},
            "hours": hours, "days": days, "aqi": aqi}


def pick_day(d: dict, day: str | None) -> dict:
    day = (day or "today").lower()
    days = d["days"]
    if day in ("today", "tonight", "now", "") or not days:
        return days[0] if days else {}
    if day == "tomorrow":
        return days[1] if len(days) > 1 else days[0]
    for x in days:
        if x["name"].lower() == day:
            return x
    return days[0]


def rain_window(hours: list[dict], threshold: int = 50) -> str | None:
    """'around 3 PM' when rain gets likely in the next 12 hours."""
    for h in hours[:12]:
        if h["rain"] >= threshold:
            return datetime.fromisoformat(h["time"]).strftime("%I %p").lstrip("0")
    return None


def advice(d: dict) -> str | None:
    today = d["days"][0] if d["days"] else {}
    tips = []
    when = rain_window(d["hours"])
    if when:
        tips.append(f"rain likely around {when}: take an umbrella")
    hot, cold = (85, 40) if d["unit"] == "°F" else (29, 4)
    if today.get("high", 0) >= hot:
        tips.append("hot today: drink water")
    if today.get("low", 99) <= cold:
        tips.append("cold out: wear a coat")
    if (today.get("uv") or 0) >= 7:
        tips.append("strong sun: sunscreen")
    if (d.get("aqi") or 0) > 100:
        tips.append(f"air quality is poor (AQI {d['aqi']})")
    return "; ".join(tips) or None


def spoken(r: dict, day: str) -> str:
    if r.get("ok") is False:
        return r.get("error") or "No weather right now."
    u = r["unit"]
    if day in ("week", "weekend"):
        pick = r["days"][5:7] if day == "weekend" and len(r["days"]) >= 7 else r["days"][:5]
        return "; ".join(f"{x['name']} {x['text']}, {x['high']}{u}" for x in pick) + "."
    if day in ("today", "now", "") :
        n, t = r["now"], r["days"][0]
        line = f"{n['temp']}{u} and {n['text']} in {r['place'].split(',')[0]}. Today {t['low']} to {t['high']}"
        when = rain_window(r["hours"])
        line += f", rain likely around {when}." if when else (f", {t['rain']}% chance of rain." if t["rain"] >= 20 else ", dry.")
        tip = advice(r)
        return line + (f" {tip[0].upper()}{tip[1:]}." if tip and "umbrella" not in tip else "")
    x = r.get("pick") or pick_day(r, day)
    label = "Tonight" if day == "tonight" else ("Tomorrow" if day == "tomorrow" else x.get("name", day.title()))
    rain = f", {x['rain']}% chance of rain" if x.get("rain", 0) >= 20 else ", dry"
    return f"{label}: {x['text']}, {x['low']} to {x['high']}{u}{rain}."
