"""Packages and trips, from Gmail (work and personal), read-only: the Google Now cards.

"Where's my package", "any deliveries today", "when's my flight". Shipping mail (Amazon, UPS, FedEx, USPS, DHL and
shops) gives a package card: who it's from, what, the carrier and tracking number (with the carrier's tracking page),
how far along (ordered → shipped → out for delivery → delivered) and when it arrives. Airline and hotel mail gives
the trips: flight number, date and time, from/to, confirmation code; hotel check-in and check-out.

Nudges: a package out for delivery (low: the HUD feed), a flight within 24 hours, when check-in opens (normal).
On air nothing about trips or deliveries is said out loud beyond "it's on the HUD".
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta

from ..brain.router import Intent
from ..brain.tools import EMAIL_NOTE, Tool, obj
from . import Skill
from .bills import date_after, day_word, find_dates, merchant_of, scan_mail, why_no_gmail

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


class Travel(Skill):
    name, title = "travel", "On the way"
    poll_s = 1800
    game_poll_s = 7200
    delay_s = 120

    def __init__(self, svc, clock=time.time):
        super().__init__(svc)
        self.clock = clock
        self._scanned = 0.0
        self.error: str | None = None
        svc.storage.execute("CREATE TABLE IF NOT EXISTS travel_items (key TEXT PRIMARY KEY, kind TEXT, ts REAL, data TEXT)")
        svc.storage.execute("CREATE TABLE IF NOT EXISTS travel_seen (id TEXT PRIMARY KEY, ts REAL)")

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

    def status(self) -> dict:
        why = why_no_gmail(self.svc.gmail)
        pk, tr = self.packages(), self.trips()
        if why and not pk and not tr["flights"] and not tr["hotels"]:
            return {"ok": False, "error": why}
        return {"ok": True, "packages": pk, **tr, "warning": why or self.error, "scanned_at": self._scanned or None,
                "stages": STAGES, "live": bool(self.svc.live and self.svc.live())}

    def tick(self) -> None:
        try:
            self.scan()
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
        return {"scan": lambda b: self.scan(force=True), "forget": self._forget}

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
        ]

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
        return None
