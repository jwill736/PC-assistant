"""Desktop control: active window, window list/focus, launching and closing apps,
media keys, lock/sleep. Windows is the primary target (pure ctypes, no pywin32);
Linux/macOS get best-effort fallbacks so the rest of the app still runs.
"""

from __future__ import annotations

import difflib
import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import psutil

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Never offer to kill these, whatever the user or the model asks.
ALWAYS_PROTECTED = {
    "system", "idle", "registry", "smss", "csrss", "wininit", "winlogon", "services",
    "lsass", "svchost", "explorer", "dwm", "fontdrvhost", "sihost", "ctfmon",
    "python", "pythonw", "py", "systemd", "init", "launchd", "kernel_task", "windowserver",
}


@dataclass
class WindowInfo:
    title: str
    app: str
    pid: int
    handle: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_app(name: str) -> str:
    name = (name or "").strip().lower()
    return name[:-4] if name.endswith(".exe") else name


def _proc_name(pid: int) -> str:
    try:
        return normalize_app(psutil.Process(pid).name())
    except (psutil.Error, ValueError):
        return "unknown"


# --------------------------------------------------------------------------
# Windows implementation
# --------------------------------------------------------------------------
if IS_WINDOWS:  # pragma: no cover - exercised by the Windows CI job
    import ctypes
    from ctypes import wintypes

    # Private DLL handles with explicit signatures: ctypes' default int conversion
    # can overflow 64-bit handles, and the shared ctypes.windll objects are
    # re-typed by other libraries (e.g. `keyboard`).
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        dwmapi = None

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _sig(fn, restype, *argtypes):
        fn.restype, fn.argtypes = restype, list(argtypes)

    _sig(user32.GetForegroundWindow, wintypes.HWND)
    _sig(user32.GetWindowTextLengthW, ctypes.c_int, wintypes.HWND)
    _sig(user32.GetWindowTextW, ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _sig(user32.GetWindowThreadProcessId, wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _sig(user32.IsWindowVisible, wintypes.BOOL, wintypes.HWND)
    _sig(user32.IsIconic, wintypes.BOOL, wintypes.HWND)
    _sig(user32.ShowWindow, wintypes.BOOL, wintypes.HWND, ctypes.c_int)
    _sig(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
    _sig(user32.EnumWindows, wintypes.BOOL, EnumWindowsProc, wintypes.LPARAM)
    _sig(user32.GetLastInputInfo, wintypes.BOOL, ctypes.POINTER(LASTINPUTINFO))
    _sig(user32.keybd_event, None, ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ctypes.c_size_t)  # BYTE is unsigned
    _sig(user32.LockWorkStation, wintypes.BOOL)
    _sig(kernel32.GetTickCount, wintypes.DWORD)
    if dwmapi is not None:
        _sig(dwmapi.DwmGetWindowAttribute, ctypes.c_long, wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)

    def _window_text(hwnd) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _window_pid(hwnd) -> int:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _is_cloaked(hwnd) -> bool:
        if dwmapi is None:
            return False
        cloaked = ctypes.c_int(0)
        DWMWA_CLOAKED = 14
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return bool(cloaked.value)

    def _active_window() -> WindowInfo | None:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = _window_pid(hwnd)
        return WindowInfo(_window_text(hwnd), _proc_name(pid), pid, int(hwnd))

    def _list_windows() -> list[WindowInfo]:
        found: list[WindowInfo] = []

        def callback(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd) and not _is_cloaked(hwnd):
                title = _window_text(hwnd)
                if title and title not in {"Program Manager", "Windows Input Experience"}:
                    pid = _window_pid(hwnd)
                    found.append(WindowInfo(title, _proc_name(pid), pid, int(hwnd)))
            return True

        user32.EnumWindows(EnumWindowsProc(callback), 0)
        return found

    def _focus(handle: int) -> bool:
        SW_RESTORE = 9
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, SW_RESTORE)
        # Windows only lets the foreground process steal focus; a synthetic
        # Alt tap satisfies that rule.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        return bool(user32.SetForegroundWindow(handle))

    def _idle_seconds() -> float:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0

    def _press_vk(vk: int) -> None:
        KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
        user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

else:

    def _run_quiet(args: list[str]) -> str:
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def _active_window() -> WindowInfo | None:
        if IS_MAC:
            app = _run_quiet(["osascript", "-e", 'tell application "System Events" to get name of first process whose frontmost is true'])
            return WindowInfo(app, normalize_app(app), 0) if app else None
        if shutil.which("xdotool"):
            wid = _run_quiet(["xdotool", "getactivewindow"])
            if wid:
                title = _run_quiet(["xdotool", "getwindowname", wid])
                pid = _run_quiet(["xdotool", "getwindowpid", wid])
                pid_i = int(pid) if pid.isdigit() else 0
                return WindowInfo(title, _proc_name(pid_i) if pid_i else "unknown", pid_i, int(wid))
        return None

    def _list_windows() -> list[WindowInfo]:
        if shutil.which("wmctrl"):
            out = []
            for line in _run_quiet(["wmctrl", "-lp"]).splitlines():
                parts = line.split(None, 4)
                if len(parts) == 5 and parts[2].isdigit():
                    pid = int(parts[2])
                    out.append(WindowInfo(parts[4], _proc_name(pid), pid, int(parts[0], 16)))
            return out
        return []

    def _focus(handle: int) -> bool:
        if shutil.which("wmctrl"):
            return subprocess.call(["wmctrl", "-ia", hex(handle)]) == 0
        return False

    def _idle_seconds() -> float:
        if shutil.which("xprintidle"):
            out = _run_quiet(["xprintidle"])
            return int(out) / 1000.0 if out.isdigit() else 0.0
        return 0.0

    def _press_vk(vk: int) -> None:
        raise NotImplementedError("media keys are only wired up on Windows")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def active_window() -> WindowInfo | None:
    try:
        return _active_window()
    except Exception:
        log.debug("active_window failed", exc_info=True)
        return None


def list_windows() -> list[WindowInfo]:
    try:
        return _list_windows()
    except Exception:
        log.debug("list_windows failed", exc_info=True)
        return []


def idle_seconds() -> float:
    try:
        return _idle_seconds()
    except Exception:
        return 0.0


def where_am_i() -> dict:
    """What the user is looking at right now, plus everything else that's open."""
    active = active_window()
    windows = list_windows()
    by_app: dict[str, list[str]] = {}
    for w in windows:
        by_app.setdefault(w.app, []).append(w.title)
    return {
        "active": active.as_dict() if active else None,
        "open_windows": len(windows),
        "apps": {app: titles[:6] for app, titles in sorted(by_app.items())},
    }


def focus_window(query: str) -> dict:
    q = query.lower().strip()
    windows = list_windows()
    scored = []
    for w in windows:
        hay = f"{w.app} {w.title}".lower()
        score = 1.0 if q in hay else difflib.SequenceMatcher(None, q, hay).ratio()
        scored.append((score, w))
    scored.sort(key=lambda s: s[0], reverse=True)
    if not scored or scored[0][0] < 0.45:
        return {"ok": False, "error": f"No open window matches '{query}'."}
    target = scored[0][1]
    ok = _focus(target.handle) if target.handle else False
    return {"ok": ok, "window": target.as_dict()}


MEDIA_KEYS = {
    "mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "next": 0xB0, "previous": 0xB1, "play_pause": 0xB3,
}


def media_key(action: str, times: int = 1) -> dict:
    vk = MEDIA_KEYS.get(action)
    if vk is None:
        return {"ok": False, "error": f"Unknown media action '{action}'. Options: {', '.join(MEDIA_KEYS)}"}
    try:
        for _ in range(max(1, min(int(times), 25))):
            _press_vk(vk)
            time.sleep(0.02)
    except NotImplementedError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "action": action}


def power_action(action: str) -> dict:
    """lock | sleep | restart | shutdown. Callers must gate the last three."""
    if not IS_WINDOWS:
        return {"ok": False, "error": "Power actions are wired up for Windows only."}
    cmds = {  # pragma: no cover - Windows only
        "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
        "restart": ["shutdown", "/r", "/t", "10"],
        "shutdown": ["shutdown", "/s", "/t", "10"],
    }
    if action == "lock":  # pragma: no cover
        user32.LockWorkStation()
        return {"ok": True, "action": "lock"}
    if action not in cmds:
        return {"ok": False, "error": f"Unknown power action '{action}'."}
    subprocess.Popen(cmds[action])  # pragma: no cover
    return {"ok": True, "action": action}


# --------------------------------------------------------------------------
# App launching
# --------------------------------------------------------------------------

class AppLauncher:
    """Resolves spoken names ("open discord") to something launchable.

    Order: configured aliases -> executables on PATH -> Start Menu / Desktop
    shortcuts (which covers nearly every installed Windows program and Steam
    game) -> fuzzy suggestions.
    """

    def __init__(self, aliases: dict | None = None):
        self.aliases = {k.lower(): v for k, v in (aliases or {}).items()}
        self._shortcuts: dict[str, Path] | None = None
        self._index_lock = threading.Lock()

    def shortcut_index(self) -> dict[str, Path]:
        with self._index_lock:
            if self._shortcuts is None:
                self._shortcuts = self._build_shortcut_index()
            return self._shortcuts

    @staticmethod
    def _shortcut_dirs() -> list[Path]:
        env = os.environ
        dirs = []
        if IS_WINDOWS:
            for base in (env.get("ProgramData"), env.get("APPDATA")):
                if base:
                    dirs.append(Path(base) / "Microsoft/Windows/Start Menu/Programs")
            for base in (env.get("USERPROFILE"), env.get("PUBLIC")):
                if base:
                    dirs.append(Path(base) / "Desktop")
        elif IS_MAC:
            dirs += [Path("/Applications"), Path.home() / "Applications"]
        else:
            dirs += [Path("/usr/share/applications"), Path.home() / ".local/share/applications"]
        return [d for d in dirs if d.exists()]

    def _build_shortcut_index(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        suffixes = {".lnk", ".url", ".app", ".desktop"}
        for root in self._shortcut_dirs():
            depth0 = len(root.parts)
            for dirpath, dirnames, filenames in os.walk(root):
                if len(Path(dirpath).parts) - depth0 > 3:
                    dirnames.clear()
                    continue
                for fname in filenames + [d for d in dirnames if d.endswith(".app")]:
                    p = Path(dirpath) / fname
                    if p.suffix.lower() in suffixes:
                        name = p.stem.lower()
                        if "uninstall" in name:
                            continue
                        index.setdefault(name, p)
        return index

    def resolve(self, name: str) -> dict:
        key = name.lower().strip()
        key = key.removeprefix("the ").removesuffix(" app").strip()
        if key in self.aliases:
            return {"kind": "alias", "name": key, "spec": self.aliases[key]}
        close = difflib.get_close_matches(key, list(self.aliases), n=1, cutoff=0.8)
        if close:
            return {"kind": "alias", "name": close[0], "spec": self.aliases[close[0]]}
        exe = shutil.which(key) or shutil.which(key.replace(" ", ""))
        if exe:
            return {"kind": "exe", "name": key, "path": exe}
        index = self.shortcut_index()
        if key in index:
            return {"kind": "shortcut", "name": key, "path": str(index[key])}
        # "chrome" should find "google chrome"; "premiere" finds "adobe premiere pro 2025"
        contains = sorted((n for n in index if key in n), key=len)
        if contains:
            return {"kind": "shortcut", "name": contains[0], "path": str(index[contains[0]])}
        fuzzy = difflib.get_close_matches(key, list(index), n=3, cutoff=0.6)
        if fuzzy:
            return {"kind": "shortcut", "name": fuzzy[0], "path": str(index[fuzzy[0]])}
        suggestions = difflib.get_close_matches(key, list(index) + list(self.aliases), n=5, cutoff=0.3)
        return {"kind": "missing", "name": key, "suggestions": suggestions}

    def launch(self, name: str) -> dict:
        target = self.resolve(name)
        kind = target["kind"]
        try:
            if kind == "missing":
                return {"ok": False, "error": f"I couldn't find an app called '{name}'.", "suggestions": target["suggestions"]}
            if kind == "alias":
                spec = target["spec"]
                if isinstance(spec, str):
                    spec = {"path": spec}
                _start(os.path.expandvars(spec["path"]), spec.get("args"), spec.get("cwd"))
            else:
                _start(target["path"], None, None)
        except OSError as exc:
            return {"ok": False, "error": f"Launching {target['name']} failed: {exc}"}
        return {"ok": True, "launched": target["name"], "via": kind}

    def process_names_for(self, name: str) -> set[str]:
        """Process names to look for when closing an app by its spoken name."""
        key = name.lower().strip()
        names = {normalize_app(key), normalize_app(key.replace(" ", ""))}
        spec = self.aliases.get(key)
        if isinstance(spec, dict):
            if spec.get("process"):
                procs = spec["process"] if isinstance(spec["process"], list) else [spec["process"]]
                names |= {normalize_app(p) for p in procs}
            if spec.get("path"):
                names.add(normalize_app(Path(os.path.expandvars(spec["path"]).split(" --")[0]).name))
        return {n for n in names if n}


def _start(path: str, args: list | str | None, cwd: str | None) -> None:
    extra = shlex.split(args, posix=not IS_WINDOWS) if isinstance(args, str) else list(args or [])
    p = Path(path)
    if IS_WINDOWS and not extra and (p.suffix.lower() in {".lnk", ".url"} or path.startswith(("steam://", "ms-", "shell:"))):
        os.startfile(path)  # type: ignore[attr-defined]  # pragma: no cover
        return
    if IS_MAC and p.suffix == ".app":
        subprocess.Popen(["open", "-a", str(p), *extra])
        return
    if not IS_WINDOWS and p.suffix == ".desktop" and shutil.which("gtk-launch"):
        subprocess.Popen(["gtk-launch", p.stem])
        return
    flags = 0x00000008 | 0x00000200 if IS_WINDOWS else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen(
        [path, *extra],
        cwd=cwd or (str(p.parent) if p.is_absolute() else None),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        start_new_session=not IS_WINDOWS,
    )


def find_processes(names: set[str]) -> list[psutil.Process]:
    wanted = {normalize_app(n) for n in names}
    out = []
    for proc in psutil.process_iter(["name"]):
        try:
            if normalize_app(proc.info["name"] or "") in wanted:
                out.append(proc)
        except psutil.Error:
            continue
    return out


def close_processes(names: set[str], protected: set[str] | None = None) -> dict:
    blocked = ALWAYS_PROTECTED | {normalize_app(p) for p in (protected or set())}
    targets = {normalize_app(n) for n in names} - blocked
    if not targets:
        return {"ok": False, "error": "That process is protected."}
    procs = [p for p in find_processes(targets) if p.pid != os.getpid()]
    if not procs:
        return {"ok": False, "error": f"Nothing running named {', '.join(sorted(targets))}."}
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=4)
    return {"ok": True, "closed": len(gone), "still_running": len(alive), "names": sorted(targets)}
