"""Diagnostics: one report J can send, and Setup → Skills (every skill with an on/off switch and its last error).

"Vesper, run diagnostics", the HUD's Setup → Diagnostics → Run diagnostics, or ``python -m assistant --diagnose``
(which asks this copy when it's running). The report itself is made by ``assistant/diagnostics.py`` and written to
data/diagnostics-<date>.md (+ .json): secrets taken out, email addresses partly hidden, no message text.

This skill can't be switched off (it's where the others are switched back on).
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .. import diagnostics as diag
from .. import resources
from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill

IS_WINDOWS = sys.platform == "win32"
log = logging.getLogger(__name__)


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
        self.measuring: dict | None = None  # {"started", "starts_at", "seconds", "progress"} while it measures
        self.measure_fn = resources.measure
        self.wait = time.sleep  # the "start in a minute" delay (a test passes its own)

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

    # ---- what Vesper is using (resources.py) ------------------------------------------------------------------
    def measure(self, seconds: float = resources.MEASURE_S, delay: float = 0.0) -> dict:
        """Watch Vesper for 30 s in the background; the card shows the result when it's done. ``delay``: start that
        many seconds from now (time to switch to a game: what it uses mid-game is the point)."""
        if self.measuring is not None:
            return {"ok": True, "running": True, **{k: v for k, v in self.measuring.items() if not k.startswith("_")}}
        seconds = max(1.0, min(float(seconds or resources.MEASURE_S), 120.0))
        delay = max(0.0, min(float(delay or 0), 600.0))
        now = time.time()
        self.measuring = started = {"started": now, "starts_at": now + delay, "seconds": seconds, "progress": 0.0}

        def progress(frac: float) -> None:
            m = self.measuring
            if m is not None:
                step = int(frac * 5)  # every 20 %: the card's bar moves without 30 messages
                m["progress"] = frac
                if step != m.get("_step"):
                    m["_step"] = step
                    self.publish()

        def work() -> None:
            try:
                if delay:
                    self.wait(delay)
                    self.publish()  # the card: measuring now
                self.measure_fn(self.runtime, seconds, on_progress=progress)
            except Exception:
                log.exception("measuring what Vesper uses failed")
            finally:
                self.measuring = None
                self.publish()
        self.publish()
        threading.Thread(target=work, name="resources-measure", daemon=True).start()
        return {"ok": True, "running": True, **{k: v for k, v in started.items() if not k.startswith("_")}}

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
                "skills": sk.overview() if sk is not None else [],
                "resources": resources.LAST[0], "measuring": self.measuring}

    def actions(self):
        return {"run": lambda b: self.run(), "open": lambda b: self.open(str(b.get("what") or "file")),
                "skill": self.toggle,
                "measure": lambda b: self.measure(b.get("seconds") or resources.MEASURE_S, b.get("delay") or 0)}

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
