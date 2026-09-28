"""Deterministic Windows controls: volume (master and per app), brightness,
settings pages and virtual desktops.

API first, keys second: ``pycaw`` sets exact levels through Core Audio, with
the volume keys (2% a press) as the fallback; ``screen-brightness-control``
uses WMI for laptop panels and DDC/CI for monitors; ``pyvda`` is optional and
feature-detected because a Windows update has broken it before, so
next/previous desktop always works through Ctrl+Win+arrow. Everything returns
``{"ok": False, "error": ...}`` rather than raising.
"""

from __future__ import annotations

import difflib
import logging
import os
import sys

from . import desktop

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
VK_CONTROL, VK_LWIN, VK_LEFT, VK_RIGHT = 0x11, 0x5B, 0x25, 0x27

SETTINGS_PAGES = {
    "sound": "ms-settings:sound", "display": "ms-settings:display", "bluetooth": "ms-settings:bluetooth",
    "wifi": "ms-settings:network-wifi", "network": "ms-settings:network-status",
    "windows update": "ms-settings:windowsupdate", "notifications": "ms-settings:notifications",
    "do not disturb": "ms-settings:notifications", "apps": "ms-settings:appsfeatures",
    "startup apps": "ms-settings:startupapps", "storage": "ms-settings:storagesense",
    "power": "ms-settings:powersleep", "microphone privacy": "ms-settings:privacy-microphone",
    "camera privacy": "ms-settings:privacy-webcam", "default apps": "ms-settings:defaultapps",
    "game mode": "ms-settings:gaming-gamemode", "night light": "ms-settings:nightlight",
    "mouse": "ms-settings:mousetouchpad", "personalization": "ms-settings:personalization",
    "about": "ms-settings:about", "graphics": "ms-settings:display-advancedgraphics",
}


def _windows_only(what: str) -> dict:
    return {"ok": False, "error": f"{what} is only wired up on Windows."}


def _com_init() -> None:
    """Core Audio is COM; each thread that touches it needs its own apartment."""
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:  # already initialised on this thread, or comtypes missing (pycaw pulls it in)
        pass


# ---- volume ----------------------------------------------------------------------

def _endpoint():
    from pycaw.pycaw import AudioUtilities

    _com_init()
    speakers = AudioUtilities.GetSpeakers()
    volume = getattr(speakers, "EndpointVolume", None)  # pycaw 2025+
    if volume is None:  # older pycaw: activate the interface by hand
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioEndpointVolume

        volume = cast(speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
    return volume


def _pct(x: float) -> int:
    return int(round(max(0.0, min(1.0, float(x))) * 100))


def volume(level: int | None = None, change: int | None = None, mute: bool | None = None, endpoint=None) -> dict:
    """Set the master volume to ``level`` %, or move it by ``change`` points; ``mute`` True/False.
    No arguments reports the current level."""
    if not IS_WINDOWS and endpoint is None:
        return _windows_only("Volume control")
    try:
        ep = endpoint or _endpoint()
    except Exception as exc:
        log.debug("pycaw unavailable (%s); falling back to volume keys", exc)
        return _volume_by_keys(level, change, mute)
    try:
        if level is not None or change is not None:
            current = _pct(ep.GetMasterVolumeLevelScalar())
            target = max(0, min(100, int(level) if level is not None else current + int(change)))
            ep.SetMasterVolumeLevelScalar(target / 100.0, None)
            if target > 0 and mute is None:
                ep.SetMute(0, None)  # "volume 40" while muted means you want to hear it
        if mute is not None:
            ep.SetMute(1 if mute else 0, None)
        return {"ok": True, "volume": _pct(ep.GetMasterVolumeLevelScalar()), "muted": bool(ep.GetMute())}
    except Exception as exc:
        return {"ok": False, "error": f"Couldn't change the volume: {exc}"}


def _volume_by_keys(level, change, mute) -> dict:
    """No pycaw: the volume keys move Windows' volume 2 points a press."""
    out = {"ok": True, "approximate": True}
    if level is not None:  # bottom out, then climb: exact to within 2 points
        for _ in range(2):
            r = desktop.media_key("volume_down", 25)
            if not r.get("ok"):
                return r
        if int(level) >= 2:
            left = int(level) // 2
            while left > 0:
                r = desktop.media_key("volume_up", min(left, 25))
                if not r.get("ok"):
                    return r
                left -= 25
        out["volume"] = int(level) // 2 * 2
    elif change:
        r = desktop.media_key("volume_up" if change > 0 else "volume_down", max(1, abs(int(change)) // 2))
        if not r.get("ok"):
            return r
    if mute is not None:  # the mute key only toggles; say so
        r = desktop.media_key("mute")
        if not r.get("ok"):
            return r
        out["toggled_mute"] = True
    return out


def _sessions():
    from pycaw.pycaw import AudioUtilities

    _com_init()
    return AudioUtilities.GetAllSessions()


def app_volume(app: str, level: int | None = None, mute: bool | None = None, names: set[str] | None = None,
               sessions=None) -> dict:
    """Per-app volume / mute (only apps currently holding an audio session, i.e. that have made sound)."""
    if not IS_WINDOWS and sessions is None:
        return _windows_only("Per-app volume")
    try:
        found = sessions if sessions is not None else _sessions()
    except Exception as exc:
        return {"ok": False, "error": f"Per-app volume needs pycaw (pip install -r requirements.txt): {exc}"}
    wanted = {n.lower().removesuffix(".exe") for n in (names or set())} | {app.lower().replace(" ", "")}
    hits = []
    for s in found:
        proc = getattr(s, "Process", None)
        pname = (proc.name() if proc else "").lower().removesuffix(".exe")
        if not pname or not any(w == pname or w in pname for w in wanted):
            continue
        vol = s.SimpleAudioVolume
        if level is not None:
            vol.SetMasterVolume(max(0, min(100, int(level))) / 100.0, None)
        if mute is not None:
            vol.SetMute(1 if mute else 0, None)
        hits.append({"app": pname, "volume": _pct(vol.GetMasterVolume()), "muted": bool(vol.GetMute())})
    if not hits:
        playing = sorted({(s.Process.name() if getattr(s, "Process", None) else "").removesuffix(".exe")
                          for s in found} - {""})
        return {"ok": False, "error": f"{app} isn't playing audio right now."
                + (f" Apps with sound: {', '.join(playing)}." if playing else "")}
    return {"ok": True, "apps": hits}


# ---- brightness --------------------------------------------------------------------

def brightness(level: int | None = None, change: int | None = None, sbc=None) -> dict:
    if sbc is None:
        if not IS_WINDOWS:
            return _windows_only("Brightness control")
        try:
            import screen_brightness_control as sbc
        except ImportError:
            return {"ok": False, "error": "Brightness needs screen-brightness-control (pip install -r requirements.txt)."}
    try:
        current = sbc.get_brightness()
        if not current:
            raise RuntimeError("no display reported a brightness")
        if level is not None or change is not None:
            target = max(0, min(100, int(level) if level is not None else int(current[0]) + int(change)))
            sbc.set_brightness(target)
            current = sbc.get_brightness()
        return {"ok": True, "brightness": [int(b) for b in current]}
    except Exception as exc:
        return {"ok": False, "error": "This screen won't take brightness from Windows (on a desktop monitor, "
                                      f"turn on DDC/CI in its menu): {exc}"}


# ---- settings pages ----------------------------------------------------------------

def open_settings(page: str, opener=None) -> dict:
    key = page.lower().strip().removesuffix(" settings")
    if key not in SETTINGS_PAGES:
        match = difflib.get_close_matches(key, list(SETTINGS_PAGES), n=1, cutoff=0.6)
        if not match:
            match = [k for k in SETTINGS_PAGES if key and (key in k or k in key)][:1]
        if not match:
            return {"ok": False, "error": f"No settings page called {page}. Try: {', '.join(sorted(SETTINGS_PAGES))}."}
        key = match[0]
    uri = SETTINGS_PAGES[key]
    if opener is None:
        if not IS_WINDOWS:
            return _windows_only("Opening Settings")
        opener = os.startfile  # type: ignore[attr-defined]
    try:
        opener(uri)
    except OSError as exc:
        return {"ok": False, "error": f"Couldn't open {key} settings: {exc}"}
    return {"ok": True, "page": key, "uri": uri}


# ---- virtual desktops ----------------------------------------------------------------

def _pyvda():
    try:
        import pyvda

        pyvda.get_virtual_desktops()  # the COM interface is what breaks between Windows builds
        return pyvda
    except Exception:
        return None


def virtual_desktop(action: str = "status", number: int | None = None, vda=None, chord=None) -> dict:
    """action: next | previous | go (to ``number``, 1-based) | status.
    ``vda``: None detects pyvda, False skips it (keyboard shortcut only), or a pyvda-like module."""
    if not IS_WINDOWS and vda is None and chord is None:
        return _windows_only("Virtual desktops")
    if vda is None:
        vda = _pyvda()
    chord = chord or desktop.press_chord
    try:
        if vda:
            count = len(vda.get_virtual_desktops())
            current = vda.VirtualDesktop.current().number
            target = {"next": current + 1, "previous": current - 1, "go": number}.get(action)
            if action in ("next", "previous", "go"):
                if not target or not 1 <= int(target) <= count:
                    return {"ok": False, "error": f"There's no desktop {target}; you have {count}."}
                vda.VirtualDesktop(int(target)).go()
                current = int(target)
            return {"ok": True, "desktop": current, "count": count}
        if action in ("next", "previous"):
            chord(VK_CONTROL, VK_LWIN, VK_RIGHT if action == "next" else VK_LEFT)
            return {"ok": True, "moved": action}
        return {"ok": False, "error": "Going to a numbered desktop needs pyvda (pip install -r requirements.txt)."}
    except Exception as exc:
        return {"ok": False, "error": f"Virtual desktops: {exc}"}
