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
        try:
            self._send(request)
        except Exception as exc:
            return {"ok": False, "error": _describe(exc)}
        return {"ok": True, "action": action}

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
