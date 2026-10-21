"""Subscription audit: receipts (Gmail, faked) crossed with app use (the activity table, seeded): what's flagged, the
savings, Keep, Remind me to cancel, the config mapping, not enough history, voice, and nothing about money on air."""

from datetime import datetime

import httpx
import pytest
from test_gmail import FakeGmail
from test_google import CLIENT_ID, SECRET, connect
from test_skill_bills import NOW, NY, inbox

from assistant.brain.router import RouterContext, route
from assistant.integrations.gmail import Gmail
from assistant.integrations.google import GoogleAccounts
from assistant.skills import Skills
from assistant.skills import bills as bl


@pytest.fixture
def bills(svc, tmp_path):
    fake = FakeGmail()
    fake.mail = inbox()
    acc = GoogleAccounts(CLIENT_ID, SECRET, tmp_path / "t.json", http=httpx.Client(transport=httpx.MockTransport(fake)))
    connect(acc, fake, "code-work")
    connect(acc, fake, "code-home")
    svc.gmail = Gmail(acc)
    b = bl.Bills(svc, clock=lambda: NOW.timestamp())
    b.nudged = []
    b.nudge = lambda text, **kw: b.nudged.append((text, kw.get("priority")))
    return b


def ts(month, day, hour=20):
    return datetime(2026, month, day, hour, tzinfo=NY).timestamp()


def seed(svc, since=(7, 1)):
    st = svc.storage
    st.add_activity(ts(*since, 8), ts(*since, 9), "explorer", "File Explorer", "other")  # tracking began
    st.add_activity(ts(7, 20), ts(7, 20, 21), "chrome", "Netflix - Stranger Things", "other")
    st.add_activity(ts(10, 1), ts(10, 1, 21), "chrome", "Netflix", "idle")  # idle with Netflix open: not use
    st.add_activity(ts(10, 10), ts(10, 10, 21), "spotify", "Lo-fi beats", "other")
    st.add_activity(ts(10, 12, 9), ts(10, 12, 10), "chrome", "jdub/vesper - GitHub", "work")


@pytest.fixture
def audited(bills, svc):
    seed(svc)
    bills.scan()
    return bills


def test_flags_what_you_dont_use(audited):
    a = audited.audit()
    assert a["ready"] and a["history_days"] == 104 and a["days"] == 60
    items = {i["merchant"]: i for i in a["items"]}
    assert set(items) == {"Netflix", "Spotify", "GitHub", "Disney+"}  # Hulu lapsed: not audited
    nf = items["Netflix"]
    assert nf["flag"] and nf["idle_days"] == 84 and nf["line"] == "You haven't opened Netflix in 84 days — $15.49/month"
    assert items["Disney+"]["flag"] and items["Disney+"]["last_used"] is None and items["Disney+"]["idle_days"] == 104
    sp = items["Spotify"]
    assert not sp["flag"] and sp["days_since"] == 2 and sp["used_30d_min"] == 60
    assert not items["GitHub"]["flag"] and items["GitHub"]["days_since"] == 0 and items["GitHub"]["price"] == "$48.00/year"
    assert [i["merchant"] for i in a["flagged"]] == ["Netflix", "Disney+"]  # the biggest saving first
    assert a["savings"] == 15.49 and a["savings_year"] == 185.88 and a["currency"] == "USD"  # Disney+ is in euros
    assert audited.status()["audit"]["flagged"]


def test_keep_mutes_for_90_days(audited, svc):
    assert audited.actions()["keep"]({"merchant": "Netflix"})["ok"]
    a = audited.audit()
    nf = next(i for i in a["items"] if i["merchant"] == "Netflix")
    assert nf["kept"] and not nf["flag"] and a["savings"] == 0
    assert nf["kept_until"] == pytest.approx(NOW.timestamp() + 90 * 86400)
    svc.storage.execute("UPDATE bills_audit SET kept_until = ?", (NOW.timestamp() - 1,))  # 90 days later
    assert any(i["merchant"] == "Netflix" for i in audited.audit()["flagged"])
    assert audited.actions()["keep"]({}) == {"ok": False, "error": "Which one?"}


def test_remind_me_to_cancel(audited, svc):
    r = audited.actions()["remind_cancel"]({"merchant": "Netflix"})
    assert r["ok"] and r["text"] == "Cancel Netflix before it renews on Oct 14 ($15.49/month)"
    assert r["at"] == NOW.timestamp() + 3600  # renews tomorrow: remind in an hour
    auto = svc.skills.get("automations")
    assert any(i["text"] == r["text"] and i["kind"] == "reminder" for i in auto.items)
    r2 = audited.actions()["remind_cancel"]({"merchant": "spotify"})
    assert r2["at"] == datetime(2026, 10, 26, 10, 0, tzinfo=NY).timestamp()  # two days before it renews
    nf = next(i for i in audited.audit()["items"] if i["merchant"] == "Netflix")
    assert nf["reminder_at"] == r["at"]
    audited._nudges()
    assert not any("opened Netflix" in t for t, _ in audited.nudged)  # reminded: no more nudges about it
    assert ("You haven't opened Disney+ in 104 days — €9.99/month.", "low") in audited.nudged
    assert audited.actions()["remind_cancel"]({"merchant": "Nope"})["ok"] is False


def test_no_reminders_skill(audited, svc):
    svc.skills = Skills(svc, ["bills"])
    assert audited.actions()["remind_cancel"]({"merchant": "Netflix"}) == {
        "ok": False, "error": "Reminders are turned off (skills.automations)."}


def test_mapping_is_extendable(audited):
    audited.cfg["usage"] = {"GitHub": {"apps": ["Code.exe"], "titles": []}, "Disney+": {"titles": ["stranger things"]}}
    items = {i["merchant"]: i for i in audited.audit()["items"]}
    assert items["GitHub"]["flag"] and items["GitHub"]["last_used"] is None  # VS Code never used in the seed
    assert items["Disney+"]["idle_days"] == 84  # now matched by a title


def test_not_enough_history_yet(bills, svc):
    seed(svc, since=(10, 3))
    svc.storage.execute("DELETE FROM activity WHERE start < ?", (ts(10, 3, 0),))
    bills.scan()
    a = bills.audit()
    assert not a["ready"] and a["history_days"] == 10 and a["flagged"] == []
    disney = next(i for i in a["items"] if i["merchant"] == "Disney+")
    assert disney["last_used"] is None and not disney["flag"]
    assert bills.say("subscription_audit", {}, a) == ("Nothing to cut yet: I've only tracked 10 days of app use, and I flag "
                                                      "what you haven't opened in 60.")


def test_say(audited, svc):
    said = audited.say("subscription_audit", {}, audited._audit())
    assert said == ("2 subscriptions you're not using: Netflix, not opened in 84 days, $15.49 a month; Disney+, not opened "
                    "in 104 days, €9.99 a month. Cancelling saves $15.49 a month. Tap “Remind me to cancel” on the HUD and "
                    "I'll remind you before they renew.")
    svc.live = lambda: True
    assert audited.say("subscription_audit", {}, audited._audit()) == "2 subscriptions look unused. The details are on the HUD."
    audited.nudged.clear()
    audited._nudges()
    assert not any("haven't opened" in t for t, _ in audited.nudged)  # never on air
    svc.live = None
    for m in ("Netflix", "Disney+"):
        audited.actions()["keep"]({"merchant": m})
    assert audited.say("subscription_audit", {}, audited._audit()) == "You've used everything you pay for in the last 60 days."


def test_tracking_off_and_no_receipts(bills, svc):
    bills.scan()
    assert bills.say("subscription_audit", {}, bills.audit()).startswith("Activity tracking is off")
    svc.gmail = None
    svc.storage.execute("DELETE FROM bills_items")
    b2 = type(bills)(svc, clock=bills.clock)
    assert b2.say("subscription_audit", {}, b2._audit()) == "Gmail isn't set up: Setup → Google accounts."


@pytest.mark.parametrize("said", ["audit my subscriptions", "do a subscription audit", "subscription audit",
                                  "which subscriptions am I not using", "what subscriptions should I cancel",
                                  "am I wasting money on subscriptions"])
def test_routes(svc, said):
    i = route(said, RouterContext(skill_route=Skills(svc, ["bills"]).route))
    assert i is not None and i.tool == "subscription_audit"
