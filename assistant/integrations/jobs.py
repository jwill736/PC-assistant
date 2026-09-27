"""Background work that runs while you do something else.

* ``claude_code`` — runs Claude Code headless (``claude -p``) inside one of
  your repos, e.g. "have Claude add tests to the clipforge repo".
* ``research`` — a Claude API call with web search that writes up a brief.

Finished jobs are announced by voice and kept in the Jobs panel.
"""

from __future__ import annotations

import difflib
import json
import logging
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from ..bus import EventBus
from ..storage import Storage

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, storage: Storage, bus: EventBus, cfg: dict,
                 repo_lookup: Callable[[], list[Path]],
                 research_fn: Callable[[str], str] | None = None,
                 announce: Callable[[str], None] | None = None):
        self.storage, self.bus = storage, bus
        self.cmd = cfg.get("claude_code_cmd", "claude")
        self.permission_mode = cfg.get("permission_mode", "acceptEdits")
        self.timeout_s = int(cfg.get("timeout_minutes", 30)) * 60
        self.repo_lookup = repo_lookup
        self.research_fn = research_fn
        self.announce = announce
        self.pool = ThreadPoolExecutor(max_workers=int(cfg.get("max_concurrent", 2)), thread_name_prefix="job")
        self._procs: dict[int, subprocess.Popen] = {}

    def resolve_project(self, name: str | None) -> Path | None:
        if not name:
            return None
        p = Path(name).expanduser()
        if p.is_dir():
            return p
        repos = {r.name.lower(): r for r in self.repo_lookup()}
        key = name.lower().replace(" ", "-")
        if key in repos:
            return repos[key]
        close = difflib.get_close_matches(key, list(repos), n=1, cutoff=0.5)
        if close:
            return repos[close[0]]
        squashed = {k.replace("-", "").replace("_", ""): v for k, v in repos.items()}
        close = difflib.get_close_matches(key.replace("-", ""), list(squashed), n=1, cutoff=0.5)
        return squashed[close[0]] if close else None

    def submit(self, kind: str, prompt: str, project: str | None = None, title: str | None = None) -> dict:
        cwd = None
        if kind == "claude_code":
            if not shutil.which(self.cmd):
                return {"ok": False, "error": "Claude Code CLI isn't installed or not on PATH (`npm i -g @anthropic-ai/claude-code`)."}
            path = self.resolve_project(project)
            if path is None:
                known = ", ".join(r.name for r in self.repo_lookup()[:12]) or "none found — add projects.scan_dirs"
                return {"ok": False, "error": f"Which repo? I couldn't match '{project}'. Known: {known}."}
            cwd = str(path)
        elif kind == "research":
            if self.research_fn is None:
                return {"ok": False, "error": "Research jobs need the Claude API key configured."}
        else:
            return {"ok": False, "error": f"Unknown job kind '{kind}'."}
        title = title or (prompt[:60] + ("…" if len(prompt) > 60 else ""))
        job_id = self.storage.add_job(kind, title, prompt, cwd)
        self._emit(job_id)
        self.pool.submit(self._run, job_id, kind, prompt, cwd)
        return {"ok": True, "job_id": job_id, "title": title, "cwd": cwd}

    def _emit(self, job_id: int) -> None:
        self.bus.publish("job", self.storage.get_job(job_id))

    def _run(self, job_id: int, kind: str, prompt: str, cwd: str | None) -> None:
        self.storage.update_job(job_id, "running")
        self._emit(job_id)
        try:
            output = self._claude_code(job_id, prompt, cwd) if kind == "claude_code" else self.research_fn(prompt)  # type: ignore[misc]
            status = "done"
        except Exception as exc:
            log.exception("job %s failed", job_id)
            output, status = f"{type(exc).__name__}: {exc}", "failed"
        if (self.storage.get_job(job_id) or {}).get("status") == "cancelled":
            return
        self.storage.update_job(job_id, status, (output or "")[:20000])
        self._emit(job_id)
        job = self.storage.get_job(job_id) or {}
        if self.announce:
            self.announce(f"Background job {'finished' if status == 'done' else 'failed'}: {job.get('title', '')}.")

    def _claude_code(self, job_id: int, prompt: str, cwd: str | None) -> str:
        args = [shutil.which(self.cmd) or self.cmd, "-p", prompt, "--output-format", "json",
                "--permission-mode", self.permission_mode]
        proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                creationflags=0x08000000 if sys.platform == "win32" else 0)
        self._procs[job_id] = proc
        try:
            out, err = proc.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise TimeoutError(f"gave up after {self.timeout_s // 60} minutes")
        finally:
            self._procs.pop(job_id, None)
        if proc.returncode not in (0, None) and not out.strip():
            raise RuntimeError(err.strip()[-2000:] or f"claude exited {proc.returncode}")
        try:
            result = json.loads(out)
            text = result.get("result") or ""
            cost = result.get("total_cost_usd")
            if result.get("is_error"):
                raise RuntimeError(text or "Claude Code reported an error")
            return text + (f"\n\n(cost ${cost:.2f})" if isinstance(cost, (int, float)) else "")
        except json.JSONDecodeError:
            return out

    def cancel(self, job_id: int) -> dict:
        proc = self._procs.get(job_id)
        if proc:
            proc.kill()
        self.storage.update_job(job_id, "cancelled")
        self._emit(job_id)
        return {"ok": True, "job_id": job_id}

    def shutdown(self) -> None:
        for proc in list(self._procs.values()):
            proc.kill()
        self.pool.shutdown(wait=False, cancel_futures=True)
