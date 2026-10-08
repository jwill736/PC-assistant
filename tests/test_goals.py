"""Goals, one per area, edited in the HUD; and a HUD that never runs a stale cached script after an update."""

import yaml
from fastapi.testclient import TestClient

from assistant.config import goal_lines
from assistant.runtime import Runtime
from assistant.server import WEB, create_app


def test_goal_lines_takes_one_line_or_a_list():
    assert goal_lines({"north_star": "Grow the channel"}) == ["Grow the channel"]
    assert goal_lines({"north_star": ["Streaming: 100 viewers by June", " ", "Work: ship on time"]}) == \
        ["Streaming: 100 viewers by June", "Work: ship on time"]
    assert goal_lines({"north_star": ""}) == [] and goal_lines({}) == [] and goal_lines(None) == []


def test_goals_edited_in_the_hud_are_saved_and_ranked_against(cfg, svc):
    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=False)
    events = []
    rt.bus.on(events.append)
    with TestClient(app) as c:
        h = {"x-assistant-token": app.state.token}
        r = c.post("/api/goals", headers=h, json={
            "goals": ["Streaming: $5k/month from streaming by June 2027", "Work: every client project on time in 2026", ""],
            "this_week": ["Stream 4 nights", "Finish the overlay"]}).json()
    rt._commands.shutdown()
    assert r["ok"] and r["north_star"] == ["Streaming: $5k/month from streaming by June 2027",
                                           "Work: every client project on time in 2026"]
    saved = yaml.safe_load((cfg.data_dir / "settings.yaml").read_text())["goals"]
    assert saved["this_week"] == ["Stream 4 nights", "Finish the overlay"]
    prompt = rt.assistant.system_prompt
    assert "Streaming: $5k/month from streaming by June 2027; Work: every client project on time in 2026" in prompt
    assert "This week: Stream 4 nights; Finish the overlay" in prompt
    assert any(e["type"] == "goals" for e in events)
    # a single goal stays a plain line; clearing them says so in the prompt
    rt.set_goals(["Grow the channel"])
    assert cfg["goals"]["north_star"] == "Grow the channel"
    rt.set_goals([])
    assert "(not set" in rt.assistant.system_prompt


def test_the_hud_loads_its_script_by_content_version_and_is_never_cached(cfg, svc):
    import hashlib

    rt = Runtime(cfg, services=svc)
    app = create_app(rt, start_background=False)
    version = hashlib.sha1(b"".join((WEB / f).read_bytes() for f in ("app.js", "styles.css"))).hexdigest()[:10]
    with TestClient(app) as c:
        r = c.get("/")
        assert f'/static/app.js?v={version}"' in r.text and f'/static/styles.css?v={version}"' in r.text
        assert r.headers["cache-control"] == "no-store"
        assert c.get(f"/static/app.js?v={version}").status_code == 200
    rt._commands.shutdown()
