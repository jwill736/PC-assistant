"""PC health and tidying: GPU heat and power, drives filling up, what's eating the space, and a tidy Downloads.

- Every minute: the GPU's temperature, power and throttling (nvidia-smi), the CPU's temperature where it can be read,
  and free space on every drive. Speaks up at a good moment (never on air) when the GPU sits above 85 °C for two
  minutes, when it's pinned at its power limit and throttling in a game, or when a drive has under 10% free.
- "Why is my drive full" / "free up space": the biggest folders on that drive (a time-boxed scan in the background),
  Windows' temp folder, the Recycle Bin, Downloads older than 60 days and big duplicate files.
- "Clean up my downloads": sorts Downloads into Images, Documents, Installers, Archives and Video folders, after your
  yes. It only moves files, never deletes; "undo the downloads tidy" puts them back (data/janitor_undo.json).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

import psutil

from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill, check

IS_WINDOWS = sys.platform == "win32"
MB, GB = 1 << 20, 1 << 30
THROTTLE = {0x4: "power cap", 0x8: "hardware slowdown", 0x20: "thermal slowdown", 0x40: "hardware thermal slowdown",
            0x80: "power brake"}
SKIP_ROOT = {"windows", "$recycle.bin", "system volume information", "$winreagent", "recovery", "$windows.~bt",
             "$windows.~ws", "config.msi", "proc", "sys", "dev", "run", "snap"}
KINDS = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".tif", ".tiff", ".svg", ".psd", ".avif"},
    "Documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".csv", ".odt", ".rtf",
                  ".epub", ".pages", ".json"},
    "Installers": {".exe", ".msi", ".msix", ".msixbundle", ".appx", ".dmg", ".pkg", ".deb", ".rpm", ".appimage"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso", ".zst"},
    "Video": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv", ".m4v"},
}
PARTIAL = {".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload"}


# ---- reading the machine (each one replaceable in tests) -------------------------------------------------------

def run_smi(args: list[str]) -> str | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=5,
                           creationflags=0x08000000 if IS_WINDOWS else 0)  # CREATE_NO_WINDOW
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _num(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None  # "[N/A]", "[Not Supported]"


def parse_gpus(out: str) -> list[dict]:
    """nvidia-smi --query-gpu=temperature.gpu,power.draw,power.limit,<throttle reasons>,utilization.gpu,name"""
    gpus = []
    for line in (out or "").strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 5:
            continue
        try:
            bits = int(p[3], 16) if p[3].lower().startswith("0x") else int(p[3])
        except ValueError:
            bits = 0
        gpus.append({"name": p[5] if len(p) > 5 and p[5] else "GPU", "temp_c": _num(p[0]), "power_w": _num(p[1]),
                     "limit_w": _num(p[2]), "util": _num(p[4]), "throttle": [v for k, v in THROTTLE.items() if bits & k],
                     "power_capped": bool(bits & 0x4), "thermal": bool(bits & 0x60)})
    return gpus


def drives() -> list[dict]:
    out, seen = [], set()
    for part in psutil.disk_partitions(all=False):
        opts = set((part.opts or "").split(","))
        if not part.fstype or "cdrom" in opts or "ro" in opts or part.mountpoint in seen:
            continue
        if part.fstype in {"squashfs", "tmpfs", "devtmpfs", "iso9660", "udf", "overlay"} or \
                part.mountpoint.startswith(("/snap", "/boot", "/proc", "/sys", "/dev", "/var/lib", "/run")):
            continue
        seen.add(part.mountpoint)
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        if u.total:
            out.append(drive_row(part.mountpoint, u.total, u.free))
    return out


def drive_row(mount: str, total: int, free: int) -> dict:
    free_pct = 100 * free / total
    return {"mount": mount, "label": mount.rstrip("\\/") or mount, "total": total, "free": free,
            "free_pct": round(free_pct, 1), "used_pct": round(100 - free_pct, 1)}


def cpu_temp() -> float | None:
    try:
        temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
    except Exception:
        return None
    for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
        if temps.get(key):
            return max(e.current for e in temps[key])
    return None


def recycle_bin_bytes() -> int | None:
    if not IS_WINDOWS:
        return None
    try:  # pragma: no cover - Windows only
        import ctypes

        class SHQUERYRBINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]
        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(info)
        return int(info.i64Size) if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0 else None
    except Exception:  # pragma: no cover
        return None


def gaming() -> bool:
    from .nudges import fullscreen_state

    return fullscreen_state() == "game"


# ---- what's taking the space ------------------------------------------------------------------------------------

def scan(root: str, seconds: float = 25, report_depth: int = 2, max_depth: int = 14, big_mb: float = 50,
         clock: Callable[[], float] = time.monotonic) -> dict:
    """Folder sizes down to ``report_depth`` (every file below counts), the big files, and whether time ran out."""
    deadline, root = clock() + seconds, os.path.abspath(root)
    sizes: dict[str, int] = defaultdict(int)
    big: list[tuple[int, str]] = []
    total, files, partial = 0, 0, False
    stack = [(root, 0, ())]
    while stack:
        if clock() > deadline:
            partial = True
            break
        path, depth, owners = stack.pop()
        try:
            it = os.scandir(path)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink() or getattr(e, "is_junction", lambda: False)():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if (depth == 0 and e.name.lower() in SKIP_ROOT) or depth + 1 > max_depth:
                            continue
                        stack.append((e.path, depth + 1, owners + ((e.path,) if depth + 1 <= report_depth else ())))
                    elif e.is_file(follow_symlinks=False):
                        size = e.stat(follow_symlinks=False).st_size
                        total, files = total + size, files + 1
                        for o in owners:
                            sizes[o] += size
                        if size >= big_mb * MB:
                            big.append((size, e.path))
                except OSError:
                    continue
    tops = sorted(((p, s) for p, s in sizes.items() if os.path.dirname(p) == root), key=lambda x: (-x[1], x[0]))[:8]
    folders = [{"path": p, "name": os.path.basename(p), "size": s,
                "children": [{"name": os.path.basename(c), "size": cs} for c, cs in
                             sorted(((c, cs) for c, cs in sizes.items() if os.path.dirname(c) == p), key=lambda x: -x[1])[:3]]}
               for p, s in tops]
    return {"root": root, "folders": folders, "scanned": total, "files": files, "partial": partial,
            "big": [p for _s, p in sorted(big, reverse=True)[:400]]}


def fingerprint(path: str, size: int) -> str | None:
    h = hashlib.sha1(str(size).encode())
    try:
        with open(path, "rb") as f:
            h.update(f.read(MB))
            if size > 2 * MB:
                f.seek(size - MB)
                h.update(f.read(MB))
    except OSError:
        return None
    return h.hexdigest()


def duplicates(paths: list[str]) -> list[dict]:
    """Same size and the same first and last megabyte: almost surely the same file twice."""
    by_size: dict[int, list[str]] = defaultdict(list)
    for p in paths:
        try:
            by_size[os.path.getsize(p)].append(p)
        except OSError:
            continue
    groups = []
    for size, ps in by_size.items():
        if len(ps) < 2:
            continue
        by_hash: dict[str, list[str]] = defaultdict(list)
        for p in ps:
            fp = fingerprint(p, size)
            if fp:
                by_hash[fp].append(p)
        groups += [{"size": size, "paths": same, "wasted": size * (len(same) - 1)} for same in by_hash.values() if len(same) > 1]
    return sorted(groups, key=lambda g: -g["wasted"])[:10]


def dir_size(path, seconds: float = 5, older_than: float | None = None) -> tuple[int, int]:
    """Bytes and files under ``path`` (only files last changed before ``older_than``, if given)."""
    deadline, total, count = time.monotonic() + seconds, 0, 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            try:
                st = os.stat(os.path.join(root, n), follow_symlinks=False)
            except OSError:
                continue
            if older_than is None or st.st_mtime < older_than:
                total, count = total + st.st_size, count + 1
        if time.monotonic() > deadline:
            break
    return total, count


def tidy_plan(folder: Path, now: float, min_age_s: float = 600) -> list[dict]:
    moves = []
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
    except OSError:
        return []
    for e in entries:
        ext = os.path.splitext(e.name)[1].lower()
        if e.name.startswith(".") or e.name.lower() == "desktop.ini" or ext in PARTIAL:
            continue
        kind = next((k for k, exts in KINDS.items() if ext in exts), None)
        try:
            if not kind or not e.is_file(follow_symlinks=False) or now - e.stat().st_mtime < min_age_s:
                continue  # still downloading, maybe
            moves.append({"name": e.name, "to": kind, "size": e.stat().st_size})
        except OSError:
            continue
    return moves


def free_name(dst: Path) -> Path:
    n, out = 1, dst
    while out.exists():
        out = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
        n += 1
    return out


def on_drive(path: str, mount: str, mounts: list[str]) -> bool:
    p = os.path.abspath(path)
    if IS_WINDOWS:
        return os.path.splitdrive(p)[0].lower() == os.path.splitdrive(mount)[0].lower()
    best = max((m for m in mounts if p == m or p.startswith(m.rstrip("/") + "/")), key=len, default=None)
    return best == mount


def gb(n: float | None) -> str:
    if n is None:
        return "?"
    return f"{n / (1 << 40):.1f} TB" if n >= 1 << 40 else f"{n / GB:.0f} GB" if n >= 10 * GB else \
        f"{n / GB:.1f} GB" if n >= GB else f"{n / MB:.0f} MB"


# ---- the skill ----------------------------------------------------------------------------------------------------

class Janitor(Skill):
    name, title = "janitor", "PC Health"
    poll_s = 60
    game_essential = True  # the GPU heat/power watch matters most in a game
    delay_s = 20

    def __init__(self, svc, runner=run_smi, list_drives=drives, read_cpu=cpu_temp, recycle=recycle_bin_bytes,
                 in_game=gaming, clock=time.time):
        super().__init__(svc)
        self.runner, self.list_drives, self.read_cpu = runner, list_drives, read_cpu
        self.recycle, self.in_game, self.clock = recycle, in_game, clock
        self._reasons = "clocks_throttle_reasons.active"
        self._hot_since: dict[int, float] = {}
        self._said: dict[str, float] = {}
        self.sample: dict = {}
        self.reports: dict[str, dict] = {}
        self._scans: dict[str, threading.Thread] = {}
        self._announce: set[str] = set()
        self.preview: dict | None = None
        self.error: str | None = None

    # ---- health -------------------------------------------------------------------------------------------
    def gpus(self) -> list[dict]:
        for reasons in dict.fromkeys([self._reasons, "clocks_event_reasons.active"]):
            out = self.runner([f"--query-gpu=temperature.gpu,power.draw,power.limit,{reasons},utilization.gpu,name",
                               "--format=csv,noheader,nounits"])
            if out:
                self._reasons = reasons  # newer drivers renamed "throttle" to "event"
                return parse_gpus(out)
        return []

    def check(self) -> dict:
        try:
            ds = self.list_drives()
        except Exception:
            ds = []
        self.sample = {"at": self.clock(), "gpus": self.gpus(), "cpu_temp": self.read_cpu(), "drives": ds}
        self.sample["issues"] = self._issues(self.sample)
        return self.sample

    def _issues(self, s: dict) -> list[str]:
        hot, low = float(self.setting("gpu_hot_c", 85)), float(self.setting("low_free_pct", 10))
        out = [f"{g['name']} at {g['temp_c']:.0f}°C" for g in s["gpus"] if (g["temp_c"] or 0) > hot]
        out += [f"{g['name']} throttling ({', '.join(g['throttle'])})" for g in s["gpus"] if g["throttle"]]
        out += [f"{d['label']} is {d['used_pct']:.0f}% full, {gb(d['free'])} free" for d in s["drives"] if d["free_pct"] < low]
        return out

    def _once(self, key: str, text: str, priority: str, cooldown: float) -> None:
        now = self.clock()
        if now - self._said.get(key, -1e12) < cooldown:
            return
        self._said[key] = now
        self.nudge(text, priority=priority, key=key, cooldown=cooldown, expires_in=min(cooldown, 6 * 3600))

    def tick(self) -> None:
        try:
            s = self.check()
            self.error = None
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.publish()
            return
        now = s["at"]
        for i, g in enumerate(s["gpus"]):
            t = g["temp_c"]
            if t is not None and t > float(self.setting("gpu_hot_c", 85)):
                since = self._hot_since.setdefault(i, now)
                if now - since >= 120:
                    self._once(f"janitor:gpu-hot:{i}", f"Your GPU has been at {t:.0f}°C for {int((now - since) // 60)} minutes. "
                               "Check the fans and airflow, or cap the frame rate.", "urgent", 1800)
            else:
                self._hot_since.pop(i, None)
            if g["power_capped"] and g["power_w"] and g["limit_w"] and g["power_w"] >= 0.97 * g["limit_w"] and self.in_game():
                self._once(f"janitor:gpu-power:{i}", f"Your GPU is pinned at its {g['limit_w']:.0f} watt power limit and "
                           "throttling. A frame cap or a light undervolt keeps the clocks steady.", "urgent", 3600)
        for d in s["drives"]:
            if d["free_pct"] < float(self.setting("low_free_pct", 10)):
                self._once(f"janitor:low:{d['mount']}", f"Your {d['label']} drive is {d['used_pct']:.0f}% full, only "
                           f"{gb(d['free'])} left. Say “why is my drive full” and I'll find what's taking the space.",
                           "normal", 86400)
        self.publish()

    def pc_health(self, focus: str = "") -> dict:
        s = self.check() if not self.sample or self.clock() - self.sample["at"] > 10 else self.sample
        return {"ok": True, "focus": focus, **s}

    # ---- space ----------------------------------------------------------------------------------------------
    def downloads(self) -> Path:
        return Path(self.setting("downloads") or Path.home() / "Downloads").expanduser()

    def _pick(self, drive: str) -> dict | None:
        ds = self.sample.get("drives") or self.check()["drives"]
        letter = re.sub(r"[^a-z]", "", (drive or "").lower().replace("drive", ""))[:1]
        if letter:
            hit = next((d for d in ds if d["label"].lower().startswith(letter)), None)
            if hit:
                return hit
        return min(ds, key=lambda d: d["free_pct"]) if ds else None

    def disk_report(self, drive: str = "", wait_s: float = 8, announce: bool = True) -> dict:
        d = self._pick(drive)
        if d is None:
            return {"ok": False, "error": "I can't read any drives."}
        rep = self.reports.get(d["mount"])
        if not (rep and self.clock() - rep["at"] < 600) and d["mount"] not in self._scans:
            th = threading.Thread(target=self._scan, args=(d,), daemon=True, name=f"janitor scan {d['label']}")
            self._scans[d["mount"]] = th
            th.start()
        th = self._scans.get(d["mount"])
        if th is not None:
            th.join(wait_s)
        if d["mount"] in self._scans and announce:
            self._announce.add(d["mount"])  # say what it found when it's done
        rep = self.reports.get(d["mount"])
        if d["mount"] in self._scans or rep is None:
            return {"ok": True, "scanning": True, "drive": d}
        return {"ok": True, **rep}

    def _scan(self, d: dict) -> None:
        try:
            res = scan(d["mount"], seconds=float(self.setting("scan_seconds", 25)), big_mb=float(self.setting("big_mb", 50)))
            dl, temp = self.downloads(), tempfile.gettempdir()
            mounts, extra = [x["mount"] for x in self.sample.get("drives") or [d]], {}
            if on_drive(temp, d["mount"], mounts):
                extra["temp"] = dict(zip(("bytes", "files"), dir_size(temp)))
            if dl.is_dir() and on_drive(str(dl), d["mount"], mounts):
                extra["old_downloads"] = dict(zip(("bytes", "files"), dir_size(dl, older_than=self.clock() - 60 * 86400)))
            dupes = duplicates(res.pop("big"))
            rep = {"at": self.clock(), "drive": d, **res, **extra, "recycle_bin": self.recycle(), "duplicates": dupes,
                   "duplicates_bytes": sum(g["wasted"] for g in dupes)}
            self.reports[d["mount"]] = rep
        except Exception as exc:
            self.error = f"Scan of {d['label']} failed: {type(exc).__name__}"
            rep = None
        finally:
            self._scans.pop(d["mount"], None)
        if rep and d["mount"] in self._announce:
            self._announce.discard(d["mount"])
            self.nudge(spoken_report(rep), priority="normal", key=f"janitor:report:{d['mount']}:{int(rep['at'])}")
        self.publish()

    # ---- tidy Downloads -------------------------------------------------------------------------------------
    def tidy_downloads(self, dry_run: bool = True) -> dict:
        folder = self.downloads()
        if not folder.is_dir():
            return {"ok": False, "error": f"I can't find your Downloads folder ({folder})."}
        plan = tidy_plan(folder, self.clock())
        by = sorted(Counter(m["to"] for m in plan).items(), key=lambda kv: (-kv[1], list(KINDS).index(kv[0])))
        out = {"ok": True, "dry_run": bool(dry_run), "folder": str(folder), "count": len(plan), "by_folder": dict(by),
               "bytes": sum(m["size"] for m in plan), "files": plan[:300]}
        self.preview = out
        if dry_run or not plan:
            return out
        moved, failed = [], []
        for m in plan:
            src = folder / m["name"]
            try:
                (folder / m["to"]).mkdir(exist_ok=True)
                dst = free_name(folder / m["to"] / m["name"])
                os.rename(src, dst)  # same folder tree: a rename, never a copy-and-delete
                moved.append([str(src), str(dst)])
            except OSError:
                failed.append(m["name"])
        if moved:
            self._save_undo(self._undo_log()[-9:] + [{"at": self.clock(), "moves": moved}])
        self.preview = None
        return {**out, "moved": len(moved), "failed": failed}

    def _undo_log(self) -> list:
        try:
            data = json.loads((Path(self.svc.cfg.data_dir) / "janitor_undo.json").read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError, AttributeError, KeyError):
            return []

    def _save_undo(self, log: list) -> None:
        try:
            Path(self.svc.cfg.data_dir).mkdir(parents=True, exist_ok=True)
            (Path(self.svc.cfg.data_dir) / "janitor_undo.json").write_text(json.dumps(log, indent=1), encoding="utf-8")
        except (OSError, AttributeError, KeyError):
            pass

    def undo_tidy(self) -> dict:
        log = self._undo_log()
        if not log:
            return {"ok": False, "error": "There's no Downloads tidy to undo."}
        last, back = log.pop(), 0
        for src, dst in reversed(last["moves"]):
            try:
                if os.path.exists(dst) and not os.path.exists(src):
                    os.rename(dst, src)
                    back += 1
            except OSError:
                continue
        self._save_undo(log)
        return {"ok": True, "restored": back, "of": len(last["moves"])}

    def _confirm(self, a: dict) -> str:
        p = self.tidy_downloads(dry_run=True)
        return f"move {p.get('count', 0)} files in Downloads into {_folders(p.get('by_folder') or {})} (nothing is deleted)"

    # ---- skill surface ------------------------------------------------------------------------------------
    def selftest(self) -> list[dict]:
        out = self.runner(["--query-gpu=name,driver_version", "--format=csv,noheader"])
        if not out:
            return [check("nvidia-smi", "warn", "didn't answer: no GPU temperature or throttling alerts",
                          "Install or update the NVIDIA driver")]
        return [check("nvidia-smi", "pass", out.strip().splitlines()[0])]

    def status(self) -> dict:
        s = self.sample or {}
        rep = max(self.reports.values(), key=lambda r: r["at"], default=None)
        return {"at": s.get("at"), "gpus": s.get("gpus") or [], "cpu_temp": s.get("cpu_temp"), "drives": s.get("drives") or [],
                "issues": s.get("issues") or [], "error": self.error, "scanning": [k for k in self._scans],
                "report": rep, "preview": self.preview, "undo": len(self._undo_log())}

    def actions(self):
        return {"refresh": lambda b: {"ok": True, **self.check()},
                "scan": lambda b: self.disk_report(str(b.get("drive") or ""), wait_s=0, announce=False),
                "preview": lambda b: self.tidy_downloads(dry_run=True),
                "tidy": lambda b: self.tidy_downloads(dry_run=False),
                "undo": lambda b: self.undo_tidy()}

    def briefing(self):
        s = self.sample
        return {"issues": s["issues"]} if s and s.get("issues") else None

    def tools(self):
        return [
            Tool("pc_health", "The PC's health now: GPU temperature, power draw vs its limit, throttling, CPU "
                              "temperature, free space on each drive. focus='gpu' for GPU questions.",
                 obj({"focus": {"type": "string"}}), self.pc_health),
            Tool("disk_report", "What's filling a drive (drive letter, or empty for the fullest): biggest folders, temp "
                                "files, Recycle Bin, old Downloads, duplicate big files. Use for 'why is my drive full', "
                                "'free up space'.", obj({"drive": {"type": "string"}}), self.disk_report),
            Tool("tidy_downloads", "Sort the Downloads folder into Images/Documents/Installers/Archives/Video subfolders. "
                                   "dry_run=true (default) only returns the plan; dry_run=false moves the files (never "
                                   "deletes; can be undone).", obj({"dry_run": {"type": "boolean"}}), self.tidy_downloads,
                 tier=lambda a: 2 if a.get("dry_run") is False else 0, confirm_text=self._confirm),
            Tool("undo_downloads_tidy", "Put the files from the last Downloads tidy back where they were.", obj(),
                 self.undo_tidy, tier=1),
        ]

    def route(self, t: str):
        if re.fullmatch(r"how'?s (?:my|the) (?:pc|computer|rig)(?: doing| holding up)?|how is (?:my|the) (?:pc|computer|rig)"
                        r"(?: doing)?|(?:pc|computer) health(?: check)?|is my (?:pc|computer) (?:ok|okay|alright|healthy)", t):
            return Intent("tool", "pc_health", {})
        if re.fullmatch(r"how (?:hot|warm) is (?:my|the) (?:gpu|graphics card|video card)|(?:what'?s|what is) (?:my|the) gpu "
                        r"(?:temp|temperature)|is (?:my|the) gpu (?:hot|overheating|throttling)", t):
            return Intent("tool", "pc_health", {"focus": "gpu"})
        m = re.fullmatch(r"why is (?:my |the )?(?:([a-z]) )?(?:drive|disk|hard drive|ssd|storage)(?: ([a-z]))? (?:so |this )?full"
                         r"|what'?s (?:taking|eating|using) (?:up )?(?:all )?(?:the |my )?(?:space|storage|disk space)"
                         r"(?: on (?:my |the )?([a-z])(?: drive)?)?|free up (?:some )?(?:disk |drive )?space"
                         r"(?: on (?:my |the )?([a-z])(?: drive)?)?", t)
        if m:
            return Intent("tool", "disk_report", {"drive": next((g for g in m.groups() if g), "")})
        if re.fullmatch(r"(?:what would you|preview) (?:move|tidy|the downloads tidy|sort)(?: in my downloads)?", t):
            return Intent("tool", "tidy_downloads", {"dry_run": True})
        if re.fullmatch(r"(?:clean|tidy|sort|organi[sz]e) (?:up )?(?:my |the )?downloads(?: folder)?", t):
            has = bool(tidy_plan(self.downloads(), self.clock())) if self.downloads().is_dir() else False
            return Intent("tool", "tidy_downloads", {"dry_run": not has})
        if re.fullmatch(r"undo (?:the |my )?(?:downloads )?(?:tidy|clean ?up)|put my downloads back", t):
            return Intent("tool", "undo_downloads_tidy", {})
        return None

    def say(self, tool, args, result):
        if tool == "pc_health":
            return spoken_health(result, gpu_only=args.get("focus") == "gpu")
        if tool == "disk_report":
            if result.get("scanning"):
                d = result["drive"]
                return (f"{d['label']} is {d['used_pct']:.0f}% full, {gb(d['free'])} free. I'm looking at what's taking "
                        "the space; I'll tell you in a minute, and it'll be on the HUD.")
            return spoken_report(result)
        if tool == "tidy_downloads":
            if not result["count"]:
                return "Downloads is already tidy."
            if result["dry_run"]:
                return (f"I'd move {result['count']} files into {_folders(result['by_folder'])}. Nothing gets deleted. "
                        "Say “clean up my downloads” to do it.")
            fail = f" {len(result['failed'])} were in use and stayed put." if result.get("failed") else ""
            return f"Moved {result['moved']} files into {_folders(result['by_folder'])}.{fail} Say “undo the downloads tidy” to put them back."
        if tool == "undo_downloads_tidy":
            return f"Put {result['restored']} files back in Downloads."
        return None


def _folders(by: dict) -> str:
    parts = [f"{k} ({v})" for k, v in by.items()]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1] if parts else "folders"


def spoken_health(r: dict, gpu_only: bool = False) -> str:
    out = []
    for g in r.get("gpus") or []:
        line = f"Your {g['name']} is at {g['temp_c']:.0f}°C" if g["temp_c"] is not None else f"Your {g['name']}"
        if g["power_w"] is not None:
            line += f", drawing {g['power_w']:.0f}" + (f" of {g['limit_w']:.0f} watts" if g["limit_w"] else " watts")
        if g["util"] is not None:
            line += f", {g['util']:.0f}% busy"
        out.append(line + (f", and throttling ({', '.join(g['throttle'])})." if g["throttle"] else "."))
    if not out:
        out.append("I can't read the GPU here (no NVIDIA driver tools).")
    if gpu_only:
        return " ".join(out)
    if r.get("cpu_temp"):
        out.append(f"CPU {r['cpu_temp']:.0f}°C.")
    low = [d for d in r.get("drives") or [] if d["free_pct"] < 10]
    out.append(" ".join(f"{d['label']} is {d['used_pct']:.0f}% full, only {gb(d['free'])} left." for d in low)
               if low else "Drives have room.")
    return " ".join(out)


def spoken_report(r: dict) -> str:
    d = r["drive"]
    out = [f"{d['label']} is {d['used_pct']:.0f}% full, {gb(d['free'])} free of {gb(d['total'])}."]
    if r.get("folders"):
        out.append("The biggest: " + ", ".join(f"{f['name']} {gb(f['size'])}" for f in r["folders"][:3]) + ".")
    extra = [f"{gb(n)} {what}" for n, what in (
        (r.get("duplicates_bytes") or 0, "in duplicate files"), ((r.get("old_downloads") or {}).get("bytes", 0),
                                                                 "of downloads older than two months"),
        (r.get("recycle_bin") or 0, "in the Recycle Bin"), ((r.get("temp") or {}).get("bytes", 0), "of temp files"))
        if n > (0 if what == "in duplicate files" else GB)]
    if extra:
        out.append("You could free " + ", ".join(extra) + ".")
    return " ".join(out) + " Details are on the HUD."
