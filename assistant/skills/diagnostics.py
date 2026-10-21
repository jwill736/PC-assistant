"""Diagnostics: one report J can send, and Setup → Skills (every skill with an on/off switch and its last error).

"Vesper, run diagnostics", the HUD's Setup → Diagnostics → Run diagnostics, or ``python -m assistant --diagnose``
(which asks this copy when it's running). The report itself is made by ``assistant/diagnostics.py`` and written to
data/diagnostics-<date>.md (+ .json): secrets taken out, email addresses partly hidden, no message text.

This skill can't be switched off (it's where the others are switched back on).
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .. import diagnostics as diag
from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill

IS_WINDOWS = sys.platform == "win32"


def open_path(path: Path, reveal: bool = False) -> None:
    """Notepad for the report (an .md file may have no app set to open it), Explorer with it selected for the folder."""
    if IS_WINDOWS:  # pragma: no cover - Windows only
        args = ["explorer", f"/select,{path}"] if reveal else ["notepad.exe", str(path)]
        subprocess.Popen(args, creationflags=0x08000000)  # CREATE_NO_WINDOW
        return
    webbrowser.open((path.parent if reveal else path).resolve().as_uri())


class Diagnostics(Skill):
    name, title = "diagnostics", "Diagnostics"
    essential = True

    def __init__(self, svc, opener=open_path, run_report=None):
        super().__init__(svc)
        self.opener = opener
        self.run_report = run_report or diag.run
        self.runtime = None
        self.last: dict | None = None
        self._loaded = False
        self._busy = threading.Lock()

    def start(self, runtime) -> None:
        self.runtime = runtime
        sk = self.svc.skills
        if sk is not None and self._changed not in sk.listeners:
            sk.listeners.append(self._changed)
        self.publish()

    def _changed(self) -> None:
        self.publish()

    # ---- the report ------------------------------------------------------------------------------------------
    def latest(self) -> dict | None:
        if self.last is None and not self._loaded:
            self._loaded = True
            self.last = diag.latest(self.svc.cfg.data_dir)
        return self.last

    def run(self) -> dict:
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "Diagnostics are already running: give it a few more seconds."}
        try:
            self.publish()  # the panel shows "running"
            report = self.run_report(self.svc.cfg, self.svc, self.runtime)
        finally:
            self._busy.release()
        self.last = diag.brief(report)
        # the Health check panel shows the same checks
        checks = report.get("checks") or []
        self.svc.bus.publish("doctor", {"ran_at": time.time(), "checks": checks,
                                        "summary": {k: v for k, v in (report.get("summary") or {}).items()
                                                    if k != "verdict"}}, sticky=True)
        self.publish()
        return {"ok": True, **self.last}

    def open(self, what: str = "file") -> dict:
        files = (self.latest() or {}).get("files") or {}
        path = Path(files.get("md") or "")
        # only ever a report this skill wrote, in the data folder
        if not files.get("md") or not path.exists() or path.parent.resolve() != Path(self.svc.cfg.data_dir).resolve() \
                or not re.fullmatch(r"diagnostics-[\d-]+\.md", path.name):
            return {"ok": False, "error": "No report yet: click Run diagnostics first."}
        try:
            self.opener(path, reveal=what == "folder")
        except Exception as exc:
            return {"ok": False, "error": f"Couldn't open it ({type(exc).__name__}): it's at {path}"}
        return {"ok": True, "path": str(path)}

    def toggle(self, body: dict) -> dict:
        sk = self.svc.skills
        name = str(body.get("name") or "")
        if sk is None or not name:
            return {"ok": False, "error": "Which skill?"}
        return sk.set_enabled(name, bool(body.get("on")))

    # ---- plugged into Vesper ---------------------------------------------------------------------------------
    def status(self) -> dict:
        sk = self.svc.skills
        return {"last": self.latest(), "running": self._busy.locked(),
                "skills": sk.overview() if sk is not None else []}

    def actions(self):
        return {"run": lambda b: self.run(), "open": lambda b: self.open(str(b.get("what") or "file")),
                "skill": self.toggle}

    def tools(self):
        return [Tool("run_diagnostics", "Run Vesper's full self-check (every connection, model, skill and recent error) "
                     "and save a report file the user can send for help. Use when asked to run diagnostics, a "
                     "self-test, or why something in Vesper isn't working.", obj(), self.run, tier=0)]

    def route(self, t: str):
        me = re.escape(str(self.svc.cfg["assistant"].get("name") or "vesper").lower())
        if re.fullmatch(rf"(?:(?:hey )?{me} )?(?:(?:run|do|start|make)(?: a| the| me a| your| some)?(?: full| quick)? "
                        r"(?:diagnostics?|diagnostic report|self[- ]?(?:test|check|diagnostics?))(?: report| check)?"
                        r"(?: on yourself)?|diagnose yourself|diagnostics)", t):
            return Intent("tool", "run_diagnostics", {})
        return None

    def say(self, tool, args, result):
        if tool != "run_diagnostics":
            return None
        if result.get("ok") is False:
            return result.get("error")
        s = result.get("summary") or {}
        fail, warn, ok = s.get("fail", 0), s.get("warn", 0), s.get("pass", 0)
        name = Path((result.get("files") or {}).get("md") or "diagnostics.md").name
        head = (f"Diagnostics done: {ok} passed, {warn} warning{'s' if warn != 1 else ''}, {fail} failed."
                if fail or warn else f"Diagnostics done: all {ok} checks passed.")
        return f"{head} The report is {name} in the Vesper data folder; Setup shows the summary."
