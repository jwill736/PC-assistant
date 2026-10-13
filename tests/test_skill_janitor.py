"""PC health and tidying: GPU heat/power watch, low drives, what fills a drive, and a reversible Downloads tidy."""

import os
import time

import pytest

from assistant.brain.router import RouterContext, route
from assistant.skills import Skills
from assistant.skills import janitor as jn

GB = 1 << 30
LINE = "{temp}, {draw}, 575.00, {bits}, 99, NVIDIA GeForce RTX 5090"


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(svc, tmp_path):
    nudged, calls = [], []
    svc.nudge = lambda text, **kw: nudged.append((text, kw))
    state = {"temp": 70, "draw": 300, "bits": "0x0000000000000000", "gaming": False, "smi": True, "old_field": True}

    def runner(args):
        calls.append(args[0])
        if not state["smi"] or (not state["old_field"] and "throttle" in args[0]):
            return None
        return LINE.format(**state) + "\n"
    clock = Clock()
    dl = tmp_path / "Downloads"
    dl.mkdir()
    j = jn.Janitor(svc, runner=runner, read_cpu=lambda: 55.0, recycle=lambda: 3 * GB, in_game=lambda: state["gaming"],
                   clock=clock, list_drives=lambda: [jn.drive_row("C:\\", 1000 * GB, 400 * GB),
                                                     jn.drive_row("D:\\", 2000 * GB, 18 * GB)])
    j.cfg["downloads"] = str(dl)
    return j, state, clock, nudged, calls, dl


def test_parse_nvidia_smi():
    g = jn.parse_gpus("87, 571.20, 575.00, 0x0000000000000024, 99, NVIDIA GeForce RTX 5090\n")[0]
    assert g == {"name": "NVIDIA GeForce RTX 5090", "temp_c": 87.0, "power_w": 571.2, "limit_w": 575.0, "util": 99.0,
                 "throttle": ["power cap", "thermal slowdown"], "power_capped": True, "thermal": True}
    assert jn.parse_gpus("[N/A], [N/A], [N/A], [N/A], 3")[0]["temp_c"] is None
    assert jn.parse_gpus("") == []


def test_newer_drivers_use_the_event_reasons_field(env):
    j, state, clock, nudged, calls, dl = env
    state["old_field"] = False
    assert j.gpus()[0]["temp_c"] == 70
    assert "clocks_event_reasons.active" in calls[-1] and j._reasons == "clocks_event_reasons.active"


def test_hot_gpu_for_two_minutes_is_urgent_once(env):
    j, state, clock, nudged, calls, dl = env
    state["temp"] = 88
    j.tick()
    assert not [n for n in nudged if "GPU" in n[0]]  # one hot reading is not a problem
    clock.t += 130
    j.tick()
    clock.t += 60
    j.tick()
    hot = [n for n in nudged if "GPU has been at 88" in n[0]]
    assert len(hot) == 1 and hot[0][1]["priority"] == "urgent"
    state["temp"] = 60
    j.tick()
    assert j._hot_since == {}


def test_power_limit_throttle_only_matters_in_a_game(env):
    j, state, clock, nudged, calls, dl = env
    state.update(draw=572, bits="0x0000000000000004")
    j.tick()
    assert not [n for n in nudged if "power limit" in n[0]]
    state["gaming"] = True
    j.tick()
    assert [n for n in nudged if "575 watt power limit" in n[0]][0][1]["priority"] == "urgent"


def test_nearly_full_drive_is_mentioned_once_a_day(env):
    j, state, clock, nudged, calls, dl = env
    j.tick()
    j.tick()
    low = [n for n in nudged if "drive" in n[0]]
    assert len(low) == 1 and low[0][0].startswith("Your D: drive is 99% full, only 18 GB left.")
    assert low[0][1]["priority"] == "normal"
    assert j.status()["issues"] == ["D: is 99% full, 18 GB free"]
    assert j.briefing() == {"issues": ["D: is 99% full, 18 GB free"]}


def test_health_spoken(env):
    j, state, clock, nudged, calls, dl = env
    r = j.pc_health()
    said = j.say("pc_health", {}, r)
    assert said.startswith("Your NVIDIA GeForce RTX 5090 is at 70°C, drawing 300 of 575 watts, 99% busy.")
    assert "CPU 55°C." in said and "D: is 99% full, only 18 GB left." in said
    assert j.say("pc_health", {"focus": "gpu"}, r).endswith("99% busy.")
    state["smi"] = False
    j.check()
    assert "can't read the GPU" in j.say("pc_health", {}, j.pc_health())


def test_no_gpu_no_drives_still_works(svc):
    j = jn.Janitor(svc, runner=lambda a: None, list_drives=lambda: (_ for _ in ()).throw(OSError("x")),
                   read_cpu=lambda: None)
    j.tick()
    assert j.status()["gpus"] == [] and j.status()["drives"] == []
    assert j.disk_report()["ok"] is False


def _write(p, size, fill=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(fill * size)


def test_scan_sizes_folders_skips_system_and_finds_duplicates(tmp_path):
    root = tmp_path / "drive"
    _write(root / "Games" / "Big" / "a.pak", 3_000_000)
    _write(root / "Games" / "Small" / "b.pak", 1_000_000)
    _write(root / "Videos" / "clip.mp4", 2_000_000, b"v")
    _write(root / "Backup" / "clip copy.mp4", 2_000_000, b"v")
    _write(root / "Windows" / "System32" / "huge.dll", 9_000_000)
    r = jn.scan(str(root), big_mb=1)
    assert [(f["name"], f["size"]) for f in r["folders"]] == [("Games", 4_000_000), ("Backup", 2_000_000), ("Videos", 2_000_000)]
    assert r["folders"][0]["children"] == [{"name": "Big", "size": 3_000_000}, {"name": "Small", "size": 1_000_000}]
    assert not r["partial"] and r["scanned"] == 8_000_000
    dupes = jn.duplicates(r["big"])
    assert len(dupes) == 1 and dupes[0]["wasted"] == 2_000_000 and len(dupes[0]["paths"]) == 2


def test_scan_is_time_boxed(tmp_path):
    for i in range(5):
        _write(tmp_path / f"d{i}" / "f", 10)
    ticks = iter(range(100))
    r = jn.scan(str(tmp_path), seconds=2, clock=lambda: next(ticks))
    assert r["partial"]


def test_disk_report_scans_in_the_background_and_reports(svc, tmp_path, monkeypatch):
    drive = tmp_path / "drive"
    _write(drive / "Games" / "x.bin", 5_000_000)
    _write(drive / "Temp" / "old.tmp", 1000)
    monkeypatch.setattr(jn.tempfile, "gettempdir", lambda: str(drive / "Temp"))
    nudged = []
    svc.nudge = lambda text, **kw: nudged.append(text)
    j = jn.Janitor(svc, runner=lambda a: None, read_cpu=lambda: None, recycle=lambda: 2 * GB,
                   list_drives=lambda: [jn.drive_row(str(drive), 100 * GB, 1 * GB)])
    j.cfg["downloads"] = str(drive / "Downloads")
    r = j.disk_report("", wait_s=10)
    assert r["ok"] and r["folders"][0]["name"] == "Games" and r["recycle_bin"] == 2 * GB
    said = j.say("disk_report", {}, r)
    assert said.startswith(f"{drive} is 99% full, 1.0 GB free of 100 GB. The biggest: Games 5 MB, Temp 0 MB.")
    assert "2.0 GB in the Recycle Bin" in said and said.endswith("Details are on the HUD.")
    assert r["temp"] == {"bytes": 1000, "files": 1} and "old_downloads" not in r
    assert j.status()["report"]["folders"][0]["name"] == "Games" and nudged == []
    # still scanning when the answer is due: says so, and tells J when it's done
    j.reports.clear()
    gate = __import__("threading").Event()
    real = jn.scan
    jn.scan = lambda *a, **k: (gate.wait(5), real(*a, **k))[1]
    try:
        r = j.disk_report("", wait_s=0)
        assert r["scanning"] and "I'm looking at what's taking the space" in j.say("disk_report", {}, r)
        gate.set()
        for _ in range(100):
            if nudged:
                break
            time.sleep(0.05)
    finally:
        jn.scan = real
    assert nudged and "The biggest: Games" in nudged[0]


def test_tidy_downloads_plan_apply_and_undo(env):
    j, state, clock, nudged, calls, dl = env
    old = clock.t - 3600
    for name in ("photo.JPG", "report.pdf", "setup.exe", "stuff.zip", "movie.mkv", "notes.xyz", "half.mp4.crdownload",
                 "fresh.pdf", ".hidden.png"):
        (dl / name).write_bytes(b"1234")
        os.utime(dl / name, (old, old if name != "fresh.pdf" else clock.t))
    (dl / "Documents").mkdir()
    (dl / "Documents" / "report.pdf").write_bytes(b"already here")
    plan = j.tidy_downloads()
    assert plan["dry_run"] and plan["count"] == 5 and sorted(os.listdir(dl / "Documents")) == ["report.pdf"]
    assert plan["by_folder"] == {"Images": 1, "Documents": 1, "Installers": 1, "Archives": 1, "Video": 1}
    assert j.say("tidy_downloads", {}, plan).startswith("I'd move 5 files into Images (1), Documents (1)")
    done = j.tidy_downloads(dry_run=False)
    assert done["moved"] == 5 and (dl / "Images" / "photo.JPG").exists() and (dl / "Installers" / "setup.exe").exists()
    assert sorted(os.listdir(dl / "Documents")) == ["report (1).pdf", "report.pdf"]  # never overwrites
    assert {"notes.xyz", "half.mp4.crdownload", "fresh.pdf", ".hidden.png"} <= set(os.listdir(dl))
    assert "undo the downloads tidy" in j.say("tidy_downloads", {}, done)
    back = j.undo_tidy()
    assert back == {"ok": True, "restored": 5, "of": 5} and (dl / "photo.JPG").exists() and (dl / "report.pdf").exists()
    assert (dl / "Documents" / "report.pdf").read_bytes() == b"already here"
    assert j.undo_tidy()["ok"] is False


def test_tidy_needs_a_yes_and_the_fast_path_asks(env, svc):
    j, state, clock, nudged, calls, dl = env
    (dl / "a.png").write_bytes(b"1")
    os.utime(dl / "a.png", (clock.t - 3600, clock.t - 3600))
    tool = {t.name: t for t in j.tools()}["tidy_downloads"]
    assert tool.tier_for({}) == 0 and tool.tier_for({"dry_run": False}) == 2
    assert tool.describe({"dry_run": False}) == "move 1 files in Downloads into Images (1) (nothing is deleted)"
    sk = Skills(svc, [])
    sk.all["janitor"] = j
    svc.skills = sk
    from assistant.brain.assistant import Assistant

    out = Assistant(svc).handle("clean up my downloads")
    assert out["kind"] == "pending" and "Images (1)" in out["reply"] and (dl / "a.png").exists()


@pytest.mark.parametrize("said,tool,args", [
    ("how's my pc", "pc_health", {}),
    ("how is my computer doing", "pc_health", {}),
    ("how hot is my gpu", "pc_health", {"focus": "gpu"}),
    ("is my gpu throttling", "pc_health", {"focus": "gpu"}),
    ("why is my drive full", "disk_report", {"drive": ""}),
    ("why is my d drive so full", "disk_report", {"drive": "d"}),
    ("what's taking up all the space on my c drive", "disk_report", {"drive": "c"}),
    ("free up space", "disk_report", {"drive": ""}),
    ("undo the downloads tidy", "undo_downloads_tidy", {}),
])
def test_routes(svc, said, tool, args):
    sk = Skills(svc, ["janitor"])
    intent = route(said, RouterContext(skill_route=sk.route))
    assert (intent.tool, intent.args) == (tool, args)


def test_clean_downloads_routes_to_a_plan_when_nothing_to_move(env):
    j, *_ = env
    assert j.route("clean up my downloads").args == {"dry_run": True}
    assert j.say("tidy_downloads", {}, j.tidy_downloads()) == "Downloads is already tidy."


def test_hud_actions(env):
    j, state, clock, nudged, calls, dl = env
    acts = j.actions()
    assert acts["refresh"]({})["ok"] and acts["preview"]({})["count"] == 0
    assert j.status()["drives"][1]["label"] == "D:"


def test_gb_and_drive_rows():
    assert jn.gb(18 * GB) == "18 GB" and jn.gb(1.5 * GB) == "1.5 GB" and jn.gb(3 * (1 << 40)) == "3.0 TB"
    assert jn.drive_row("D:\\", 100, 1) == {"mount": "D:\\", "label": "D:", "total": 100, "free": 1, "free_pct": 1.0,
                                           "used_pct": 99.0}
    assert isinstance(jn.drives(), list)  # the real psutil call, on whatever machine runs the tests
