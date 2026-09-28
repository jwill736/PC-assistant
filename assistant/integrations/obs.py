"""OBS Studio control over obs-websocket v5 (built into OBS 28+).

Enable it in OBS: Tools -> WebSocket Server Settings -> Enable, set a password,
and put that password in .env as OBS_PASSWORD.
"""

from __future__ import annotations

import difflib
import logging
import threading
import time

log = logging.getLogger(__name__)
# obsws-python logs the connection password at INFO level; keep it out of our logs.
logging.getLogger("obsws_python").setLevel(logging.CRITICAL)


class OBSController:
    def __init__(self, host: str = "localhost", port: int = 4455, password: str = "",
                 scene_aliases: dict[str, str] | None = None, enabled: bool = True):
        self.host, self.port, self.password = host, port, password
        self.enabled = enabled
        self.scene_aliases = {k.lower(): v for k, v in (scene_aliases or {}).items()}
        self._client = None
        self._lock = threading.RLock()
        self._next_attempt = 0.0
        self._last_error = ""
        self._last_bytes: tuple[float, int] | None = None
        self._inputs_cache: tuple[float, list] = (0.0, [])
        self.scenes: list[str] = []
        self.stop_requested_at = 0.0  # the assistant ended the stream itself (so "offline" isn't an alarm)
        self._events = None           # obsws EventClient: mic meters and replay-saved events
        self._events_next_try = 0.0
        self.last_replay_path: str | None = None
    # ---- connection -----------------------------------------------------
    def _connect(self) -> bool:
        if not self.enabled:
            return False
        if self._client is not None:
            return True
        if time.time() < self._next_attempt:
            return False
        try:
            import obsws_python as obs

            self._client = obs.ReqClient(host=self.host, port=self.port, password=self.password, timeout=3)
            self._last_error = ""
            return True
        except Exception as exc:  # OBS closed, wrong password, websocket disabled
            self._client = None
            self._last_error = _describe(exc)
            self._next_attempt = time.time() + 10
            return False

    def _send(self, request: str, data: dict | None = None) -> dict:
        with self._lock:
            if not self._connect():
                raise ConnectionError(self._last_error or "OBS is not reachable")
            try:
                return self._client.send(request, data, raw=True) or {}
            except Exception as exc:
                # Treat any failure as a dropped socket; next call reconnects.
                if "request" not in type(exc).__name__.lower():
                    self._disconnect()
                raise

    def _disconnect(self) -> None:
        try:
            if self._client is not None:
                self._client.disconnect()
        except Exception:
            pass
        self._client = None

    # ---- read -----------------------------------------------------------
    def status(self) -> dict:
        if not self.enabled:
            return {"connected": False, "enabled": False}
        try:
            scene_list = self._send("GetSceneList")
            stream = self._send("GetStreamStatus")
            record = self._send("GetRecordStatus")
            stats = self._send("GetStats")
        except Exception as exc:
            return {"connected": False, "enabled": True, "error": self._last_error or _describe(exc)}
        scenes = [s["sceneName"] for s in reversed(scene_list.get("scenes", []))]  # OBS lists bottom-up
        self.scenes = scenes
        now = time.time()
        kbps = None
        out_bytes = stream.get("outputBytes") or 0
        if stream.get("outputActive") and self._last_bytes:
            t0, b0 = self._last_bytes
            if now > t0 and out_bytes >= b0:
                kbps = round((out_bytes - b0) * 8 / 1000 / (now - t0))
        self._last_bytes = (now, out_bytes) if stream.get("outputActive") else None
        total = stream.get("outputTotalFrames") or 0
        skipped = stream.get("outputSkippedFrames") or 0
        return {
            "connected": True,
            "enabled": True,
            "current_scene": scene_list.get("currentProgramSceneName"),
            "scenes": scenes,
            "streaming": {
                "active": bool(stream.get("outputActive")),
                "reconnecting": bool(stream.get("outputReconnecting")),
                "timecode": stream.get("outputTimecode"),
                "duration_ms": stream.get("outputDuration"),
                "congestion": stream.get("outputCongestion"),
                "kbps": kbps,
                "dropped_frames": skipped,
                "total_frames": total,
                "dropped_pct": round(skipped / total * 100, 2) if total else 0.0,
            },
            "recording": {
                "active": bool(record.get("outputActive")),
                "paused": bool(record.get("outputPaused")),
                "timecode": record.get("outputTimecode"),
            },
            "stats": {
                "cpu": round(stats.get("cpuUsage") or 0, 1),
                "memory_mb": round(stats.get("memoryUsage") or 0),
                "fps": round(stats.get("activeFps") or 0, 1),
                "render_ms": round(stats.get("averageFrameRenderTime") or 0, 2),
                "render_skipped": stats.get("renderSkippedFrames"),
                "render_total": stats.get("renderTotalFrames"),
                "encoder_skipped": stats.get("outputSkippedFrames"),
                "encoder_total": stats.get("outputTotalFrames"),
            },
            "audio": self.audio_inputs(),
        }

    def audio_inputs(self) -> list[dict]:
        ts, cached = self._inputs_cache
        if time.time() - ts < 15:
            return cached
        inputs = []
        try:
            for item in self._send("GetInputList").get("inputs", []):
                name = item.get("inputName")
                try:
                    muted = self._send("GetInputMute", {"inputName": name}).get("inputMuted")
                except Exception:
                    continue  # not an audio source
                inputs.append({"name": name, "muted": bool(muted)})
        except Exception:
            return cached
        self._inputs_cache = (time.time(), inputs)
        return inputs

    # ---- write ----------------------------------------------------------
    def match_scene(self, spoken: str) -> str | None:
        key = spoken.lower().strip().removesuffix(" scene").removeprefix("the ").strip()
        if key in self.scene_aliases:
            return self.scene_aliases[key]
        if not self.scenes:
            try:
                self.status()
            except Exception:
                return None
        lowered = {s.lower(): s for s in self.scenes}
        if key in lowered:
            return lowered[key]
        contains = [s for low, s in lowered.items() if key in low or low in key]
        if len(contains) == 1:
            return contains[0]
        close = difflib.get_close_matches(key, list(lowered), n=1, cutoff=0.55)
        return lowered[close[0]] if close else (contains[0] if contains else None)

    def switch_scene(self, spoken: str) -> dict:
        scene = self.match_scene(spoken)
        if not scene:
            return {"ok": False, "error": f"No scene matches '{spoken}'.", "scenes": self.scenes}
        try:
            self._send("SetCurrentProgramScene", {"sceneName": scene})
        except Exception as exc:
            return {"ok": False, "error": _describe(exc)}
        return {"ok": True, "scene": scene}

    ACTIONS = {
        "start_stream": "StartStream",
        "stop_stream": "StopStream",
        "start_recording": "StartRecord",
        "stop_recording": "StopRecord",
        "pause_recording": "PauseRecord",
        "resume_recording": "ResumeRecord",
        "save_replay": "SaveReplayBuffer",
        "start_replay_buffer": "StartReplayBuffer",
        "stop_replay_buffer": "StopReplayBuffer",
        "start_virtualcam": "StartVirtualCam",
        "stop_virtualcam": "StopVirtualCam",
    }

    def control(self, action: str) -> dict:
        request = self.ACTIONS.get(action)
        if not request:
            return {"ok": False, "error": f"Unknown OBS action '{action}'. Options: {', '.join(self.ACTIONS)}"}
        if action == "stop_stream":
            self.stop_requested_at = time.time()
        try:
            self._send(request)
        except Exception as exc:
            return {"ok": False, "error": _describe(exc)}
        out = {"ok": True, "action": action}
        if action == "save_replay":  # OBS writes the file a moment later
            time.sleep(0.6)
            path = self.replay_path()
            if path:
                out["path"] = path
        return out

    def replay_path(self) -> str | None:
        try:
            path = self._send("GetLastReplayBufferReplay").get("savedReplayPath")
        except Exception:
            return self.last_replay_path
        self.last_replay_path = path or self.last_replay_path
        return self.last_replay_path

    # ---- sources in the current scene ("hide the cam") ----------------------
    def scene_items(self, scene: str | None = None) -> tuple[str, list[dict]]:
        scene = scene or self._send("GetCurrentProgramScene").get("currentProgramSceneName")
        items = self._send("GetSceneItemList", {"sceneName": scene}).get("sceneItems", [])
        return scene, [{"id": i.get("sceneItemId"), "name": i.get("sourceName"), "enabled": bool(i.get("sceneItemEnabled"))}
                       for i in items]

    SOURCE_WORDS = {"cam": ("cam", "webcam", "camera", "facecam", "video capture"), "chat": ("chat",),
                    "alerts": ("alert",), "overlay": ("overlay",)}

    def set_source_visible(self, source: str, visible: bool | None = None) -> dict:
        """Show/hide a source in the current scene. visible None toggles. "the cam" finds a webcam-ish source."""
        try:
            scene, items = self.scene_items()
        except Exception as exc:
            return {"ok": False, "error": _describe(exc)}
        key = source.lower().strip().removeprefix("the ").removeprefix("my ").strip()
        names = {i["name"].lower(): i for i in items if i.get("name")}
        words = next((ws for k, ws in self.SOURCE_WORDS.items() if key in (k, *ws) or key.rstrip("s") in (k, *ws)), (key,))
        match = names.get(key) or next((i for n, i in names.items() if any(w in n for w in words)), None)
        if match is None:
            close = difflib.get_close_matches(key, list(names), n=1, cutoff=0.5)
            match = names[close[0]] if close else None
        if match is None:
            return {"ok": False, "error": f"No source like '{source}' in {scene}.",
                    "sources": [i["name"] for i in items]}
        target = (not match["enabled"]) if visible is None else bool(visible)
        try:
            self._send("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": match["id"], "sceneItemEnabled": target})
        except Exception as exc:
            return {"ok": False, "error": _describe(exc)}
        return {"ok": True, "source": match["name"], "scene": scene, "visible": target}

    # ---- what the pre-stream check asks --------------------------------------
    def replay_buffer_active(self) -> bool | None:
        try:
            return bool(self._send("GetReplayBufferStatus").get("outputActive"))
        except Exception:
            return None  # replay buffer not enabled in OBS settings, or OBS unreachable

    def record_directory(self) -> str | None:
        try:
            return self._send("GetRecordDirectory").get("recordDirectory")
        except Exception:
            return None

    # ---- events: mic levels every 50 ms, replay saved -------------------------
    def ensure_events(self, on_meters=None, on_replay_saved=None) -> bool:
        """Keep an event connection open (reconnects after OBS restarts). False if it can't right now."""
        if not self.enabled:
            return False
        ev = self._events
        if ev is not None and getattr(getattr(ev, "worker", None), "is_alive", lambda: False)():
            return True
        if time.time() < self._events_next_try:
            return False
        try:
            import obsws_python as obs
            from obsws_python.subs import Subs

            ev = obs.EventClient(host=self.host, port=self.port, password=self.password, timeout=3,
                                 subs=Subs.OUTPUTS | Subs.INPUTVOLUMEMETERS)
        except Exception as exc:
            self._events = None
            self._events_next_try = time.time() + 15
            log.debug("OBS events unavailable: %s", _describe(exc))
            return False

        # obsws dispatches by function name (on_<event_in_snake_case>) with snake_case fields.
        def on_input_volume_meters(data):
            if on_meters:
                on_meters(getattr(data, "inputs", []) or [])

        def on_replay_buffer_saved(data):
            self.last_replay_path = getattr(data, "saved_replay_path", None) or self.last_replay_path
            if on_replay_saved:
                on_replay_saved(self.last_replay_path)
        ev.callback.register([on_input_volume_meters, on_replay_buffer_saved])
        self._events = ev
        return True

    def close_events(self) -> None:
        ev, self._events = self._events, None
        if ev is not None:
            try:
                ev.disconnect()
            except Exception:
                pass

    def set_mute(self, source: str, muted: bool | None = None) -> dict:
        names = [i["name"] for i in self.audio_inputs()] or []
        match = difflib.get_close_matches(source, names, n=1, cutoff=0.4) or [n for n in names if source.lower() in n.lower()]
        target = match[0] if match else source
        try:
            if muted is None:
                self._send("ToggleInputMute", {"inputName": target})
            else:
                self._send("SetInputMute", {"inputName": target, "inputMuted": muted})
            state = self._send("GetInputMute", {"inputName": target}).get("inputMuted")
        except Exception as exc:
            return {"ok": False, "error": _describe(exc)}
        self._inputs_cache = (0.0, [])
        return {"ok": True, "source": target, "muted": bool(state)}


def _describe(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    if "refused" in text.lower() or isinstance(exc, ConnectionRefusedError):
        return "OBS isn't running or its WebSocket server is off."
    if "auth" in text.lower():
        return "OBS rejected the WebSocket password."
    return text
