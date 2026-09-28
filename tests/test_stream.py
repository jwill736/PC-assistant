"""Phase 5a: stream health while live, the mic cross-check, the pre-stream check, source toggles."""

import sys
import time
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace


from assistant.brain.router import route
from assistant.integrations import prestream
from assistant.integrations.obs import OBSController
from assistant.integrations.stream_health import MicWatch, StreamHealth, level_for


# ---- stream health -----------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def status(net=(0, 0), enc=(0, 0), render=(0, 0), active=True, reconnecting=False, connected=True, kbps=6000):
    return {"connected": connected, "streaming": {"active": active, "reconnecting": reconnecting, "kbps": kbps,
                                                  "dropped_frames": net[0], "total_frames": net[1]},
            "stats": {"encoder_skipped": enc[0], "encoder_total": enc[1], "render_skipped": render[0], "render_total": render[1]}}


def feed(h, clock, frames_per_tick, bad_per_tick, ticks, cls="net", start=(0, 0)):
    """Advance 3 s per tick at 60 fps, adding bad frames to one class; returns the alerts."""
    out = []
    bad, total = start
    for _ in range(ticks):
        clock.t += 3
        bad += bad_per_tick
        total += frames_per_tick
        out += h.update(status(**{cls: (bad, total)}))
    return out, (bad, total)


def test_thresholds_follow_the_research():
    assert [level_for(p) for p in (None, 0.05, 0.3, 0.8, 2.5)] == ["unknown", "ok", "notice", "warning", "critical"]


def test_network_drops_warn_once_with_advice_escalate_and_then_clear():
    clock = Clock()
    h = StreamHealth(window_s=60, clock=clock)
    h.update(status())  # stream starts
    alerts, counts = feed(h, clock, 180, 2, 25)  # ~1.1% of frames dropped by the network
    assert [a.kind for a in alerts] == ["network"]
    assert "lower the bitrate by 200 to 500 kbps (you're sending 6000 kbps now)" in alerts[0].text
    assert alerts[0].level == "warning"
    more, counts = feed(h, clock, 180, 6, 15, start=counts)  # gets worse: the last minute averages 2.8%
    assert [a.level for a in more] == ["critical"]
    quiet, counts = feed(h, clock, 180, 0, 50, start=counts)  # clean for well over a minute
    assert [a.kind for a in quiet] == ["recovered"] and "cleared up" in quiet[0].text
    assert h.snapshot()["classes"]["network"]["level"] == "ok"


def test_repeats_are_spaced_out_while_the_problem_lasts():
    clock = Clock()
    h = StreamHealth(window_s=60, repeat_s=180, clock=clock)
    h.update(status())
    alerts, _ = feed(h, clock, 180, 2, 100)  # five minutes of steady 1% drops
    assert [a.kind for a in alerts] == ["network", "network"]  # at the start, and once 3 minutes later


def test_encoder_and_render_are_told_apart():
    clock = Clock()
    h = StreamHealth(clock=clock)
    h.update(status())
    enc, _ = feed(h, clock, 180, 3, 25, cls="enc")
    assert enc and "NVENC" in enc[0].text
    h2 = StreamHealth(clock=clock)
    h2.update(status())
    ren, _ = feed(h2, clock, 180, 3, 25, cls="render")
    assert ren and "cap the game's frame rate" in ren[0].text


def test_reconnect_offline_and_lost_obs():
    clock = Clock()
    h = StreamHealth(clock=clock)
    h.update(status())
    assert [a.kind for a in h.update(status(reconnecting=True))] == ["reconnecting"]
    assert [a.kind for a in h.update(status(reconnecting=False))] == ["reconnected"]
    clock.t += 3600
    off = h.update(status(active=False))
    assert off[0].kind == "offline" and off[0].level == "critical" and "1h 00m" in off[0].text and off[0].speak
    h.update(status())
    h.note_commanded_stop()  # "Vesper, end the stream"
    ended = h.update(status(active=False))
    assert ended[0].level == "info" and not ended[0].speak
    h.update(status())
    assert h.update(status(connected=False))[0].kind == "obs_lost"


def test_a_counter_reset_is_ignored():
    clock = Clock()
    h = StreamHealth(clock=clock)
    h.update(status())
    feed(h, clock, 180, 0, 10)
    clock.t += 3
    assert h.update(status(net=(0, 50))) == []  # OBS restarted its counters mid-window


# ---- the mic cross-check ------------------------------------------------------------

def meters(name, peak):
    return [{"inputName": "Desktop Audio", "inputLevelsMul": [[0.5, 0.9, 0.9]]},
            {"inputName": name, "inputLevelsMul": [[peak / 2, peak, peak], [peak / 2, peak, peak]]}]


def test_mic_pick_prefers_config_then_obs_default_then_a_mic_name():
    assert MicWatch("rode").pick(["Desktop Audio", "Rode NT-USB", "Mic/Aux"]) == "Rode NT-USB"
    assert MicWatch().pick(["Desktop Audio", "Mic/Aux", "Microphone 2"]) == "Mic/Aux"
    assert MicWatch().pick(["Desktop Audio", "Shure Mic"]) == "Shure Mic"
    assert MicWatch().pick(["Desktop Audio"]) is None


def test_talking_into_a_dead_or_muted_mic_is_caught_but_a_noise_gate_is_not():
    clock = Clock()
    mic = MicWatch(window_s=60, clock=clock)
    for _ in range(130):  # a minute of silence from OBS's mic (noise gate closed, or wrong device)
        clock.t += 0.5
        mic.on_meters(meters("Mic/Aux", 0.0))
    assert mic.name == "Mic/Aux" and mic.snapshot()["peak_db"] == -100.0
    assert mic.check(live=True, muted=False, you_spoke=0) == []  # quiet streamer + gate: fine
    silent = mic.check(live=True, muted=False, you_spoke=3)       # but the assistant heard you talk
    assert [a.kind for a in silent] == ["mic_silent"] and "isn't picking anything up" in silent[0].text
    assert mic.check(live=True, muted=False, you_spoke=3) == []   # not again for 5 minutes
    muted = MicWatch(clock=clock)
    muted.on_meters(meters("Mic/Aux", 0.3))
    assert [a.kind for a in muted.check(live=True, muted=True, you_spoke=2)] == ["mic_muted"]
    assert muted.check(live=False, muted=True, you_spoke=5) == []  # only while live


def test_clipping_mic_warns():
    clock = Clock()
    mic = MicWatch(clock=clock)
    for _ in range(6):
        clock.t += 1
        mic.on_meters(meters("Mic/Aux", 1.0))  # 0 dBFS
    alerts = mic.check(live=True, muted=False, you_spoke=0)
    assert [a.kind for a in alerts] == ["mic_clipping"] and mic.snapshot()["clipping"]


# ---- OBS: sources, replay path, events ----------------------------------------------

class FakeOBS(OBSController):
    def __init__(self, items=None, **kw):
        super().__init__(**kw)
        self.sent = []
        self.items = items if items is not None else [
            {"sceneItemId": 1, "sourceName": "Gameplay Capture", "sceneItemEnabled": True},
            {"sceneItemId": 2, "sourceName": "Facecam", "sceneItemEnabled": True},
            {"sceneItemId": 3, "sourceName": "Chat Box", "sceneItemEnabled": False}]

    def _send(self, request, data=None):
        self.sent.append((request, data))
        return {"GetCurrentProgramScene": {"currentProgramSceneName": "Gameplay"},
                "GetSceneItemList": {"sceneItems": self.items},
                "GetLastReplayBufferReplay": {"savedReplayPath": "C:/Videos/Replay 2026-09-28.mkv"}}.get(request, {})


def test_hide_the_cam_finds_a_webcam_like_source_in_the_current_scene():
    obs = FakeOBS()
    assert obs.set_source_visible("the cam", False) == {"ok": True, "source": "Facecam", "scene": "Gameplay", "visible": False}
    assert ("SetSceneItemEnabled", {"sceneName": "Gameplay", "sceneItemId": 2, "sceneItemEnabled": False}) in obs.sent
    assert obs.set_source_visible("chat")["visible"] is True  # omitted = toggle (it was hidden)
    missing = obs.set_source_visible("donation goal", True)
    assert not missing["ok"] and "Facecam" in missing["sources"]


def test_clip_returns_the_saved_file_and_ending_the_stream_is_remembered(monkeypatch):
    monkeypatch.setattr("assistant.integrations.obs.time.sleep", lambda s: None)
    obs = FakeOBS()
    assert obs.control("save_replay")["path"].endswith(".mkv")
    obs.control("stop_stream")
    assert time.time() - obs.stop_requested_at < 5


def test_event_feed_registers_callbacks_by_name(monkeypatch):
    registered = []

    class EventClient:
        def __init__(self, **kw):
            self.kw = kw
            self.worker = SimpleNamespace(is_alive=lambda: True)
            self.callback = SimpleNamespace(register=lambda fns: registered.extend(fns))

    fake = ModuleType("obsws_python")
    fake.EventClient = EventClient
    subs = ModuleType("obsws_python.subs")
    subs.Subs = SimpleNamespace(OUTPUTS=64, INPUTVOLUMEMETERS=65536)
    monkeypatch.setitem(sys.modules, "obsws_python", fake)
    monkeypatch.setitem(sys.modules, "obsws_python.subs", subs)
    got, saved = [], []
    obs = OBSController()
    assert obs.ensure_events(on_meters=got.append, on_replay_saved=saved.append)
    names = {fn.__name__: fn for fn in registered}
    assert set(names) == {"on_input_volume_meters", "on_replay_buffer_saved"}  # obsws matches on these names
    names["on_input_volume_meters"](SimpleNamespace(inputs=[{"inputName": "Mic/Aux"}]))
    names["on_replay_buffer_saved"](SimpleNamespace(saved_replay_path="C:/clip.mkv"))
    assert got == [[{"inputName": "Mic/Aux"}]] and saved == ["C:/clip.mkv"] and obs.last_replay_path == "C:/clip.mkv"
    assert obs.ensure_events() is True  # the live connection is reused


# ---- pre-stream check ----------------------------------------------------------------

def checklist_services(cfg, tmp_path, *, muted=False, replay=True, connected=True, twitch=None):
    obs = SimpleNamespace(enabled=True, match_scene=lambda s: "Starting Soon" if s == "starting soon" else None,
                          replay_buffer_active=lambda: replay, record_directory=lambda: str(tmp_path),
                          status=lambda: {"connected": connected, "enabled": True, "current_scene": "Starting Soon",
                                          "error": "OBS isn't running or its WebSocket server is off.",
                                          "streaming": {"active": False},
                                          "audio": [{"name": "Desktop Audio", "muted": False}, {"name": "Mic/Aux", "muted": muted}]})
    cfg["profiles"]["stream"]["launch"]["obs_scene"] = "starting soon"
    return SimpleNamespace(cfg=cfg, obs=obs, twitch=twitch or SimpleNamespace(enabled=False))


def test_prestream_all_good(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(prestream, "MIN_FREE_GB", 0)
    twitch = SimpleNamespace(enabled=True, channel_info=lambda: {"title": "Building my own JARVIS", "game": "Software and Game Development"})
    out = prestream.run_checklist(checklist_services(cfg, tmp_path, twitch=twitch),
                                  system_snapshot={"cpu": {"percent": 22}, "gpus": [{"util": 30}]})
    assert out["level"] == "good" and out["spoken"].startswith("Pre-stream check: all 7 good")
    names = [i["name"] for i in out["items"]]
    assert names == ["OBS", "Start scene", "Mic", "Replay buffer", "Recording space", "PC load", "Twitch title"]


def test_prestream_names_the_problems_and_their_fixes(cfg, tmp_path):
    out = prestream.run_checklist(checklist_services(cfg, tmp_path, muted=True, replay=False))
    assert out["level"] == "critical"
    assert "Mic: Mic/Aux is muted in OBS." in out["spoken"] and "start replay buffer" in out["spoken"]
    down = prestream.run_checklist(checklist_services(cfg, tmp_path, connected=False))
    assert down["items"][0]["level"] == "critical" and "isn't running" in down["spoken"]


def test_prestream_runs_once_per_stream_event_shortly_before_it():
    tz = timezone.utc
    now = datetime(2026, 9, 28, 19, 50, tzinfo=tz)
    events = [{"title": "Work call", "profile": "work", "start": (now + timedelta(minutes=5)).isoformat()},
              {"title": "Live: ranked grind", "profile": "stream", "start": (now + timedelta(minutes=10)).isoformat()},
              {"title": "Later stream", "profile": "stream", "start": (now + timedelta(hours=3)).isoformat()}]
    seen = set()
    assert prestream.next_stream_event(events, now, 15, seen)["title"] == "Live: ranked grind"
    assert prestream.next_stream_event(events, now, 15, seen) is None  # announced once


# ---- runtime wiring, voice, tiers ------------------------------------------------------

def test_runtime_speaks_stream_alerts_and_publishes_health(cfg, svc):
    from assistant.runtime import Runtime

    rt = Runtime(cfg, services=svc)
    said, events = [], []
    rt.speaker = SimpleNamespace(say=lambda text, **kw: said.append(text))
    real_publish = rt.bus.publish
    rt.bus.publish = lambda kind, data, **kw: (events.append((kind, data)), real_publish(kind, data, **kw))
    svc.obs.enabled = True
    svc.obs.ensure_events = lambda **kw: False
    seq = iter([status(), status(reconnecting=True)])
    svc.obs.status = lambda: {**next(seq), "audio": []}
    rt._poll_obs()
    rt._poll_obs()
    assert said == ["The stream is reconnecting: your internet connection to Twitch dropped."]
    assert [d["kind"] for k, d in events if k == "stream_alert"] == ["reconnecting"]
    assert rt.bus.latest["obs"]["data"]["health"]["reconnecting"] is True
    cfg["obs"]["speak_alerts"] = False
    svc.obs.status = lambda: {**status(reconnecting=False), "audio": []}
    rt._poll_obs()
    assert len(said) == 1  # "reconnected" shown, not spoken
    rt._commands.shutdown()


def test_voice_commands_and_tiers(svc):
    from assistant.brain.assistant import Assistant

    assert route("hide the cam").args == {"source": "cam", "visible": False}
    assert route("toggle chat").args == {"source": "chat"}
    assert route("hide the intro video source").args == {"source": "intro video", "visible": False}
    assert route("am I ready to stream?").tool == route("pre-stream check").tool == "prestream_check"
    assert route("how is the stream").tool == "obs_status"
    tools = Assistant(svc).tools.tools
    assert tools["obs_source"].tier_for({}) == 1 and tools["prestream_check"].tier_for({}) == 0


def test_listener_counts_your_speech_but_not_its_own():
    import numpy as np

    from assistant.voice.listener import VoiceListener

    speaker = SimpleNamespace(speaking=__import__("threading").Event(), last_text="", last_end=0,
                              chime=lambda: None, interrupt=lambda **k: None, expects_reply=False)
    lst = VoiceListener(SimpleNamespace(publish=lambda *a, **k: None), speaker, ["friday"], lambda t: None, {})
    lst.stt = SimpleNamespace(name="parakeet", transcribe=lambda s, hints=(): "just chatting with chat")
    audio = np.zeros(16000, np.int16)
    lst._handle_audio(audio)
    lst._handle_audio(audio)
    lst._handle_audio(audio, during_speech=True)  # that one overlapped our own voice
    assert lst.heard_speech(60) == 2
