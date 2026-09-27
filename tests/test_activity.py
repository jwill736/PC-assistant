from assistant.integrations.activity import ActivityTracker, Categorizer, focus_sessions, summarize, timeline
from assistant.storage import Storage

PROFILES = {
    "work": {"apps": ["code", "slack"], "title_keywords": ["github"]},
    "stream": {"apps": ["obs64"], "title_keywords": ["twitch.tv"]},
}


def test_categorizer_titles_beat_apps():
    cat = Categorizer(PROFILES)
    assert cat("Code.exe", "main.py") == "work"
    assert cat("chrome", "Pull requests · GitHub - Google Chrome") == "work"
    assert cat("chrome", "Creator dashboard | twitch.tv") == "stream"
    assert cat("chrome", "YouTube") == "other"
    assert cat("idle", "Away") == "idle"


def test_tracker_merges_samples_into_segments():
    store = Storage(":memory:")
    tr = ActivityTracker(store, PROFILES, sample_seconds=5)
    t = 1_000_000.0
    for i in range(6):
        tr.observe("code", "a.py", now=t + i * 5)
    tr.observe("chrome", "YouTube", now=t + 30)
    tr.observe("chrome", "YouTube", now=t + 35)
    rows = store.activity_between(t - 1, t + 100)
    assert len(rows) == 1 and rows[0]["app"] == "code"
    assert rows[0]["end"] - rows[0]["start"] == 30  # closed where the next segment began
    live = tr.segments(t - 1, t + 100)
    assert [s["app"] for s in live] == ["code", "chrome"]


def seg(start_min, end_min, cat, app="x"):
    return {"start": start_min * 60, "end": end_min * 60, "category": cat, "app": app, "title": app}


def test_focus_sessions_tolerate_short_detours():
    segs = [seg(0, 20, "work"), seg(20, 21, "other"), seg(21, 40, "work"), seg(40, 60, "other"), seg(60, 70, "work")]
    sessions = focus_sessions(segs)
    assert len(sessions) == 1 and sessions[0]["minutes"] == 40


def test_summarize_numbers():
    segs = [seg(0, 60, "work", "code"), seg(60, 90, "stream", "obs64"), seg(90, 100, "idle", "idle"), seg(100, 130, "work", "slack")]
    s = summarize(segs, "d")
    assert s["active_seconds"] == 120 * 60
    assert s["by_category"]["work"] == 90 * 60
    assert s["top_apps"][0] == {"app": "code", "category": "work", "seconds": 3600}
    assert s["context_switches"] == 2
    assert [f["minutes"] for f in s["focus_sessions"]] == [60, 30]  # idle gap splits them
    assert [b["category"] for b in timeline(segs)] == ["work", "stream", "idle", "work"]
