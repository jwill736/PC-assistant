"""The HUD's other surfaces and its layout: routes the main server mounts (``register``).

- /api/hud/layout: the panel order, widths and hidden panels per tab, kept in data/settings.yaml (hud.layout) so a
  cleared browser cache never loses them.
- /mini: a 360×520 mini HUD (orb, what was said, next event, latest update, music, timers, mic). The tray, Setup and
  Ctrl+K open it as its own Chrome window in the top-right corner (/api/hud/mini/open).
- /pin: the phone's PIN page (assistant/phone.py), and /api/hud/remote to turn phone access on and change the PIN.
- /overlay: the OBS overlay (assistant/overlay.py) with its own key, and /api/hud/overlay for its settings.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import overlay
from .config import save_setting
from .phone import COOKIE, SESSION_S, PhoneAccess, is_lan

WEB = Path(__file__).parent / "web"
MINI_SIZE = (380, 560)  # the window around a ~360×520 page
VIEW_NAME = re.compile(r"^[a-z0-9_-]{1,24}$")
PANEL_KEY = re.compile(r"^[a-z0-9_:#-]{1,48}$")
SPANS = {4, 6, 8, 12}
MAX_PANELS = 80


# ---- layout ---------------------------------------------------------------------------------------------------

def clean_view_layout(raw: Any) -> dict:
    """One tab's layout from the HUD, checked: {"order": [keys], "spans": {key: 4|6|8|12}, "hidden": [keys]}."""
    raw = raw if isinstance(raw, dict) else {}
    order: list[str] = []
    for k in raw.get("order") or []:
        if isinstance(k, str) and PANEL_KEY.match(k) and k not in order:
            order.append(k)
    spans = {k: int(v) for k, v in (raw.get("spans") or {}).items()
             if isinstance(k, str) and PANEL_KEY.match(k) and isinstance(v, (int, float)) and int(v) in SPANS}
    hidden = list(dict.fromkeys(k for k in raw.get("hidden") or [] if isinstance(k, str) and PANEL_KEY.match(k)))
    return {"order": order[:MAX_PANELS], "spans": dict(list(spans.items())[:MAX_PANELS]), "hidden": hidden[:MAX_PANELS]}


def current_layout(cfg: Any) -> dict:
    lay = (cfg.get("hud") or {}).get("layout") or {}
    return {v: clean_view_layout(x) for v, x in lay.items() if isinstance(v, str) and VIEW_NAME.match(v)}


# ---- the mini HUD window ----------------------------------------------------------------------------------------

def mini_position(size: tuple[int, int] = MINI_SIZE) -> tuple[int, int] | None:
    """Top-right of the main screen, clear of the corner (Windows); elsewhere wherever the window manager likes."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        width = ctypes.windll.user32.GetSystemMetrics(0)  # type: ignore[attr-defined]  # SM_CXSCREEN
    except Exception:
        return None
    return (max(0, width - size[0] - 24), 72) if width else None


def open_mini(url: str, data_dir: Path, browser: Any = None) -> bool:
    """The mini HUD as its own Chrome app window. False when there's no Chrome (the default browser gets a tab)."""
    from .integrations.browser import Browser

    b = browser or Browser()
    if b.app_window(url, size=MINI_SIZE, position=mini_position(), profile=Path(data_dir) / "chrome-mini"):
        return True
    webbrowser.open(url)
    return False


# ---- the routes -------------------------------------------------------------------------------------------------

def register(app: FastAPI, runtime: Any, *, page: Callable[..., HTMLResponse], phone: PhoneAccess,
             host_ok: Callable[[str | None], bool]) -> None:
    from .server import hud_version

    cfg, bus = runtime.cfg, runtime.bus
    keys = {"overlay": overlay.load_overlay_token(cfg.data_dir)}
    version = hud_version()

    # -- layout
    @app.get("/api/hud/layout")
    async def get_layout():
        return {"layout": current_layout(cfg)}

    @app.post("/api/hud/layout")
    async def set_layout(body: dict):
        """{"view": "command", "layout": {order, spans, hidden}} · {"view": "command", "reset": true} · {"reset_all": true}"""
        lay = current_layout(cfg)
        if body.get("reset_all"):
            lay = {}
        else:
            view = str(body.get("view") or "")
            if not VIEW_NAME.match(view):
                raise HTTPException(400, "which tab?")
            if body.get("reset"):
                lay.pop(view, None)
            else:
                lay[view] = clean_view_layout(body.get("layout"))
        try:
            save_setting(cfg, "hud.layout", lay)
        except OSError as exc:
            return {"ok": False, "error": f"Couldn't save the layout: {exc}"[:200], "layout": lay}
        bus.publish("hud_layout", {"layout": lay, "by": str(body.get("by") or "")[:40]}, sticky=True)
        return {"ok": True, "layout": lay}

    # -- mini HUD
    @app.get("/mini", response_class=HTMLResponse)
    async def mini(request: Request):
        return page("mini.html", request)

    @app.post("/api/hud/mini/open")
    async def mini_open(request: Request):
        url = str(request.base_url).rstrip("/") + "/mini"
        ok = await run_in_threadpool(open_mini, url, cfg.data_dir)
        return {"ok": True, "window": ok, "url": url}

    # -- phone over home Wi-Fi
    def remote_status() -> dict:
        return phone.status()

    @app.get("/api/hud/remote")
    async def remote_get():
        return await run_in_threadpool(remote_status)

    @app.post("/api/hud/remote")
    async def remote_set(body: dict):
        """{"enabled": true|false} · {"new_pin": true} · {"sign_out_all": true}"""
        if body.get("new_pin"):
            phone.new_pin()
        if body.get("sign_out_all"):
            phone.sign_out_all()
        if "enabled" in body:
            on = bool(body["enabled"])
            save_setting(cfg, "hud.remote.enabled", on)
            if on:
                phone.pin  # noqa: B018 - the first PIN is made now, so Setup can show it
                await phone.start(app)
                for _ in range(40):  # up to 2 s for the listener to come up (or say why it didn't)
                    if phone.running or phone.error or phone.task is None or phone.task.done():
                        break
                    await asyncio.sleep(0.05)
            else:
                await phone.stop()
        st = await run_in_threadpool(remote_status)
        bus.publish("hud_remote", {k: v for k, v in st.items() if k != "pin"}, sticky=True)
        return {"ok": True, **st}

    @app.get("/pin", response_class=HTMLResponse)
    async def pin_page(request: Request):
        if not is_lan(request.scope):
            return RedirectResponse("/", status_code=303)
        if phone.session(request.cookies.get(COOKIE)):
            return RedirectResponse("/", status_code=303)
        html = (WEB / "pin.html").read_text(encoding="utf-8").replace("{{NAME}}", cfg["assistant"]["name"])
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.post("/pin/login")
    async def pin_login(request: Request):
        if not is_lan(request.scope):
            raise HTTPException(404)
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/").lower() != f"http://{(request.headers.get('host') or '').lower()}":
            return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        ip = request.client.host if request.client else ""
        result = phone.attempt(ip, str((body or {}).get("pin") or ""))
        if not result["ok"]:
            headers = {"Retry-After": str(result["retry_after"])} if result.get("retry_after") else None
            return JSONResponse({k: v for k, v in result.items() if k != "status"}, status_code=result["status"],
                                headers=headers)
        res = JSONResponse({"ok": True})
        res.set_cookie(COOKIE, result["cookie"], max_age=SESSION_S, httponly=True, samesite="strict", path="/")
        return res

    @app.post("/pin/logout")
    async def pin_logout(request: Request):
        if not is_lan(request.scope):
            raise HTTPException(404)
        res = JSONResponse({"ok": True})
        res.delete_cookie(COOKIE, path="/")
        return res

    # -- OBS overlay (its own key; read-only)
    def overlay_ok(key: str | None) -> bool:
        return hmac.compare_digest((key or "").encode(), keys["overlay"].encode())

    def latest() -> dict:
        return {k: v["data"] for k, v in list(bus.latest.items()) if k in overlay.WATCH}

    def recent_events() -> list[dict]:
        feed = getattr(runtime, "twitch_feed", None)
        return feed.recent_events(10) if feed is not None else []

    def overlay_info(request: Request) -> dict:
        base = str(request.base_url).rstrip("/")
        return {"ok": True, "key": keys["overlay"], "base": f"{base}/overlay", "settings": overlay.settings(cfg),
                "panels": list(overlay.PANELS), "themes": list(overlay.THEMES),
                "url": f"{base}/overlay?panels={','.join(overlay.PANELS[:3])}&theme=transparent&key={keys['overlay']}"}

    @app.get("/api/hud/overlay")
    async def overlay_get(request: Request):
        return overlay_info(request)

    @app.post("/api/hud/overlay")
    async def overlay_set(request: Request, body: dict):
        """{"goals": [{"kind": "subs", "target": 10, "label": "Sub goal"}], "countdown_min": 5, "countdown_label": "…"}"""
        conf = overlay.settings(cfg)
        if "goals" in body:
            conf["goals"] = [{"kind": g["kind"], "target": max(0, min(1_000_000, int(g.get("target") or 0))),
                              **({"label": str(g["label"]).strip()[:40]} if str(g.get("label") or "").strip() else {})}
                             for g in (body.get("goals") or [])[:3]
                             if isinstance(g, dict) and g.get("kind") in overlay.GOAL_KINDS
                             and isinstance(g.get("target"), (int, float, str)) and str(g.get("target")).strip().isdigit()]
        if "countdown_min" in body:
            try:
                mins = float(body.get("countdown_min") or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, "countdown_min: minutes, e.g. 5") from None
            conf["countdown"] = ({"at": time.time() + min(mins, 24 * 60) * 60,
                                  "label": str(body.get("countdown_label") or "Starting soon").strip()[:40]}
                                 if mins > 0 else {})
        save_setting(cfg, "hud.overlay", conf)
        bus.publish("hud_overlay", {"goals": conf.get("goals") or [], "countdown": conf.get("countdown") or {}})
        return overlay_info(request)

    @app.post("/api/hud/overlay/rotate")
    async def overlay_rotate(request: Request):
        """A new key: every OBS link made before stops working (open overlays drop on their next update)."""
        keys["overlay"] = overlay.load_overlay_token(cfg.data_dir, rotate=True)
        return overlay_info(request)

    @app.get("/overlay", response_class=HTMLResponse)
    async def overlay_page(key: str = ""):
        if not overlay_ok(key):
            return HTMLResponse("<!doctype html><meta charset=utf-8><title>Overlay</title><body style='background:transparent'>"
                                "<p style='font:20px system-ui;color:#fff;text-shadow:0 1px 3px #000'>This overlay link "
                                "is old: copy the new one from Vesper → Setup → OBS overlay.</p>", status_code=401)
        html = (WEB / "overlay.html").read_text(encoding="utf-8")
        html = re.sub(r'(/static/[\w./-]+\.(?:js|css))"', rf'\1?v={version}"', html)
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/overlay/state")
    async def overlay_state(key: str = ""):
        if not overlay_ok(key):
            return JSONResponse({"error": "bad key"}, status_code=401)
        return overlay.snapshot(latest(), overlay.settings(cfg), await run_in_threadpool(recent_events))

    @app.websocket("/overlay/ws")
    async def overlay_ws(socket: WebSocket):
        key = socket.query_params.get("key", "")
        if is_lan(socket.scope) or not host_ok(socket.headers.get("host")) or not overlay_ok(key):
            await socket.close(code=4401)
            return
        await socket.accept()
        queue = bus.subscribe()
        try:
            snap = overlay.snapshot(latest(), overlay.settings(cfg), recent_events())
            await socket.send_text(json.dumps({"type": "snapshot", "data": snap}))
            last = {k: v for k, v in snap.items() if k != "followers"}
            while True:
                payload = await queue.get()
                if not overlay_ok(key):  # the link was replaced
                    await socket.close(code=4401)
                    break
                kind = overlay.event_type(payload)
                if kind not in overlay.WATCH:
                    continue
                data = json.loads(payload).get("data")
                if kind == "twitch_event":
                    item = overlay.follower(data)
                    if item:
                        await socket.send_text(json.dumps({"type": "follower", "data": item}))
                    continue
                for part, value in overlay.parts_for(kind, data, latest(), overlay.settings(cfg)).items():
                    if last.get(part) != value:
                        last[part] = value
                        await socket.send_text(json.dumps({"type": part, "data": value}))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bus.unsubscribe(queue)
