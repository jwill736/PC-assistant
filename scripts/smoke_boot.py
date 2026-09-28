"""Boot the assistant the way start.bat does and check it actually comes up.

Run from the repo root after setup.bat: ``.venv\\Scripts\\python scripts\\smoke_boot.py``.
CI runs it on a fresh Windows machine with no microphone, speakers, OBS or API
keys, which is exactly the first launch on a new PC: everything that can't
connect must degrade to a message on the HUD, never take the app down.

Fails (exit 1) when the HUD never answers, an API call errors, the process
dies, or the log holds a crash; prints what each part ended up doing either way.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
# Log lines that are expected on a bare machine, not crashes.
EXPECTED = ("speech model load failed",)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--boot-timeout", type=float, default=180, help="seconds for the HUD to answer")
    ap.add_argument("--settle", type=float, default=240, help="seconds for voice to finish loading")
    args = ap.parse_args()

    data = ROOT / "data"
    log_file = data / "logs" / "assistant.log"
    log_start = log_file.stat().st_size if log_file.exists() else 0
    base = f"http://127.0.0.1:{args.port}"
    out = open(ROOT / "smoke_boot.out", "w", encoding="utf-8")  # noqa: SIM115 - closed below
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    t0 = time.time()
    proc = subprocess.Popen([sys.executable, "-m", "assistant", "--no-window", "--port", str(args.port)],
                            cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, env=env)
    problems: list[str] = []
    try:
        html = wait_for_hud(base, proc, args.boot_timeout)
        if html is None:
            problems.append("the HUD never answered" if proc.poll() is None else f"the app exited with code {proc.returncode}")
            return report(problems, proc, log_file, log_start)
        print(f"HUD up after {time.time() - t0:.0f}s")
        token = (data / "api_token").read_text().strip()
        if token not in html:
            problems.append("the HUD page doesn't carry the API token")
        client = httpx.Client(base_url=base, headers={"x-assistant-token": token}, timeout=30)

        for path in ("/api/state", "/api/health", "/api/voice", "/api/control", "/api/tasks", "/api/jobs"):
            r = client.get(path)
            print(f"GET {path}: {r.status_code}")
            if r.status_code != 200:
                problems.append(f"GET {path} returned {r.status_code}: {r.text[:300]}")
        r = client.get("/api/health", headers={"x-assistant-token": "wrong"})
        if r.status_code != 401:
            problems.append(f"a bad token got {r.status_code}, not 401")

        # Answered locally by the router: no Claude key needed.
        for text in ("what time is it", "what's my volume"):
            r = client.post("/api/command", json={"text": text})
            body = r.json() if r.status_code == 200 else {}
            print(f"say {text!r}: {r.status_code} {json.dumps(body)[:200]}")
            if r.status_code != 200:
                problems.append(f"command {text!r} returned {r.status_code}")

        state = settle(client, args.settle)
        print("\nWhat each part is doing:")
        for part in state.get("health", []):
            err = f" ({part['last_error'][:120]})" if part.get("last_error") else ""
            print(f"  {part['name']:<20} {part['state']:<10} runs={part['runs']} errors={part['errors']} restarts={part['restarts']}{err}")
        voice = state.get("voice") or {}
        print(f"  voice listener       {voice.get('state')}: {voice.get('engine') or '-'} / {voice.get('vad') or '-'} {voice.get('error') or ''}")
        a = state.get("assistant") or {}
        print(f"  speech output        {a.get('tts')}")
        print(f"  claude               {'ready' if a.get('claude') else 'no API key (expected on a bare machine)'}")
        if proc.poll() is not None:
            problems.append(f"the app exited with code {proc.returncode} after booting")
    except Exception as exc:  # the smoke test itself must report, not crash
        problems.append(f"{type(exc).__name__}: {exc}")
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        out.close()
    return report(problems, proc, log_file, log_start)


def wait_for_hud(base: str, proc: subprocess.Popen, timeout: float) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        try:
            r = httpx.get(base + "/", timeout=3)
            if r.status_code == 200 and "HUD</title>" in r.text:
                return r.text
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return None


def settle(client: httpx.Client, timeout: float) -> dict:
    """Wait for the first-run model downloads to finish, then return the state."""
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        state = client.get("/api/state").json()
        voice = (state.get("voice") or {}).get("state")
        if voice not in ("starting", "loading", None):
            break
        time.sleep(3)
    time.sleep(5)  # a few poller passes
    return client.get("/api/state").json()


def report(problems: list[str], proc: subprocess.Popen, log_file: Path, log_start: int) -> int:
    crashes = []
    if log_file.exists():
        with open(log_file, encoding="utf-8", errors="replace") as f:
            f.seek(log_start)
            lines = f.read().splitlines()
        for i, line in enumerate(lines):
            if "Traceback" in line:
                context = lines[max(0, i - 1):i + 25]
                if not any(e in context[0] for e in EXPECTED):
                    crashes.append("\n".join(context))
    for c in crashes:
        print("\n--- traceback in the log ---\n" + c)
    if crashes:
        problems.append(f"{len(crashes)} traceback(s) in the log")
    console = ROOT / "smoke_boot.out"
    if problems and console.exists():
        print("\n--- console output ---\n" + console.read_text(encoding="utf-8", errors="replace")[-6000:])
    print()
    for p in problems:
        print(f"FAIL: {p}")
    print("FAILED" if problems else "OK: the app boots and serves the HUD")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
