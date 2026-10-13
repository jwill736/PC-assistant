"""Packages and trips from Gmail: real-looking Amazon, UPS, FedEx, USPS, Delta, United and Marriott mail, merged into
cards, nudges at the right time, voice, and nothing about trips said on air."""

from datetime import date, datetime

import httpx
import pytest
from test_google import CLIENT_ID, HOME, SECRET, WORK, connect
from test_skill_bills import NY, at, mail

from assistant.brain.router import RouterContext, route
from assistant.integrations.gmail import Gmail
from assistant.integrations.google import GoogleAccounts
from assistant.skills import Skills
from assistant.skills import travel as tv

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=NY)  # a Tuesday
CABLE = "Anker USB-C to USB-C Cable (6 ft)"

DELTA = """Hello J,
Thank you for choosing Delta.

Confirmation #: G7KX2P

Fri, 16OCT
FLIGHT  DELTA 1234
Main Cabin (V)
DEPART  ATLANTA 7:05am
ARRIVE  AUSTIN 8:40am

Sun, 18OCT
FLIGHT  DELTA 2290
DEPART  AUSTIN 6:15pm
ARRIVE  ATLANTA 9:50pm

You can check in 24 hours before departure on fly.delta.com or the Fly Delta app."""

UNITED = """Thanks for choosing United.
Confirmation number: KX7P2Q

Flight 1 of 1
UA 1587   Tue, Oct 20, 2026
Departs 10:30 AM Atlanta, GA (ATL)
Arrives 12:05 PM Denver, CO (DEN)
Economy (K)"""

MARRIOTT = """Thank you for booking with us, J.

Confirmation Number: 81726354
Check-in: Friday, October 16, 2026 (after 4:00 PM)
Check-out: Sunday, October 18, 2026 (before 11:00 AM)

Courtyard Austin Downtown/Convention Center
300 E 4th St, Austin, TX 78701"""


def inbox():
    amazon = "Amazon.com <shipment-tracking@amazon.com>"
    return {
        HOME: [
            mail("a1", "Amazon.com <auto-confirm@amazon.com>", f'Ordered: "{CABLE}"',
                 "Thanks for your order, J!\nOrder # 112-4567890-1234567\nArriving Thursday", at(10, 10)),
            mail("a2", amazon, f'Shipped: "{CABLE}"', "Your package is on the way.\nOrder # 112-4567890-1234567\n"
                 "Arriving Thursday, October 15\nCarrier: Amazon Logistics, Tracking ID: TBA312345678901", at(10, 12)),
            mail("a3", amazon, f'Out for delivery: "{CABLE}"', "Your package is out for delivery.\nArriving today by 9 PM",
                 datetime(2026, 10, 13, 7, 0, tzinfo=NY)),
            mail("u1", "UPS <mcinfo@ups.com>", "UPS Update: Package Scheduled for Delivery Thursday, October 15, 2026",
                 "Shipper: Best Buy\nTracking Number: 1Z999AA10123456784\nScheduled Delivery: Thursday, 10/15/2026", at(10, 12)),
            mail("f1", "FedEx <TrackingUpdates@fedex.com>", "Your package has been delivered",
                 "Tracking number 123456789012\nDelivered: Mon 10/12/2026 at 2:14 PM\nShipper: B&H Photo", at(10, 12)),
            mail("o1", "FedEx <TrackingUpdates@fedex.com>", "Your package has been delivered",
                 "Tracking number 111122223333\nShipper: Old Shop", at(9, 20)),  # long ago: off the list
            mail("p1", "USPS <auto-reply@usps.com>", "USPS® Expected Delivery on Tuesday, October 13, 2026 arriving by 9:00pm",
                 "Tracking Number: 9400 1000 0000 0000 0000 00\nExpected Delivery on Tuesday", at(10, 12)),
            mail("dl", "Delta Air Lines <DeltaAirLines@t.delta.com>", "Your Delta Flight Confirmation ATL > AUS", DELTA, at(10, 1)),
            mail("mr", "Marriott Bonvoy <marriott@email-marriott.com>",
                 "Reservation Confirmation #81726354 for Courtyard Austin Downtown/Convention Center", MARRIOTT, at(10, 1)),
            mail("nf", "Netflix <info@account.netflix.com>", "Your Netflix receipt", "Amount charged: $15.49", at(10, 1)),
            mail("mom", "Mom <mom@example.com>", "Sunday dinner", "Are you coming at 6? Dad's flight lands at 5.", at(10, 12)),
        ],
        WORK: [mail("ua", "United Airlines <unitedairlines@united.com>", "eTicket Itinerary and Receipt for Confirmation KX7P2Q",
                    UNITED, at(10, 5))],
    }


class Clock:
    t = NOW.timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def travel(svc, tmp_path):
    from test_gmail import FakeGmail

    fake = FakeGmail()
    fake.mail = inbox()
    acc = GoogleAccounts(CLIENT_ID, SECRET, tmp_path / "t.json", http=httpx.Client(transport=httpx.MockTransport(fake)))
    connect(acc, fake, "code-work")
    connect(acc, fake, "code-home")
    svc.gmail = Gmail(acc)
    clock = Clock()
    t = tv.Travel(svc, clock=clock)
    t.clock_obj, t.fake, t.nudged = clock, fake, []
    t.nudge = lambda text, **kw: t.nudged.append((text, kw.get("priority")))
    return t


# ---- reading one email ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,carrier,num", [
    ("Tracking Number: 1Z999AA10123456784", "UPS", "1Z999AA10123456784"),
    ("USPS Tracking 9400 1000 0000 0000 0000 00", "USPS", "9400100000000000000000"),
    ("USPS: 9205590164917312751089", "USPS", "9205590164917312751089"),
    ("Tracking ID: TBA312345678901", "Amazon", "TBA312345678901"),
    ("FedEx tracking number 123456789012", "FedEx", "123456789012"),
    ("DHL Express waybill, tracking 1234567890", "DHL", "1234567890"),
    ("International: RA123456789US", "USPS", "RA123456789US"),
])
def test_tracking_numbers_and_carrier_links(text, carrier, num):
    c, n, url = tv.tracking(text)
    assert (c, n) == (carrier, num) and num in url and url.startswith("https://")


def test_no_tracking_from_phone_numbers_or_order_totals():
    assert tv.tracking("Call DHL at 8005551234 any time") is None  # no "tracking" before it
    assert tv.tracking("Your order of 123456789012 widgets") is None  # FedEx-shaped, but no FedEx


def test_amazon_stages_and_item():
    def p(mid):
        return tv.parse_package({**next(m for m in _flat() if m["id"] == mid)})
    a1, a2, a3 = p("a1"), p("a2"), p("a3")
    assert [a1["stage"], a2["stage"], a3["stage"]] == [0, 1, 2]
    assert a1["item"] == CABLE and a1["merchant"] == "Amazon" and a1["eta"] == "2026-10-15"
    assert a2["carrier"] == "Amazon" and a2["track_url"].endswith("TBA312345678901") and a2["eta"] == "2026-10-15"
    assert a3["eta"] == "2026-10-13"
    assert set(a1["ids"]) & set(a2["ids"]) and set(a2["ids"]) & set(a3["ids"])  # all three are one package


def test_carrier_mail_names_the_shop():
    u = tv.parse_package(next(m for m in _flat() if m["id"] == "u1"))
    assert (u["merchant"], u["carrier"], u["stage"], u["eta"]) == ("Best Buy", "UPS", 1, "2026-10-15")
    f = tv.parse_package(next(m for m in _flat() if m["id"] == "f1"))
    assert (f["merchant"], f["carrier"], f["stage"]) == ("B&H Photo", "FedEx", 3)


def test_flights_delta_two_legs_and_united():
    legs = tv.parse_flights(next(m for m in _flat() if m["id"] == "dl"))
    assert [(x["flight"], x["date"], x["time"], x["from"], x["to"]) for x in legs] == [
        ("DL 1234", "2026-10-16", "07:05", "Atlanta", "Austin"), ("DL 2290", "2026-10-18", "18:15", "Austin", "Atlanta")]
    assert legs[0]["confirmation"] == "G7KX2P" and legs[0]["airline"] == "Delta"
    ua = tv.parse_flights(next(m for m in _flat() if m["id"] == "ua"))
    assert [(x["flight"], x["date"], x["time"], x["from"], x["to"], x["confirmation"]) for x in ua] == [
        ("UA 1587", "2026-10-20", "10:30", "ATL", "DEN", "KX7P2Q")]


def test_hotel_marriott():
    h = tv.parse_hotel(next(m for m in _flat() if m["id"] == "mr"))
    assert (h["hotel"], h["checkin"], h["checkout"], h["time"], h["confirmation"]) == \
        ("Courtyard Austin Downtown/Convention Center", "2026-10-16", "2026-10-18", "16:00", "81726354")


def test_other_mail_is_left_alone():
    for mid in ("nf", "mom"):
        assert tv.parse_travel(next(m for m in _flat() if m["id"] == mid)) == []


def _flat():
    """The sample mail in Gmail ``read()`` shape."""
    from assistant.integrations.gmail import body_text, summarize

    out = []
    for acct, box in inbox().items():
        for m in box:
            out.append({**summarize(acct, m), "body": body_text(m["payload"])})
    return out


# ---- the skill -----------------------------------------------------------------------------------------------

def test_scan_merges_packages_and_lists_trips(travel):
    assert travel.scan()["ok"]
    pk = travel.packages()
    names = [(p["merchant"], p["stage_name"], p["when"]) for p in pk]
    assert names == [("Amazon", "Out for delivery", "today"), ("USPS", "Shipped", "today"),
                     ("Best Buy", "Shipped", "Thursday"), ("B&H Photo", "Delivered", "Oct 12")]
    amazon = pk[0]
    assert amazon["item"] == CABLE and amazon["tracking"] == "TBA312345678901" and amazon["progress"] == 2 / 3
    assert amazon["link"].startswith("https://mail.google.com/") and amazon["today"]
    tr = travel.trips()
    assert [f["flight"] for f in tr["flights"]] == ["DL 1234", "DL 2290", "UA 1587"]
    nf = tr["next_flight"]
    assert nf["when"] == "Friday" and nf["clock"] == "7:05 AM" and nf["in_s"] == int((datetime(2026, 10, 16, 7, 5, tzinfo=NY) - NOW).total_seconds())
    assert tr["hotels"][0]["hotel"].startswith("Courtyard") and tr["hotels"][0]["when"] == "Friday"
    st = travel.status()
    assert st["ok"] and st["stages"][2] == "Out for delivery" and st["warning"] is None


def test_nudges_out_for_delivery_and_check_in(travel):
    travel.tick()
    assert ("Your package from Amazon is out for delivery.", "low") in travel.nudged
    assert not any("Flight" in n for n, _ in travel.nudged)  # Friday's flight is days away
    travel.clock_obj.t = datetime(2026, 10, 15, 9, 0, tzinfo=NY).timestamp()
    travel.tick()
    assert ("Flight DL 1234 tomorrow 7:05 AM — check in opens.", "normal") in travel.nudged
    travel.clock_obj.t = datetime(2026, 10, 16, 3, 0, tzinfo=NY).timestamp()  # Vesper only started now
    travel.tick()
    assert travel.nudged[-1] == ("Flight DL 1234 today 7:05 AM — check in is open.", "normal")


def test_past_flights_and_trips_drop_off(travel):
    travel.scan()
    travel.clock_obj.t = datetime(2026, 10, 19, 12, 0, tzinfo=NY).timestamp()
    tr = travel.trips()
    assert [f["flight"] for f in tr["flights"]] == ["UA 1587"] and tr["hotels"] == []


def test_forget_a_card(travel):
    travel.scan()
    key = travel.packages()[-1]["key"]
    assert travel.actions()["forget"]({"key": key})["ok"]
    assert key not in {p["key"] for p in travel.packages()}


def test_without_gmail_says_why(svc):
    t = tv.Travel(svc, clock=lambda: NOW.timestamp())
    st = t.status()
    assert st["ok"] is False and "Setup → Google accounts" in st["error"]
    assert "Setup → Google accounts" in t.say("packages", {}, t._packages())
    assert "Setup → Google accounts" in t.say("trips", {}, t._trips())


# ---- voice ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("said,tool,args", [
    ("where's my package", "packages", {"when": "all"}),
    ("where are my orders", "packages", {"when": "all"}),
    ("any deliveries today", "packages", {"when": "today"}),
    ("is anything arriving today", "packages", {"when": "today"}),
    ("when's my flight", "trips", {}),
    ("when's my next flight", "trips", {}),
    ("what time is my flight", "trips", {}),
])
def test_routes(svc, said, tool, args):
    i = route(said, RouterContext(skill_route=Skills(svc, ["travel"]).route))
    assert (i.tool, i.args) == (tool, args)


def test_spoken_replies_and_nothing_private_on_air(travel, svc):
    tools = {t.name: t for t in travel.tools()}
    assert tools["packages"].untrusted and tools["trips"].untrusted_label == "an email"
    today = tools["packages"].handler(when="today")
    assert travel.say("packages", {"when": "today"}, today) == \
        f"Today: your {CABLE} from Amazon, out for delivery; a package from USPS, shipped, arriving today."
    allp = tools["packages"].handler(when="all")
    assert travel.say("packages", {}, allp).startswith("On the way: your ") and "Best Buy, shipped, arriving Thursday" in \
        travel.say("packages", {}, allp)
    trips = tools["trips"].handler()
    assert travel.say("trips", {}, trips) == "Delta 1234, Friday at 7:05 AM, Atlanta to Austin. Confirmation G 7 K X 2 P."
    svc.live = lambda: True
    assert travel.say("trips", {}, trips) == "Your trip details are on the HUD."
    assert travel.say("packages", {}, allp) == "3 packages on the HUD."
    assert travel.say("packages", {}, {"ok": True, "when": "today", "packages": []}) == "Nothing's due to arrive today."


def test_briefing(travel):
    travel.scan()
    travel.clock_obj.t = datetime(2026, 10, 14, 7, 0, tzinfo=NY).timestamp()
    b = travel.briefing()
    assert b["flight"] == "DL 1234 Friday 7:05 AM" and CABLE in b["arriving_today"]


def test_dates_round_trip():
    assert tv.relative_day("thursday", date(2026, 10, 10)) == date(2026, 10, 15)
    assert tv.relative_day("tuesday", date(2026, 10, 13)) == date(2026, 10, 13)
    assert tv.relative_day("soon", date(2026, 10, 13)) is None


def test_fast_path_end_to_end(travel, svc):
    from assistant.brain.assistant import Assistant

    svc.skills = Skills(svc, ["travel"])
    svc.skills.all["travel"] = travel
    assert Assistant(svc).handle("when's my flight")["reply"].startswith("Delta 1234, Friday at 7:05 AM, Atlanta to Austin.")
