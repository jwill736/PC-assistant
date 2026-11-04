"""The test suite's own guards (tests/conftest.py): no test starts a real app, opens a real file, reaches the network or
leaves a poller running. Notepad once opened on the Windows CI runner, and a weather poller left behind by one test
called the real internet during later ones."""

import socket
import subprocess
import sys
import time
import webbrowser

import conftest
import httpx
import pytest

from assistant.watchdog import Supervisor


@pytest.mark.parametrize("args", [
    ["notepad.exe", "C:/data/diagnostics.md"], ["explorer", "/select,C:/data"], ["cmd", "/c", "start", "x.md"],
    ["xdg-open", "/tmp/x.md"], ["chrome", "--app=http://127.0.0.1:8765/"], [sys.executable, "-c", "pass"],
    ["claude", "-p", "do a job"], ["definitely-not-a-real-command-xyz"],
])
def test_starting_a_program_is_blocked_and_noted(args, blocked_calls):
    with pytest.raises(PermissionError, match="blocked in tests"):
        subprocess.Popen(args)
    assert len(blocked_calls) == 1 and repr(args[0]) in blocked_calls[0]


def test_a_shell_line_and_asyncio_go_through_the_same_check(blocked_calls):
    import asyncio

    with pytest.raises(PermissionError):
        subprocess.run("start notepad", shell=True)
    with pytest.raises(PermissionError):
        asyncio.run(asyncio.create_subprocess_exec("notepad.exe"))
    assert len(blocked_calls) == 2


def test_what_vesper_only_asks_things_of_still_runs(tmp_path, blocked_calls):
    assert subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], capture_output=True).returncode == 0
    assert conftest._refused(["nvidia-smi", "--query-gpu=name"], {}) is None
    assert conftest._refused(["powercfg", "/getactivescheme"], {}) is None
    assert conftest._refused("ver", {"shell": True}) is None
    assert conftest._refused(["powercfg", "/setactive", "x"], {})  # changing the power plan is not a question
    assert blocked_calls == []


def test_git_on_this_checkout_is_blocked(blocked_calls):
    with pytest.raises(PermissionError, match="git on this checkout"):
        subprocess.run(["git", "-C", str(conftest.REPO), "status", "--porcelain"], capture_output=True)
    with pytest.raises(PermissionError):
        subprocess.run(["git", "status"], cwd=conftest.REPO / "assistant", capture_output=True)
    assert len(blocked_calls) == 2


@pytest.mark.real_subprocess
def test_a_marked_test_may_start_its_own_child(blocked_calls):
    assert subprocess.run([sys.executable, "-c", "print('hi')"], capture_output=True, text=True).stdout == "hi\n"
    assert blocked_calls == []


def test_opening_a_page_or_a_file_is_blocked(blocked_calls):
    with pytest.raises(PermissionError, match="webbrowser"):
        webbrowser.open("https://example.com")
    with pytest.raises(PermissionError):
        webbrowser.get().open("file:///C:/data/report.md")
    with pytest.raises(PermissionError):
        webbrowser.open_new_tab("https://example.com")
    assert len(blocked_calls) == 3


@pytest.mark.skipif(sys.platform != "win32", reason="os.startfile and ShellExecute are Windows only")
def test_startfile_and_shell_execute_are_blocked(blocked_calls):  # pragma: no cover - Windows only
    import ctypes
    import os

    with pytest.raises(PermissionError):
        os.startfile("C:/Windows/notepad.exe")  # type: ignore[attr-defined]
    with pytest.raises(PermissionError):
        ctypes.windll.shell32.ShellExecuteW(None, "open", "notepad.exe", None, None, 1)
    assert len(blocked_calls) == 2


def test_the_internet_is_out_of_reach(blocked_calls):
    with pytest.raises(httpx.ConnectError, match="blocked in tests"):
        httpx.get("https://ipapi.co/json/")
    with pytest.raises(OSError):
        socket.getaddrinfo("ipapi.co", 443)
    with pytest.raises(OSError):
        socket.create_connection(("93.184.216.34", 80), timeout=1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(OSError):
        s.sendto(b"x", ("192.168.1.40", 9123))  # a key light on J's network
    assert len(blocked_calls) == 4


def test_this_pc_is_in_reach(blocked_calls):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    with socket.create_connection(server.getsockname(), timeout=5), server.accept()[0]:
        pass
    server.close()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.254.254.254", 1))  # how the phone bridge finds its address: nothing is sent
    faked = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    assert faked.get("https://x.com").status_code == 204  # a faked service is not the network
    assert blocked_calls == []


def test_proxy_settings_are_unset_so_nothing_tunnels_out():
    import os

    assert not any(os.environ.get(v) for v in conftest._PROXY_VARS)


def test_a_poller_a_test_started_is_stopped_when_it_ends():
    sup = Supervisor()
    ran = []
    sup.poller("weather", 0.01, lambda: ran.append(1))
    deadline = time.monotonic() + 10
    while not ran and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ran and (sup, conftest._stop_supervisor) in conftest._STARTED
    conftest._stop_supervisor(sup)  # what the guard does at teardown
    thread = sup.parts["weather"].thread
    assert not thread.is_alive()


@pytest.mark.real_subprocess
def test_a_swallowed_attempt_still_fails_the_test(tmp_path):
    """The code under test may catch the error (an app that won't start is no crash): the test fails anyway."""
    (tmp_path / "test_child.py").write_text(
        "import subprocess, threading\n\n\n"
        "def test_swallows_it():\n"
        "    try:\n"
        "        subprocess.Popen(['notepad.exe', 'report.md'])\n"
        "    except OSError:\n"
        "        pass\n\n\n"
        "def poll():\n"
        "    import httpx\n"
        "    try:\n"
        "        httpx.get('https://ipapi.co/json/')\n"
        "    except httpx.HTTPError:\n"
        "        pass  # offline: the weather waits\n\n\n"
        "def test_from_a_thread():\n"
        "    t = threading.Thread(target=poll, name='poll-skill · Weather')\n"
        "    t.start()\n"
        "    t.join()\n\n\n"
        "def test_fine():\n"
        "    pass\n", encoding="utf-8")
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "pytest.ini",
                          "--rootdir", ".", "-p", "conftest", str(tmp_path / "test_child.py")],
                         cwd=conftest.REPO, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert "3 passed, 2 errors in" in out.stdout, out.stdout + out.stderr
    assert "starting ['notepad.exe', 'report.md']" in out.stdout
    assert "an httpx request to https://ipapi.co/json/ (from thread 'poll-skill · Weather')" in out.stdout


def test_the_guards_patch_once():
    """Tests import conftest (FakeClient): it must be the module pytest loaded, not a second copy patching again."""
    assert sys.modules["conftest"] is conftest and subprocess.Popen.__init__ is conftest._guarded_popen_init
    assert not hasattr(conftest._real_popen_init, "__wrapped__")
