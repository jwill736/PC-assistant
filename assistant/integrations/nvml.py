"""The graphics card's numbers read in this process through NVML (nvidia-ml-py), not by launching nvidia-smi.

nvidia-smi starts the whole management library again on every run: a process launch and roughly 50-150 ms of CPU
each, which games feel as frame-time spikes. With the HUD open the system panel asked every 4 seconds (888 launches
an hour, measured), and PC Health every minute (PERF-6). NVML is the library nvidia-smi itself calls: loaded once,
each reading takes microseconds and starts nothing.

nvidia-ml-py is optional (requirements.txt). Without it, or without the driver's library (nvml.dll ships with every
NVIDIA driver; no NVIDIA card), every function here returns None and the callers launch nvidia-smi as before.
Tests never touch the real driver: tests/conftest.py sets ``DISABLED``.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

DISABLED = False  # True: behave as if NVML weren't there (tests)
_LOCK = threading.Lock()
_STATE: dict = {"tried": False, "nvml": None, "handles": [], "error": ""}
LAUNCHES_SAVED = [0]  # readings that would have launched nvidia-smi (the resource report counts them)


def _lib():
    """The initialised pynvml module and the cards' handles, or (None, [])."""
    if DISABLED:
        return None, []
    with _LOCK:
        if not _STATE["tried"]:
            _STATE["tried"] = True
            try:
                import pynvml

                pynvml.nvmlInit()
                handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(pynvml.nvmlDeviceGetCount())]
                _STATE.update(nvml=pynvml, handles=handles)
                log.info("GPU readings through NVML (%d card%s): no nvidia-smi launches", len(handles),
                         "" if len(handles) == 1 else "s")
            except Exception as exc:  # no package, no driver library, no card
                _STATE["error"] = f"{type(exc).__name__}: {exc}"[:200]
                log.debug("NVML unavailable (%s): GPU readings launch nvidia-smi", _STATE["error"])
        return _STATE["nvml"], _STATE["handles"]


def available() -> bool:
    return _lib()[0] is not None


def status() -> dict:
    nv, handles = _lib()
    return {"nvml": nv is not None, "cards": len(handles), "error": _STATE["error"] if nv is None else ""}


def _get(fn, *a, scale: float = 1.0):
    """One reading; None where the card doesn't support it ("[N/A]" in nvidia-smi)."""
    try:
        v = fn(*a)
    except Exception:
        return None
    return v * scale if isinstance(v, (int, float)) else v


def _name(nv, h) -> str:
    n = _get(nv.nvmlDeviceGetName, h) or "GPU"
    return n.decode(errors="replace") if isinstance(n, bytes) else str(n)


def gpus() -> list[dict] | None:
    """Each card as SystemMonitor.gpus() gives it: name, util, mem_used_mb, mem_total_mb, temp_c, power_w,
    encoder_util. None: NVML isn't available here."""
    nv, handles = _lib()
    if nv is None:
        return None
    out = []
    for h in handles:
        mem = _get(nv.nvmlDeviceGetMemoryInfo, h)
        util = _get(nv.nvmlDeviceGetUtilizationRates, h)
        enc = _get(nv.nvmlDeviceGetEncoderUtilization, h)
        out.append({
            "name": _name(nv, h),
            "util": float(util.gpu) if util is not None else None,
            "mem_used_mb": round(mem.used / 1048576) if mem is not None else None,
            "mem_total_mb": round(mem.total / 1048576) if mem is not None else None,
            "temp_c": _get(nv.nvmlDeviceGetTemperature, h, nv.NVML_TEMPERATURE_GPU, scale=1.0),
            "power_w": _get(nv.nvmlDeviceGetPowerUsage, h, scale=0.001),
            "encoder_util": float(enc[0]) if enc else None,
        })
    LAUNCHES_SAVED[0] += 1
    return out


def health() -> list[dict] | None:
    """Each card as PC Health reads it (janitor.parse_gpus): temp_c, power_w, limit_w, util, name and the active
    clock-limit reasons (the same bits nvidia-smi prints). None: NVML isn't available here."""
    nv, handles = _lib()
    if nv is None:
        return None
    out = []
    for h in handles:
        reasons = getattr(nv, "nvmlDeviceGetCurrentClocksEventReasons", None) or \
            nv.nvmlDeviceGetCurrentClocksThrottleReasons
        limit = _get(nv.nvmlDeviceGetPowerManagementLimit, h, scale=0.001)
        if limit is None:
            limit = _get(nv.nvmlDeviceGetEnforcedPowerLimit, h, scale=0.001)
        util = _get(nv.nvmlDeviceGetUtilizationRates, h)
        out.append({"name": _name(nv, h), "temp_c": _get(nv.nvmlDeviceGetTemperature, h, nv.NVML_TEMPERATURE_GPU),
                    "power_w": _get(nv.nvmlDeviceGetPowerUsage, h, scale=0.001), "limit_w": limit,
                    "util": float(util.gpu) if util is not None else None, "bits": int(_get(reasons, h) or 0)})
    LAUNCHES_SAVED[0] += 1
    return out


def driver() -> str | None:
    """"<first card's name>, <driver version>" (as nvidia-smi --query-gpu=name,driver_version), or None."""
    nv, handles = _lib()
    if nv is None or not handles:
        return None
    version = _get(nv.nvmlSystemGetDriverVersion) or "?"
    return f"{_name(nv, handles[0])}, {version.decode() if isinstance(version, bytes) else version}"
