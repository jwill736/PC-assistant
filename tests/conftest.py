import functools
import inspect
import ipaddress
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from types import SimpleNamespace

import httpx
import psutil
import pytest

from assistant import library as _library
from assistant import resources as _resources
from assistant.integrations import nvml as _nvml
from assistant.bus import EventBus
from assistant.config import DEFAULTS, Config, deep_merge
from assistant.integrations.activity import ActivityTracker
from assistant.services import build_services
from assistant.skills.mcp import MCP
from assistant.skills.phone import Phone
from assistant.storage import Storage
from assistant.voice.confirm_window import ConfirmWindow
from assistant.voice.tts import Speaker
from assistant.watchdog import Supervisor

# ---- guards: a test never starts a real app, opens a real file, reaches the network or leaves pollers running ------
#
# Each blocked attempt raises BlockedInTests (a PermissionError, so the code under test sees an ordinary failure), and
# the test then fails at teardown naming what was tried and from which thread, even if the code swallowed the error.
#
# - Programs: subprocess.Popen and everything built on it (run, call, check_output, asyncio's subprocesses). Allowed
#   without a mark, because they are only asked things: git (never on this checkout or a folder holding it), uname,
#   nvidia-smi, ffmpeg/ffprobe, ldconfig/objdump (ctypes.util), xprintidle, xdotool get..., wmctrl -l..., powercfg's
#   queries (/getactivescheme, /list, /query) and the shell's `ver` (the platform module). Anything else (notepad,
#   explorer, cmd /c start, xdg-open, a browser, python, node, claude, even a program that doesn't exist) needs
#   @pytest.mark.real_subprocess on the test: for tests that start their own children on purpose and end them.
# - Opening things: os.startfile, webbrowser (open, open_new, open_new_tab, get().open, and a reference to
#   webbrowser.open taken before this ran) and shell32's ShellExecute*. No mark lifts this: give the code a fake opener.
# - The network: DNS lookups of other hosts, TCP connects and UDP sends to anything but this PC (loopback, the
#   unspecified address, this PC's own addresses and name), and every httpx or httpx2 (the anthropic SDK's) request
#   through a real transport to another host, sync or async (MockTransport and TestClient are untouched). Proxy
#   variables are unset during a test, so nothing tunnels out through a local proxy. @pytest.mark.real_network lifts
#   it (the opt-in model downloads). The cfg fixture also unsets the developer's own service keys (GITHUB_TOKEN...),
#   and svc's weather and travel skills get an offline client.
# - Threads: the Supervisors (their pollers), Speakers, ConfirmWindows, Phone loops, MCP loops, activity trackers and
#   libraries a test makes are stopped when it ends, so nothing it started runs on into the next test. (So make them
#   in function-scoped fixtures.) A blocked call from a thread an earlier test left running says so.
# - The PC's own File Explorer pins (library.quick_access, read with powershell) are always empty.
# - The graphics card's driver is never read: NVML (integrations/nvml.py) acts as if absent, so a GPU reading
#   takes the nvidia-smi path tests already fake, on a PC with an NVIDIA card as on CI.
#
# tests/test_guards.py tests these.

REPO = Path(__file__).resolve().parent.parent
_GUARD = {"test": None, "processes": False, "network": False}
_BLOCKED: list[str] = []
_STARTED: list[tuple[object, object]] = []  # (object, how to stop it) for what the running test made


class BlockedInTests(PermissionError):
    pass


def _block(what: str) -> BlockedInTests:
    t = threading.current_thread()
    where = "" if t is threading.main_thread() else f" (from thread {t.name!r})"
    if _GUARD["test"] is None:
        where += " between tests: a thread an earlier test left running"
    _BLOCKED.append(what + where)
    return BlockedInTests(f"blocked in tests: {what}{where}. See the guards in tests/conftest.py.")


# programs ------------------------------------------------------------------------------------------------------------

_POPEN = inspect.signature(subprocess.Popen.__init__)
_real_popen_init = subprocess.Popen.__init__
_QUIET = {"uname", "nvidia-smi", "ffmpeg", "ffprobe", "xprintidle", "ldconfig", "objdump"}
_POWERCFG_QUERIES = {"/getactivescheme", "/list", "/l", "/query", "/q"}


def _on_this_checkout(folder) -> bool:
    d = Path(folder or os.getcwd()).resolve()
    return d == REPO or REPO in d.parents or d in REPO.parents


def _refused(args, kw: dict) -> str | None:
    """Why this program may not start in a test, or None."""
    shell = bool(kw.get("shell"))
    if isinstance(args, (str, bytes, os.PathLike)):
        line = os.fsdecode(args)
        if shell and line.strip().lower() == "ver":
            return None
        try:
            parts = shlex.split(line, posix=sys.platform != "win32")
        except ValueError:
            parts = line.split()
    else:
        parts = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in args]
    if not parts:
        return None
    if shell:
        return f"a shell command line {parts!r}"
    name = Path(os.fsdecode(kw.get("executable") or parts[0])).name.lower()
    for ext in (".exe", ".com", ".cmd", ".bat"):
        name = name.removesuffix(ext)
    rest = parts[1:]
    if name == "git":
        folder = kw.get("cwd")
        for i, a in enumerate(rest[:-1]):
            if a == "-C":
                folder = rest[i + 1]
        if _on_this_checkout(folder):
            return f"git on this checkout {parts!r}: point the test at a repo in tmp_path"
        return None
    if name in _QUIET:
        return None
    if name == "powercfg" and rest[:1] and rest[0].lower() in _POWERCFG_QUERIES:
        return None
    if name == "xdotool" and rest[:1] and rest[0].startswith("get"):
        return None
    if name == "wmctrl" and rest[:1] and rest[0].startswith("-l"):
        return None
    if name == "cmd" and [a.lower() for a in rest] == ["/c", "ver"]:
        return None
    return f"starting {parts!r} (mark the test real_subprocess if it starts its own child on purpose)"


@functools.wraps(_real_popen_init)
def _guarded_popen_init(self, *args, **kwargs):
    if not _GUARD["processes"]:
        bound = _POPEN.bind(self, *args, **kwargs).arguments
        why = _refused(bound["args"], bound)
        if why:
            raise _block(why)
    _real_popen_init(self, *args, **kwargs)


subprocess.Popen.__init__ = _guarded_popen_init  # the class itself stays (mcp subscripts subprocess.Popen[...])

# opening things ------------------------------------------------------------------------------------------------------

if hasattr(os, "startfile"):  # Windows
    def _startfile(path, *a, **k):
        raise _block(f"os.startfile({path!r}) would open it on this PC")

    os.startfile = _startfile


def _browser_open(url, new=0, autoraise=True):
    raise _block(f"webbrowser.open({url!r}) would open a real browser")


class _NoBrowser(webbrowser.BaseBrowser):
    def open(self, url, new=0, autoraise=True):
        return _browser_open(url, new, autoraise)


webbrowser.open = _browser_open
webbrowser.open_new = lambda url: _browser_open(url, 1)
webbrowser.open_new_tab = lambda url: _browser_open(url, 2)
webbrowser.get = lambda using=None: _NoBrowser("vesper-tests")
webbrowser._tryorder = ["vesper-tests"]  # a reference to the real webbrowser.open still ends at get() above

if sys.platform == "win32":  # pragma: no cover - Windows only
    try:
        import ctypes

        def _shell_execute(*a, **k):
            raise _block(f"ShellExecute{a[1:3]!r} would open it on this PC")

        for _name in ("ShellExecuteW", "ShellExecuteA", "ShellExecuteExW", "ShellExecuteExA"):
            setattr(ctypes.windll.shell32, _name, _shell_execute)
    except (ImportError, OSError, AttributeError):
        pass

# the network ---------------------------------------------------------------------------------------------------------


@functools.cache
def _own_addresses() -> frozenset[str]:
    try:
        return frozenset(a.address.split("%")[0].lower() for addrs in psutil.net_if_addrs().values() for a in addrs)
    except OSError:
        return frozenset()


def _this_pc(host) -> bool:
    if host is None:
        return True
    h = (host.decode() if isinstance(host, bytes) else str(host)).strip("[]").split("%")[0].lower()
    if h in ("", "localhost") or h.endswith(".localhost") or h == socket.gethostname().lower():
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified or h in _own_addresses()


_real_getaddrinfo = socket.getaddrinfo
_real_connect, _real_connect_ex, _real_sendto = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto


def _getaddrinfo(host, *a, **k):
    if not _GUARD["network"] and not _this_pc(host):
        raise _block(f"looking up {host!r} on the network")
    return _real_getaddrinfo(host, *a, **k)


def _check_connect(sock, address) -> None:
    if (not _GUARD["network"] and sock.family in (socket.AF_INET, socket.AF_INET6)
            and sock.type != socket.SOCK_DGRAM and not _this_pc(address[0])):  # a UDP connect sends nothing
        raise _block(f"connecting to {address[0]}:{address[1]}")


def _connect(self, address):
    _check_connect(self, address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _check_connect(self, address)
    return _real_connect_ex(self, address)


def _sendto(self, data, *args):
    address = args[-1]
    if (not _GUARD["network"] and self.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple)
            and not _this_pc(address[0])):
        raise _block(f"sending to {address[0]}:{address[1]}")
    return _real_sendto(self, data, *args)


socket.getaddrinfo = _getaddrinfo
socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto = _connect, _connect_ex, _sendto

def _guard_transports(mod) -> None:
    """httpx (and httpx2, the anthropic SDK's own copy): checked where a request leaves, so a client made with a
    proxy before the test unset it can't reach out either."""
    real, real_async = mod.HTTPTransport.handle_request, mod.AsyncHTTPTransport.handle_async_request

    def check(request) -> None:
        if not _GUARD["network"] and not _this_pc(request.url.host):
            raise mod.ConnectError(str(_block(f"an httpx request to {request.url}")), request=request)

    def handle(self, request):
        check(request)
        return real(self, request)

    async def handle_async(self, request):
        check(request)
        return await real_async(self, request)

    mod.HTTPTransport.handle_request, mod.AsyncHTTPTransport.handle_async_request = handle, handle_async


_guard_transports(httpx)
try:
    import httpx2
except ImportError:
    pass
else:
    _guard_transports(httpx2)
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

# threads -------------------------------------------------------------------------------------------------------------


def _track(cls, method: str, stop) -> None:
    real = getattr(cls, method)

    @functools.wraps(real)
    def tracked(self, *a, **k):
        out = real(self, *a, **k)
        _STARTED.append((self, stop))
        return out

    setattr(cls, method, tracked)


def _stop_supervisor(sup) -> None:
    sup.stop()
    for part in list(sup.parts.values()):  # a poller in the middle of a run finishes it first
        if part.thread is not None and part.thread is not threading.current_thread():
            part.thread.join(timeout=2)


def _stop_speaker(sp) -> None:
    sp.stop()
    if sp._thread is not None:
        sp._thread.join(timeout=2)
    if sp.out is not None:
        sp.out.close()  # a fake sound card's own loop ends with it


def _close_window(cw) -> None:
    with cw._lock:
        cw.current = None  # its poller returns on the next tick


def _stop_mcp(m) -> None:
    m.close()
    loop = m._loop
    if loop is not None:
        loop.call_soon_threadsafe(loop.stop)
        m._loop = None


_track(Supervisor, "__init__", _stop_supervisor)
_track(Speaker, "__init__", _stop_speaker)
_track(ConfirmWindow, "__init__", _close_window)
_track(Phone, "start", Phone.stop)
_track(MCP, "_submit", _stop_mcp)
_track(ActivityTracker, "restart", ActivityTracker.stop)
_track(_library.Library, "__init__", _library.Library.stop)
_nvml.DISABLED = True  # (and per test, below): even a thread left over from collection never reads the driver
_resources._HOOKED[0] = True  # the resource report's launch counter never wraps Popen here: the guard does


@pytest.fixture(autouse=True)
def _guards(request, monkeypatch):
    _STARTED.clear()  # objects made while collecting aren't this test's
    node = request.node
    _GUARD.update(test=node.nodeid, processes=node.get_closest_marker("real_subprocess") is not None,
                  network=node.get_closest_marker("real_network") is not None)
    if not _GUARD["network"]:
        for var in _PROXY_VARS:
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_library, "quick_access", list)  # the PC's own Explorer pins (read by powershell)
    monkeypatch.setattr(_nvml, "DISABLED", True)  # the real graphics driver: never in a test
    yield
    started = {id(o): (o, s) for o, s in _STARTED}
    for obj, stop in reversed(list(started.values())):
        try:
            stop(obj)
        except Exception:
            pass  # best effort: the test itself is over
    _STARTED.clear()
    _GUARD.update(test=None, processes=False, network=False)
    blocked, _BLOCKED[:] = list(_BLOCKED), []
    if blocked:
        pytest.fail("the test reached outside itself:\n- " + "\n- ".join(dict.fromkeys(blocked)), pytrace=False)


@pytest.fixture
def blocked_calls():
    """What the guards blocked so far in this test, emptied at its end (for tests of the guards themselves)."""
    yield _BLOCKED
    _BLOCKED.clear()


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Config:
    for name in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OBS_PASSWORD",
                 "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"):  # a developer's own keys never switch on a real service
        monkeypatch.delenv(name, raising=False)
    c = Config(deep_merge(DEFAULTS, {
        "data_dir": str(tmp_path / "data"),
        "assistant": {"name": "Friday", "wake_words": ["friday", "hey friday"], "user_name": "J", "timezone": "America/New_York"},
        "voice": {"enabled": False},
        "tracking": {"enabled": False},
        "obs": {"enabled": False},
        "claude": {"enabled": False},
        "pc_control": {"toast_confirm": False},  # no real Windows notifications from the test run
        "brain": {"local": {"enabled": False}},  # a developer's running Ollama must not change test results
        "profiles": {
            "work": {"label": "Work", "apps": ["code", "slack"], "title_keywords": ["github", "jira"],
                     "launch": {"apps": ["slack"], "urls": ["gmail"], "obs_scene": None}, "close_apps": []},
            "stream": {"label": "Stream", "apps": ["obs64"], "title_keywords": ["twitch.tv"],
                       "launch": {"apps": [], "urls": [], "obs_scene": None}, "close_apps": []},
        },
        "news": {"feeds": [{"name": "Local", "url": "http://127.0.0.1:9/none", "topic": "tech"}]},
        "skills": {"memory": {"embed_model": ""}},  # no embedding calls to a developer's Ollama
        # not the developer's own Claude Code sessions, whose folders (this checkout) would get git run in them
        "projects": {"claude_dir": str(tmp_path / "claude")},
    }))
    c.root = tmp_path
    return c


@pytest.fixture
def svc(cfg):
    s = build_services(cfg, EventBus(), Storage(":memory:"))
    nud = s.skills.get("nudges") if s.skills is not None else None
    if nud is not None:  # never the test machine's own desktop (a CI runner reports "fullscreen")
        nud.fullscreen, nud.idle = (lambda: ""), (lambda: 0)
    if s.skills is not None:  # self-tests (health check, diagnostics) never read the real clipboard or GPU
        if s.skills.get("clipboard") is not None:
            s.skills["clipboard"].board = None
        if s.skills.get("janitor") is not None:
            s.skills["janitor"].runner = lambda args: None
        if s.skills.get("weather") is not None:  # no real weather service: every call fails as it does offline
            s.skills["weather"].http = offline_http()
        if s.skills.get("travel") is not None:  # nor its forecast, place lookup and drive time
            s.skills["travel"]._http = offline_http()
    return s


def offline_http() -> httpx.Client:
    """An httpx client whose every request fails the way it does with no internet."""
    def refuse(request):
        raise httpx.ConnectError(f"offline in tests: {request.url}", request=request)
    return httpx.Client(transport=httpx.MockTransport(refuse))


# ---- fake Anthropic client -------------------------------------------------

def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(name, args, id_="toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs, default=lambda o: getattr(o, "__dict__", str(o)))))
        content, stop = self.script.pop(0)
        return SimpleNamespace(content=content, stop_reason=stop)


class FakeClient:
    def __init__(self, script):
        self.messages = FakeMessages(script)
        self.beta = SimpleNamespace(messages=self.messages)


@pytest.fixture
def fake_client():
    return FakeClient
