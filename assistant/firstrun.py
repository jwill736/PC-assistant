"""First-run questions, asked by install.ps1 once the packages are in: what to call you, your main goal,
and which model answers open questions (one on this computer, one on another PC on your home network,
Claude, or later). Answers go into config.yaml, comments and layout kept, and the Claude key into .env.

    .venv\\Scripts\\python -m assistant.firstrun                  ask (run it again any time to change)
    .venv\\Scripts\\python -m assistant.firstrun --unattended --name J --brain-url GAMING-PC
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml

from .brain import local_llm
from .config import ROOT

# Default ports to try when you type just a PC's name: Ollama, then LM Studio.
REMOTE_PORTS = (11434, 1234)
_KEY = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_\-]+):(?P<rest>.*)$")

MAIN_PC_HELP = """\
  Vesper can't reach a model at {where} yet. On that PC:
    1. Open PowerShell and run:   setx OLLAMA_HOST 0.0.0.0
    2. Quit Ollama (right-click its icon by the clock > Quit), then start it again from the Start menu.
    3. If Windows Firewall asks, allow it on Private networks.
    LM Studio instead: Developer tab > Settings > turn on "Serve on Local Network".
  If the name doesn't work, use the PC's IP address: run ipconfig on it and copy "IPv4 Address".
  Saved anyway. Vesper checks every minute; Setup > Brain shows when it connects."""

NO_BRAIN = """\
  Without a model, Vesper still opens apps, sets the volume, runs OBS and Twitch, keeps your tasks and notes,
  and answers "what did I say about...". Open questions wait until you connect one: Setup > Brain in the HUD,
  or run this again:  .venv\\Scripts\\python -m assistant.firstrun"""


# ---- config.yaml editing that keeps your comments ---------------------------------------------------------

def _content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_end(lines: list[str], parent: int, pindent: int) -> int:
    if parent < 0:
        return len(lines)
    for i in range(parent + 1, len(lines)):
        if _content(lines[i]) and _indent(lines[i]) <= pindent:
            return i
    return len(lines)


def _split_value(rest: str) -> tuple[str, str]:
    """' "a # b"   # note' -> ('"a # b"', '   # note'): the value, then any spacing and comment after it."""
    s = rest.strip(" ")
    if s[:1] in ('"', "'"):
        quote, i = s[0], 1
        while i < len(s):
            if quote == '"' and s[i] == "\\":
                i += 2
                continue
            if s[i] == quote:
                if quote == "'" and s[i + 1:i + 2] == "'":
                    i += 2
                    continue
                break
            i += 1
        end = min(i + 1, len(s))
    else:
        m = re.search(r"\s#", s)
        end = m.start() if m else len(s)
    return s[:end].rstrip(), s[end:]


def _scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, str) else yaml.safe_dump(value).split("\n")[0]


def _dig(data: Any, parts: list[str]) -> Any:
    for part in parts:
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def set_yaml_value(text: str, dotted: str, value: Any) -> str:
    """Set one scalar in YAML text, changing only that line (or adding it) so comments and layout survive.

    ``set_yaml_value(text, "brain.local.url", "http://gaming-pc:11434")``. A commented-out ``# url:`` line in
    the right place is replaced. Raises ValueError when the result wouldn't read back as the value.
    """
    lines = text.split("\n")
    parts = dotted.split(".")
    parent, pindent = -1, -1
    for depth, key in enumerate(parts):
        start, end = parent + 1, _block_end(lines, parent, pindent)
        child = next((_indent(lines[i]) for i in range(start, end) if _content(lines[i])), None)
        cindent = child if child is not None and child > pindent else pindent + 2 if parent >= 0 else 0
        idx = next((i for i in range(start, end) if (m := _KEY.match(lines[i])) and m["key"] == key
                    and len(m["indent"]) == cindent), None)
        last = depth == len(parts) - 1
        if idx is None:
            new = [" " * (cindent + 2 * j) + f"{k}:" for j, k in enumerate(parts[depth:-1])]
            new.append(" " * (cindent + 2 * len(new)) + f"{parts[-1]}: {_scalar(value)}")
            commented = re.compile(rf"^ {{{cindent}}}#\s*{re.escape(key)}:")
            spot = next((i for i in range(start, end) if commented.match(lines[i])), None) if last else None
            if spot is not None:
                lines[spot] = new[0]
            elif parent >= 0:
                lines[parent + 1:parent + 1] = new
            else:
                at = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
                lines[at:at] = ([""] if at and lines[at - 1].strip() else []) + new
            break
        m = _KEY.match(lines[idx])
        val, trailer = _split_value(m["rest"])
        nxt = next((i for i in range(idx + 1, end) if _content(lines[i])), None)
        has_children = nxt is not None and _indent(lines[nxt]) > cindent
        if not last:
            if val:
                raise ValueError(f"{'.'.join(parts[:depth + 1])} is written on one line; edit config.yaml by hand")
            parent, pindent = idx, cindent
            continue
        if has_children:
            raise ValueError(f"{dotted} is a section, not a single value")
        head = f"{m['indent']}{key}: {_scalar(value)}"
        comment = trailer.strip(" ")
        if comment:  # keep the comment in its column when the new value fits
            col = len(lines[idx].rstrip(" ")) - len(comment)
            head += " " * max(1, col - len(head)) + comment
        lines[idx] = head
    out = "\n".join(lines)
    try:
        check = _dig(yaml.safe_load(out), parts)
    except yaml.YAMLError as exc:
        raise ValueError(f"config.yaml wouldn't read back: {exc}") from exc
    if check != value:
        raise ValueError(f"{dotted} didn't read back as {value!r}")
    return out


def set_env_value(text: str, key: str, value: str) -> str:
    """Set KEY=value in .env text: the existing line, else a new one at the end."""
    lines = text.split("\n")
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}"
            return "\n".join(lines)
    at = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
    lines.insert(at, f"{key}={value}")
    return "\n".join(lines)


# ---- finding the model --------------------------------------------------------------------------------

def candidate_urls(where: str) -> list[str]:
    """'gaming-pc' -> Ollama's and LM Studio's ports on it; a full URL or host:port is used as given."""
    where = where.strip().rstrip("/")
    if re.match(r"^https?://", where):
        return [where]
    if re.search(r":\d+$", where):
        return [f"http://{where}"]
    return [f"http://{where}:{port}" for port in REMOTE_PORTS]


def probe(where: str, http: httpx.Client | None = None) -> dict | None:
    """The model server at a PC name / IP / URL, or None. {kind, url, models} like local_llm.detect."""
    http = http or httpx.Client(timeout=3, trust_env=False)
    for url in candidate_urls(where):
        found = local_llm.detect(http, extra_url=url, timeout=3, defaults=False)
        if found:
            return found
    return None


def _models(found: dict) -> str:
    names = [m["name"] for m in found.get("models") or []]
    return ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")


# ---- the questions --------------------------------------------------------------------------------------

class Setup:
    def __init__(self, root: Path = ROOT, ask: Callable[[str], str] = input,
                 ask_secret: Callable[[str], str] = getpass.getpass, say: Callable[[str], None] = print,
                 http: httpx.Client | None = None, unattended: bool = False):
        self.root, self.ask_fn, self.ask_secret, self.say = root, ask, ask_secret, say
        self.http, self.unattended = http, unattended
        self.cfg_path, self.env_path = root / "config.yaml", root / ".env"
        example = root / "config.example.yaml"
        self.text = self.cfg_path.read_text(encoding="utf-8") if self.cfg_path.exists() else (
            example.read_text(encoding="utf-8") if example.exists() else "")
        self.env = self.env_path.read_text(encoding="utf-8") if self.env_path.exists() else ""
        self.changes: dict[str, Any] = {}
        self.key: str | None = None

    def ask(self, prompt: str, default: str = "") -> str:
        if self.unattended:
            return default
        try:
            answer = self.ask_fn(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
        except EOFError:  # stdin closed (piped install): keep the default
            return default
        return answer or default

    def current(self, dotted: str, default: str = "") -> str:
        try:
            data = yaml.safe_load(self.text) or {}
        except yaml.YAMLError:
            data = {}
        value = _dig(data, dotted.split("."))
        return str(value) if value not in (None, "") else default

    def run(self, name: str | None = None, goal: str | None = None, brain_url: str | None = None,
            claude_key: str | None = None) -> dict:
        self.say("\nThree quick questions. Press Enter to keep what's in [brackets].\n")
        name = name or self.ask("1/3  What should Vesper call you?", self.current("assistant.user_name", "boss"))
        if name and name != self.current("assistant.user_name"):
            self.changes["assistant.user_name"] = name
        if not goal:
            self.say("2/3  Your main goal, as a number and a date. Every plan is ranked against it.")
            self.say('     For example: "$5k/month from streaming by June 2027"')
        goal = goal or self.ask("    ", self.current("goals.north_star"))
        if goal and goal != self.current("goals.north_star"):
            self.changes["goals.north_star"] = goal
        brain = self.choose_brain(brain_url, claude_key)
        self.save()
        return {"changes": self.changes, "brain": brain, "claude_key": bool(self.key)}

    def choose_brain(self, brain_url: str | None, claude_key: str | None) -> str:
        self.say("\n3/3  Which model answers your questions?")
        if brain_url:
            return self.use_remote(brain_url)
        if claude_key:
            return self.use_claude(claude_key)
        self.say("     Looking for one on this computer...")
        here = local_llm.detect(self.http, timeout=1.5)
        if here and here["models"]:
            model = local_llm.pick_model(here["models"]) or here["models"][0]["name"]
            self.say(f"     Found {model} on {local_llm.LABELS.get(here['kind'], here['kind'])} on this computer. "
                     "Vesper will use it.")
            return "here"
        if here:
            self.say(f"     {local_llm.LABELS.get(here['kind'], here['kind'])} is running here but has no models yet:"
                     "  ollama pull llama3.2:3b")
        else:
            self.say("     None running here.")
        if self.unattended:
            self.say(NO_BRAIN)
            return "later"
        self.say("       1  My Llama is on another PC on my home network (e.g. my main PC)")
        self.say("       2  I'll run Ollama or LM Studio on this computer")
        self.say("       3  A Claude API key (paid, billed separately from a Claude subscription)")
        self.say("       4  Decide later")
        choice = self.ask("     Choose 1-4", "4")
        if choice == "1":
            where = self.ask("     That PC's name (Settings > System > About > Device name on it) or IP address")
            return self.use_remote(where) if where else self.later()
        if choice == "2":
            self.say("     Start it before Vesper, or any time: Vesper looks every minute. No model yet? Run\n"
                     "       ollama pull llama3.2:3b    (2 GB; answers take several seconds without a GPU)\n"
                     "       ollama pull llama3.1:8b    (5 GB; wants a GPU with 8 GB)")
            return "here-later"
        if choice == "3":
            key = (self.ask_secret("     Paste the key (it stays hidden), then Enter: ") or "").strip()
            return self.use_claude(key) if key else self.later()
        return self.later()

    def later(self) -> str:
        self.say(NO_BRAIN)
        return "later"

    def use_remote(self, where: str) -> str:
        found = probe(where, self.http)
        if found:
            self.changes["brain.local.url"] = found["url"]
            if found["models"]:
                self.say(f"     Connected to {found['url']}: {_models(found)}.")
            else:
                self.say(f"     Connected to {found['url']}, but it has no models yet. On that PC: "
                         "ollama pull llama3.1:8b")
            return "remote"
        url = candidate_urls(where)[0]
        self.changes["brain.local.url"] = url
        self.say(MAIN_PC_HELP.format(where=where))
        return "remote-unreachable"

    def use_claude(self, key: str) -> str:
        if not key.startswith("sk-ant-"):
            self.say("     That doesn't look like a Claude API key (they start with sk-ant-). Not saved.")
            return self.later()
        self.key = key
        self.say(f"     Saved the key ending ...{key[-4:]} to .env. Claude answers; a local model is the backup.")
        return "claude"

    def save(self) -> None:
        if self.changes:
            text = self.text
            for dotted, value in self.changes.items():
                try:
                    text = set_yaml_value(text, dotted, value)
                except ValueError as exc:
                    # A hand-edited config.yaml this can't safely change: the HUD's settings file wins over it.
                    self.say(f"     ({exc}; saved to data/settings.yaml instead)")
                    self._settings(dotted, value)
            if text != self.text:
                self.cfg_path.write_text(text, encoding="utf-8")
                self.text = text
        if self.key:
            self.env = set_env_value(self.env, "ANTHROPIC_API_KEY", self.key)
            self.env_path.write_text(self.env, encoding="utf-8")
        if self.changes or self.key:
            what = ", ".join(sorted(self.changes)) + (" and the Claude key" if self.key else "")
            self.say(f"\nSaved {what}.")

    def _settings(self, dotted: str, value: Any) -> None:
        from .config import load_config, save_setting

        save_setting(load_config(self.cfg_path), dotted, value)


def write_icon(path: Path) -> bool:
    """The tray's ring mark as a Windows .ico, for the desktop and Start menu shortcuts."""
    try:
        from .tray import Tray

        path.parent.mkdir(parents=True, exist_ok=True)
        Tray.image("listening").save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
        return True
    except Exception:  # no Pillow: the shortcuts keep the default icon
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m assistant.firstrun", description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", help="what Vesper calls you")
    ap.add_argument("--goal", help="your main goal, a number and a date")
    ap.add_argument("--brain-url", help="the PC (name, IP or URL) running your model, if not this one")
    ap.add_argument("--claude-key", help="a Claude API key to save in .env")
    ap.add_argument("--unattended", action="store_true", help="ask nothing; keep anything not given")
    ap.add_argument("--icon", help="also write the Vesper icon (.ico) here")
    ap.add_argument("--no-questions", action="store_true", help="only write the icon (an update keeps your answers)")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    if args.icon:
        write_icon(Path(args.icon))
    if args.no_questions:
        return 0
    setup = Setup(Path(args.root), unattended=args.unattended)
    setup.run(args.name, args.goal, args.brain_url, args.claude_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
