"""What you're building: local git repos, Claude Code sessions, and GitHub.

Claude Code keeps every session as JSONL under ``~/.claude/projects/<dir>/``;
each human prompt is a ``type: "user"`` record carrying ``cwd`` and
``gitBranch``. Reading those gives a truthful "what have we been working on"
without any API.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _git(path: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                              timeout=5, creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def find_repos(scan_dirs: list[str], max_depth: int = 2) -> list[Path]:
    repos: list[Path] = []
    for d in scan_dirs:
        root = Path(os.path.expandvars(d)).expanduser()
        if not root.is_dir():
            continue
        stack = [(root, 0)]
        while stack:
            cur, depth = stack.pop()
            if (cur / ".git").exists():
                repos.append(cur)
                continue
            if depth >= max_depth:
                continue
            try:
                for child in cur.iterdir():
                    if child.is_dir() and not child.name.startswith(".") and child.name not in {"node_modules", "venv", ".venv"}:
                        stack.append((child, depth + 1))
            except OSError:
                continue
    return repos


def repo_status(path: Path) -> dict:
    head = _git(path, "log", "-1", "--format=%ct%x1f%s")
    ts, msg = (head.split("\x1f", 1) + [""])[:2] if head else ("0", "")
    midnight = datetime.combine(datetime.now().date(), dtime.min).isoformat()
    dirty = [line for line in _git(path, "status", "--porcelain").splitlines() if line.strip()]
    today = _git(path, "rev-list", "--count", f"--since={midnight}", "HEAD")
    return {
        "name": path.name,
        "path": str(path),
        "branch": _git(path, "rev-parse", "--abbrev-ref", "HEAD") or "?",
        "last_commit": {"ts": int(ts) if ts.isdigit() else 0, "message": msg},
        "dirty_files": len(dirty),
        "commits_today": int(today) if today.isdigit() else 0,
    }


def commits_between(path: Path, since: datetime, until: datetime) -> list[dict]:
    out = _git(path, "log", f"--since={since.isoformat()}", f"--until={until.isoformat()}", "--format=%ct%x1f%s")
    rows = []
    for line in out.splitlines():
        if "\x1f" in line:
            ts, msg = line.split("\x1f", 1)
            rows.append({"repo": path.name, "ts": int(ts), "message": msg})
    return rows


# ---------------------------------------------------------------------------
# Claude Code sessions
# ---------------------------------------------------------------------------

def _human_text(record: dict) -> str | None:
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return None
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    text = text.strip()
    if not text or text.startswith("<"):  # slash-command echoes, system reminders
        return None
    return text


def parse_session(path: Path) -> dict | None:
    first = last = None
    prompts = 0
    cwd = branch = None
    last_ts = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"type":"user"' not in line and '"type": "user"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = rec.get("cwd") or cwd
                branch = rec.get("gitBranch") or branch
                text = _human_text(rec)
                if text:
                    prompts += 1
                    first = first or text
                    last = text
                    last_ts = rec.get("timestamp") or last_ts
    except OSError:
        return None
    if not prompts:
        return None
    return {
        "session_id": path.stem,
        "cwd": cwd,
        "project": Path(cwd).name if cwd else path.parent.name,
        "branch": branch,
        "prompts": prompts,
        "first_prompt": first[:200],
        "last_prompt": last[:200],
        "last_activity": path.stat().st_mtime,
        "last_prompt_at": last_ts,
    }


class ClaudeSessions:
    def __init__(self, claude_dir: str = "~/.claude", max_age_days: int = 30):
        self.root = Path(os.path.expandvars(claude_dir)).expanduser() / "projects"
        self.max_age = max_age_days * 86400
        self._cache: dict[str, tuple[float, dict | None]] = {}

    def sessions(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        cutoff = time.time() - self.max_age
        out = []
        for f in self.root.glob("*/*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            cached = self._cache.get(str(f))
            if cached and cached[0] == mtime:
                info = cached[1]
            else:
                info = parse_session(f)
                self._cache[str(f)] = (mtime, info)
            if info:
                out.append(info)
        return sorted(out, key=lambda s: s["last_activity"], reverse=True)

    def by_project(self) -> list[dict]:
        projects: dict[str, dict] = {}
        for s in self.sessions():
            key = s["cwd"] or s["project"]
            p = projects.setdefault(key, {
                "project": s["project"], "cwd": s["cwd"], "sessions": 0, "prompts": 0,
                "last_activity": 0, "latest_ask": "", "branch": s["branch"],
            })
            p["sessions"] += 1
            p["prompts"] += s["prompts"]
            if s["last_activity"] > p["last_activity"]:
                p.update(last_activity=s["last_activity"], latest_ask=s["last_prompt"], branch=s["branch"])
        return sorted(projects.values(), key=lambda p: p["last_activity"], reverse=True)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

class GitHub:
    def __init__(self, user: str = "", token: str = ""):
        self.user, self.token = user, token
        self._cache: tuple[float, dict] = (0.0, {})

    @property
    def enabled(self) -> bool:
        return bool(self.user or self.token)

    def summary(self) -> dict:
        if not self.enabled:
            return {"enabled": False}
        ts, cached = self._cache
        if time.time() - ts < 600 and cached:
            return cached
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with httpx.Client(base_url="https://api.github.com", headers=headers, timeout=10) as gh:
                if self.token:
                    repos = gh.get("/user/repos", params={"sort": "pushed", "per_page": 12}).json()
                else:
                    repos = gh.get(f"/users/{self.user}/repos", params={"sort": "pushed", "per_page": 12}).json()
                login = self.user or gh.get("/user").json().get("login", "")
                prs = gh.get("/search/issues", params={"q": f"is:pr is:open author:{login}", "per_page": 20}).json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"enabled": True, "error": type(exc).__name__}
        if isinstance(repos, dict):  # error payload, e.g. bad token
            return {"enabled": True, "error": repos.get("message", "GitHub error")}
        data = {
            "enabled": True,
            "repos": [{"name": r["full_name"], "pushed_at": r.get("pushed_at"), "private": r.get("private"),
                       "open_issues": r.get("open_issues_count"), "url": r.get("html_url")} for r in repos],
            "open_prs": [{"title": p["title"], "repo": p["repository_url"].split("/repos/")[-1],
                          "url": p["html_url"], "draft": p.get("draft", False), "updated_at": p.get("updated_at")}
                         for p in (prs.get("items") or [])],
        }
        self._cache = (time.time(), data)
        return data


class ProjectTracker:
    def __init__(self, scan_dirs: list[str], claude_dir: str, github: GitHub):
        self.scan_dirs = scan_dirs or []
        self.claude = ClaudeSessions(claude_dir)
        self.github = github
        self._repos: list[Path] = []
        self._repos_scanned = 0.0
        self._lock = threading.Lock()

    def repos(self) -> list[Path]:
        with self._lock:
            if time.time() - self._repos_scanned > 900:
                found = set(find_repos(self.scan_dirs))
                # Any repo Claude Code has worked in counts, even outside scan_dirs.
                for p in self.claude.by_project():
                    if p["cwd"] and (Path(p["cwd"]) / ".git").exists():
                        found.add(Path(p["cwd"]))
                self._repos = sorted(found)
                self._repos_scanned = time.time()
            return self._repos

    def summary(self) -> dict:
        repos = [repo_status(p) for p in self.repos()]
        repos.sort(key=lambda r: r["last_commit"]["ts"], reverse=True)
        return {
            "repos": repos,
            "claude": self.claude.by_project()[:12],
            "github": self.github.summary(),
        }

    def commits_between(self, since: datetime, until: datetime) -> list[dict]:
        rows = []
        for p in self.repos():
            rows += commits_between(p, since, until)
        return sorted(rows, key=lambda r: r["ts"])
