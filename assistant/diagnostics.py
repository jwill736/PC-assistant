"""One diagnostic report J can send: everything about this Vesper in a single file.

HUD Setup → Diagnostics → Run diagnostics, "Vesper, run diagnostics", or ``python -m assistant --diagnose``. Every
check runs side by side with a time limit (the health check's checks, each skill's self-test, the graphics card and the
models), and the result is written to ``data/diagnostics-<date>.md`` (to read and send) and ``.json`` (the same, for a
program). It holds versions, the PC, the graphics card, the Ollama models (and whether the chat model sees images and
takes actions, whether the embedding model is there), each skill (on, off or failed, how it's doing, its last error),
the connections (Google accounts and what each allowed, Gmail, Telegram, Spotify, Home Assistant, Key Light, YouTube,
Discord, tool servers), how long startup took, the last 50 warnings and errors from data/logs/assistant.log, and the
watchdog's view of every running part.

Private by design: secrets from .env and the saved sign-ins are taken out wherever they appear, so are tokens and keys
by their shape, email addresses are partly hidden (jw***@gmail.com), the home folder becomes ~, and no message text is
included (only the log's own warning lines, cut short).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .doctor import FAIL, PASS, SKIP, TIMEOUT, WARN, check_skills, run_doctor, run_parallel

log = logging.getLogger(__name__)

PREFIX = "diagnostics-"
STARTUP_FILE = "startup.json"  # in data/: the runtime's last start, for a report made by --diagnose
LOG_LINES = 50
PACKAGES = ["fastapi", "uvicorn", "httpx", "anthropic", "pydantic", "psutil", "mcp", "faster-whisper", "sherpa-onnx",
            "onnxruntime", "sounddevice", "numpy", "livekit-wakeword", "pycaw", "pystray", "winrt-runtime", "winsdk"]
EMBED_DEFAULT = "embeddinggemma"


# ---- keeping secrets out ------------------------------------------------------------------------------------------

PATTERNS = [
    (re.compile(r"(?i)\b(bearer|token|bot)\s*[:=]?\s*([A-Za-z0-9._~+/=-]{16,})"), r"\1 [hidden]"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "[telegram token]"),  # a Telegram bot token
    (re.compile(r"(?i)(discord(?:app)?\.com/api/webhooks/)[^\s\"'<>]+"), r"\1[hidden]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "[google key]"),
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"), "[api key]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+"), "[google token]"),
    (re.compile(r"\b1//[A-Za-z0-9_-]{20,}"), "[google token]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[token]"),  # Home Assistant & JWTs
    (re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}\b"), "[github token]"),
    (re.compile(r"(?i)([?&](?:key|token|access_token|refresh_token|code|state|client_secret|password|secret|"
                r"api_key|apikey|sig|signature)=)[^&\s\"'<>]+"), r"\1[hidden]"),
    (re.compile(r"(?i)\b(token|key)=(?!\[)[^&\s\"'<>]+"), r"\1=[hidden]"),
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|client_secret|refresh_token|access_token)(\s*[=:]\s*)"
                r"(?!\[)[^\s,;\"'}]+"), r"\1\2[hidden]"),
    # long random strings (keys, tokens) with letters and digits mixed; ordinary words and paths don't match
    (re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{36,}\b"), "[hidden]"),
]
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|_KEY$|WEBHOOK|NTFY|HA_URL|CLIENT_ID", re.I)
EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")


def mask_email(email: str) -> str:
    """jwill736@gmail.com -> jw***@gmail.com: enough to tell work from personal, not enough to write to."""
    return EMAIL.sub(lambda m: f"{m.group(1)[:2]}***@{m.group(2)}", email)


class Redactor:
    """Takes secrets out of any text: the values in .env and the saved sign-ins wherever they appear, tokens and keys
    by their shape, email addresses partly hidden, and the home folder shown as ~."""

    def __init__(self, secrets: list[str] | None = None, home: str | None = None):
        vals = {s.strip() for s in (secrets or []) if isinstance(s, str) and len(s.strip()) >= 6}
        self.secrets = sorted(vals, key=len, reverse=True)
        home = home if home is not None else str(Path.home())
        self.home = (re.compile(re.escape(home).replace(r"\\", r"[\\/]") + r"(?![\w.-])", re.I)
                     if home and len(home) > 3 else None)

    @classmethod
    def for_cfg(cls, cfg) -> "Redactor":
        found: list[str] = []
        root = Path(getattr(cfg, "root", ".") or ".")
        try:
            for line in (root / ".env").read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = (x.strip().strip("'\"") for x in line.split("=", 1))
                    if SECRET_NAME.search(key) or len(value) >= 16:  # never a short ordinary word
                        found.append(value)
        except OSError:
            pass
        for k, v in os.environ.items():
            if SECRET_NAME.search(k):
                found.append(v)
        try:
            data_dir = Path(cfg.data_dir)
        except Exception:
            data_dir = None
        for name in ("google_tokens.json", "spotify_tokens.json", "twitch_token.json", "api_token", "phone.json"):
            path = data_dir / name if data_dir else None
            if path is None or not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                found += _token_values(json.loads(text))
            except ValueError:
                found.append(text.strip())  # api_token: the HUD's own key
        return cls(found)

    def __call__(self, text: Any) -> str:
        s = str(text if text is not None else "")
        for secret in self.secrets:
            if secret in s:
                s = s.replace(secret, "[hidden]")
        for pat, repl in PATTERNS:
            s = pat.sub(repl, s)
        s = mask_email(s)
        if self.home is not None:
            s = self.home.sub("~", s)
        return s

    def deep(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self(obj)
        if isinstance(obj, dict):
            return {k: self.deep(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.deep(v) for v in obj]
        return obj


def _token_values(obj: Any, key: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _token_values(v, str(k))
    elif isinstance(obj, list):
        for v in obj:
            out += _token_values(v, key)
    elif isinstance(obj, str) and re.search(r"token|secret|code|key|chat|verifier", key, re.I) and len(obj) >= 6:
        out.append(obj)
    elif isinstance(obj, int) and re.search(r"chat", key, re.I):
        out.append(str(obj))  # the paired Telegram chat
    return out


# ---- what's on this PC --------------------------------------------------------------------------------------------

def versions(cfg) -> dict:
    from importlib import metadata

    root = Path(getattr(cfg, "root", ".") or ".")
    code = Path(__file__).parent
    out: dict[str, Any] = {"python": sys.version.split()[0], "executable": sys.executable}
    try:
        from .server import hud_version

        out["hud_version"] = hud_version()
    except Exception as exc:  # a missing HUD file is itself worth seeing
        out["hud_version"] = f"unknown ({type(exc).__name__})"
    out["git"] = _git_head(root) or _git_head(code.parent) or "not a git copy (installed from the download)"
    try:
        newest = max(p.stat().st_mtime for p in code.rglob("*.py"))
        out["code_date"] = datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        pass
    pkgs = {}
    for name in PACKAGES:
        try:
            pkgs[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        except Exception:
            continue
    out["packages"] = pkgs
    return out


def _git_head(root: Path) -> str | None:
    head = root / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if ref.startswith("ref:"):
        name = ref.split(":", 1)[1].strip()
        try:
            sha = (root / ".git" / name).read_text(encoding="utf-8").strip()
        except OSError:
            sha = ""
            try:
                for line in (root / ".git" / "packed-refs").read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + name):
                        sha = line.split()[0]
            except OSError:
                pass
        return f"{name.rsplit('/', 1)[-1]} @ {sha[:10]}" if sha else name
    return ref[:10]


def system_info(cfg) -> dict:
    out: dict[str, Any] = {"os": platform.platform(), "machine": platform.machine(),
                           "processor": platform.processor() or platform.machine()}
    try:
        import psutil

        out["cpus"] = psutil.cpu_count(logical=True)
        out["cores"] = psutil.cpu_count(logical=False)
        out["ram_gb"] = round(psutil.virtual_memory().total / 2 ** 30, 1)
        out["ram_used_pct"] = psutil.virtual_memory().percent
        du = psutil.disk_usage(str(cfg.data_dir))
        out["data_disk_free_gb"] = round(du.free / 2 ** 30, 1)
        out["uptime_h"] = round((time.time() - psutil.boot_time()) / 3600, 1)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def gpu_info(runner: Callable[[list[str]], str | None] | None = None) -> dict:
    """nvidia-smi's view of each graphics card."""
    if runner is None:
        from .skills.janitor import run_smi as runner
    out = runner(["--query-gpu=name,driver_version,memory.total,memory.used,temperature.gpu,utilization.gpu",
                  "--format=csv,noheader,nounits"])
    if not out:
        return {"ok": False, "error": "nvidia-smi didn't answer (no NVIDIA card, or the driver isn't installed)"}
    cards = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 6:
            continue
        num = lambda v: float(v) if re.fullmatch(r"[\d.]+", v) else None  # noqa: E731
        cards.append({"name": p[0], "driver": p[1], "memory_gb": round((num(p[2]) or 0) / 1024, 1),
                      "memory_used_gb": round((num(p[3]) or 0) / 1024, 1), "temp_c": num(p[4]), "busy_pct": num(p[5])})
    return {"ok": bool(cards), "cards": cards}


def model_info(cfg, svc=None, runtime=None) -> dict:
    """The model server, its models, the one that answers, and what it can do; the embedding model for memory."""
    from .brain.local_llm import GIB, LocalBrain, server_label

    lcfg = (cfg.get("brain") or {}).get("local") or {}
    mem = ((cfg.get("skills") or {}).get("memory") or {})
    embed = mem.get("embed_model", EMBED_DEFAULT)
    out: dict[str, Any] = {"enabled": lcfg.get("enabled", True), "embed_model": embed or None,
                           "claude_key": bool(cfg.secret((cfg.get("claude") or {}).get("api_key_env"))),
                           "claude_model": (cfg.get("claude") or {}).get("model"),
                           "provider": (cfg.get("brain") or {}).get("provider", "auto")}
    if not out["enabled"]:
        return out
    brain = getattr(getattr(runtime, "assistant", None), "local", None)
    llm = None
    if brain is not None:
        llm = brain.refresh()
    if brain is None or brain.found is None:
        brain = LocalBrain.for_report(lcfg, getattr(cfg, "data_dir", None))
        llm = brain.refresh(force=True)
    found = brain.found or {}
    models = found.get("models") or []
    out.update({"server": server_label(found.get("kind"), found.get("url")), "url": found.get("url"),
                "configured_url": (lcfg.get("url") or "").rstrip("/") or None,
                "models": [{"name": m["name"], "size_gb": round(m["bytes"] / GIB, 1) if m.get("bytes") else None,
                            "caps": m.get("caps")} for m in models]})
    if llm is not None:
        caps = next((m.get("caps") for m in models if m["name"] == llm.model), None)
        out.update({"chat_model": llm.model, "chat_caps": caps,
                    "vision": None if caps is None else "vision" in caps,
                    "tools": None if caps is None else "tools" in caps})
    out["vision_models"] = [m["name"] for m in models if "vision" in (m.get("caps") or [])]
    if embed:
        out["embed_found"] = any(m["name"] == embed or m["name"].split(":")[0] == embed.split(":")[0] for m in models)
    return out


def model_checks(m: dict) -> list[dict]:
    rows = []
    if not m.get("enabled", True):
        return [{"name": "Models · local model", "status": SKIP, "detail": "turned off (brain.local.enabled)", "fix": ""}]
    if not m.get("server"):
        where = m.get("configured_url")
        rows.append({"name": "Models · Ollama", "status": WARN if m.get("claude_key") else FAIL,
                     "detail": f"no model server answers{' at ' + where if where else ''}",
                     "fix": "Start Ollama (it runs in the tray), then run this again. See docs/VESPER-2.md Part C."})
        return rows
    if m.get("chat_model"):
        rows.append({"name": "Models · chat model", "status": PASS,
                     "detail": f"{m['chat_model']} on {m['server']}", "fix": ""})
        if m.get("tools") is False:
            rows.append({"name": "Models · actions", "status": WARN,
                         "detail": f"{m['chat_model']} can't take actions (no tool support)",
                         "fix": "Pick a model with tools in Setup → Brain (qwen3, llama3.1+)."})
    else:
        rows.append({"name": "Models · chat model", "status": WARN, "detail": "the server answers but has no chat model",
                     "fix": "In a terminal: ollama pull qwen3:8b. See docs/VESPER-2.md Part C."})
    if m.get("vision") or m.get("vision_models"):
        rows.append({"name": "Models · vision", "status": PASS,
                     "detail": "the chat model sees images" if m.get("vision") else
                     f"sees images with {m['vision_models'][0]}", "fix": ""})
    else:
        rows.append({"name": "Models · vision", "status": WARN, "detail": "no model that sees images",
                     "fix": "For “look at this”: ollama pull qwen3-vl:8b. See docs/VESPER-2.md Part C."})
    if m.get("embed_model"):
        ok = m.get("embed_found")
        rows.append({"name": "Models · embeddings", "status": PASS if ok else WARN,
                     "detail": f"{m['embed_model']} {'is there' if ok else 'is missing'}",
                     "fix": "" if ok else f"In a terminal: ollama pull {m['embed_model']}. See docs/VESPER-2.md Part C."})
    return rows


def google_rows(svc) -> list[dict]:
    google = getattr(svc, "google", None)
    if google is None or not getattr(google, "configured", False):
        return [{"name": "Google", "status": SKIP, "detail": "not set up"}]
    from .integrations.google import ALLOWS

    rows = []
    for a in google.accounts():
        allowed = [n for n in ALLOWS.values() if n not in (a.get("missing") or [])]
        status = FAIL if a.get("error") else WARN if a.get("missing") else PASS
        detail = a.get("error") or (f"allowed {', '.join(allowed) or 'nothing'}" +
                                     (f"; not allowed: {', '.join(a['missing'])}" if a.get("missing") else ""))
        rows.append({"name": f"Google · {a['email']}", "status": status, "detail": detail})
    return rows or [{"name": "Google", "status": WARN, "detail": "sign-in key saved, no account connected"}]


CONNECTION_SKILLS = [("inbox_zero", "Gmail"), ("phone", "Telegram"), ("spotify", "Spotify"), ("home", "Home"),
                     ("youtube", "YouTube"), ("discord", "Discord"), ("mcp", "Tool servers (MCP)")]


def recent_log_lines(data_dir: Path, limit: int = LOG_LINES) -> list[str]:
    """The last ``limit`` WARNING / ERROR lines of data/logs/assistant.log (and the file before it, if needed)."""
    logs = Path(data_dir) / "logs"
    pat = re.compile(r"^\d{4}-\d\d-\d\d[ T][\d:,.]+\s+(WARNING|ERROR|CRITICAL)\b")
    picked: list[str] = []
    for name in ("assistant.log", "assistant.log.1"):
        path = logs / name
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 3_000_000))
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        lines = [ln[:300] for ln in text.splitlines() if pat.match(ln)]
        picked = lines + picked
        if len(picked) >= limit:
            break
    return picked[-limit:]


def status_line(skill, checks: list[dict]) -> str:
    """One line on how a skill is doing, from its self-test (never from its panel, which can hold private text)."""
    real = [c for c in checks if c.get("status") != SKIP]
    if real:
        worst = next((c for c in real if c["status"] == FAIL), None) or next(
            (c for c in real if c["status"] == WARN), None) or real[0]
        return f"{worst.get('name')}: {worst.get('detail')}".strip(": ")
    if checks:
        return checks[0].get("detail") or "not set up"
    err = getattr(skill, "error", None) if skill is not None else None
    return f"problem: {err}" if isinstance(err, str) and err else "running"


# ---- the report -------------------------------------------------------------------------------------------------

def run(cfg, svc=None, runtime=None, *, timeout: float = 15.0, write: bool = True, gpu_runner=None, startup_extra: dict | None = None,
        now: datetime | None = None) -> dict:
    """Make the report (and write it, unless ``write`` is False). Returns it, with ``files`` when written."""
    t0 = time.perf_counter()
    now = now or datetime.now().astimezone()
    sk = getattr(svc, "skills", None) if svc is not None else None
    jan = sk.get("janitor") if sk is not None else None
    runner = gpu_runner or (getattr(jan, "runner", None) if jan is not None else None)
    jobs: list[tuple[str, Callable[[], Any]]] = [
        ("doctor", lambda: run_doctor(cfg, svc, test_mic=False, load_model=False, check_ports=False, network=True,
                                      skills=False, timeout=timeout)),
        ("gpu", lambda: gpu_info(runner)),
        ("models", lambda: model_info(cfg, svc, runtime)),
        ("versions", lambda: versions(cfg)),
        ("system", lambda: system_info(cfg)),
    ]
    if sk is not None:
        jobs.append(("selftests", lambda: sk.selftests(min(timeout, 10.0))))
    results = run_parallel(jobs, timeout + 2)  # the health check inside has its own limit: a little longer

    def got(key: str, default: Any) -> Any:
        kind, value = results.get(key, (TIMEOUT, None))
        if kind == "ok":
            return value
        return {**(default if isinstance(default, dict) else {}),
                "error": "no answer in time" if kind == TIMEOUT else f"{type(value).__name__}: {value}"}

    doctor = got("doctor", {"checks": []})
    tests = got("selftests", {}) if sk is not None else {}
    tests = tests if isinstance(tests, dict) and "error" not in tests else {}
    models = got("models", {})
    checks = list(doctor.get("checks") or [])
    if doctor.get("error"):
        checks.append({"name": "Health check", "status": WARN, "detail": doctor["error"], "fix": ""})
    checks += model_checks(models) if not models.get("error") else [
        {"name": "Models", "status": WARN, "detail": models["error"], "fix": ""}]
    if sk is not None:
        from dataclasses import asdict

        checks += [asdict(c) for c in check_skills(sk, tests=tests)]  # the self-tests already ran, side by side
    skills = []
    if sk is not None:
        for row in sk.overview():
            skill = sk.get(row["name"])
            mine = tests.get(row["name"], [])
            skills.append({**row, "status": status_line(skill, mine) if row["state"] == "on" else
                           {"off": "turned off", "crashed": "switched off after errors",
                            "failed": "didn't load"}.get(row["state"], row["state"]),
                           "checks": mine})
    connections = google_rows(svc) if svc is not None else []
    for name, label in CONNECTION_SKILLS:
        for c in tests.get(name, []):
            sub = c.get("name") or label
            connections.append({"name": sub if sub.startswith(label) else f"{label} · {sub}", "status": c.get("status"),
                                "detail": c.get("detail") or ""})
    startup = dict(getattr(runtime, "timings", None) or {})
    if not startup:  # made apart from the running copy: its last start, from data/startup.json
        try:
            last = json.loads((Path(cfg.data_dir) / STARTUP_FILE).read_text(encoding="utf-8"))
            startup = {"from_last_start": datetime.fromtimestamp(last.get("at") or 0).isoformat(timespec="seconds"),
                       **{k: v for k, v in last.items() if k != "at"}}
        except (OSError, ValueError, TypeError, AttributeError):
            startup = {}
    startup.update(startup_extra or {})
    if sk is not None and not startup.get("skill_load_ms"):
        startup.setdefault("skill_load_ms", dict(sk.timings))
        if sk.started:
            startup.setdefault("skill_start_ms", dict(sk.started))
    watchdog = runtime.supervisor.snapshot() if runtime is not None and getattr(runtime, "supervisor", None) else None
    counts = Counter(c.get("status") for c in checks)
    verdict = FAIL if counts.get(FAIL) else WARN if counts.get(WARN) else PASS
    report = {
        "made_at": now.isoformat(timespec="seconds"), "ran_s": round(time.perf_counter() - t0, 2),
        "mode": "live" if runtime is not None else "separate (--diagnose with Vesper closed)",
        "summary": {PASS: counts.get(PASS, 0), WARN: counts.get(WARN, 0), FAIL: counts.get(FAIL, 0),
                    SKIP: counts.get(SKIP, 0), "verdict": verdict},
        "vesper": {"name": cfg["assistant"]["name"], **got("versions", {}),
                   "running_since": datetime.fromtimestamp(runtime.started).isoformat(timespec="seconds")
                   if runtime is not None and getattr(runtime, "started", None) else None},
        "system": got("system", {}), "gpu": got("gpu", {}), "models": models,
        "checks": checks, "skills": skills, "connections": connections, "startup": startup,
        "watchdog": watchdog, "log": recent_log_lines(cfg.data_dir),
    }
    report = Redactor.for_cfg(cfg).deep(report)
    if write:
        report["files"] = write_report(report, Path(cfg.data_dir), now)
    return report


def write_report(report: dict, data_dir: Path, now: datetime) -> dict:
    stem = f"{PREFIX}{now.strftime('%Y-%m-%d')}"
    md, js = data_dir / f"{stem}.md", data_dir / f"{stem}.json"
    files = {"md": str(md), "json": str(js)}
    body = dict(report, files={"md": md.name, "json": js.name})
    for path, text in ((js, json.dumps(body, indent=1, default=str)), (md, to_markdown(body))):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    return files


def latest(data_dir: Path) -> dict | None:
    """The newest report on disk, in brief (for the HUD after a restart)."""
    try:
        paths = sorted(Path(data_dir).glob(f"{PREFIX}*.json"))
        if not paths:
            return None
        rep = json.loads(paths[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    md = paths[-1].with_suffix(".md")
    return brief({**rep, "files": {"md": str(md), "json": str(paths[-1])}})


def brief(report: dict) -> dict:
    """What the HUD panel and the spoken reply need."""
    order = {FAIL: 0, WARN: 1}
    attention = sorted((c for c in report.get("checks") or [] if c.get("status") in order),
                       key=lambda c: order[c["status"]])
    return {"made_at": report.get("made_at"), "ran_s": report.get("ran_s"), "summary": report.get("summary") or {},
            "files": report.get("files") or {}, "attention": attention[:12], "more": max(0, len(attention) - 12),
            "mode": report.get("mode")}


# ---- markdown -----------------------------------------------------------------------------------------------------

MARK = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "skip"}


def _cell(v: Any) -> str:
    s = "" if v is None else str(v)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip() or " "


def _table(head: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return out + [""]


def to_markdown(r: dict) -> str:
    s = r.get("summary") or {}
    v = r.get("vesper") or {}
    lines = [f"# {v.get('name', 'Vesper')} diagnostics · {r.get('made_at', '')}", "",
             f"**{MARK.get(s.get('verdict'), '?')}** · {s.get(PASS, 0)} passed · {s.get(WARN, 0)} warnings · "
             f"{s.get(FAIL, 0)} failed · {s.get(SKIP, 0)} not set up · made in {r.get('ran_s')} s ({r.get('mode')})", "",
             "Safe to send as it is: secrets and tokens are taken out, email addresses are partly hidden, and no "
             "message text is included.", ""]
    attention = [c for c in r.get("checks") or [] if c.get("status") in (FAIL, WARN)]
    lines += ["## Needs attention", ""]
    if attention:
        attention.sort(key=lambda c: c["status"] != FAIL)
        for c in attention:
            fix = f" → {c['fix']}" if c.get("fix") else ""
            lines.append(f"- **{MARK[c['status']]}** {c['name']}: {c.get('detail') or ''}{fix}")
    else:
        lines.append("Nothing: every check passed.")
    lines.append("")
    pk = v.get("packages") or {}
    lines += ["## Vesper", ""] + _table(["", ""], [
        ["Code", v.get("git")], ["Code date", v.get("code_date")], ["HUD version", v.get("hud_version")],
        ["Python", f"{v.get('python')} ({v.get('executable')})"], ["Running since", v.get("running_since") or "—"],
        ["Packages", ", ".join(f"{k} {x}" for k, x in pk.items())]])
    sy = r.get("system") or {}
    lines += ["## This PC", ""] + _table(["", ""], [
        ["Windows / OS", sy.get("os")], ["CPU", f"{sy.get('processor')} · {sy.get('cores')} cores / {sy.get('cpus')} threads"],
        ["Memory", f"{sy.get('ram_gb')} GB ({sy.get('ram_used_pct')}% in use)"],
        ["Free on the data drive", f"{sy.get('data_disk_free_gb')} GB"], ["Up for", f"{sy.get('uptime_h')} h"]]
        + ([["Problem", sy["error"]]] if sy.get("error") else []))
    g = r.get("gpu") or {}
    lines += ["## Graphics card", ""]
    if g.get("cards"):
        lines += _table(["Card", "Driver", "Memory", "In use", "Temp", "Busy"], [
            [c["name"], c["driver"], f"{c['memory_gb']} GB", f"{c['memory_used_gb']} GB",
             f"{c['temp_c']:.0f} °C" if c.get("temp_c") is not None else "—",
             f"{c['busy_pct']:.0f}%" if c.get("busy_pct") is not None else "—"] for c in g["cards"]])
    else:
        lines += [g.get("error") or "No card found.", ""]
    m = r.get("models") or {}
    lines += ["## Models", ""]
    yn = lambda x: "yes" if x else "no" if x is False else "unknown"  # noqa: E731
    rows = [["Brain setting", m.get("provider")], ["Claude key", yn(m.get("claude_key"))],
            ["Claude model", m.get("claude_model")], ["Local model", "off" if m.get("enabled") is False else
                                                     m.get("server") or "no model server answers"]]
    if m.get("configured_url"):
        rows.append(["brain.local.url", m["configured_url"]])
    if m.get("chat_model"):
        rows += [["Chat model", m["chat_model"]], ["Sees images (vision)", yn(m.get("vision"))],
                 ["Takes actions (tools)", yn(m.get("tools"))]]
    rows += [["Vision models", ", ".join(m.get("vision_models") or []) or "none"],
             ["Embedding model (memory)", f"{m.get('embed_model')}: {'there' if m.get('embed_found') else 'missing'}"
              if m.get("embed_model") else "off"]]
    if m.get("error"):
        rows.append(["Problem", m["error"]])
    lines += _table(["", ""], rows)
    if m.get("models"):
        lines += _table(["Installed model", "Size", "Can"], [
            [x["name"], f"{x['size_gb']} GB" if x.get("size_gb") else "—", ", ".join(x.get("caps") or []) or "—"]
            for x in m["models"]])
    lines += ["## Connections", ""] + _table(["", "Connection", "Detail"], [
        [MARK.get(c.get("status"), c.get("status")), c.get("name"), c.get("detail")] for c in r.get("connections") or []])
    lines += ["## Skills", ""] + _table(["Skill", "State", "How it's doing", "Last error", "Errors", "Load ms"], [
        [f"{x['title']} ({x['name']})", x["state"], x.get("status"),
         f"{x['last_error']} (in {x['where']})" if x.get("last_error") and x.get("where") else x.get("last_error") or "—",
         x.get("errors") or 0, x.get("load_ms") if x.get("load_ms") is not None else "—"]
        for x in r.get("skills") or []])
    lines += ["## All checks", ""] + _table(["", "Check", "Detail", "Fix"], [
        [MARK.get(c.get("status"), c.get("status")), c.get("name"), c.get("detail"), c.get("fix")]
        for c in r.get("checks") or []])
    st = r.get("startup") or {}
    lines += ["## Startup", ""]
    if st:
        simple = [[k, v2] for k, v2 in st.items() if not isinstance(v2, dict)]
        lines += _table(["", ""], simple) if simple else []
        for key, label in (("skill_load_ms", "Slowest skills to load (ms)"), ("skill_start_ms", "Slowest skills to start (ms)")):
            ms = st.get(key) or {}
            if ms:
                top = sorted(ms.items(), key=lambda kv: -(kv[1] or 0))[:8]
                lines += [f"{label}: " + ", ".join(f"{k} {v2:.0f}" for k, v2 in top), ""]
    else:
        lines += ["Not measured (Vesper hasn't started since this was added).", ""]
    wd = r.get("watchdog")
    lines += ["## Watchdog · running parts", ""]
    if wd:
        lines += _table(["Part", "State", "Runs", "Errors", "Restarts", "Last error"], [
            [p.get("name"), p.get("state"), p.get("runs"), p.get("errors"), p.get("restarts"), p.get("last_error") or "—"]
            for p in wd])
    else:
        lines += ["Vesper wasn't running in this process: run diagnostics from the HUD (Setup → Diagnostics) for this.", ""]
    lines += [f"## Recent warnings and errors (last {LOG_LINES}, data/logs/assistant.log)", ""]
    log_lines = r.get("log") or []
    lines += (["```", *log_lines, "```"] if log_lines else ["None."]) + [""]
    return "\n".join(lines)


# ---- python -m assistant --diagnose ---------------------------------------------------------------------------------

def from_running(host: str, port: int, data_dir: Path, timeout: float = 90) -> dict | None:
    """Ask the copy of Vesper that's running (it knows the watchdog and the startup timings). None if none is."""
    import httpx

    token_file = Path(data_dir) / "api_token"
    try:
        token = token_file.read_text().strip()
        r = httpx.post(f"http://{host}:{port}/api/skill/diagnostics/run", headers={"x-assistant-token": token},
                       json={}, timeout=timeout)
        out = r.json() if r.status_code == 200 else None
    except (OSError, ValueError, httpx.HTTPError):
        return None
    return out if isinstance(out, dict) and out.get("ok") and out.get("files") else None


def cli(cfg, host: str = "127.0.0.1", port: int = 8765, ask_running: bool = True,
        out: Callable[[str], None] = print) -> int:
    out("Running diagnostics…")
    result = from_running(host, port, cfg.data_dir) if ask_running else None
    if result is None:
        from .services import build_services

        t0 = time.perf_counter()
        svc = build_services(cfg)
        built = round(time.perf_counter() - t0, 2)
        report = run(cfg, svc, startup_extra={"this_check_build_services_s": built})
        result = {**brief(report), "build_services_s": built}
    s = result.get("summary") or {}
    out(f"\n{MARK.get(s.get('verdict'), '?')}: {s.get(PASS, 0)} passed, {s.get(WARN, 0)} warnings, "
        f"{s.get(FAIL, 0)} failed ({s.get(SKIP, 0)} not set up).")
    for c in result.get("attention") or []:
        out(f"  [{MARK[c['status']]}] {c['name']}: {c.get('detail') or ''}")
    files = result.get("files") or {}
    out(f"\nReport: {files.get('md')}\n(also {files.get('json')})\nSend the .md file as it is: secrets are taken out.")
    return 0
