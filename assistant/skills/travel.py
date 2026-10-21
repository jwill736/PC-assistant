"""Packages and trips, from Gmail (work and personal), read-only: the Google Now cards.

"Where's my package", "any deliveries today", "when's my flight". Shipping mail (Amazon, UPS, FedEx, USPS, DHL and
shops) gives a package card: who it's from, what, the carrier and tracking number (with the carrier's tracking page),
how far along (ordered → shipped → out for delivery → delivered) and when it arrives. Airline and hotel mail gives
the trips: flight number, date and time, from/to, confirmation code; hotel check-in and check-out.

Nudges: a package out for delivery (low: the HUD feed), a flight within 24 hours, when check-in opens (normal).
On air nothing about trips or deliveries is said out loud beyond "it's on the HUD".

Travel day: from the day before a flight, the Life tab shows a Travel Day card: a countdown, when to leave (departure
− the airport buffer, 2 h domestic / 3 h international − the drive), the airline's check-in page, the weather where
you're going, and a packing list made from the trip's length and that weather (tick, add, remove; kept). The drive is
skills.travel.drive_min if set, else the free OSRM router from home (skills.travel.home, or the weather's place) to the
airport, else a 45-minute guess. "Leave by 5:40 AM for DL 1234" comes 30 minutes before (urgent).
"When do I need to leave for my flight", "what's the weather in Denver".
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import date, datetime, timedelta

import httpx

from ..brain.router import Intent
from ..brain.speech import clock as say_clock, duration
from ..brain.tools import EMAIL_NOTE, Tool, obj
from . import Skill
from .bills import date_after, day_word, find_dates, merchant_of, scan_mail, why_no_gmail
from .weather import FORECAST, GEOCODE, advice, asked_place, shape, spoken

QUERY = ('(shipped OR "out for delivery" OR delivered OR "tracking number" OR "your order" OR "order confirmation" OR '
         'flight OR itinerary OR "boarding pass" OR "check-in" OR reservation OR "booking confirmation") newer_than:45d')
STAGES = ["Ordered", "Shipped", "Out for delivery", "Delivered"]
STAGE_RX = [(3, re.compile(r"\b(?:was|has been|been|got) delivered\b|^delivered\b|\bdelivered:|\bdelivered today\b", re.I)),
            (2, re.compile(r"\bout for delivery\b", re.I)),
            (1, re.compile(r"\bshipped\b|\bon (?:its|the) way\b|\bin transit\b|\bshipment\b|\btracking number\b|^arriving\b|"
                           r"\bscheduled for delivery\b", re.I)),
            (0, re.compile(r"^ordered:|\border (?:confirmation|received|placed)\b|\bthanks for your order\b|"
                           r"\bwe(?:'ve| have) received your order\b|\border #", re.I))]
CARRIERS = [("UPS", re.compile(r"\b1Z[0-9A-Z]{16}\b"), "https://www.ups.com/track?tracknum={}", False),
            ("USPS", re.compile(r"\b(?:9[2-5]\d{20}|9[2-5]\d{2}(?: \d{4}){4} \d{2}|[A-Z]{2}\d{9}US)\b"),
             "https://tools.usps.com/go/TrackConfirmAction?tLabels={}", False),
            ("Amazon", re.compile(r"\bTBA\d{12}\b"), "https://track.amazon.com/tracking/{}", False),
            ("FedEx", re.compile(r"\b(?:\d{15}|\d{12})\b"), "https://www.fedex.com/fedextrack/?trknbr={}", True),
            ("DHL", re.compile(r"\b\d{10}\b"), "https://www.dhl.com/us-en/home/tracking/tracking-express.html?tracking-id={}", True)]
AIRLINES = {"DL": "Delta", "UA": "United", "AA": "American", "WN": "Southwest", "B6": "JetBlue", "AS": "Alaska",
            "NK": "Spirit", "F9": "Frontier", "HA": "Hawaiian", "G4": "Allegiant", "AC": "Air Canada", "BA": "British Airways",
            "LH": "Lufthansa", "AF": "Air France", "KL": "KLM", "VS": "Virgin Atlantic", "EK": "Emirates", "QR": "Qatar"}
BY_NAME = {v.lower(): k for k, v in AIRLINES.items()}
FLIGHT_CODE = re.compile(r"\b(" + "|".join(AIRLINES) + r") ?(\d{1,4})\b")
FLIGHT_NAME = re.compile(r"\b(" + "|".join(AIRLINES.values()) + r")(?: Air(?:lines|ways)?)?(?: flight)? #?(\d{1,4})\b", re.I)
CITY = re.compile(r"(?i:depart(?:s|ing|ure)?|arriv(?:e|es|al|ing))[:\s]+(?:from |in )?([A-Z][A-Za-z .']{1,30}?)\s+\d{1,2}:\d{2}")
TIME = re.compile(r"\b(\d{1,2}):(\d{2}) ?([ap])\.?m\.?", re.I)
CONF = re.compile(r"(?i:confirmation(?: code| number| #| no\.?)?|record locator|booking (?:reference|code)|PNR)\s*(?:is|#|:)?\s*#?"
                  r"\s*([A-Z0-9]{6,12})\b")
HOTELS = re.compile(r"marriott|courtyard|westin|sheraton|hilton|hampton|hyatt|ihg|holiday inn|best western|airbnb|"
                    r"booking\.com|expedia|hotels\.com|\bhotel\b|\bresort\b|\binn\b|\bsuites\b", re.I)
ETA = re.compile(r"\b(?:arriving|arrives|estimated delivery|expected delivery|delivery date|scheduled delivery|"
                 r"scheduled for delivery|deliver(?:y|ed)? by|delivery:)", re.I)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
OSRM = "https://router.project-osrm.org/route/v1/driving/{},{};{},{}"
CHECKIN = {"DL": "https://www.delta.com/mytrips/", "UA": "https://www.united.com/en/us/checkin",
           "AA": "https://www.aa.com/reservation/view/find-your-trip", "WN": "https://www.southwest.com/air/check-in/",
           "B6": "https://www.jetblue.com/check-in", "AS": "https://www.alaskaair.com/checkin",
           "NK": "https://www.spirit.com/check-in", "F9": "https://www.flyfrontier.com/travel/my-trips/check-in/",
           "HA": "https://www.hawaiianairlines.com/check-in", "G4": "https://www.allegiantair.com/check-in",
           "AC": "https://www.aircanada.com/check-in", "BA": "https://www.britishairways.com/travel/olcilandingpageauthreq/public/en_us",
           "LH": "https://www.lufthansa.com/us/en/online-check-in", "AF": "https://wwws.airfrance.us/check-in",
           "KL": "https://www.klm.us/check-in", "VS": "https://www.virginatlantic.com/check-in",
           "EK": "https://www.emirates.com/us/english/manage-booking/online-check-in/",
           "QR": "https://www.qatarairways.com/en-us/manage-booking/check-in.html"}
# The airports booking mail names most: code -> (city, lat, lon, country). Others are found by name (Open-Meteo).
AIRPORTS = {
    "ATL": ("Atlanta", 33.6407, -84.4277, "US"), "AUS": ("Austin", 30.1975, -97.6664, "US"),
    "DEN": ("Denver", 39.8561, -104.6737, "US"), "DFW": ("Dallas", 32.8998, -97.0403, "US"),
    "DAL": ("Dallas", 32.8471, -96.8518, "US"), "IAH": ("Houston", 29.9902, -95.3368, "US"),
    "HOU": ("Houston", 29.6454, -95.2789, "US"), "ORD": ("Chicago", 41.9742, -87.9073, "US"),
    "MDW": ("Chicago", 41.7868, -87.7522, "US"), "LAX": ("Los Angeles", 33.9416, -118.4085, "US"),
    "SFO": ("San Francisco", 37.6213, -122.3790, "US"), "SEA": ("Seattle", 47.4502, -122.3088, "US"),
    "JFK": ("New York", 40.6413, -73.7781, "US"), "LGA": ("New York", 40.7769, -73.8740, "US"),
    "EWR": ("Newark", 40.6895, -74.1745, "US"), "BOS": ("Boston", 42.3656, -71.0096, "US"),
    "MIA": ("Miami", 25.7959, -80.2870, "US"), "FLL": ("Fort Lauderdale", 26.0742, -80.1506, "US"),
    "MCO": ("Orlando", 28.4312, -81.3081, "US"), "TPA": ("Tampa", 27.9755, -82.5332, "US"),
    "CLT": ("Charlotte", 35.2144, -80.9473, "US"), "PHX": ("Phoenix", 33.4342, -112.0116, "US"),
    "LAS": ("Las Vegas", 36.0840, -115.1537, "US"), "SAN": ("San Diego", 32.7338, -117.1933, "US"),
    "MSP": ("Minneapolis", 44.8848, -93.2223, "US"), "DTW": ("Detroit", 42.2162, -83.3554, "US"),
    "PHL": ("Philadelphia", 39.8744, -75.2424, "US"), "DCA": ("Washington", 38.8512, -77.0402, "US"),
    "IAD": ("Washington", 38.9531, -77.4565, "US"), "BWI": ("Baltimore", 39.1774, -76.6684, "US"),
    "BNA": ("Nashville", 36.1263, -86.6774, "US"), "MSY": ("New Orleans", 29.9934, -90.2580, "US"),
    "SLC": ("Salt Lake City", 40.7899, -111.9791, "US"), "PDX": ("Portland", 45.5898, -122.5951, "US"),
    "RDU": ("Raleigh", 35.8801, -78.7880, "US"), "SAV": ("Savannah", 32.1276, -81.2021, "US"),
    "HNL": ("Honolulu", 21.3187, -157.9225, "US"), "YYZ": ("Toronto", 43.6777, -79.6248, "CA"),
    "YVR": ("Vancouver", 49.1967, -123.1815, "CA"), "LHR": ("London", 51.4700, -0.4543, "GB"),
    "CDG": ("Paris", 49.0097, 2.5479, "FR"), "AMS": ("Amsterdam", 52.3105, 4.7683, "NL"),
    "FRA": ("Frankfurt", 50.0379, 8.5622, "DE"), "DXB": ("Dubai", 25.2532, 55.3657, "AE"),
    "DOH": ("Doha", 25.2731, 51.6081, "QA"), "CUN": ("Cancun", 21.0365, -86.8771, "MX"),
    "MEX": ("Mexico City", 19.4361, -99.0719, "MX"), "NRT": ("Tokyo", 35.7720, 140.3929, "JP"),
    "HND": ("Tokyo", 35.5494, 139.7798, "JP")}
BY_CITY = {}
for _code, _a in AIRPORTS.items():
    BY_CITY.setdefault(_a[0].lower(), _code)


# ---- reading one email ----------------------------------------------------------------------------------------

def tracking(text: str) -> tuple[str, str, str] | None:
    """(carrier, number, tracking page) for the first tracking number in ``text``."""
    low = text.lower()
    for carrier, rx, url, needs_word in CARRIERS:
        if needs_word and carrier.lower() not in low:
            continue
        for m in rx.finditer(text):
            if needs_word and "tracking" not in low[max(0, m.start() - 120): m.start()]:
                continue
            num = m.group(0).replace(" ", "")
            return carrier, num, url.format(num)
    return None


def relative_day(word: str, ref: date) -> date | None:
    w = word.lower()
    if w == "today":
        return ref
    if w == "tomorrow":
        return ref + timedelta(days=1)
    if w in WEEKDAYS:
        return ref + timedelta(days=(WEEKDAYS.index(w) - ref.weekday()) % 7)
    return None


def parse_package(m: dict) -> dict | None:
    subject, body = m.get("subject") or "", m.get("body") or m.get("snippet") or ""
    text = f"{subject}\n{body}"
    stage = next((s for s, rx in STAGE_RX if rx.search(subject)), None)
    if stage is None:
        stage = next((s for s, rx in STAGE_RX if rx.search(body)), None)
    track = tracking(text)
    if stage is None or (track is None and not re.search(r"order|ship|deliver|package", subject, re.I)):
        return None
    sender = (m.get("from") or "") + " " + (m.get("from_email") or "")
    carrier = track[0] if track else next((c for c, *_ in CARRIERS if c.lower() in sender.lower()), "")
    merchant = merchant_of(m.get("from") or "", m.get("from_email") or "")
    if merchant.upper() in ("UPS", "USPS", "FEDEX", "DHL") or merchant.lower() in ("ups", "fedex", "usps", "dhl"):
        shop = re.search(r"(?i:shipper|from|sender|ordered from)[:\s]+([A-Z][\w&'.-]*(?: [A-Z][\w&'.-]*){0,3})", body)
        merchant = shop.group(1) if shop else merchant
    item = re.search(r'"(.+?)"|“(.+?)”|order of (.+?) (?:has|is|was)\b|^(?:shipped|delivered|ordered|out for delivery|'
                     r'arriving \w+): (.+?)(?: and \d+ more items?)?$', subject, re.I)
    ref = datetime.fromtimestamp(m.get("ts") or time.time())
    eta = None
    for e in ETA.finditer(text):
        w = re.match(r"[:\s]*(?:on |by )?(today|tomorrow|" + "|".join(WEEKDAYS) + r")\b", text[e.end(): e.end() + 30], re.I)
        eta = relative_day(w.group(1), ref.date()) if w else date_after(text[e.start():], ETA, ref, 50)
        if eta:
            break
    if stage == 3:
        eta = ref.date()
    order = re.search(r"\b\d{3}-\d{7}-\d{7}\b", text)
    name = next((g.strip('"“” ') for g in item.groups() if g), None) if item else None
    ids = [x for x in (track[1] if track else None, order.group(0) if order else None,
                       f"{merchant}:{name}".lower() if name else None) if x] or [m["id"]]
    return {"key": ids[0], "ids": ids, "kind": "package", "merchant": merchant, "item": (name or "")[:80], "carrier": carrier,
            "tracking": track[1] if track else "", "track_url": track[2] if track else "", "stage": stage,
            "eta": eta.isoformat() if eta else None, "ts": m.get("ts") or time.time(), "link": m.get("link", ""),
            "account": m.get("account", "")}


def _clock_of(m: re.Match) -> tuple[int, int]:
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    return (h % 12 + (12 if ap == "p" else 0)), mi


def parse_flights(m: dict) -> list[dict]:
    subject, body = m.get("subject") or "", m.get("body") or ""
    text = f"{subject}\n{body}"
    if not re.search(r"\bflight\b|\bitinerary\b|boarding pass|e-?ticket", text, re.I):
        return []
    ref = datetime.fromtimestamp(m.get("ts") or time.time())
    sender_code = next((c for n, c in BY_NAME.items() if n in (m.get("from") or "").lower() + (m.get("from_email") or "")), None)
    hits = [(x.start(), x.group(1), x.group(2)) for x in FLIGHT_CODE.finditer(text)]
    hits += [(x.start(), BY_NAME[x.group(1).lower()], x.group(2)) for x in FLIGHT_NAME.finditer(text)]
    if not hits and sender_code:
        hits = [(x.start(), sender_code, x.group(1)) for x in re.finditer(r"(?i:flight)(?: number| no\.?| #)?:? ?(\d{1,4})\b", text)]
    hits.sort()
    conf = CONF.search(text)
    dates = find_dates(text, ref)
    out, seen = [], set()
    for i, (pos, code, num) in enumerate(hits):
        if (code, num) in seen:
            continue
        seen.add((code, num))
        end = next((p for p, c, n in hits[i + 1:] if (c, n) != (code, num)), pos + 400)
        near = [(abs(p - pos), d) for p, d in dates if pos - 160 <= p <= end]
        if not near:
            continue
        day = min(near)[1]
        times = [t for t in TIME.finditer(text, pos, end)] or list(TIME.finditer(text, max(0, pos - 160), pos))
        hm = _clock_of(times[0]) if times else None
        ports = re.findall(r"\(([A-Z]{3})\)", text[pos:end]) or \
            list(next(iter(re.findall(r"\b([A-Z]{3}) ?(?:→|->|>|–|to|-) ?([A-Z]{3})\b", text[max(0, pos - 80):end])), ())) or \
            [c.group(1).strip().title() for c in CITY.finditer(text, pos, end)][:2]
        out.append({"key": f"flight:{code}{num}:{day.isoformat()}", "kind": "flight", "airline": AIRLINES[code],
                    "flight": f"{code} {num}", "date": day.isoformat(), "time": f"{hm[0]:02d}:{hm[1]:02d}" if hm else None,
                    "from": ports[0] if ports else "", "to": ports[1] if len(ports) > 1 else "",
                    "confirmation": conf.group(1) if conf else "", "ts": m.get("ts") or time.time(),
                    "link": m.get("link", ""), "account": m.get("account", "")})
    return out


def parse_hotel(m: dict) -> dict | None:
    subject, body = m.get("subject") or "", m.get("body") or ""
    text = f"{subject}\n{body}"
    if not (re.search(r"check-?\s?in", text, re.I) and re.search(r"check-?\s?out", text, re.I) and HOTELS.search(text)):
        return None
    ref = datetime.fromtimestamp(m.get("ts") or time.time())
    cin = date_after(text, re.compile(r"check-?\s?in(?: date)?", re.I), ref, 50)
    cout = date_after(text, re.compile(r"check-?\s?out(?: date)?", re.I), ref, 50)
    if not cin:
        return None
    name = re.search(r"(?i:reservation|booking|stay)\b.*?\b(?:at|for) (?:the )?([A-Z][^,|\n#]{2,60}?)(?:\s*[,|#\n]|$)", subject)
    hotel = name.group(1).strip() if name else merchant_of(m.get("from") or "", m.get("from_email") or "")
    t = TIME.search(text, re.search(r"check-?\s?in", text, re.I).start())
    conf = CONF.search(text)
    return {"key": f"hotel:{hotel.lower()}:{cin.isoformat()}", "kind": "hotel", "hotel": hotel[:60],
            "checkin": cin.isoformat(), "checkout": cout.isoformat() if cout else None,
            "time": f"{_clock_of(t)[0]:02d}:{_clock_of(t)[1]:02d}" if t else None,
            "confirmation": conf.group(1) if conf else "", "ts": m.get("ts") or time.time(),
            "link": m.get("link", ""), "account": m.get("account", "")}


def parse_travel(m: dict) -> list[dict]:
    """One email -> its flights, its hotel stay, or its package (in that order of trying)."""
    flights = parse_flights(m)
    if flights:
        return flights
    hotel = parse_hotel(m)
    if hotel:
        return [hotel]
    pkg = parse_package(m)
    return [pkg] if pkg else []


def merge(old: dict | None, new: dict) -> dict:
    """A later email about the same package moves it along; an older one only fills gaps."""
    if old is None:
        return new
    newer, older = (new, old) if new["ts"] >= old["ts"] else (old, new)
    out = {**older, **{k: v for k, v in newer.items() if v not in (None, "")}}
    if out.get("kind") == "package":
        out["stage"] = max(old["stage"], new["stage"])
    return out


# ---- travel day helpers ----------------------------------------------------------------------------------------

def km_between(a: dict, b: dict) -> float:
    """Great-circle distance between two {lat, lon} places."""
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def packing_template(nights: int | None, international: bool = False, weather: dict | None = None,
                     extra: list | None = None, laptop: bool = True) -> list[str]:
    """A packing list from the trip's length and the weather there (``weather``: {"unit", "days": [{high, low, rain,
    icon}]}). Umbrella when rain is likely, a jacket when it's cold, sunscreen when it's hot."""
    n = max(1, nights if nights is not None else 2)
    out = ["Passport" if international else "ID & wallet", "Phone charger", "Headphones", "Toiletries", "Medications",
           f"Clothes for {n + 1} days" if n < 7 else "Clothes for a week", f"Underwear & socks × {min(n + 1, 8)}",
           "Sleepwear"]
    if laptop:
        out.append("Laptop + charger")
    if international:
        out.append("Power adapter")
    if n >= 4:
        out.append("Laundry bag")
    days = (weather or {}).get("days") or []
    if days:
        f = (weather or {}).get("unit", "°F") == "°F"
        cold, freezing, hot = (50, 32, 85) if f else (10, 0, 29)
        icons = {d.get("icon") for d in days}
        if any((d.get("rain") or 0) >= 50 for d in days) or icons & {"rain", "drizzle", "storm"}:
            out.append("Umbrella")
        if min(d.get("low", 99) for d in days) <= cold:
            out.append("Jacket")
        if min(d.get("low", 99) for d in days) <= freezing or "snow" in icons:
            out += ["Gloves & hat", "Warm boots"] if "snow" in icons else ["Gloves & hat"]
        if max(d.get("high", -99) for d in days) >= hot:
            out += ["Sunscreen", "Sunglasses"]
    out += [str(x)[:60] for x in extra or [] if str(x).strip()]
    return list(dict.fromkeys(out))


def countdown_words(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60} minutes"
    return duration(seconds - seconds % 300)


class Travel(Skill):
    name, title = "travel", "On the way"
    poll_s = 300  # the "leave by" nudge needs a fine tick; the Gmail scan itself runs once an hour
    delay_s = 120

    def __init__(self, svc, clock=time.time, http: httpx.Client | None = None):
        super().__init__(svc)
        self.clock = clock
        self._http = http
        self._scanned = 0.0
        self.error: str | None = None
        self._geo: dict[str, dict | None] = {}
        self._wx: dict[str, tuple[float, dict]] = {}
        self._route_down_until = 0.0
        self._day: dict | None = None
        svc.storage.execute("CREATE TABLE IF NOT EXISTS travel_items (key TEXT PRIMARY KEY, kind TEXT, ts REAL, data TEXT)")
        svc.storage.execute("CREATE TABLE IF NOT EXISTS travel_seen (id TEXT PRIMARY KEY, ts REAL)")
        svc.storage.execute("CREATE TABLE IF NOT EXISTS travel_packing (trip TEXT NOT NULL, item TEXT NOT NULL, done INTEGER "
                            "NOT NULL DEFAULT 0, auto INTEGER NOT NULL DEFAULT 1, removed INTEGER NOT NULL DEFAULT 0, "
                            "ord REAL NOT NULL, PRIMARY KEY (trip, item))")
        svc.storage.execute("CREATE TABLE IF NOT EXISTS travel_drive (route TEXT PRIMARY KEY, minutes REAL, ts REAL)")

    @property
    def http(self) -> httpx.Client:
        """Made on first use: only travel days and "weather in <city>" go online."""
        if self._http is None:
            self._http = httpx.Client(timeout=8, headers={"User-Agent": "Vesper/2.1 (personal assistant)"})
        return self._http

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.svc.tz)

    def scan(self, force: bool = False) -> dict:
        gm = self.svc.gmail
        why = why_no_gmail(gm)
        if why:
            self.error = why
            return {"ok": False, "error": why}
        if not force and self.clock() - self._scanned < float(self.setting("scan_minutes", 60)) * 60:
            return {"ok": True, "new": 0}
        db = self.svc.storage
        seen = {r["id"] for r in db.query("SELECT id FROM travel_seen")}
        try:
            mail, errors = scan_mail(gm, [QUERY], seen)
        except Exception as exc:
            self.error = f"Couldn't read Gmail ({type(exc).__name__})."
            return {"ok": False, "error": self.error}
        new = 0
        for m in sorted(mail, key=lambda x: x.get("ts") or 0):
            for item in parse_travel(m):
                self._store(item)
                new += 1
            db.execute("INSERT OR REPLACE INTO travel_seen VALUES (?,?)", (m["id"], self.clock()))
        self._scanned = self.clock()
        self.error = "; ".join(f"{k}: {v}" for k, v in errors.items()) or None
        return {"ok": True, "new": new}

    def _store(self, item: dict) -> None:
        """Save, merged with what's known about the same package (any shared tracking/order number or item) or trip."""
        old = next((o for o in self._items(item["kind"]) if o["key"] == item["key"] or
                    set(o.get("ids") or ()) & set(item.get("ids") or ())), None)
        if old:
            item = {**merge(old, item), "key": old["key"]}
            if "ids" in old:
                item["ids"] = sorted(set(old["ids"]) | set(item.get("ids") or ()))
        self.svc.storage.execute("INSERT OR REPLACE INTO travel_items VALUES (?,?,?,?)",
                                 (item["key"], item["kind"], item["ts"], json.dumps(item)))

    def _items(self, kind: str) -> list[dict]:
        return [json.loads(r["data"]) for r in self.svc.storage.query("SELECT data FROM travel_items WHERE kind=?", (kind,))]

    def packages(self) -> list[dict]:
        now, today = self.clock(), self.now().date()
        out = []
        for p in self._items("package"):
            age_d = (now - p["ts"]) / 86400
            if (p["stage"] == 3 and age_d > 2) or age_d > 21:
                continue  # delivered a while ago, or gone quiet
            eta = date.fromisoformat(p["eta"]) if p.get("eta") else None
            out.append({**p, "stage_name": STAGES[p["stage"]], "progress": p["stage"] / 3,
                        "when": day_word(eta, today) if eta else "", "today": bool(eta == today or p["stage"] == 2)})
        return sorted(out, key=lambda p: (p["stage"] == 3, p.get("eta") or "9999", -p["ts"]))

    def trips(self) -> dict:
        now = self.now()
        flights = []
        for f in self._items("flight"):
            hm = [int(x) for x in (f.get("time") or "00:00").split(":")]
            dep = datetime.combine(date.fromisoformat(f["date"]), datetime.min.time()).replace(
                hour=hm[0], minute=hm[1], tzinfo=self.svc.tz)
            if dep > now - timedelta(hours=3):
                flights.append({**f, "depart": dep.isoformat(), "in_s": int((dep - now).total_seconds()),
                                "when": day_word(dep.date(), now.date()),
                                "clock": dep.strftime("%I:%M %p").lstrip("0") if f.get("time") else ""})
        hotels = [{**x, "when": day_word(date.fromisoformat(x["checkin"]), now.date())} for x in self._items("hotel")
                  if date.fromisoformat(x.get("checkout") or x["checkin"]) >= now.date()]
        flights.sort(key=lambda f: f["depart"])
        return {"flights": flights, "hotels": sorted(hotels, key=lambda x: x["checkin"]),
                "next_flight": flights[0] if flights else None}

    # ---- travel day: places, the drive, the weather there, packing -------------------------------------------
    def resolve(self, name: str, fetch: bool = True) -> dict | None:
        """An airport code, a city or a place name -> {name, city, code, lat, lon, country} (None: unknown)."""
        raw = (name or "").strip()
        if not raw:
            return None
        code = raw.upper() if re.fullmatch(r"[A-Za-z]{3}", raw) and raw.upper() in AIRPORTS else BY_CITY.get(raw.lower())
        if code:
            city, lat, lon, country = AIRPORTS[code]
            return {"name": city, "city": city, "code": code, "lat": lat, "lon": lon, "country": country}
        key = raw.lower()
        if key in self._geo or not fetch:
            return self._geo.get(key)
        try:
            res = self.http.get(GEOCODE, params={"name": raw, "count": 1, "language": "en"}).json().get("results") or []
        except (httpx.HTTPError, ValueError):
            return None  # not cached: try again next time
        r = res[0] if res else None
        self._geo[key] = {"name": ", ".join(x for x in (r.get("name"), r.get("admin1")) if x), "city": r.get("name") or raw,
                          "code": "", "lat": r["latitude"], "lon": r["longitude"],
                          "country": (r.get("country_code") or "").upper()} if r else None
        return self._geo[key]

    def home(self, fetch: bool = True) -> dict | None:
        h = self.setting("home")
        if isinstance(h, dict) and "lat" in h and "lon" in h:
            return h
        if isinstance(h, str) and h.strip():
            return self.resolve(h, fetch)
        w = ((self.svc.cfg.get("skills") or {}).get("weather") or {}).get("place")
        return w if isinstance(w, dict) and "lat" in w else None

    def drive(self, airport: dict | None, fetch: bool = True) -> tuple[int, str]:
        """(minutes from home to ``airport``, where that number came from)."""
        home = self.home(fetch)
        if home and airport and km_between(home, airport) > float(self.setting("away_km", 150)):
            return int(self.setting("away_drive_min", 30)), "away"  # flying home: from wherever you're staying
        if self.setting("drive_min"):
            return int(self.setting("drive_min")), "setting"
        if home and airport:
            route = f"{home['lat']:.3f},{home['lon']:.3f}>{airport['lat']:.3f},{airport['lon']:.3f}"
            row = self.svc.storage.query("SELECT minutes, ts FROM travel_drive WHERE route=?", (route,))
            if row and self.clock() - row[0]["ts"] < 30 * 86400:
                return int(row[0]["minutes"]), "route"
            if fetch and self.clock() >= self._route_down_until:
                try:
                    r = self.http.get(OSRM.format(home["lon"], home["lat"], airport["lon"], airport["lat"]),
                                      params={"overview": "false"}).json()
                    secs = float(r["routes"][0]["duration"]) if r.get("code") == "Ok" and r.get("routes") else None
                except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                    secs = None
                if secs is not None:
                    mins = math.ceil(secs / 60 * float(self.setting("traffic_factor", 1.2)) / 5) * 5
                    self.svc.storage.execute("INSERT OR REPLACE INTO travel_drive VALUES (?,?,?)", (route, mins, self.clock()))
                    return int(mins), "route"
                self._route_down_until = self.clock() + 3600  # blocked or down: guess for an hour, then try again
        return int(self.setting("drive_default_min", 45)), "guess"

    def forecast_at(self, place: dict | None, fetch: bool = True) -> dict:
        """The weather skill's forecast shape (now, hours, days, advice) for any place."""
        if not place:
            return {"ok": False, "error": "I don't know where that is."}
        key = f"{place['lat']:.2f},{place['lon']:.2f}"
        hit = self._wx.get(key)
        if hit and (self.clock() - hit[0] < 3600 or not fetch):
            return hit[1]
        if not fetch:
            return {"ok": False, "error": "No forecast yet."}
        units = str(((self.svc.cfg.get("skills") or {}).get("weather") or {}).get("units", "fahrenheit")).lower()
        f = units.startswith("f")
        try:
            r = self.http.get(FORECAST, params={
                "latitude": place["lat"], "longitude": place["lon"], "timezone": "auto", "forecast_days": 7,
                "temperature_unit": "fahrenheit" if f else "celsius", "wind_speed_unit": "mph" if f else "kmh",
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day,precipitation",
                "hourly": "temperature_2m,precipitation_probability,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset,uv_index_max",
            }).json()
            if "current" not in r:
                return {"ok": False, "error": str(r.get("reason") or "No forecast came back.")}
            data = shape(r, {"name": place.get("name") or place.get("city")}, None, "°F" if f else "°C")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "error": f"The weather service didn't answer ({type(exc).__name__})."}
        data["advice"] = advice(data)
        self._wx[key] = (self.clock(), data)
        return data

    def weather_in(self, place: str = "") -> dict:
        where = self.resolve(place)
        if where is None:
            return {"ok": False, "error": f"I can't find a place called {place}." if place else "Where?"}
        return self.forecast_at(where)

    def _nights(self, f: dict, flights: list[dict]) -> int | None:
        back = [x for x in flights if x["date"] > f["date"] and f.get("to") and x.get("from") and
                (x["from"] == f["to"] or self._same(x["from"], f["to"]))]
        if back:
            return (date.fromisoformat(back[0]["date"]) - date.fromisoformat(f["date"])).days
        for h in self._items("hotel"):
            if h["checkin"] in (f["date"], (date.fromisoformat(f["date"]) + timedelta(days=1)).isoformat()) and h.get("checkout"):
                return (date.fromisoformat(h["checkout"]) - date.fromisoformat(h["checkin"])).days
        return None

    def _same(self, a: str, b: str) -> bool:
        x, y = self.resolve(a, fetch=False), self.resolve(b, fetch=False)
        return bool(x and y and x["city"] == y["city"])

    def plan(self, f: dict, fetch: bool = True, flights: list[dict] | None = None) -> dict:
        """Leave-by, check-in, the weather there and the packing list for one flight (``trips()`` shape)."""
        dep = datetime.fromisoformat(f["depart"])
        origin, dest = self.resolve(f.get("from") or "", fetch), self.resolve(f.get("to") or "", fetch)
        home_c = str(self.setting("home_country", "US")).upper()
        countries = {p["country"] for p in (origin, dest) if p and p.get("country")}
        intl = len(countries) > 1 or bool(countries and countries != {home_c})
        buffer_min = int(self.setting("buffer_international_min" if intl else "buffer_domestic_min", 180 if intl else 120))
        drive_min, drive_from = self.drive(origin, fetch)
        leave = None
        if f.get("time"):
            leave = dep - timedelta(minutes=buffer_min + drive_min)
            leave -= timedelta(minutes=leave.minute % 5, seconds=leave.second)
        wx = self.forecast_at(dest, fetch) if dest else {"ok": False}
        nights = self._nights(f, flights if flights is not None else self.trips()["flights"])
        weather = None
        if wx.get("ok"):
            last = date.fromisoformat(f["date"]) + timedelta(days=max(0, nights if nights is not None else 2))
            days = [d for d in wx["days"] if f["date"] <= d["date"] <= last.isoformat()] or wx["days"][:3]
            weather = {"place": (dest or {}).get("city") or wx["place"], "unit": wx["unit"], "now": wx["now"], "days": days,
                       "advice": wx.get("advice")}
        code = f["flight"].split()[0]
        template = packing_template(nights, intl, weather, self.setting("packing_extra") or [],
                                    bool(self.setting("pack_laptop", True)))
        return {"key": f["key"], "flight": f["flight"], "airline": f["airline"], "from": f.get("from") or "",
                "to": f.get("to") or "", "dest": (dest or {}).get("city") or f.get("to") or "", "date": f["date"],
                "depart": f["depart"], "clock": f.get("clock") or "", "confirmation": f.get("confirmation") or "",
                "link": f.get("link") or "", "international": intl, "buffer_min": buffer_min, "drive_min": drive_min,
                "drive_from": drive_from, "leave_by": leave.isoformat() if leave else None,
                "leave_clock": say_clock(leave) if leave else "",
                "checkin_url": (self.setting("checkin_urls") or {}).get(code) or CHECKIN.get(code, ""),
                "checkin_opens": (dep - timedelta(hours=24)).isoformat(), "nights": nights, "weather": weather,
                "template": template}

    def _timed(self, p: dict) -> dict:
        now = self.now()
        dep = datetime.fromisoformat(p["depart"])
        leave = datetime.fromisoformat(p["leave_by"]) if p.get("leave_by") else None
        return {**{k: v for k, v in p.items() if k != "template"}, "in_s": int((dep - now).total_seconds()),
                "leave_in_s": int((leave - now).total_seconds()) if leave else None,
                "today": dep.date() == now.date(), "when": day_word(dep.date(), now.date()),
                "checkin_open": now >= datetime.fromisoformat(p["checkin_opens"]), "packing": self.packing(p["key"], p["template"])}

    def travel_day(self, fetch: bool = True) -> dict | None:
        """The next flight's plan, from the day before it until it leaves (None: no flight that soon)."""
        tr = self.trips()
        f = next((x for x in tr["flights"] if -1800 < x["in_s"] <= float(self.setting("day_hours", 36)) * 3600), None)
        if f is None:
            return None
        if fetch or self._day is None or self._day.get("key") != f["key"]:
            self._day = self.plan(f, fetch, tr["flights"])
        return self._timed(self._day)

    # ---- the packing list (kept per trip: tick, add, remove) --------------------------------------------------
    def packing(self, trip: str, template: list[str] | None = None) -> list[dict]:
        db = self.svc.storage
        if template:
            n = len(db.query("SELECT item FROM travel_packing WHERE trip=?", (trip,)))
            for i, item in enumerate(template):  # new auto items only; ones J removed stay removed
                db.execute("INSERT OR IGNORE INTO travel_packing (trip, item, done, auto, removed, ord) VALUES (?,?,0,1,0,?)",
                           (trip, item, n + i))
        rows = db.query("SELECT item, done, auto FROM travel_packing WHERE trip=? AND removed=0 ORDER BY ord", (trip,))
        return [{"item": r["item"], "done": bool(r["done"]), "auto": bool(r["auto"])} for r in rows]

    def _pack(self, b: dict, what: str) -> dict:
        trip, item = str(b.get("trip") or ""), re.sub(r"\s+", " ", str(b.get("item") or b.get("text") or "")).strip()[:60]
        if not trip or not item:
            return {"ok": False, "error": "Which item?"}
        db = self.svc.storage
        if what == "add":
            top = db.query("SELECT MAX(ord) AS m FROM travel_packing WHERE trip=?", (trip,))
            db.execute("INSERT INTO travel_packing (trip, item, done, auto, removed, ord) VALUES (?,?,0,0,0,?) "
                       "ON CONFLICT(trip, item) DO UPDATE SET removed=0", (trip, item, (top[0]["m"] or 0) + 1))
        elif what == "toggle":
            db.execute("UPDATE travel_packing SET done = 1 - done WHERE trip=? AND item=?", (trip, item))
        else:
            db.execute("UPDATE travel_packing SET removed=1, done=0 WHERE trip=? AND item=?", (trip, item))
        return {"ok": True, "packing": self.packing(trip)}

    def status(self) -> dict:
        why = why_no_gmail(self.svc.gmail)
        pk, tr = self.packages(), self.trips()
        if why and not pk and not tr["flights"] and not tr["hotels"]:
            return {"ok": False, "error": why}
        try:
            day = self.travel_day(fetch=False)
        except Exception:  # the card is extra: never lose the panel over it
            day = None
        return {"ok": True, "packages": pk, **tr, "warning": why or self.error, "scanned_at": self._scanned or None,
                "stages": STAGES, "live": bool(self.svc.live and self.svc.live()), "travel_day": day}

    def tick(self) -> None:
        try:
            self.scan()
            self.travel_day(fetch=True)
            self._nudges()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        self.publish()

    def _nudges(self) -> None:
        for p in self.packages():
            if p["stage"] == 2 and self.clock() - p["ts"] < 18 * 3600:
                self.nudge(f"Your package from {p['merchant']} is out for delivery.", priority="low",
                           key=f"travel:out:{p['key']}")
        for f in self.trips()["flights"]:
            if 0 < f["in_s"] <= 24 * 3600:
                opens = "check in opens" if f["in_s"] > 20 * 3600 else "check in is open"
                self.nudge(f"Flight {f['flight']} {f['when']}{' ' + f['clock'] if f['clock'] else ''} — {opens}.",
                           priority="normal", key=f"travel:checkin:{f['key']}", expires_in=f["in_s"])
        day = self.travel_day(fetch=False)
        left = (day or {}).get("leave_in_s")
        if left is not None and 0 < left <= int(self.setting("leave_warn_min", 30)) * 60:
            self.nudge(f"Leave by {day['leave_clock']} for {day['flight']}.", priority="urgent",
                       key=f"travel:leave:{day['key']}", expires_in=left + 1800)

    def briefing(self):
        pk = [p for p in self.packages() if p["today"] and p["stage"] < 3]
        nxt = self.trips()["next_flight"]
        out = {}
        if pk:
            out["arriving_today"] = [p["item"] or f"a package from {p['merchant']}" for p in pk[:4]]
        if nxt and nxt["in_s"] < 3 * 86400:
            out["flight"] = f"{nxt['flight']} {nxt['when']} {nxt['clock']}".strip()
        return out or None

    def actions(self):
        return {"scan": lambda b: self.scan(force=True), "forget": self._forget,
                "pack_toggle": lambda b: self._pack(b, "toggle"), "pack_add": lambda b: self._pack(b, "add"),
                "pack_remove": lambda b: self._pack(b, "remove")}

    def _forget(self, b: dict) -> dict:
        self.svc.storage.execute("DELETE FROM travel_items WHERE key=?", (str(b.get("key") or ""),))
        return {"ok": True}

    # ---- the brain ----------------------------------------------------------------------------------------
    def tools(self):
        kw = {"untrusted": True, "untrusted_label": "an email", "untrusted_note": EMAIL_NOTE}
        return [
            Tool("packages", "Packages on the way, from shipping emails in Gmail: from whom, what, carrier and tracking, "
                             "stage (ordered/shipped/out for delivery/delivered) and arrival day. when='today' for "
                             "today's deliveries.", obj({"when": {"type": "string", "enum": ["all", "today"]}}),
                 self._packages, **kw),
            Tool("trips", "Upcoming flights (airline, flight number, date, time, from/to, confirmation code) and hotel "
                          "stays, from booking emails in Gmail.", obj(), self._trips, **kw),
            Tool("leave_for_flight", "When the user needs to leave for their next flight: departure minus the airport "
                                     "buffer and the drive, plus check-in, the weather where they're going and the "
                                     "packing list.", obj(), self._leave, **kw),
            Tool("weather_in", "The weather in another city (now and the week): 'what's the weather in Denver'.",
                 obj({"place": {"type": "string"}}, ["place"]), self.weather_in),
        ]

    def _leave(self) -> dict:
        r = self.scan()
        tr = self.trips()
        f = tr["next_flight"]
        if f is None:
            return {"ok": r.get("ok", True), "plan": None, **({"error": r["error"]} if not r.get("ok") else {})}
        p = self.plan(f, True, tr["flights"])
        if self._day is None or self._day.get("key") == p["key"]:
            self._day = p
        return {"ok": True, "plan": self._timed(p)}

    def _packages(self, when: str = "all") -> dict:
        r = self.scan()
        pk = self.packages()
        if when == "today":
            pk = [p for p in pk if p["today"] or (p["stage"] == 3 and p.get("eta") == self.now().date().isoformat())]
        return {"ok": r.get("ok", True) or bool(pk), "when": when, "packages": pk, **({"error": r["error"]} if not r.get("ok") else {})}

    def _trips(self) -> dict:
        r = self.scan()
        tr = self.trips()
        return {"ok": r.get("ok", True) or bool(tr["flights"] or tr["hotels"]), **tr,
                **({"error": r["error"]} if not r.get("ok") else {})}

    def route(self, t: str):
        if re.fullmatch(r"(?:are there )?any deliveries(?: coming)? today|is anything (?:being delivered|arriving|coming) today|"
                        r"what'?s (?:arriving|being delivered|coming) today|any packages (?:coming |arriving )?today", t):
            return Intent("tool", "packages", {"when": "today"})
        if re.fullmatch(r"where'?s my (?:package|order|delivery|stuff)|where are my (?:packages|orders)|"
                        r"track my (?:package|order)s?|(?:any|my) packages(?: on the way)?|what'?s on the way", t):
            return Intent("tool", "packages", {"when": "all"})
        if re.fullmatch(r"when'?s my (?:next )?flight|when do i (?:fly|leave)(?: next)?|what'?s my (?:flight|trip)(?: info| details)?|"
                        r"(?:any|my) (?:upcoming )?(?:trips|flights)|what time is my flight", t):
            return Intent("tool", "trips", {})
        if re.fullmatch(r"(?:when|what time) (?:do|should|must) i (?:need to |have to )?(?:leave|head out|go|get going) "
                        r"(?:for|to) (?:my|the) (?:flight|airport|plane)|when (?:do|should) i (?:head|go) to the airport|"
                        r"what time should i leave(?: for the airport)?|when'?s my leave(?:-| )by(?: time)?", t):
            return Intent("tool", "leave_for_flight", {})
        place = asked_place(t)
        if place:
            return Intent("tool", "weather_in", {"place": place})
        return None

    def say(self, tool, args, result):
        live = bool(self.svc.live and self.svc.live())
        if tool == "packages":
            pk = [p for p in result.get("packages") or [] if p["stage"] < 3 or result.get("when") == "today"]
            if not pk:
                return result.get("error") or ("Nothing's due to arrive today." if result.get("when") == "today"
                                               else "No packages on the way.")
            if live:
                return f"{len(pk)} package{'s' if len(pk) != 1 else ''} on the HUD."
            parts = [f"{'your ' + p['item'] if p['item'] else 'a package'} from {p['merchant']}, "
                     + (p["stage_name"].lower() + (f", arriving {p['when']}" if p["when"] and p["stage"] < 2 else ""))
                     for p in pk[:3]]
            return ("Today: " if result.get("when") == "today" else "On the way: ") + "; ".join(parts) + \
                (f"; and {len(pk) - 3} more on the HUD." if len(pk) > 3 else ".")
        if tool == "trips":
            f = result.get("next_flight")
            if live:
                return "Your trip details are on the HUD."
            if not f:
                h = (result.get("hotels") or [None])[0]
                if h:
                    return f"No flights coming up. You check in at {h['hotel']} {h['when']}."
                return result.get("error") or "I don't see any upcoming flights in your email."
            route = f", {f['from']} to {f['to']}" if f.get("from") and f.get("to") else ""
            conf = f" Confirmation {' '.join(f['confirmation'])}." if f.get("confirmation") else ""
            return f"{f['airline']} {f['flight'].split()[1]}, {f['when']}{' at ' + f['clock'] if f['clock'] else ''}{route}.{conf}"
        if tool == "leave_for_flight":
            p = result.get("plan")
            if not p:
                return result.get("error") or "I don't see a flight coming up in your email."
            if live:
                return "Your travel times are on the HUD."
            if not p.get("leave_by"):
                return f"{p['flight']} is {p['when']}, but I couldn't read its departure time. Check the email."
            how = (f"{p['buffer_min'] // 60} hours at the airport" + (f" and about {p['drive_min']} minutes' drive"
                                                                        if p["drive_from"] != "guess" else
                                                                        f" and a {p['drive_min']}-minute drive I guessed"))
            if p["leave_in_s"] is not None and p["leave_in_s"] < 0:
                return f"You should have left at {p['leave_clock']}: {p['flight']} leaves at {p['clock']}. Go now."
            day = "" if p["today"] else f"{p['when'][0].upper()}{p['when'][1:]}, l"
            soon = f" That's in {countdown_words(p['leave_in_s'])}." if p["today"] else ""
            return f"{day or 'L'}eave by {p['leave_clock']} for {p['flight']} at {p['clock']}: {how}.{soon}"
        if tool == "weather_in":
            return spoken(result, "today")
        return None
