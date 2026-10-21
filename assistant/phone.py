"""The HUD on J's phone over home Wi-Fi, behind a 6-digit PIN (Setup → Phone; off until he turns it on).

When it's on, a second listener answers on this PC's home-network address (port 8766 by default). Everything that
arrives there is marked by ``LanGate`` (an ASGI wrapper, so no header can fake it) and checked by ``lan_guard``:

- only phones on the home network (a private address) and only this PC's own address or name in the Host header;
- no session yet: every page goes to /pin. Six digits right gets a session cookie signed with the install token
  (HMAC, 30 days, HttpOnly, SameSite=Strict); five wrong in a row locks that phone out for 15 minutes, and 20 wrong
  across the network in an hour locks everyone out for an hour (J gets a card either way);
- with a session: the same HUD, with a per-session token instead of the install token in the page. Every /api call
  must send that token in its header as well as the cookie, so another site open on the phone can't drive the PC;
- a new PIN, or "Sign out all phones", ends every session at once.

The loopback listener (127.0.0.1:8765) is untouched: it still wants the install token, and never takes a cookie.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

LAN_SCOPE = "vesper.lan"          # set on every request that came in on the phone listener
COOKIE = "vesper_phone"
DEFAULT_PORT = 8766
SESSION_S = 30 * 86400
MAX_FAILS = 5                     # wrong PINs from one phone before it's locked out
LOCK_S = 15 * 60
NET_FAILS = 20                    # wrong PINs from the whole network before everyone is locked out
NET_LOCK_S = 3600
MIN_GAP_S = 1.0                   # one try a second, per phone
OPEN_PATHS = {"/pin", "/pin/login", "/pin/logout", "/favicon.ico"}
# Managed from the PC only: closing Vesper, the phone PIN itself, the OBS link and opening windows on the PC.
PC_ONLY = ("/api/quit", "/api/hud/remote", "/api/hud/overlay", "/api/hud/mini", "/overlay")


def _private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    return a.is_private and not a.is_loopback and not a.is_link_local


def lan_addresses() -> list[str]:
    """This PC's home-network IPv4 addresses, the one the default route uses first. Nothing is sent: a UDP
    "connect" only picks the interface."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.254.254.254", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return [ip for ip in dict.fromkeys(found) if _private(ip)]


def split_host(host: str | None) -> tuple[str, int | None]:
    """"192.168.1.5:8766" -> ("192.168.1.5", 8766); "[fe80::1]:80" -> ("fe80::1", 80)."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        name, _, rest = host[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        name, _, port = host.partition(":")
    return name, int(port) if port.isdigit() else None


class PhoneAccess:
    def __init__(self, cfg: Any, bus: Any, token: str, *, clock: Callable[[], float] = time.time,
                 addresses: Callable[[], list[str]] = lan_addresses, hostname: Callable[[], str] = socket.gethostname):
        self.cfg, self.bus, self.token, self.clock = cfg, bus, token, clock
        self._addresses, self._hostname = addresses, hostname
        self.path = Path(cfg.data_dir) / "phone_access.json"
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._last_try: dict[str, float] = {}
        self._net: deque[float] = deque()
        self._net_until = 0.0
        self._cache: tuple[float, list[str], set[str]] = (-1e9, [], set())
        self.server: Any = None
        self.task: asyncio.Task | None = None
        self.error: str | None = None

    # ---- settings (data/settings.yaml hud.remote) and the PIN file ---------------------------------------
    @property
    def conf(self) -> dict:
        return ((self.cfg.get("hud") or {}).get("remote") or {})

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", False))

    @property
    def port(self) -> int:
        try:
            return int(self.conf.get("port") or DEFAULT_PORT)
        except (TypeError, ValueError):
            return DEFAULT_PORT

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if re.fullmatch(r"\d{6}", str(data.get("pin", ""))):
                return {"pin": str(data["pin"]), "epoch": int(data.get("epoch") or 0)}
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)

    def _state(self) -> dict:
        data = self._read()
        if not data:
            data = {"pin": f"{secrets.randbelow(10 ** 6):06d}", "epoch": 0}
            self._write(data)
        return data

    @property
    def pin(self) -> str:
        return self._state()["pin"]

    def new_pin(self) -> str:
        """A fresh PIN; every phone has to type it again."""
        data = self._state()
        data = {"pin": f"{secrets.randbelow(10 ** 6):06d}", "epoch": data["epoch"] + 1}
        self._write(data)
        return data["pin"]

    def sign_out_all(self) -> None:
        data = self._state()
        self._write({**data, "epoch": data["epoch"] + 1})

    # ---- who may reach the phone listener ----------------------------------------------------------------
    def _names(self) -> tuple[list[str], set[str]]:
        at, ips, names = self._cache
        if self.clock() - at > 60:
            ips = list(self._addresses())
            try:
                host = (self._hostname() or "").lower().strip(".")
            except OSError:
                host = ""
            names = {n for n in (host, f"{host}.local", f"{host}.lan", f"{host}.home") if host} | set(ips)
            self._cache = (self.clock(), ips, names)
        return ips, names

    def addresses(self) -> list[str]:
        return self._names()[0]

    def host_ok(self, host: str | None) -> bool:
        """Only this PC's own home-network address or name, on the phone port: a web page that points some other
        name at this PC (DNS rebinding) is turned away."""
        name, port = split_host(host)
        return name in self._names()[1] and port in (None, self.port)

    @staticmethod
    def client_ok(ip: str | None) -> bool:
        return _private(ip or "")

    def urls(self) -> list[str]:
        ips, names = self._names()
        host = next((n for n in names if n not in ips and n.endswith(".local")), None)
        return [f"http://{ip}:{self.port}/" for ip in ips] + ([f"http://{host}:{self.port}/"] if host else [])

    # ---- sessions -----------------------------------------------------------------------------------------
    def _sign(self, msg: str) -> str:
        return hmac.new(self.token.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        sid, exp, epoch = secrets.token_urlsafe(18), int(self.clock() + SESSION_S), self._state()["epoch"]
        return f"{sid}.{exp}." + self._sign(f"v1|{sid}|{exp}|{epoch}")

    def session(self, cookie: str | None) -> str | None:
        """The session id when the cookie is ours, unexpired and from before the last new PIN; else None."""
        parts = (cookie or "").split(".")
        if len(parts) != 3 or not parts[1].isdigit():
            return None
        sid, exp, sig = parts
        if int(exp) < self.clock():
            return None
        good = self._sign(f"v1|{sid}|{exp}|{self._state()['epoch']}")
        return sid if hmac.compare_digest(sig, good) else None

    def csrf(self, sid: str) -> str:
        """The token the phone's page sends in its header: tied to its session, never the install token."""
        return self._sign(f"csrf|{sid}")[:40]

    # ---- the PIN, with a lockout ---------------------------------------------------------------------------
    def attempt(self, ip: str, pin: str) -> dict:
        now = self.clock()
        with self._lock:
            if now < self._net_until:
                wait = self._net_until - now
                return {"ok": False, "status": 429, "retry_after": round(wait),
                        "error": f"Too many wrong PINs on this network. Try again in {_mins(wait)}."}
            until = self._locked.get(ip, 0)
            if now < until:
                return {"ok": False, "status": 429, "retry_after": round(until - now),
                        "error": f"Locked after {MAX_FAILS} wrong PINs. Try again in {_mins(until - now)}."}
            if now - self._last_try.get(ip, -1e9) < MIN_GAP_S:
                return {"ok": False, "status": 429, "retry_after": 1, "error": "One try a second."}
            self._last_try[ip] = now
            pin = re.sub(r"\D", "", str(pin or ""))
            if len(pin) == 6 and hmac.compare_digest(pin, self.pin):
                self._fails.pop(ip, None)
                return {"ok": True, "cookie": self.issue()}
            fails = [t for t in self._fails.get(ip, []) if now - t < LOCK_S] + [now]
            self._fails[ip] = fails
            self._net.append(now)
            while self._net and now - self._net[0] > NET_LOCK_S:
                self._net.popleft()
            if len(self._net) >= NET_FAILS:
                self._net_until, self._net = now + NET_LOCK_S, deque()
                self._warn(f"{NET_FAILS} wrong phone PINs on your network in an hour: phone sign-in is locked for "
                           "an hour. Setup → Phone → New PIN if you didn't do this.")
            if len(fails) >= MAX_FAILS:
                self._locked[ip] = now + LOCK_S
                self._fails.pop(ip, None)
                self._warn(f"{MAX_FAILS} wrong phone PINs from {ip}: that device is locked out for 15 minutes.")
                return {"ok": False, "status": 429, "retry_after": LOCK_S,
                        "error": f"Locked after {MAX_FAILS} wrong PINs. Try again in 15 minutes."}
            left = MAX_FAILS - len(fails)
            return {"ok": False, "status": 401, "left": left,
                    "error": f"Wrong PIN. {left} more {'try' if left == 1 else 'tries'} before a 15-minute lockout."}

    def _warn(self, text: str) -> None:
        try:
            self.bus.publish("notice", {"source": "phone", "text": text, "level": "warning", "ts": self.clock(),
                                        "key": f"phone:{text[:40]}:{int(self.clock() // 60)}"})
        except Exception:  # a card is a courtesy; the lockout already holds
            log.debug("phone warning not published", exc_info=True)

    # ---- the second listener -------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self.server is not None and getattr(self.server, "started", False))

    def status(self) -> dict:
        return {"enabled": self.enabled, "running": self.running, "port": self.port, "urls": self.urls() if self.enabled else [],
                "addresses": self.addresses(), "pin": self.pin, "error": self.error,
                "session_days": SESSION_S // 86400, "max_fails": MAX_FAILS, "lock_minutes": LOCK_S // 60}

    async def start(self, app: Any) -> None:
        """Listen on the home-network address too (same app, same event loop, so live updates reach the phone)."""
        if self.task is not None and not self.task.done():
            return
        ips = self.addresses()
        bind = self.conf.get("bind") or (ips[0] if ips else None)
        if not bind:
            self.error = "This PC isn't on a home network (no 192.168.x.x or 10.x.x.x address)."
            return
        import uvicorn

        class Quiet(uvicorn.Server):  # Ctrl+C and the tray's Quit belong to the main listener
            @contextlib.contextmanager
            def capture_signals(self):
                yield

        config = uvicorn.Config(LanGate(app), host=bind, port=self.port, lifespan="off", log_config=None,
                                log_level="warning", proxy_headers=False, timeout_graceful_shutdown=2)
        self.server, self.error = Quiet(config), None
        self.task = asyncio.create_task(self._serve(self.server))

    async def _serve(self, server: Any) -> None:
        try:
            await server.serve()
            if not server.started and not server.should_exit:
                self.error = f"Port {self.port} is busy: another program is using it (hud.remote.port in config.yaml)."
        except (OSError, SystemExit) as exc:  # uvicorn exits when it can't bind
            self.error = f"Couldn't listen on port {self.port}: {exc}"[:200]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("phone listener stopped")
            self.error = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            if self.server is server:
                self.server = None

    async def stop(self) -> None:
        server, task = self.server, self.task
        if server is not None:
            server.should_exit = True
        if task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, 5)
        self.server, self.task = None, None


def _mins(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"{m} minute{'s' if m != 1 else ''}"


class LanGate:
    """Marks every request from the phone listener, so the shared app knows where it came from."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, LAN_SCOPE: True}
        await self.app(scope, receive, send)


def is_lan(scope: dict) -> bool:
    return bool(scope.get(LAN_SCOPE))


async def lan_guard(phone: PhoneAccess, request: Any, call_next: Callable) -> Any:
    """The phone listener's front door (see the module doc)."""
    from fastapi.responses import JSONResponse, RedirectResponse

    path = request.url.path
    if not phone.enabled:
        return JSONResponse({"error": "phone access is off"}, status_code=403)
    if not phone.client_ok(request.client.host if request.client else None):
        return JSONResponse({"error": "only phones on your home network"}, status_code=403)
    if not phone.host_ok(request.headers.get("host")):
        return JSONResponse({"error": "bad host"}, status_code=403)
    if any(path == p or path.startswith(p + "/") for p in PC_ONLY):
        return JSONResponse({"error": "only from the PC itself"}, status_code=403)
    if path.startswith("/static/") or path in OPEN_PATHS:
        return await call_next(request)
    sid = phone.session(request.cookies.get(COOKIE))
    if not sid:
        if path.startswith("/api"):
            return JSONResponse({"error": "sign in with the PIN"}, status_code=401)
        return RedirectResponse("/pin", status_code=303)
    if path.startswith("/api") and not hmac.compare_digest(request.headers.get("x-assistant-token", ""), phone.csrf(sid)):
        return JSONResponse({"error": "missing or bad token"}, status_code=401)
    request.state.phone_sid = sid
    return await call_next(request)


def lan_socket_ok(phone: PhoneAccess, socket_: Any) -> bool:
    """A phone's live-update socket: signed in, its session token, and opened by the HUD page itself."""
    if not phone.enabled or not phone.client_ok(socket_.client.host if socket_.client else None):
        return False
    host = socket_.headers.get("host")
    if not phone.host_ok(host):
        return False
    origin = socket_.headers.get("origin")
    if origin and origin.rstrip("/").lower() != f"http://{(host or '').lower()}":
        return False
    sid = phone.session(socket_.cookies.get(COOKIE))
    return bool(sid) and hmac.compare_digest(socket_.query_params.get("token", ""), phone.csrf(sid))
