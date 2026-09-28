"""System vitals (CPU/GPU/RAM/disk/network) and the PC optimizer."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

import psutil

from .desktop import ALWAYS_PROTECTED, normalize_app

# The local model's server is supposed to hold gigabytes: never suggest closing it as a "memory hog"
# (the first morning plan written by llama3.2:3b told the user to deal with llama-server). Closing it on
# request still works.
LOCAL_AI_PROCESSES = {"ollama", "ollama_llama_server", "ollama app", "llama-server", "llama_server", "lm studio",
                      "lms", "lmstudio", "jan", "koboldcpp"}

IS_WINDOWS = sys.platform == "win32"

POWER_PLANS = {  # powercfg aliases, stable across Windows installs
    "high": "SCHEME_MIN",
    "balanced": "SCHEME_BALANCED",
    "saver": "SCHEME_MAX",
}


class SystemMonitor:
    def __init__(self, history: int = 150, heavy_process_mb: int = 1500, protected: list[str] | None = None):
        self.history: deque[dict] = deque(maxlen=history)
        self.heavy_bytes = heavy_process_mb * 1024 * 1024
        self.protected = ALWAYS_PROTECTED | {normalize_app(p) for p in (protected or [])}
        self._last_net = psutil.net_io_counters()
        self._last_disk = psutil.disk_io_counters() if hasattr(psutil, "disk_io_counters") else None
        self._last_t = time.time()
        self._gpu_cache: tuple[float, list] = (0.0, [])
        self._nvidia_smi = shutil.which("nvidia-smi")
        self._ncpu = psutil.cpu_count() or 1
        psutil.cpu_percent(percpu=True)  # prime the counters

    # ---- sampling -------------------------------------------------------
    def gpus(self) -> list[dict]:
        if not self._nvidia_smi:
            return []
        ts, cached = self._gpu_cache
        if time.time() - ts < 4:
            return cached
        fields = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,utilization.encoder"
        try:
            out = subprocess.run(
                [self._nvidia_smi, f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
                creationflags=0x08000000 if IS_WINDOWS else 0,  # CREATE_NO_WINDOW
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return cached
        gpus = []
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue

            def num(v: str) -> float | None:
                try:
                    return float(v)
                except ValueError:
                    return None

            gpus.append({
                "name": parts[0],
                "util": num(parts[1]),
                "mem_used_mb": num(parts[2]),
                "mem_total_mb": num(parts[3]),
                "temp_c": num(parts[4]),
                "power_w": num(parts[5]) if len(parts) > 5 else None,
                "encoder_util": num(parts[6]) if len(parts) > 6 else None,
            })
        self._gpu_cache = (time.time(), gpus)
        return gpus

    def top_processes(self, limit: int = 8) -> dict:
        procs = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
            try:
                info = p.info
                mem = info["memory_info"].rss if info["memory_info"] else 0
                procs.append({
                    "pid": info["pid"],
                    "name": normalize_app(info["name"] or "?"),
                    "cpu": round((info["cpu_percent"] or 0.0) / self._ncpu, 1),
                    "mem_mb": round(mem / 1048576),
                })
            except psutil.Error:
                continue
        # Group by app so 40 chrome.exe helpers read as one line.
        grouped: dict[str, dict] = {}
        for p in procs:
            g = grouped.setdefault(p["name"], {"name": p["name"], "cpu": 0.0, "mem_mb": 0, "count": 0})
            g["cpu"] = round(g["cpu"] + p["cpu"], 1)
            g["mem_mb"] += p["mem_mb"]
            g["count"] += 1
        rows = [g for g in grouped.values() if g["name"] not in {"system idle process", "idle"}]
        return {
            "by_cpu": sorted(rows, key=lambda r: r["cpu"], reverse=True)[:limit],
            "by_mem": sorted(rows, key=lambda r: r["mem_mb"], reverse=True)[:limit],
            "count": len(procs),
        }

    def snapshot(self, include_processes: bool = True) -> dict:
        now = time.time()
        dt = max(now - self._last_t, 1e-3)
        per_core = psutil.cpu_percent(percpu=True)
        cpu_total = round(sum(per_core) / max(len(per_core), 1), 1)
        vm = psutil.virtual_memory()
        net = psutil.net_io_counters()
        up_rate = (net.bytes_sent - self._last_net.bytes_sent) / dt
        down_rate = (net.bytes_recv - self._last_net.bytes_recv) / dt
        self._last_net, self._last_t = net, now

        disk_io = None
        if self._last_disk is not None:
            cur = psutil.disk_io_counters()
            if cur is not None:
                disk_io = {
                    "read_bps": max(0, (cur.read_bytes - self._last_disk.read_bytes) / dt),
                    "write_bps": max(0, (cur.write_bytes - self._last_disk.write_bytes) / dt),
                }
                self._last_disk = cur

        disks, seen = [], set()
        for part in psutil.disk_partitions(all=False):
            opts = set(part.opts.split(","))
            if not part.fstype or part.mountpoint in seen or "cdrom" in opts:
                continue
            if "ro" in opts or part.fstype in {"squashfs", "tmpfs", "devtmpfs", "iso9660", "udf"}:
                continue  # read-only / virtual mounts: nothing to free, just noise
            if part.mountpoint.startswith(("/snap", "/boot", "/var/lib", "/proc", "/sys", "/dev")):
                continue
            seen.add(part.mountpoint)
            try:
                u = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            disks.append({"mount": part.mountpoint, "total_gb": round(u.total / 1e9, 1),
                          "used_gb": round(u.used / 1e9, 1), "percent": u.percent})

        freq = psutil.cpu_freq()
        battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        temps = {}
        if hasattr(psutil, "sensors_temperatures"):
            try:
                for name, entries in (psutil.sensors_temperatures() or {}).items():
                    if entries:
                        temps[name] = max(e.current for e in entries)
            except Exception:
                temps = {}
        gpus = self.gpus()
        snap = {
            "ts": now,
            "cpu": {"percent": cpu_total, "per_core": per_core, "freq_mhz": round(freq.current) if freq else None,
                    "cores": psutil.cpu_count(logical=False), "threads": self._ncpu},
            "memory": {"percent": vm.percent, "used_gb": round(vm.used / 1e9, 1), "total_gb": round(vm.total / 1e9, 1),
                       "available_gb": round(vm.available / 1e9, 1)},
            "swap_percent": psutil.swap_memory().percent,
            "disks": disks,
            "disk_io": disk_io,
            "net": {"up_bps": up_rate, "down_bps": down_rate},
            "gpus": gpus,
            "battery": {"percent": battery.percent, "plugged": battery.power_plugged} if battery else None,
            "temps": temps,
            "uptime_s": now - psutil.boot_time(),
        }
        if include_processes:
            snap["processes"] = self.top_processes()
        self.history.append({
            "ts": now, "cpu": cpu_total, "mem": vm.percent,
            "gpu": gpus[0]["util"] if gpus else None,
            "up": up_rate, "down": down_rate,
        })
        return snap

    # ---- optimizer ------------------------------------------------------
    def temp_dir_size(self, max_files: int = 20000) -> tuple[int, int]:
        total, count = 0, 0
        for root, _dirs, files in os.walk(tempfile.gettempdir()):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
                count += 1
                if count >= max_files:
                    return total, count
        return total, count

    def active_power_plan(self) -> str | None:
        if not IS_WINDOWS:
            return None
        try:  # pragma: no cover - Windows only
            out = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True, timeout=3,
                                 creationflags=0x08000000).stdout
            return out.split("(")[-1].rstrip(") \r\n") if "(" in out else out.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    def analyze(self, streaming: bool = False) -> dict:
        snap = self.snapshot()
        findings: list[dict] = []
        mem = snap["memory"]
        procs = snap["processes"]
        heavy = [p for p in procs["by_mem"] if p["mem_mb"] * 1048576 >= self.heavy_bytes and p["name"] not in self.protected
                 and normalize_app(p["name"]) not in LOCAL_AI_PROCESSES]
        if mem["percent"] >= 85:
            findings.append({
                "severity": "high", "title": f"Memory at {mem['percent']}%",
                "detail": "Biggest consumers: " + ", ".join(f"{p['name']} {p['mem_mb']/1024:.1f} GB" for p in procs["by_mem"][:3]),
                "action": {"tool": "close_app", "args": {"name": heavy[0]["name"]}} if heavy else None,
            })
        elif heavy:
            findings.append({
                "severity": "info", "title": "Heavy apps running",
                "detail": ", ".join(f"{p['name']} {p['mem_mb']/1024:.1f} GB ({p['count']} procs)" for p in heavy[:4]),
                "action": None,
            })
        if snap["cpu"]["percent"] >= 85:
            findings.append({
                "severity": "high", "title": f"CPU pegged at {snap['cpu']['percent']}%",
                "detail": "Top: " + ", ".join(f"{p['name']} {p['cpu']}%" for p in procs["by_cpu"][:3]),
                "action": None,
            })
        for d in snap["disks"]:
            if d["percent"] >= 90:
                findings.append({"severity": "high", "title": f"Drive {d['mount']} is {d['percent']}% full",
                                 "detail": f"{d['total_gb'] - d['used_gb']:.1f} GB free.", "action": {"tool": "clean_temp", "args": {}}})
        temp_bytes, temp_files = self.temp_dir_size()
        if temp_bytes > 1e9:
            findings.append({"severity": "medium", "title": f"Temp folder holds {temp_bytes/1e9:.1f} GB",
                             "detail": f"{temp_files} files in {tempfile.gettempdir()}.", "action": {"tool": "clean_temp", "args": {}}})
        for g in snap["gpus"]:
            if g.get("temp_c") and g["temp_c"] >= 83:
                findings.append({"severity": "high", "title": f"GPU running hot ({g['temp_c']:.0f}°C)",
                                 "detail": "Check fan curve / airflow before going live.", "action": None})
        plan = self.active_power_plan()
        if streaming and plan and "high" not in plan.lower() and "ultimate" not in plan.lower():
            findings.append({"severity": "medium", "title": f"Power plan is '{plan}'",
                             "detail": "Switch to High performance while streaming.",
                             "action": {"tool": "set_power_plan", "args": {"plan": "high"}}})
        if not findings:
            findings.append({"severity": "ok", "title": "System is healthy", "detail": "Nothing worth touching right now.", "action": None})
        return {"findings": findings, "temp_gb": round(temp_bytes / 1e9, 2), "power_plan": plan,
                "cpu": snap["cpu"]["percent"], "memory": mem["percent"]}

    def clean_temp(self, older_than_hours: float = 24) -> dict:
        cutoff = time.time() - older_than_hours * 3600
        freed, removed, skipped = 0, 0, 0
        root = Path(tempfile.gettempdir())
        for path in root.rglob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    size = path.stat().st_size
                    path.unlink()
                    freed += size
                    removed += 1
            except OSError:
                skipped += 1  # in use / permission denied: leave it
        return {"ok": True, "freed_mb": round(freed / 1048576, 1), "removed": removed, "skipped": skipped}

    def set_power_plan(self, plan: str) -> dict:
        scheme = POWER_PLANS.get(plan.lower())
        if not scheme:
            return {"ok": False, "error": f"Plan must be one of {', '.join(POWER_PLANS)}."}
        if not IS_WINDOWS:
            return {"ok": False, "error": "Power plans are a Windows feature."}
        rc = subprocess.call(["powercfg", "/setactive", scheme], creationflags=0x08000000)  # pragma: no cover
        return {"ok": rc == 0, "plan": plan}  # pragma: no cover
