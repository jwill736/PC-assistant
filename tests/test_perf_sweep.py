"""PERF sweep: what Vesper costs J's PC while he games and streams (audit2/perf.md, PERF-1 … PERF-14), and the resource
report that measures it on his PC (assistant/resources.py, Setup → Diagnostics → What Vesper is using).

Each test here failed on 1571574 and passes with the fix. Model sizes are the fake Ollama's: the 35B 24 GB, the 4B 2.6 GB.
"""

import json
import subprocess
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from assistant.brain import local_llm
from assistant.brain.local_llm import LocalBrain
from assistant.watchdog import Supervisor

BIG, LIGHT = "qwen3.6:35b-a3b", "qwen3:4b"
GIB = 1024 ** 3


class FakeOllama:
    """Ollama's /api/tags, /api/show, /api/ps, /api/chat, /api/generate and /api/embed, keeping keep_alive's books."""

    def __init__(self, loaded=(), clock=time.time):
        self.clock = clock
        self.loaded = {m: clock() + 900 for m in loaded}
        self.chats: list[dict] = []  # every request that could load a model: model, keep_alive, was it loaded

    def _ask(self, path, body):
        model = body["model"]
        self.chats.append({"path": path, "model": model, "keep_alive": body.get("keep_alive"),
                           "was_loaded": model in self.loaded})
        keep = local_llm.keep_alive_s(body.get("keep_alive", "5m"))
        if keep == 0:
            self.loaded.pop(model, None)
        else:
            self.loaded[model] = self.clock() + keep

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": BIG, "size": 24 * GIB, "details": {"parameter_size": "35B"}},
                {"name": LIGHT, "size": int(2.6 * GIB), "details": {"parameter_size": "4B"}}]})
        if path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"name": m, "size": 24 * GIB, "size_vram": 24 * GIB,
                 "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))} for m, t in self.loaded.items()]})
        if path in ("/api/chat", "/api/generate", "/api/embed"):
            body = json.loads(request.content)
            self._ask(path, body)
            if path == "/api/embed":
                return httpx.Response(200, json={"embeddings": [[0.1] * 8 for _ in body["input"]], "load_duration": 3})
            content = "{}" if body.get("format") else "ok"
            return httpx.Response(200, json={"message": {"role": "assistant", "content": content}, "done": True,
                                             "load_duration": 4_000_000_000 if not self.chats[-1]["was_loaded"] else 2})
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def loads(self):
        return [c for c in self.chats if not c["was_loaded"] and c["keep_alive"] != 0]


def brain(fake: FakeOllama, tmp_path) -> LocalBrain:
    bench = tmp_path / "brain_bench.json"
    bench.write_text(json.dumps({"best": BIG, "light": LIGHT, "kept": True, "results": [
        {"name": BIG, "result": "pass", "strength": 35}, {"name": LIGHT, "result": "pass", "strength": 4}]}))
    return LocalBrain({"enabled": True, "url": "http://127.0.0.1:11434"}, http=fake.client(), bench_path=bench)


@pytest.fixture
def rt(cfg, svc, monkeypatch):
    from assistant import runtime as rmod

    monkeypatch.setattr(rmod, "register_hotkey", lambda *a, **k: None)
    r = rmod.Runtime(cfg, services=svc)
    yield r
    r._commands.shutdown()


def secs(k) -> float:
    return local_llm.keep_alive_s(k) or 0.0


# ---- PERF-1: no background work loads a model onto the card mid-game, or keeps one there --------------------------

def test_a_background_question_in_a_game_doesnt_renew_the_big_models_15_minutes(rt, tmp_path):
    fake = FakeOllama(loaded=[BIG])  # J asked something before starting the game: still loaded
    a = rt.assistant
    a.local = brain(fake, tmp_path)
    rt.cfg["brain"]["local"]["enabled"] = True
    a.low_power = lambda: "game"
    a.think("From J's week in numbers, give ONE suggestion", schema={"type": "object"})
    assert fake.chats and all(c["model"] == BIG and secs(c["keep_alive"]) <= 60 for c in fake.chats), fake.chats


def test_a_background_question_in_a_game_loads_nothing(rt, tmp_path):
    fake = FakeOllama()  # nothing loaded: the game has the card
    a = rt.assistant
    a.local = brain(fake, tmp_path)
    rt.cfg["brain"]["local"]["enabled"] = True
    a.low_power = lambda: "game"
    assert a.think("Write J's morning Pulse", schema={"type": "object"}) is None
    assert not fake.loads(), fake.chats


def test_live_with_a_fullscreen_game_is_still_the_games_card(rt, tmp_path, monkeypatch):
    fake = FakeOllama()
    a = rt.assistant
    a.local = brain(fake, tmp_path)
    rt.cfg["brain"]["local"]["enabled"] = True
    rt.svc.live = lambda: True  # low power says "live", and the stream brain is the light model
    monkeypatch.setattr(rt, "_fullscreen_game", lambda: True)
    assert rt._low_power() == "live" and a.gaming()
    assert a.think("Title ideas for this stream") is None and not fake.loads(), fake.chats
    a.local.set_streaming(True)  # (the OBS poller does this on going live: the light model answers)
    with local_llm.asking():  # J's own request, live and in a game: the stream's light model, as on any stream night
        assert a.think("Title ideas for this stream") == "ok"
    assert [(c["model"], c["keep_alive"]) for c in fake.loads()] == [(LIGHT, "2m")]


def test_the_days_pulse_waits_for_the_game_to_end_rather_than_settle_for_its_rules(rt, tmp_path, monkeypatch):
    from datetime import datetime

    fake = FakeOllama()
    a = rt.assistant
    a.local = brain(fake, tmp_path)
    rt.cfg["brain"]["local"]["enabled"] = True
    a.low_power = lambda: "game"
    pulse = rt.svc.skills["pulse"]
    pulse.now = lambda: datetime(2026, 11, 4, 9, 0, tzinfo=rt.svc.tz)
    built = []
    monkeypatch.setattr(pulse, "build", lambda reason="asked": built.append(reason) or {"ok": True})
    assert a.model_waits()
    pulse.tick()
    assert built == [] and not fake.chats  # the 4B used to be loaded for it mid-game
    a.low_power = lambda: ""  # the game is over
    pulse.tick()
    assert built == ["schedule"]


def test_the_embedding_model_isnt_loaded_for_background_work_mid_game(rt, tmp_path):
    from assistant.memory import Embedder

    fake = FakeOllama()
    a = rt.assistant
    a.low_power = lambda: "game"
    e = Embedder("http://127.0.0.1:11434", "embeddinggemma", http=fake.client())
    e.keep_alive = a.gpu_keep_alive  # as the memory skill sets it at start
    assert e.embed(["J likes rally games"]) is None and fake.chats == []  # waits: embed_missing tries again later
    with local_llm.asking():  # J's own "what do you know about…": as before, kept briefly
        assert e.embed(["rally"], query=True)
    assert [c["keep_alive"] for c in fake.chats] == ["2m"]
    fake.chats.clear()
    assert e.embed(["J likes rally games"])  # now it's loaded: background work may use it, for 30 s
    assert [c["keep_alive"] for c in fake.chats] == ["30s"]
    a.low_power = lambda: ""
    e.embed(["at the desk"])
    assert fake.chats[-1]["keep_alive"] == "30m"


def test_the_memory_skill_puts_its_embedder_under_the_game_gate(rt):
    from assistant.memory import Embedder
    from assistant.skills.memory import MemorySkill

    mem = MemorySkill(rt.svc, embedder=Embedder("http://127.0.0.1:11434", "embeddinggemma", http=FakeOllama().client()))
    mem.start(rt)
    assert mem.memory.embedder.keep_alive == rt.assistant.gpu_keep_alive


def test_js_own_screen_question_mid_game_is_kept_briefly(rt):
    sc = rt.svc.skills.get("screen")
    sc.start(rt)
    rt.assistant.low_power = lambda: "game"
    with local_llm.asking():
        assert sc.keep_alive(lambda: False, "15m") == "2m"
    assert sc.keep_alive(lambda: False, "15m") is None  # (background: never a load)


def test_a_game_starting_cuts_the_models_left_from_before_short_once(rt, monkeypatch):
    started = []
    monkeypatch.setattr(rt.assistant.local, "game_started", lambda: started.append(1) or [])
    monkeypatch.setattr(rt.svc.obs, "status", lambda: {"connected": False})
    game = [False]
    rt.assistant.gaming = lambda: game[0]
    rt._poll_obs()
    game[0] = True
    for _ in range(3):
        rt._poll_obs()
    end = time.time() + 2
    while not started and time.time() < end:
        time.sleep(0.01)
    assert started == [1]


def test_ollama_expiry_times_are_read():
    assert local_llm._when("2026-11-04T10:15:00.123456789Z") == pytest.approx(1793787300.123, abs=0.01)
    assert local_llm._when("2026-11-04T05:15:00-05:00") == pytest.approx(1793787300, abs=1)
    assert local_llm._when("") is None and local_llm._when(None) is None
    assert local_llm.keep_alive_s("30s") == 30 and local_llm.keep_alive_s("15m") == 900 and local_llm.keep_alive_s(-1) == float("inf")


# ---- PERF-2: speech-to-text threads ----------------------------------------------------------------------------------

def test_speech_to_text_runs_on_one_thread_by_default(cfg, monkeypatch, tmp_path):
    from assistant.voice import models, stt

    got = {}
    monkeypatch.setattr(stt, "resolve", lambda c: "parakeet")
    monkeypatch.setattr(models, "ensure", lambda engine, d, progress=None: tmp_path)
    monkeypatch.setattr(stt, "SherpaSTT", lambda engine, path, threads, hotwords_score: got.update(threads=threads))
    stt.load(cfg["voice"], tmp_path)
    assert got["threads"] == 1  # 42 ms of CPU per second of speech; two threads were 115 ms for 9 ms less wait


# ---- PERF-3: one BLAS thread ----------------------------------------------------------------------------------------

@pytest.mark.real_subprocess
def test_vesper_starts_with_single_threaded_blas():
    code = "import assistant.__main__, os; print(os.environ.get('OPENBLAS_NUM_THREADS'))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         env={k: v for k, v in __import__("os").environ.items() if k != "OPENBLAS_NUM_THREADS"})
    assert out.stdout.strip() == "1", out.stderr[-300:]


# ---- PERF-4: OBS's mic meters only while they're read --------------------------------------------------------------

class FakeEvents:
    made: list = []

    def __init__(self, **kw):
        FakeEvents.made.append(kw.get("subs", 0))
        self.callback = SimpleNamespace(register=lambda fns: None)
        self.worker = SimpleNamespace(is_alive=lambda: True)

    def disconnect(self):
        pass


@pytest.fixture
def obs_events(monkeypatch):
    import obsws_python

    FakeEvents.made = []
    monkeypatch.setattr(obsws_python, "EventClient", FakeEvents)
    return FakeEvents.made


def test_obs_mic_meters_are_only_subscribed_while_live(rt, monkeypatch, obs_events):
    from obsws_python.subs import Subs

    obs = rt.svc.obs
    obs.enabled = True
    rt.cfg["obs"]["health_alerts"] = True
    live = [False]
    monkeypatch.setattr(obs, "status", lambda: {"connected": True, "streaming": {"active": live[0]}, "audio": []})
    rt._poll_obs()
    rt._poll_obs()
    assert len(obs_events) == 1 and not obs_events[0] & Subs.INPUTVOLUMEMETERS  # OBS open all evening: no 20 Hz feed
    assert obs_events[0] & Subs.OUTPUTS and obs_events[0] & Subs.INPUTS  # replay saved, mute changes: still heard
    live[0] = True
    rt._poll_obs()
    assert len(obs_events) == 2 and obs_events[1] & Subs.INPUTVOLUMEMETERS  # live: the mic check reads them
    live[0] = False
    rt._poll_obs()
    assert len(obs_events) == 3 and not obs_events[2] & Subs.INPUTVOLUMEMETERS


def test_the_prestream_check_turns_the_meters_on_for_a_minute(rt, monkeypatch, obs_events):
    from obsws_python.subs import Subs

    obs = rt.svc.obs
    obs.enabled = True
    monkeypatch.setattr(obs, "status", lambda: {"connected": True, "streaming": {"active": False}, "audio": []})
    rt._poll_obs()
    rt._meters_for_check(wait_s=0)
    assert obs_events[-1] & Subs.INPUTVOLUMEMETERS
    rt._poll_obs()  # within the minute: they stay on
    assert len(obs_events) == 2
    rt._meters_until = 0
    rt._poll_obs()
    assert not obs_events[-1] & Subs.INPUTVOLUMEMETERS


# ---- PERF-5: the SDKs Vesper may not need are imported when used ---------------------------------------------------

@pytest.mark.real_subprocess
def test_starting_vesper_doesnt_import_the_anthropic_sdk():
    code = "import sys, assistant.__main__, assistant.runtime; print('anthropic' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.stdout.strip() == "False", out.stderr[-300:]  # a second of every start, ~60 MB


@pytest.mark.real_subprocess
def test_mcp_panel_with_no_servers_does_not_import_the_sdk(tmp_path):
    code = (
        "import sys, types\n"
        "from assistant.skills.mcp import MCP\n"
        "svc = types.SimpleNamespace(cfg=types.SimpleNamespace(get=lambda *a: {}, data_dir=__import__('pathlib').Path(sys.argv[1])),"
        " bus=types.SimpleNamespace(publish=lambda *a, **k: None))\n"
        "m = MCP.__new__(MCP); m.svc = svc; m.cfg = {}; m.last_status = None; m.runtime = None; m._loop = None\n"
        "import threading; m._lock = threading.RLock(); m.servers = {}\n"
        "print(m.status()['sdk'], 'mcp' in sys.modules)\n")
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "None False", out.stderr[-300:]


# ---- PERF-6: the GPU through NVML, no nvidia-smi launches ----------------------------------------------------------

@pytest.fixture
def fake_nvml(monkeypatch):
    from assistant.integrations import nvml

    calls = []
    m = ModuleType("pynvml")
    m.NVML_TEMPERATURE_GPU = 0
    h = object()
    m.nvmlInit = lambda: calls.append("init")
    m.nvmlDeviceGetCount = lambda: 1
    m.nvmlDeviceGetHandleByIndex = lambda i: h
    m.nvmlDeviceGetName = lambda _h: b"NVIDIA GeForce RTX 5090"
    m.nvmlDeviceGetMemoryInfo = lambda _h: SimpleNamespace(total=32607 * 1048576, used=21000 * 1048576)
    m.nvmlDeviceGetUtilizationRates = lambda _h: SimpleNamespace(gpu=55)
    m.nvmlDeviceGetEncoderUtilization = lambda _h: [12, 167000]
    m.nvmlDeviceGetTemperature = lambda _h, sensor: 62
    m.nvmlDeviceGetPowerUsage = lambda _h: 572500
    m.nvmlDeviceGetPowerManagementLimit = lambda _h: 575000

    def unsupported(_h):
        raise RuntimeError("NVML_ERROR_NOT_SUPPORTED")
    m.nvmlDeviceGetEnforcedPowerLimit = unsupported
    m.nvmlDeviceGetCurrentClocksThrottleReasons = lambda _h: 0x4  # (an older driver: no "event" name)
    m.nvmlSystemGetDriverVersion = lambda: "581.15"
    monkeypatch.setitem(sys.modules, "pynvml", m)
    monkeypatch.setattr(nvml, "DISABLED", False)
    monkeypatch.setattr(nvml, "_STATE", {"tried": False, "nvml": None, "handles": [], "error": ""})
    return calls


def test_the_gpu_is_read_through_nvml_without_launching_nvidia_smi(fake_nvml, monkeypatch):
    from assistant.integrations import system

    launched = []
    monkeypatch.setattr(system.subprocess, "run", lambda *a, **k: launched.append(a) or None)
    mon = system.SystemMonitor()
    mon._nvidia_smi = "nvidia-smi"
    t = [1000.0]
    mon.clock = lambda: t[0]
    for _ in range(5):
        t[0] += 5
        g = mon.gpus()
    assert launched == [] and fake_nvml == ["init"]  # once, for the life of Vesper
    assert g == [{"name": "NVIDIA GeForce RTX 5090", "util": 55.0, "mem_used_mb": 21000, "mem_total_mb": 32607,
                  "temp_c": 62.0, "power_w": 572.5, "encoder_util": 12.0}]


def test_pc_health_reads_power_and_throttling_through_nvml(fake_nvml, svc):
    from assistant.skills import janitor

    jan = janitor.Janitor(svc)  # the real runner (nvidia-smi): replaced by NVML when it's there
    jan.runner = janitor.run_smi
    g = jan.gpus()[0]
    assert g["power_w"] == 572.5 and g["limit_w"] == 575.0 and g["power_capped"] and g["throttle"] == ["power cap"]
    assert jan.selftest()[0]["detail"].startswith("NVIDIA GeForce RTX 5090, 581.15")


def test_without_nvml_the_gpu_is_read_with_nvidia_smi_as_before(monkeypatch):
    from assistant.integrations import nvml, system

    monkeypatch.setattr(nvml, "DISABLED", False)
    monkeypatch.setattr(nvml, "_STATE", {"tried": False, "nvml": None, "handles": [], "error": ""})
    monkeypatch.setitem(sys.modules, "pynvml", None)  # not installed
    mon = system.SystemMonitor()
    mon._nvidia_smi = "nvidia-smi"

    class Done:
        stdout = "RTX 5090, 12, 20000, 32607, 55, 300, 0\n"
    monkeypatch.setattr(system.subprocess, "run", lambda *a, **k: Done())
    assert mon.gpus()[0]["util"] == 12.0 and nvml.status()["nvml"] is False


# ---- PERF-7: the Projects panel's git runs -------------------------------------------------------------------------

def test_with_no_hud_open_the_projects_panel_is_refreshed_every_15_minutes(rt, monkeypatch):
    from assistant import runtime as rmod

    asked = []
    monkeypatch.setattr(rt.svc.projects, "summary", lambda: asked.append(1) or {"repos": []})
    for _ in range(7):  # the 2-minute ticks with nobody looking: the first one runs
        rt._poll_projects()
    assert len(asked) == 1
    rt._projects_at -= rmod.UNWATCHED_PROJECTS_S  # 15 minutes on
    rt._poll_projects()
    assert len(asked) == 2
    rt.bus._clients.add(object())  # a HUD opens: every tick again
    rt._poll_projects()
    rt._poll_projects()
    assert len(asked) == 4


def test_an_unchanged_repo_costs_one_git_run_not_four(tmp_path, monkeypatch):
    from assistant.integrations import projects

    repo = tmp_path / "code" / "myrepo"
    repo.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "first commit")
    (repo / "new.txt").write_text("x")
    gits = []
    real = projects._git
    monkeypatch.setattr(projects, "_git", lambda path, *a: gits.append(a[0]) or real(path, *a))
    cache: dict = {}
    first = projects.repo_status(repo, cache)
    assert first["branch"] == "main" and first["dirty_files"] == 1 and first["commits_today"] == 1
    assert first["last_commit"]["message"] == "first commit"
    gits.clear()
    assert projects.repo_status(repo, cache) == first and gits == ["status"]
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "second")  # the ref moves
    again = projects.repo_status(repo, cache)
    assert again["last_commit"]["message"] == "second" and again["commits_today"] == 2


def test_a_growing_claude_session_is_read_from_where_it_stopped(tmp_path):
    from assistant.integrations.projects import ClaudeSessions

    f = tmp_path / "projects" / "-work-vesper" / "s.jsonl"
    f.parent.mkdir(parents=True)

    def prompt(text):
        return json.dumps({"type": "user", "cwd": "/work/vesper", "gitBranch": "main", "timestamp": "2026-11-04T10:00:00Z",
                           "message": {"role": "user", "content": text}}) + "\n"
    f.write_text(prompt("first ask") + json.dumps({"type": "assistant", "message": {"content": "x" * 5000}}) + "\n")
    cs = ClaudeSessions(str(tmp_path))
    assert cs.sessions()[0]["prompts"] == 1
    size = f.stat().st_size
    with f.open("a") as fh:
        fh.write(prompt("second ask") + prompt("third")[:30])  # the writer is mid-line
    s = cs.sessions()[0]
    assert s["prompts"] == 2 and s["last_prompt"] == "second ask" and s["first_prompt"] == "first ask"
    assert cs._cache[str(f)][2]["offset"] > size  # it didn't start from the top
    with f.open("a") as fh:
        fh.write(prompt("third")[30:])
    assert cs.sessions()[0]["prompts"] == 3 and cs.sessions()[0]["last_prompt"] == "third"


# ---- PERF-8: a start delay longer than the stall limit ------------------------------------------------------------

def test_a_poller_whose_start_delay_is_longer_than_its_stall_timeout_is_not_restarted():
    t = [1000.0]
    sup = Supervisor(clock=lambda: t[0])
    try:
        part = sup.poller("skill · Look-around", 60, lambda: None, delay=300, game_interval=0)  # skills/ambient.py
        t[0] += 241  # still inside its 300 s start delay: nothing is stuck
        sup.check()
        assert part.restarts == 0, f"restarted during its start delay: {part.last_error}"
        t[0] += 300  # past the delay and the stall limit, and it never ran: that is stuck
        sup.check()
        assert part.restarts == 1
    finally:
        sup.stop()


# ---- PERF-9: the HUD isn't sent the same data again and again ----------------------------------------------------

def test_the_system_event_carries_the_newest_sample_and_the_state_all_90(rt):
    rt.bus._clients.add(object())  # a HUD is open
    for i in range(5):
        rt.svc.system.history.append({"ts": 1000.0 + i, "cpu": i, "mem": 1, "gpu": None, "up": 0, "down": 0})
    rt.svc.system.snapshot = lambda include_processes=True: {"cpu": {"percent": 5}}
    rt._poll_system()
    sent = rt.bus.latest["system"]["data"]
    assert "history" not in sent and sent["history_add"] == [{"ts": 1004.0, "cpu": 4, "mem": 1, "gpu": None, "up": 0,
                                                              "down": 0}]
    assert [h["ts"] for h in rt.state()["system"]["history"]] == [1000.0, 1001.0, 1002.0, 1003.0, 1004.0]


def test_a_panel_with_nothing_new_isnt_sent_again_for_a_minute(svc):
    from assistant import skills as sk_mod

    sent = []
    svc.bus.on(lambda ev: sent.append(ev["data"]) if ev["type"] == "spotify" else None)
    sp = svc.skills["spotify"]
    data = {"connected": False, "track": None}
    for _ in range(6):  # its 10 s ticks for a minute, nothing playing
        sp.publish(dict(data))
    assert len(sent) == 1 and sp.last_status == data
    sp.publish({**data, "track": "Song 2"})
    assert len(sent) == 2
    sp._sent = (sp._sent[0], sp._sent[1] - sk_mod.SAME_AGAIN_S - 1)  # a minute on
    sp.publish({**data, "track": "Song 2"})  # unchanged, but sent once more (a panel's "N min ago")
    assert len(sent) == 3


# ---- PERF-12: talk that isn't to Vesper doesn't keep the speakers open --------------------------------------------

def test_talking_to_chat_doesnt_open_the_speakers_but_an_exchange_does():
    from assistant.voice.latency import Prewarm

    calls, t = [], [0.0]
    sp = SimpleNamespace(speaking=threading.Event(), prewarm=lambda: calls.append(t[0]))
    p = Prewarm(sp, gap_s=5, clock=lambda: t[0])
    ev = lambda kind, data: {"type": kind, "data": data}  # noqa: E731
    for i in range(20):  # 100 s of J talking to chat, the name never heard
        t[0] = i * 5.0
        p.on_event(ev("voice_state", {"state": "hearing"}))
        p.on_event(ev("heard", {"text": "chat, look at this", "wake": False}))
    assert calls == []
    t[0] = 200.0
    p.on_event(ev("heard", {"text": "Vesper, what time is it", "wake": True}))
    t[0] = 210.0
    p.on_event(ev("voice_state", {"state": "hearing"}))  # the follow-up: ready for it
    assert calls == [210.0]
    t[0] = 400.0
    p.on_event(ev("voice_state", {"state": "hearing"}))  # minutes later: chat again
    assert calls == [210.0]


# ---- PERF-14: the recorder's retention pass ---------------------------------------------------------------------

def test_the_recorders_retention_runs_hourly_not_every_minute(svc, monkeypatch):
    rec = svc.skills["recorder"]
    t = [10_000.0]
    rec.clock = lambda: t[0]
    passes = []
    monkeypatch.setattr(rec, "retention", lambda: passes.append(t[0]) or 0)
    for _ in range(61):
        rec.tick()
        t[0] += 60
    assert len(passes) == 2  # at the start and an hour later (each pass was a database commit)


# ---- the resource report ------------------------------------------------------------------------------------------

def test_what_vesper_is_using_measures_on_demand(rt, tmp_path, monkeypatch):
    from assistant import resources

    from collections import deque

    monkeypatch.setattr(resources, "_CALLS", deque(maxlen=50))
    monkeypatch.setattr(resources, "_LAUNCHES", deque(maxlen=50))
    fake = FakeOllama(loaded=[BIG])
    rt.assistant.local = brain(fake, tmp_path)
    rt.assistant.local.refresh()
    rt.assistant.low_power = lambda: "game"
    resources.launched("nvidia-smi")
    resources.model_call(LIGHT, "30s", 4.0, who="pulse")
    with resources.loading("speaking voice"):
        pass
    slept = []
    r = resources.measure(rt, 30, sleep=slept.append)
    assert sum(slept) == 30 and r["seconds"] >= 0
    assert r["ollama"][0]["name"] == BIG and r["ollama"][0]["vram_gb"] == 24.0 and 800 < r["ollama"][0]["unloads_in_s"] <= 900
    assert r["in_game"] and "mid-game" in r["headline"] and "24 GB, unloads in 15 min" in r["headline"]
    assert any(x["program"] == "nvidia-smi" for x in r["launches"])
    pulse = next(c for c in r["model_calls"] if c["who"] == "pulse")
    assert pulse["loads"] >= 1 and pulse["keep_alive"] == ["30s"]
    assert any(m["name"] == "speaking voice" for m in r["speech_models"])
    assert {p["name"] for p in r["pollers"]} >= set() and r["cpu_pct"] >= 0 and r["rss_mb"] > 0
    text = "\n".join(resources.markdown(r))
    assert "| Ollama model |" in text and "| poller |" in text and BIG in text


def test_a_model_call_is_put_down_to_the_skill_that_asked(rt, tmp_path):
    from assistant import resources

    fake = FakeOllama()
    rt.assistant.local = brain(fake, tmp_path)
    rt.cfg["brain"]["local"]["enabled"] = True
    before = len(resources._CALLS)
    code = {"__name__": "assistant.skills.review", "svc": rt.svc}  # a skill's own code asking, as review.py does
    exec("def ask():\n    return svc.think('one suggestion')", code)
    code["ask"]()
    who, model = resources._CALLS[-1][1], resources._CALLS[-1][2]
    assert len(resources._CALLS) == before + 1 and model == BIG and who == "review"
    with local_llm.asking():
        rt.assistant.think("one suggestion")
    assert resources._CALLS[-1][1].endswith("(you asked)")


def test_launches_are_counted_by_program():
    from assistant import resources

    assert resources.program(["C:\\Windows\\System32\\powercfg.exe", "/getactivescheme"]) == "powercfg"
    assert resources.program('"C:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe" -q') == "nvidia-smi"
    assert resources.program("git status") == "git" and resources.program(["ffmpeg", "-i"], executable=None) == "ffmpeg"


def test_the_card_measures_in_the_background_and_shows_it(svc, rt):
    from assistant import resources

    d = svc.skills["diagnostics"]
    d.start(rt)
    # nothing is measured, and no thread runs, until the button is clicked
    assert d.measuring is None and not any(t.name == "resources-measure" for t in threading.enumerate())
    done = threading.Event()

    def fake_measure(runtime, seconds, on_progress=None):
        on_progress(0.5)
        r = {"at": time.time(), "headline": "Vesper: 0.6% of one CPU core", "seconds": seconds}
        resources.LAST[0] = r
        done.set()
        return r
    d.measure_fn = fake_measure
    out = svc.skills.action("diagnostics", "measure", {})
    assert out["ok"] and out["running"] and out["seconds"] == 30
    assert done.wait(5)
    end = time.time() + 2
    while d.measuring is not None and time.time() < end:
        time.sleep(0.01)
    st = svc.bus.latest["diagnostics"]["data"]
    assert st["measuring"] is None and st["resources"]["headline"].startswith("Vesper: 0.6%")
    # "Measure in a game": starts in a minute, time to switch to the game
    waited, done = [], threading.Event()
    d.wait = waited.append
    out = svc.skills.action("diagnostics", "measure", {"delay": 60})
    assert out["starts_at"] - out["started"] == 60
    assert done.wait(5) and waited == [60.0]


def test_the_diagnostics_report_has_what_vesper_is_using(cfg, svc, rt):
    from assistant import diagnostics as diag, resources

    resources.LAST[0] = None
    rep = diag.run(cfg, svc, rt, timeout=8, write=False)
    assert rep["resources"]["while"] == "making this report" and "headline" in rep["resources"]
    md = diag.to_markdown(rep)
    assert "## What Vesper is using" in md and "| part | CPU (% of one core) |" in md
