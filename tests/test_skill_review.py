"""Weekly life review: a seeded week (activity, focus, habits, journal, tasks, streams, receipts) adds up right, the
suggestion comes from the model or a rule, nothing private on air, the Sunday nudge, voice, and the shareable card."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import review as rv

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 18, 19, 0, tzinfo=NY)  # Sunday evening
MON = datetime(2026, 10, 12, tzinfo=NY)


class Clock:
    t = NOW.timestamp()

    def __call__(self):
        return self.t


def at(day: int, hour: float) -> float:
    """Timestamp ``day`` days after Monday Oct 12, at ``hour``."""
    return (MON + timedelta(days=day, hours=hour)).timestamp()


def seed(svc):
    st = svc.storage
    for d in range(5):  # Mon-Fri: 6 h work, 1 h other, 30 min idle
        st.add_activity(at(d, 9), at(d, 15), "code", "vesper - Visual Studio Code", "work")
        st.add_activity(at(d, 15), at(d, 16), "chrome", "Reddit", "other")
        st.add_activity(at(d, 16), at(d, 16.5), "idle", "Away from keyboard", "idle")
    for d in (1, 3):  # Tue + Thu: 3 h streams
        st.add_activity(at(d, 20), at(d, 23), "obs64", "OBS", "stream")
    st.add_activity(at(-2, 10), at(-2, 20), "code", "last week", "work")  # the week before: not counted
    for i, title in enumerate(["Ship the overlay", "File taxes", "<script>alert(1)</script>"]):
        t = st.add_task(title)
        st.execute("UPDATE tasks SET status='done', done_at=? WHERE id=?", (at(i, 12), t["id"]))
    st.add_task("Still open")
    for d in (0, 2, 4):
        st.execute("INSERT INTO focus_sessions(start,end,planned_min,label,profile,distractions,off_task_s,completed) "
                   "VALUES (?,?,50,'deep','work',0,0,1)", (at(d, 10), at(d, 10.8)))
    st.execute("INSERT INTO habits(name,target,period,created) VALUES ('drink water',8,'day',?)", (at(-30, 0),))
    st.execute("INSERT INTO habits(name,target,period,created) VALUES ('gym',3,'week',?)", (at(-30, 0),))
    for d in range(7):
        st.execute("INSERT INTO habit_log(habit_id,ts,day,n) VALUES (1,?,?,?)", (at(d, 12), (MON + timedelta(days=d)).date().isoformat(),
                                                                               8 if d < 5 else 4))
    st.execute("INSERT INTO habit_log(habit_id,ts,day,n) VALUES (2,?,?,2)", (at(2, 7), "2026-10-14"))
    for d, mood in ((0, -1), (1, -1), (3, 1), (5, 2), (6, 2)):
        st.execute("INSERT INTO journal_entries(ts,day,text,mood,tags,source) VALUES (?,?,?,?,'[]','voice')",
                   (at(d, 21), (MON + timedelta(days=d)).date().isoformat(), "private words", mood))
    recap = {"recap": {"peak_viewers": 41, "follows": 6, "chapters": [{"title": "Clutch 1v3"}, {"title": "Raid from Dana"}]},
             "game": "Valorant", "viewers": []}
    st.execute("INSERT INTO stream_copilot_sessions (start, end, data) VALUES (?,?,?)", (at(1, 20), at(1, 23), json.dumps(recap)))
    st.execute("INSERT INTO stream_copilot_sessions (start, end, data) VALUES (?,?,?)",
               (at(3, 20), at(3, 22.5), json.dumps({"game": "Valorant", "viewers": [[0, 12], [1, 30]]})))


class FakeProjects:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def repos(self):
        return []

    def commits_between(self, since, until):
        return [r for r in self.rows if since.timestamp() <= r["ts"] < until.timestamp()]


@pytest.fixture(autouse=True)
def no_real_repos(svc):
    svc.projects = FakeProjects([{"repo": "vesper", "ts": at(2, 11), "message": "HUD 2.1"},
                                 {"repo": "overlay", "ts": at(4, 11), "message": "fix raid alert"},
                                 {"repo": "vesper", "ts": at(-3, 11), "message": "last week"}])


@pytest.fixture
def review(svc):
    seed(svc)
    r = rv.Review(svc, clock=Clock())
    r.nudged = []
    r.nudge = lambda text, **kw: r.nudged.append((text, kw))
    return r


def test_the_week_adds_up(review):
    r = review.build()
    assert r["week"] == "2026-10-12" and r["label"] == "Oct 12 – 18" and r["complete"] and r["days_in"] == 7
    h = r["hours"]
    assert (h["work"], h["other"], h["stream"], h["total"]) == (30.0, 5.0, 6.0, 41.0)
    assert [x["key"] for x in h["by"]] == ["work", "stream", "other"] and h["by"][0]["label"] == "Work"
    assert h["days"][0]["by"]["work"] == 6.0 and h["days"][1]["by"]["stream"] == 3.0 and "idle" not in h["days"][0]["by"]
    assert r["focus"]["sessions"] == 3 and r["focus"]["minutes"] == 144 and r["focus"]["completed"] == 3
    assert r["habits"]["count"] == 2
    water = next(x for x in r["habits"]["list"] if x["name"] == "drink water")
    gym = next(x for x in r["habits"]["list"] if x["name"] == "gym")
    assert water == {"name": "drink water", "pct": 86, "detail": "5/7 days"} and gym["pct"] == 67 and r["habits"]["pct"] == 76
    assert r["mood"]["entries"] == 5 and r["mood"]["trend"] == "up" and r["mood"]["good_days"] == 3
    assert r["tasks"]["done"] == 3 and r["tasks"]["open"] == 1
    s = r["streams"]
    assert (s["count"], s["hours"], s["peak"], s["follows"]) == (2, 5.5, 41, 6)
    assert s["highlights"] == ["Clutch 1v3", "Raid from Dana"]
    assert r["money"]["subs_monthly"] == 0 and r["money"]["flagged"] == []
    assert any(w.startswith("Closed 3 tasks") for w in r["wins"]) and any("Streamed 2×" in w for w in r["wins"])
    assert "3 good days in your journal" in r["wins"] and "2 commits to overlay, vesper" in r["wins"]
    assert r["commits"] == {"count": 2, "repos": ["overlay", "vesper"], "latest": ["fix raid alert", "HUD 2.1"]}
    assert r["by"] == "rule" and r["suggestion"]
    saved = review.get("this")
    assert saved["hours"]["total"] == 41.0 and "_segments" not in saved


def test_model_writes_the_suggestion(review, svc):
    seen = {}

    def think(prompt, schema=None, **kw):
        seen["prompt"] = prompt
        return {"suggestion": "Move Thursday's stream earlier; you were up late."}
    svc.think = think
    r = review.build()
    assert r["by"] == "model" and r["suggestion"].startswith("Move Thursday")
    assert "private words" not in seen["prompt"]  # journal text never goes to the model


def test_rule_suggestions():
    assert "drink water" in rv.fallback_suggestion(
        {"habits": {"count": 1, "pct": 30, "weakest": {"name": "drink water", "pct": 30}}})
    assert rv.fallback_suggestion({"money": {"flagged": ["Adobe"]}}).startswith("Cancel Adobe")
    assert "focus blocks" in rv.fallback_suggestion({"hours": {"work": 20}, "focus": {"sessions": 0}, "tasks": {"done": 2}})
    assert "restful" in rv.fallback_suggestion({"mood": {"trend": "down"}, "tasks": {"done": 1}})
    assert rv.fallback_suggestion({"tasks": {"done": 0}}).startswith("Pick three")


def test_empty_week_and_missing_skills(svc):
    r = rv.Review(svc, clock=Clock())
    svc.storage.execute("DROP TABLE habits")  # the habits skill turned off: no table
    out = r.build("last")
    assert out["week"] == "2026-10-05" and out["hours"]["total"] == 0 and out["habits"]["count"] == 0
    assert out["tasks"]["done"] == 0 and out["streams"]["count"] == 0 and out["wins"] == ["1 commit to vesper"]
    assert out["suggestion"].startswith("Pick three")
    assert "No activity tracked" in rv.render_card(out)


def test_on_air_nothing_private(review, svc):
    review.build()
    svc.live = lambda: True
    st = review.status()["review"]
    assert st["private"] and "money" not in st and st["mood"] == {"entries": 5, "private": True}
    assert not any("journal" in w for w in st["wins"])
    said = review.say("life_review", {}, review._review())
    assert "mood" not in said.lower() and "next week" not in said.lower() and said.endswith("The rest is on the HUD.")
    html = review.card_page({"base": "http://127.0.0.1:8765"})
    assert "moods hidden while live" in html and "Hidden while live" in html and "$" not in html


def test_say_reads_the_week(review):
    said = review.say("life_review", {}, review._review())
    assert said.startswith("This week: 41 hours at the PC, mostly work (30), 3 focus sessions, habits at 76%, 3 tasks done, "
                           "2 streams (5.5 hours).")
    assert "Your mood trended up." in said and "For next week:" in said
    assert review.say("life_review", {}, {"ok": False, "error": "nope"}) == "nope"


def test_sunday_evening_nudge_once_a_week(review):
    review.tick()
    assert review.status()["fresh"] and review.status()["review"]["week"] == "2026-10-12"
    text, kw = review.nudged[0]
    assert "life review" in text and kw["key"] == "review:2026-42" and kw["action"]["command"] == "life review"
    review.clock.t = datetime(2026, 10, 16, 12, 0, tzinfo=NY).timestamp()  # a Friday: no nudge
    review.nudged.clear()
    review.tick()
    assert review.nudged == [] and not review.status()["fresh"]


@pytest.mark.parametrize("said,week", [
    ("life review", "this"), ("my life review", "this"), ("how was my week", "this"), ("how's my week been", "this"),
    ("how has my week been", "this"), ("how was last week", "last"), ("life review for last week", "last"),
])
def test_routes(svc, said, week):
    i = route(said, RouterContext(skill_route=Skills(svc, ["review"]).route))
    assert i is not None and (i.tool, i.args) == ("life_review", {"week": week})


def test_planner_keeps_weekly_review(svc):
    sk = Skills(svc, ["planner", "review"])
    assert route("weekly review", RouterContext(skill_route=sk.route)).tool == "weekly_review"
    assert route("how did my week go", RouterContext(skill_route=sk.route)).tool == "weekly_review"
    assert route("how was my week", RouterContext(skill_route=sk.route)).tool == "life_review"


def test_card_is_escaped_and_complete(review):
    html = review.card_page({"base": "http://127.0.0.1:8765/"})
    assert html.startswith("<!doctype html>") and "Oct 12 – 18" in html
    assert "url(http://127.0.0.1:8765/static/fonts/jost.woff2)" in html
    for part in ("Where the time went", "Streams", "Mood", "Wins", "Money", "Next week", "Clutch 1v3", "76%"):
        assert part in html
    assert "<polyline" in html and "Print / PDF" in html
    r = review.get("this")
    r["wins"] = ["Closed “<script>alert(1)</script>”"]
    r["streams"]["highlights"] = ["<img src=x onerror=alert(1)>"]
    evil = rv.render_card(r, '"><script>')
    assert "<script>alert" not in evil and "<img src=x" not in evil and "&lt;script&gt;alert(1)" in evil
    assert '"><script>' not in evil


def test_card_route_needs_the_token(svc, cfg):
    from fastapi.testclient import TestClient

    from assistant.runtime import Runtime
    from assistant.server import create_app

    seed(svc)
    sk = Skills(svc, ["bills", "review"])
    svc.skills = sk
    sk["review"].clock = Clock()
    app = create_app(Runtime(cfg, services=svc), start_background=False)
    with TestClient(app) as c:
        assert c.get("/api/skill/review/card").status_code == 401
        h = {"X-Assistant-Token": app.state.token}
        r = c.get("/api/skill/review/card", headers=h)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "Oct 12 – 18" in r.text
        assert "http://testserver/static/fonts/jost.woff2" in r.text
        assert c.get("/api/skill/review/nope", headers=h).status_code == 404
        assert c.get("/api/skill/bills/card", headers=h).status_code == 404
        built = c.post("/api/skill/review/build", headers=h, json={"week": "this"}).json()
        assert built["ok"] and built["review"]["tasks"]["done"] == 3
        assert c.get("/static/skills/review.js").status_code == 200
