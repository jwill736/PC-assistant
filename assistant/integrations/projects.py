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


def _ref_stamp(path: Path) -> tuple | None:
    """What moves when a commit lands or the branch changes: HEAD, the branch's own ref and packed-refs (their
    mtimes). None when it can't be told (.git is a file: a worktree or submodule), so the log is read every time."""
    git = path / ".git"
    if not git.is_dir():
        return None
    try:
        head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    stamp = [head]
    for f in [git / "HEAD", git / "packed-refs"] + ([git / head[5:]] if head.startswith("ref: ") else []):
        try:
            stamp.append(f.stat().st_mtime_ns)
        except OSError:
            stamp.append(0)
    return tuple(stamp)


def repo_status(path: Path, cache: dict | None = None) -> dict:
    """Branch, uncommitted files, the last commit and today's count. Two git runs instead of four (PERF-7), and with
    ``cache`` the log is read again only when a ref moved or the day changed: one git run a repo, normally."""
    branch, dirty = "?", 0
    for line in _git(path, "status", "--porcelain=v2", "--branch").splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head "):].strip()
            branch = "HEAD" if branch == "(detached)" else branch  # as rev-parse --abbrev-ref says it
        elif line.strip() and not line.startswith("#"):
            dirty += 1
    day = datetime.now().date()
    stamp = _ref_stamp(path)
    hit = cache.get(str(path)) if cache is not None and stamp is not None else None
    if hit and hit[0] == (stamp, day):
        last, today = hit[1], hit[2]
    else:
        midnight = datetime.combine(day, dtime.min).timestamp()
        rows = [line.split("\x1f", 1) for line in _git(path, "log", "-n", "100", "--format=%ct%x1f%s").splitlines()
                if "\x1f" in line]
        rows = [(int(ts), msg) for ts, msg in rows if ts.isdigit()]
        last = {"ts": rows[0][0], "message": rows[0][1]} if rows else {"ts": 0, "message": ""}
        today = sum(1 for ts, _ in rows if ts >= midnight)
        if cache is not None and stamp is not None:
            cache[str(path)] = ((stamp, day), last, today)
    return {
        "name": path.name,
        "path": str(path),
        "branch": branch or "?",
        "last_commit": dict(last),
        "dirty_files": dirty,
        "commits_today": today,
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


def _read_session(path: Path, seen: dict | None = None) -> dict | None:
    """What a session file says so far: ``seen`` is what an earlier read found (with where it stopped), so only the
    lines written since are read. The session J is working in grows all the time: re-reading it whole every two
    minutes was 110 ms of CPU per 12 MB (PERF-7)."""
    st = dict(seen) if seen else {"offset": 0, "prompts": 0, "first": None, "last": None, "cwd": None, "branch": None,
                                  "last_ts": None}

    def take(raw: bytes) -> bool:
        """One line; True when it was a whole user record."""
        line = raw.decode("utf-8", errors="replace")
        if '"type":"user"' not in line and '"type": "user"' not in line:
            return False
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return False
        st["cwd"] = rec.get("cwd") or st["cwd"]
        st["branch"] = rec.get("gitBranch") or st["branch"]
        text = _human_text(rec)
        if text:
            st["prompts"] += 1
            st["first"] = st["first"] or text
            st["last"] = text
            st["last_ts"] = rec.get("timestamp") or st["last_ts"]
        return True
    try:
        with path.open("rb") as fh:  # line by line: a session can be tens of MB, never all in memory at once
            fh.seek(st["offset"])
            for raw in fh:
                if not raw.endswith(b"\n"):  # the last line, its newline not written yet
                    if take(raw):  # a whole record: counted; a half-written one is read again next time
                        st["offset"] += len(raw)
                    break
                take(raw)
                st["offset"] += len(raw)
    except OSError:
        return None
    return st


def _session_info(path: Path, st: dict | None) -> dict | None:
    if not st or not st["prompts"]:
        return None
    cwd = st["cwd"]
    return {
        "session_id": path.stem,
        "cwd": cwd,
        "project": Path(cwd).name if cwd else path.parent.name,
        "branch": st["branch"],
        "prompts": st["prompts"],
        "first_prompt": st["first"][:200],
        "last_prompt": st["last"][:200],
        "last_activity": path.stat().st_mtime,
        "last_prompt_at": st["last_ts"],
    }


def parse_session(path: Path) -> dict | None:
    try:
        return _session_info(path, _read_session(path))
    except OSError:
        return None


class ClaudeSessions:
    def __init__(self, claude_dir: str = "~/.claude", max_age_days: int = 30):
        self.root = Path(os.path.expandvars(claude_dir)).expanduser() / "projects"
        self.max_age = max_age_days * 86400
        self._cache: dict[str, tuple[float, int, dict | None]] = {}  # file -> (mtime, size, what it said so far)

    def sessions(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        cutoff = time.time() - self.max_age
        out = []
        for f in self.root.glob("*/*.jsonl"):
            try:
                stat = f.stat()
            except OSError:
                continue
            mtime, size = stat.st_mtime, stat.st_size
            if mtime < cutoff:
                continue
            cached = self._cache.get(str(f))
            if cached and cached[0] == mtime and cached[1] == size:
                st = cached[2]
            else:
                grew = cached is not None and cached[2] is not None and size >= cached[1]
                st = _read_session(f, cached[2] if grew else None)  # only what was written since, if it grew
                self._cache[str(f)] = (mtime, size, st)
            try:
                info = _session_info(f, st)
            except OSError:
                info = None
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
        self._logs: dict = {}  # repo path -> ((refs, day), last commit, commits today): see repo_status

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
        repos = [repo_status(p, self._logs) for p in self.repos()]
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
