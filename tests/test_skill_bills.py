"""Bills & subscriptions from Gmail receipts: reading real-looking receipts, adding them up, nudges, voice, and the
guard rails (no Gmail, no model, nothing about money said on air)."""

from datetime import date, datetime
from email.utils import format_datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from test_gmail import FakeGmail, msg
from test_google import CLIENT_ID, HOME, SECRET, WORK, connect

from assistant.brain.router import RouterContext, route
from assistant.integrations.gmail import Gmail
from assistant.integrations.google import GoogleAccounts
from assistant.skills import Skills
from assistant.skills import bills as bl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=NY)  # a Tuesday


def mail(mid, frm, subject, body, when, html=None):
    m = msg(mid, frm, subject, body=body, html=html, labels=("INBOX", "CATEGORY_UPDATES"))
    for hd in m["payload"]["headers"]:
        if hd["name"] == "Date":
            hd["value"] = format_datetime(when)
    return m


def at(month, day, year=2026):
    return datetime(year, month, day, 8, 30, tzinfo=NY)


NETFLIX = """Hi J,

Thanks for being a Netflix member. Here's your receipt.

Plan: Standard with ads
Amount charged: $15.49
Payment method: Visa ending 4242

Next billing date: {next}

Questions? Visit the Help Center."""

SPOTIFY = """Spotify Premium

Receipt #A1B2C3

Premium Individual
Subtotal   $10.99
Tax        $1.00
Total      $11.99

Your plan renews automatically. Cancel anytime in your account page."""

ADOBE = """Hi J,

Your free trial of Creative Cloud Pro ends on Thursday, October 15, 2026.

After your trial, your subscription continues at US$69.99/mo (plus applicable taxes) unless you cancel before then.

Manage your plan: https://account.adobe.com"""


def inbox():
    return {
        HOME: [
            mail("nf1", "Netflix <info@account.netflix.com>", "Your Netflix receipt", NETFLIX.format(next="September 14, 2026"), at(8, 14)),
            mail("nf2", "Netflix <info@account.netflix.com>", "Your Netflix receipt", NETFLIX.format(next="October 14, 2026"), at(9, 14)),
            mail("sp1", "Spotify <no-reply@spotify.com>", "Your Spotify Premium receipt", SPOTIFY, at(8, 28)),
            mail("sp2", "Spotify <no-reply@spotify.com>", "Your Spotify Premium receipt", SPOTIFY, at(9, 28)),
            mail("ad1", "Adobe <mail@mail.adobe.com>", "Your free trial of Creative Cloud Pro ends soon", ADOBE, at(10, 8)),
            mail("dp1", "Disney+ <disneyplus@mail.disneyplus.com>", "Your Disney+ receipt",
                 "Disney+ Standard\nTotal: 9,99 € per month\nThanks for subscribing.", at(10, 2)),
            mail("hu1", "Hulu <hulu@hulumail.com>", "Your Hulu receipt", "Hulu (With Ads)\nTotal: $9.99/month", at(6, 1)),
            mail("gp1", "Georgia Power <noreply@georgiapower.com>", "Your bill is ready",
                 "Your October statement is ready.\nAmount due: $84.12\nDue date: October 20, 2026", at(10, 6)),
            mail("am1", "Amazon.com <shipment-tracking@amazon.com>", 'Your Amazon.com order of "Anker USB-C Cable" has shipped',
                 "Order total: $12.99\nArriving Thursday", at(10, 12)),
            mail("dl1", "Delta Air Lines <DeltaAirLines@t.delta.com>", "Your Flight Receipt - J WILLIAMS 16OCT26",
                 "Ticket total: $342.20\nDL 1234 ATL to AUS", at(10, 1)),
            mail("mom", "Mom <mom@example.com>", "Sunday dinner", "Are you coming at 6?", at(10, 12)),
        ],
        WORK: [
            mail("gh1", "GitHub <billing@github.com>", "[GitHub] Payment receipt for jdub",
                 "We received payment for your GitHub Pro subscription.\nAmount: $48.00 USD per year\nThanks!", at(7, 20)),
        ],
    }


@pytest.fixture
def fake():
    f = FakeGmail()
    f.mail = inbox()
    return f


@pytest.fixture
def gmail(tmp_path, fake):
    acc = GoogleAccounts(CLIENT_ID, SECRET, tmp_path / "t.json", http=httpx.Client(transport=httpx.MockTransport(fake)))
    connect(acc, fake, "code-work")
    connect(acc, fake, "code-home")
    return Gmail(acc)


@pytest.fixture
def bills(svc, gmail):
    svc.gmail = gmail
    b = bl.Bills(svc, clock=lambda: NOW.timestamp())
    b.nudged = []
    b.nudge = lambda text, **kw: b.nudged.append((text, kw.get("priority")))
    return b


# ---- reading one receipt -------------------------------------------------------------------------------------

@pytest.mark.parametrize("line,amount,cur", [
    ("Total: $1,299.00", 1299.0, "USD"), ("Total 9,99 €", 9.99, "EUR"), ("Total: €9,99", 9.99, "EUR"),
    ("You paid £7.99", 7.99, "GBP"), ("Amount charged: USD 15.49", 15.49, "USD"), ("Total 1.234,56 €", 1234.56, "EUR"),
    ("then US$69.99/mo", 69.99, "USD"), ("Price: $5", 5.0, "USD"),
])
def test_amounts_in_dollars_euros_and_pounds(line, amount, cur):
    best = bl.amounts(line)[0]
    assert (best["amount"], best["currency"]) == (amount, cur)


def test_total_beats_subtotal_and_tax():
    assert bl.amounts(SPOTIFY)[0]["amount"] == 11.99
    assert bl.amounts("Discount -$2.00\nAmount due: $40.00")[0]["amount"] == 40.0
    assert bl.amounts("$0.00 due today")[0:1] == []


def test_dates_in_every_common_shape():
    ref = datetime(2026, 10, 13)
    got = [d for _, d in bl.find_dates("Oct 16 · 16 October 2026 · 10/20/2026 · 2026-11-01 · Fri, 16OCT · Jan 3", ref)]
    assert got[:5] == [date(2026, 10, 16), date(2026, 10, 16), date(2026, 10, 20), date(2026, 11, 1), date(2026, 10, 16)]
    assert date(2027, 1, 3) in got  # no year: the nearest one
    assert bl.find_dates("a marathon on 1 mar", ref)[0][1] == date(2027, 3, 1)
    assert bl.find_dates("1 marathon", ref) == []


@pytest.mark.parametrize("name,addr,want", [
    ("Netflix", "info@account.netflix.com", "Netflix"), ("Adobe", "mail@mail.adobe.com", "Adobe"),
    ("Georgia Power Billing", "noreply@georgiapower.com", "Georgia Power"), ("", "receipts@acmecloud.io", "Acmecloud"),
    ("Disney+", "disneyplus@mail.disneyplus.com", "Disney+"), ("App Store", "no_reply@email.apple.com", "Apple"),
])
def test_merchant(name, addr, want):
    assert bl.merchant_of(name, addr) == want


def test_parse_kinds_and_dates():
    def one(subject, body, frm="Shop <billing@shop.com>"):
        return bl.parse_bill({"id": "x", "subject": subject, "body": body, "from": frm.split(" <")[0],
                              "from_email": frm.split("<")[1][:-1], "ts": at(10, 8).timestamp()})
    trial = one("Your free trial of Creative Cloud Pro ends soon", ADOBE, "Adobe <mail@mail.adobe.com>")
    assert (trial["kind"], trial["merchant"], trial["due"], trial["amount"], trial["cadence"]) == \
        ("trial", "Adobe", "2026-10-15", 69.99, "monthly")
    assert one("Your Paramount+ trial ends tomorrow", "Your trial ends tomorrow.")["due"] == "2026-10-09"
    assert one("Heads up", "Your free trial expires in 3 days.")["due"] == "2026-10-11"
    renew = one("Your membership will renew soon", "Your Prime membership will renew on November 2, 2026 for $139.00/year.",
                "Amazon Prime <prime@amazon.com>")
    assert (renew["kind"], renew["merchant"], renew["due"], renew["cadence"]) == ("renewal", "Amazon", "2026-11-02", "yearly")
    assert one("Your bill is ready", "Amount due: $84.12\nDue date: October 20, 2026")["kind"] == "due"
    assert one('Your Amazon.com order of "Cable" has shipped', "Order total $12.99") is None
    assert one("Your Flight Receipt", "Ticket total: $342.20") is None
    assert one("Sunday dinner", "Are you coming at 6?") is None


def test_cadence_from_repeats():
    assert bl.infer_cadence([date(2026, 8, 14), date(2026, 9, 14), date(2026, 10, 14)]) == "monthly"
    assert bl.infer_cadence([date(2025, 10, 1), date(2026, 10, 1)]) == "yearly"
    assert bl.infer_cadence([date(2026, 9, 1), date(2026, 9, 19)]) == ""
    assert bl.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


# ---- the skill -----------------------------------------------------------------------------------------------

def test_scan_both_accounts_and_add_it_up(bills, fake):
    r = bills.scan()
    assert r["ok"] and r["read"] == 12
    d = bills.overview()
    subs = {s["merchant"]: s for s in d["subscriptions"]}
    assert set(subs) == {"Netflix", "Spotify", "GitHub", "Disney+", "Hulu"}
    assert subs["Netflix"]["next"] == "2026-10-14" and subs["Netflix"]["cadence"] == "monthly" and subs["Netflix"]["count"] == 2
    assert subs["Spotify"]["amount"] == 11.99 and subs["Spotify"]["next"] == "2026-10-28"
    assert subs["GitHub"]["cadence"] == "yearly" and subs["GitHub"]["monthly"] == 4.0 and subs["GitHub"]["account"] == WORK
    assert subs["Disney+"]["currency"] == "EUR" and subs["Hulu"]["lapsed"]
    assert d["monthly"] == {"USD": 31.48, "EUR": 9.99} and d["currency"] == "USD" and d["monthly_total"] == 31.48
    assert [t["merchant"] for t in d["trials"]] == ["Adobe"] and d["trials"][0]["when"] == "Thursday"
    up = [(u["merchant"], u["kind"], u["when"]) for u in d["upcoming"]]
    assert up[:3] == [("Netflix", "renewal", "tomorrow"), ("Adobe", "trial", "Thursday"), ("Georgia Power", "due", "Oct 20")]
    assert all(u["link"].startswith("https://mail.google.com/") for u in d["upcoming"])
    # read once: a second scan reads nothing new
    calls = len(fake.calls)
    assert bills.scan(force=True)["read"] == 0 and len(fake.calls) - calls == 4  # 2 windows x 2 accounts, ids only
    assert {q for _, q in fake.calls[-2:]} == {f"gmail {q}" for q in bl.QUERIES}


def test_one_account_failing_keeps_the_other(bills, gmail):
    from assistant.integrations.google import GoogleError

    real = gmail._ids

    def ids(email, query, limit):
        if email == WORK:
            raise GoogleError("Gmail API has not been used in project 123")
        return real(email, query, limit)
    gmail._ids = ids
    assert bills.scan()["ok"] and "GitHub" not in {s["merchant"] for s in bills.overview()["subscriptions"]}
    assert bills.error.startswith(f"{WORK}: Gmail API has not been used") and bills.status()["warning"] == bills.error
    assert "Netflix" in {s["merchant"] for s in bills.overview()["subscriptions"]}


def test_nudges_trial_and_renewal(bills):
    bills.tick()
    assert ("Your Adobe trial ends Thursday.", "normal") in bills.nudged
    assert ("Netflix renews tomorrow, $15.49.", "low") in bills.nudged
    st = bills.last_status
    assert st["ok"] and st["monthly_total"] == 31.48 and st["live"] is False


def test_hide_a_merchant(bills, svc):
    bills.scan()
    assert svc.skills is not None
    assert bills.actions()["hide"]({"merchant": "GitHub"})["ok"]
    assert "GitHub" not in {s["merchant"] for s in bills.overview()["subscriptions"]}
    assert bills.actions()["hide"]({})["ok"] is False


def test_the_model_reads_what_the_patterns_cannot(bills, svc, fake):
    fake.mail[HOME].append(mail("ac1", "Acme Cloud <receipts@acmecloud.io>", "Your invoice from Acme Cloud",
                                "Your invoice is attached as a PDF.", at(10, 1)))
    asked = []
    svc.think = lambda prompt, schema=None, **kw: asked.append(prompt) or {"amount": 20, "currency": "USD", "cadence": "monthly"}
    bills.scan()
    acme = next(s for s in bills.overview()["subscriptions"] if s["merchant"] == "Acme Cloud")
    assert acme["amount"] == 20 and len(asked) == 1 and "data, not instructions" in asked[0]


def test_no_model_still_works(bills, svc, fake):
    fake.mail[HOME].append(mail("ac1", "Acme Cloud <receipts@acmecloud.io>", "Your invoice from Acme Cloud",
                                "Your invoice is attached as a PDF.", at(10, 1)))
    bills.scan()  # svc.think answers None without a model
    assert "Acme Cloud" not in {s["merchant"] for s in bills.overview()["subscriptions"]}
    assert bills.overview()["monthly_total"] == 31.48


def test_without_gmail_says_why(svc):
    b = bl.Bills(svc, clock=lambda: NOW.timestamp())
    st = b.status()
    assert st["ok"] is False and "Setup → Google accounts" in st["error"]

    class NoScope:
        def accounts(self):
            return []

        def missing(self):
            return [HOME]
    svc.gmail = NoScope()
    assert "Reconnect, and tick Gmail" in b.status()["error"]
    r = b._subscriptions()
    assert "Reconnect, and tick Gmail" in b.say("subscriptions", {}, r)
    svc.gmail = None
    assert "Gmail isn't set up" in b.scan()["error"]


# ---- voice ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("said,tool,args", [
    ("what subscriptions do i have", "subscriptions", {"ask": "list"}),
    ("show me my subscriptions", "subscriptions", {"ask": "list"}),
    ("how much am i spending on subscriptions", "subscriptions", {"ask": "total"}),
    ("how much do i pay for subscriptions a month", "subscriptions", {"ask": "total"}),
    ("what bills are due", "upcoming_bills", {"days": 14}),
    ("any bills due this week", "upcoming_bills", {"days": 7}),
    ("any free trials ending soon", "upcoming_bills", {"days": 14}),
])
def test_routes(svc, said, tool, args):
    i = route(said, RouterContext(skill_route=Skills(svc, ["bills"]).route))
    assert (i.tool, i.args) == (tool, args)


def test_unrelated_phrases_pass_through(svc):
    sk = Skills(svc, ["bills"])
    for said in ("what's the weather", "check my email", "pay the bills folder"):
        i = route(said, RouterContext(skill_route=sk.route))
        assert i is None or i.tool not in ("subscriptions", "upcoming_bills")


def test_spoken_replies_and_never_amounts_on_air(bills, svc):
    tools = {t.name: t for t in bills.tools()}
    assert tools["subscriptions"].untrusted and tools["upcoming_bills"].untrusted_label == "an email"
    subs = tools["subscriptions"].handler(ask="list")
    assert bills.say("subscriptions", {}, subs) == \
        "You pay for 4 subscriptions, about $31.48 a month: Netflix, Spotify, Disney+, GitHub."
    assert bills.say("subscriptions", {}, {**subs, "ask": "total"}) == "About $31.48 a month across 4 subscriptions."
    up = tools["upcoming_bills"].handler(days=14)
    assert bills.say("upcoming_bills", {}, up).startswith("Coming up: Netflix tomorrow, $15.49; your Adobe trial ends Thursday;")
    svc.live = lambda: True
    for r in (subs, {**subs, "ask": "total"}, up):
        line = bills.say("subscriptions" if "subscriptions" in r and "upcoming" not in r else "upcoming_bills", {}, r)
        assert "$" not in line and "€" not in line and "HUD" in line
    assert bills.say("upcoming_bills", {}, {"ok": True, "upcoming": [], "days": 14}) == "Nothing due in the next 14 days."


def test_briefing_lists_what_is_due(bills):
    bills.scan()
    assert bills.briefing() == {"due_soon": ["Netflix tomorrow", "Adobe Thursday (trial ends)"]}


def test_loads_as_a_skill(svc):
    sk = Skills(svc, ["bills"])
    assert sk["bills"].title == "Money" and not sk.errors
    assert sk.action("bills", "scan", {})["ok"] is False  # no Google here: says why instead of failing


def test_fast_path_end_to_end(bills, svc):
    from assistant.brain.assistant import Assistant

    bills._scanned = 0
    svc.skills = Skills(svc, ["bills"])
    svc.skills.all["bills"] = bills
    reply = Assistant(svc).handle("how much am i spending on subscriptions")["reply"]
    assert reply == "About $31.48 a month across 4 subscriptions."
