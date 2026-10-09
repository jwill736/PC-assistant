"""Local HTTP + WebSocket API that the dashboard talks to.

Bound to 127.0.0.1. Every /api and /ws call must carry the per-install token
(injected into the dashboard page) and a localhost Host header, so a random
website open in your browser can't drive your PC through this port.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .runtime import Runtime

WEB = Path(__file__).parent / "web"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}


def load_token(data_dir: Path) -> str:
    path = data_dir / "api_token"
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(24)
    path.write_text(token)
    return token


class Command(BaseModel):
    text: str


class TaskIn(BaseModel):
    title: str
    profile: str = "work"
    priority: int = 2
    due: str | None = None


class TaskPatch(BaseModel):
    title: str | None = None
    profile: str | None = None
    priority: int | None = None
    status: str | None = None
    due: str | None = None


class JobIn(BaseModel):
    kind: str
    prompt: str
    project: str | None = None
    title: str | None = None


def create_app(runtime: Runtime, start_background: bool = True) -> FastAPI:
    token = load_token(runtime.cfg.data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime.bus.bind_loop(asyncio.get_running_loop())
        if start_background:
            runtime.start()
        yield
        if start_background:
            runtime.stop()

    app = FastAPI(title=f"{runtime.cfg['assistant']['name']} HUD", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.token = token
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    def host_ok(host: str | None) -> bool:
        return (host or "").rsplit(":", 1)[0] in ALLOWED_HOSTS

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not host_ok(request.headers.get("host")):
            return JSONResponse({"error": "bad host"}, status_code=403)
        # Google's sign-in sends the browser back here with no token header; a single-use state guards it instead
        if request.url.path.startswith("/api") and request.url.path != "/api/google/callback":
            supplied = request.headers.get("x-assistant-token", "")
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({"error": "missing or bad token"}, status_code=401)
        return await call_next(request)

    svc, assistant = runtime.svc, runtime.assistant
    tools = assistant.tools

    # The HUD's script and styles carry a hash of their content, so after an update the window loads the new
    # ones instead of a copy Chrome cached (an updated Vesper was showing the old Setup → Brain panel).
    version = hashlib.sha1(b"".join((WEB / f).read_bytes() for f in ("app.js", "styles.css"))).hexdigest()[:10]

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html = (WEB / "index.html").read_text(encoding="utf-8")
        html = html.replace('/static/app.js"', f'/static/app.js?v={version}"').replace(
            '/static/styles.css"', f'/static/styles.css?v={version}"')
        html = html.replace("{{TOKEN}}", token).replace("{{NAME}}", runtime.cfg["assistant"]["name"]).replace(
            "{{VERSION}}", version)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    async def state():
        # hud_version: a HUD window left open across an update reloads itself when this changes
        return {**await run_in_threadpool(runtime.state), "hud_version": version}

    @app.post("/api/command")
    async def command(body: Command):
        return await run_in_threadpool(assistant.handle, body.text, "text")

    @app.post("/api/confirm")
    async def confirm(request: Request):
        """{"id": "<pending id>"}: confirms that action only, never one that replaced it."""
        body = await request.json() if int(request.headers.get("content-length") or 0) else {}
        reply = await run_in_threadpool(runtime.confirm_pending, body.get("id"), "hud")
        return {"reply": reply}

    @app.post("/api/cancel")
    async def cancel(request: Request):
        body = await request.json() if int(request.headers.get("content-length") or 0) else {}
        assistant.cancel(body.get("id"))
        return {"ok": True}

    @app.get("/api/control")
    async def control_status():
        """Kill-switch state and the recent action log (what it did, why, who asked)."""
        return assistant.control_status()

    @app.post("/api/control/stop")
    async def control_stop():
        return await run_in_threadpool(runtime.kill_switch, "hud")

    @app.post("/api/control/resume")
    async def control_resume():
        return runtime.resume_control("hud")

    @app.post("/api/quit")
    async def quit_app():
        """Close Vesper: the installer does this before updating (``python -m assistant --quit``)."""
        stop = getattr(app.state, "on_quit", None)
        if stop is None:
            raise HTTPException(503, "this copy can't be closed from the API")
        stop()
        return {"ok": True, "pid": os.getpid()}  # so --quit can wait for this process to end, not just the port

    @app.post("/api/briefing/{kind}")
    async def make_briefing(kind: str):
        if kind not in {"morning", "recap"}:
            raise HTTPException(404)
        return await run_in_threadpool(assistant.briefing, kind)

    @app.post("/api/briefing/{kind}/speak")
    async def speak_briefing(kind: str):
        b = assistant.last_briefing
        if b:
            runtime.speaker.say(b["spoken"])
        return {"ok": bool(b)}

    @app.get("/api/calendar")
    async def calendar(days: int = 7, profile: str | None = None):
        return await run_in_threadpool(lambda: tools.run("calendar", {"days": days, "profile": profile},
                                                         context={"source": "hud"}))

    @app.get("/api/activity")
    async def activity(day: str | None = None):
        from .brain.tools import day_bounds

        return await run_in_threadpool(lambda: svc.activity.summary_for_day(day_bounds(svc, day), svc.tz))

    @app.get("/api/projects")
    async def projects():
        return await run_in_threadpool(svc.projects.summary)

    @app.get("/api/news")
    async def news(topic: str | None = None):
        return {"headlines": await run_in_threadpool(svc.news.headlines, topic, 30)}

    @app.get("/api/tasks")
    async def list_tasks(status: str | None = "open"):
        return svc.storage.list_tasks(status or None)

    @app.post("/api/tasks")
    async def add_task(body: TaskIn):
        task = svc.storage.add_task(body.title, body.profile, body.priority, body.due)
        svc.bus.publish("tasks", svc.storage.list_tasks())
        return task

    @app.patch("/api/tasks/{task_id}")
    async def patch_task(task_id: int, body: TaskPatch):
        task = svc.storage.update_task(task_id, **body.model_dump(exclude_none=True))
        if not task:
            raise HTTPException(404)
        svc.bus.publish("tasks", svc.storage.list_tasks())
        return task

    @app.get("/api/jobs")
    async def jobs():
        return svc.storage.list_jobs(30)

    @app.post("/api/jobs")
    async def start_job(body: JobIn):
        return await run_in_threadpool(svc.jobs.submit, body.kind, body.prompt, body.project, body.title)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: int):
        return svc.jobs.cancel(job_id)

    @app.post("/api/tool/{name}")
    async def run_tool(name: str, request: Request):
        """Direct actions from dashboard buttons. A click is deliberate, so no spoken confirmation."""
        if name not in tools.tools:
            raise HTTPException(404)
        args = await request.json() if int(request.headers.get("content-length") or 0) else {}
        # The HUD asks its own "are you sure?" before risky buttons, so a click counts as the yes.
        result = await run_in_threadpool(lambda: tools.run(name, args, confirmed_by="hud", context={"source": "hud"}))
        svc.bus.publish("tool", {"name": name, "args": args, "result": result})
        if name.startswith("obs_"):
            svc.bus.publish("obs", await run_in_threadpool(svc.obs.status), sticky=True)
        return result

    @app.post("/api/twitch/connect")
    async def twitch_connect():
        return await run_in_threadpool(runtime.twitch_connect)

    @app.post("/api/twitch/cancel")
    async def twitch_cancel():
        svc.twitch.auth.cancel_login()
        runtime.bus.publish("twitch_auth", svc.twitch.auth.status(), sticky=True)
        return {"ok": True}

    @app.post("/api/twitch/logout")
    async def twitch_logout():
        return await run_in_threadpool(runtime.twitch_logout)

    @app.get("/api/highlights")
    async def highlights():
        return {"highlights": list(runtime.highlights.recent)}

    @app.get("/api/health")
    async def health():
        return runtime.supervisor.snapshot()

    @app.get("/api/discovery")
    async def get_discovery():
        return await run_in_threadpool(runtime.discovery_report) or {}

    @app.post("/api/discovery/run")
    async def run_discovery():
        return await run_in_threadpool(runtime.rescan)

    @app.post("/api/doctor")
    async def doctor():
        from .doctor import FAIL, PASS, WARN, run_doctor

        def run() -> dict:
            # Live services are reused; the mic is owned by the listener, so its state stands in for a mic test.
            result = run_doctor(runtime.cfg, svc, test_mic=False, load_model=False, check_ports=False)
            if runtime.listener:
                st = runtime.listener.status()
                bad = st["state"] in {"error", "unavailable"}
                result["checks"].append({"name": "Voice listener", "status": FAIL if bad else PASS if st["state"] != "loading" else WARN,
                                         "detail": st.get("error") or st["state"], "fix": "Run `python -m assistant --doctor` for a full mic + model test." if bad else ""})
            return result

        result = await run_in_threadpool(run)
        svc.bus.publish("doctor", result, sticky=True)
        return result

    @app.post("/api/voice/arm")
    async def voice_arm():
        if runtime.listener:
            runtime.listener.arm(source="button")  # a click is as deliberate as the hotkey
        return {"ok": bool(runtime.listener)}

    @app.get("/api/voice")
    async def voice_status():
        return runtime.voice_status()

    @app.post("/api/voice/calibrate")
    async def voice_calibrate():
        return await run_in_threadpool(runtime.start_calibration)

    @app.post("/api/voice/calibrate/cancel")
    async def voice_calibrate_cancel():
        return runtime.cancel_calibration()

    @app.post("/api/voice/speaker_check")
    async def voice_speaker_check(request: Request):
        body = await request.json()
        return runtime.set_speaker_check(str(body.get("mode", "")))

    @app.get("/api/brain")
    async def brain_status():
        return await run_in_threadpool(runtime.assistant.brain_status)

    @app.post("/api/brain")
    async def brain_set(request: Request):
        """{"provider": "auto"|"claude"|"local", "model": "llama3.1:8b"}"""
        body = await request.json()
        return await run_in_threadpool(lambda: runtime.set_brain(body.get("provider"), body.get("model")))

    @app.post("/api/brain/test")
    async def brain_test():
        return await run_in_threadpool(runtime.test_brain)

    @app.post("/api/goals")
    async def set_goals(body: dict):
        """{"goals": ["Streaming: …", "Work: …"], "this_week": ["…"]}; either may be left out."""
        return runtime.set_goals(body.get("goals"), body.get("this_week"))

    @app.get("/api/library")
    async def library_status():
        return await run_in_threadpool(runtime.library_status)

    @app.get("/api/library/suggestions")
    async def library_suggestions():
        """Folders worth adding: pinned in File Explorer, Downloads, Google shared drives (cached ten minutes)."""
        return await run_in_threadpool(runtime.library_suggestions)

    @app.post("/api/library/search")
    async def library_search(body: dict):
        """{"query": "vendor support hours", "limit": 8}"""
        return await run_in_threadpool(runtime.search_library, str(body.get("query") or ""), int(body.get("limit") or 8))

    @app.post("/api/library/index")
    async def library_index():
        """Read new and changed documents now, in the background; progress arrives as "library" events."""
        return runtime.index_library()

    @app.post("/api/library/folders")
    async def library_folders(body: dict):
        """{"folders": ["C:\\Users\\you\\Documents", …]}; [] = the usual places."""
        return await run_in_threadpool(runtime.set_library_folders, body.get("folders"))

    @app.post("/api/library/scope")
    async def library_scope(body: dict):
        """{"whole_pc": true}: every drive in this PC too, ranked after your folders; false: your folders only."""
        return await run_in_threadpool(runtime.set_library_scope, bool(body.get("whole_pc")))

    @app.post("/api/library/open")
    async def library_open(body: dict):
        """{"path": "<a path from a search result>"}"""
        return await run_in_threadpool(runtime.open_document, str(body.get("path") or ""))

    @app.get("/api/google")
    async def google_status():
        return await run_in_threadpool(runtime.google_status)

    @app.post("/api/google/client")
    async def google_client(body: dict):
        """{"client_id": "….apps.googleusercontent.com", "client_secret": "GOCSPX-…"}: saved to .env."""
        return await run_in_threadpool(runtime.set_google_client, str(body.get("client_id") or ""),
                                       str(body.get("client_secret") or ""))

    @app.post("/api/google/connect")
    async def google_connect(request: Request, body: dict | None = None):
        """Opens Google's sign-in page in the browser for one more account."""
        return await run_in_threadpool(runtime.google_connect, str(request.base_url),
                                       str((body or {}).get("login_hint") or ""))

    @app.get("/api/google/callback", response_class=HTMLResponse)
    async def google_callback(state: str = "", code: str = "", error: str = ""):
        ok, said = await run_in_threadpool(runtime.google_finish, state, code, error)
        title = f"Connected {said}" if ok else "Google sign-in didn't finish"
        line = ("Vesper can read this account's Google Docs and calendars now. You can close this tab."
                if ok else f"{said} Go back to Vesper → Setup → Google accounts and try again.")
        page = (f"<!doctype html><meta charset=utf-8><title>{escape(title)}</title><body style='font:16px system-ui;"
                f"background:#111;color:#eee;padding:48px'><h2>{escape(title)}</h2><p>{escape(line)}</p></body>")
        return HTMLResponse(page, status_code=200 if ok else 400)

    @app.post("/api/google/remove")
    async def google_remove(body: dict):
        return await run_in_threadpool(runtime.google_remove, str(body.get("email") or ""))

    @app.get("/api/google/calendars")
    async def google_calendars():
        return await run_in_threadpool(runtime.google_calendar_choices)

    @app.post("/api/google/calendars")
    async def google_set_calendars(body: dict):
        """{"calendars": [{"account", "id", "name", "profile"}], "drop_ical": true}"""
        return await run_in_threadpool(runtime.set_google_calendars, body.get("calendars") or [],
                                       bool(body.get("drop_ical")))

    @app.post("/api/brain/bench")
    async def brain_bench():
        """Test every local model in the background; progress arrives as "brain_bench" events."""
        return runtime.bench_brain()

    @app.get("/api/tts")
    async def tts_status():
        return runtime.speaker.status()

    @app.post("/api/tts")
    async def tts_set(request: Request):
        """{"engine": "supertonic"|"kokoro"|"pyttsx3"|"browser"|"none"|"auto", "voice": "m1", "speed": 1.0}"""
        body = await request.json()
        return await run_in_threadpool(lambda: runtime.set_voice(body.get("engine"), body.get("voice"), body.get("speed")))

    @app.post("/api/tts/preview")
    async def tts_preview(request: Request):
        body = await request.json() if int(request.headers.get("content-length") or 0) else {}
        return runtime.preview_voice((body.get("text") or "")[:300] or None)

    @app.delete("/api/voice/profile")
    async def voice_profile_delete():
        return runtime.delete_voice_profile()

    @app.post("/api/wakewords")
    async def wakeword_upload(request: Request, name: str, threshold: float | None = None):
        """Raw .onnx bytes in the body (no multipart needed); ?name=vesper|stop|clip_that…&threshold=0.68"""
        data = await request.body()
        return await run_in_threadpool(lambda: runtime.install_wake_model(name, data, threshold))

    @app.delete("/api/wakewords/{name}")
    async def wakeword_delete(name: str):
        return await run_in_threadpool(lambda: runtime.delete_wake_model(name))

    @app.get("/api/macros")
    async def list_macros():
        return runtime.voice_status()["macros"]

    @app.post("/api/macros/{name}/run")
    async def run_macro(name: str):
        """A click is deliberate, so risky macros run without the spoken yes (the HUD asks first)."""
        if name.lower() not in assistant.macros:
            raise HTTPException(404)
        out = await run_in_threadpool(lambda: assistant.run_macro(name, confirmed=True))
        svc.bus.publish("assistant_said", {"text": out["reply"], "kind": "macro", "source": "dashboard"})
        return out

    @app.post("/api/voice/mute")
    async def voice_mute(request: Request):
        body = await request.json()
        if runtime.listener:
            runtime.listener.set_muted(bool(body.get("muted")))
        return {"ok": bool(runtime.listener)}

    @app.post("/api/voice/speak_done")
    async def speak_done():
        runtime.speaker.browser_finished()
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        supplied = socket.query_params.get("token", "")
        if not host_ok(socket.headers.get("host")) or not hmac.compare_digest(supplied, token):
            await socket.close(code=4401)
            return
        await socket.accept()
        queue = runtime.bus.subscribe()
        try:
            while True:
                payload = await queue.get()
                await socket.send_text(payload)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            runtime.bus.unsubscribe(queue)

    return app
