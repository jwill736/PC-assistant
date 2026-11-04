"""Click anything by name: Windows UI Automation (UIA), the accessibility tree every Windows app exposes.

"Click Export in Premiere", "press Join in Discord", "turn on the Dark mode toggle in Settings", "type hello into the
search box". The window's controls are read breadth-first from its root (capped by depth, count and time, so a huge
app can't hang Vesper), matched by name and kind, then acted on through UIA's patterns first: Invoke (buttons),
Toggle (switches, check boxes), Value (text boxes), SelectionItem (tabs, list items), ExpandCollapse (menus, drop
downs). Only a control with none of them gets a real mouse click, at its centre, and only when that point really is
the control (nothing on top of it).

Plain ``comtypes`` (already installed with pycaw) talks to UIAutomationCore.dll: no new dependency. Everything that
touches COM is in ``ComBackend``; the walking, matching and pattern choice below work on any backend with the same
methods, so tests use a fake tree. Elsewhere than Windows, ``default_backend()`` is None and the tools say so.
"""

from __future__ import annotations

import ctypes
import difflib
import logging
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
WINDOWS_ONLY = "Clicking things by name works on Windows only (it uses Windows UI Automation)."

MAX_DEPTH, MAX_NODES, BUDGET_S = 14, 2500, 2.5  # a walk stops at whichever comes first
MATCH_AT, TIE = 0.6, 0.04  # the lowest score that counts, and how close two have to be to ask which one

# UIA control type ids -> names (UIAutomationClient.h)
CONTROL_TYPES = {50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit",
                 50005: "Hyperlink", 50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu", 50010: "MenuBar",
                 50011: "MenuItem", 50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar", 50015: "Slider",
                 50016: "Spinner", 50017: "StatusBar", 50018: "Tab", 50019: "TabItem", 50020: "Text", 50021: "ToolBar",
                 50022: "ToolTip", 50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb",
                 50028: "DataGrid", 50029: "DataItem", 50030: "Document", 50031: "SplitButton", 50032: "Window",
                 50033: "Pane", 50034: "Header", 50035: "HeaderItem", 50036: "Table", 50037: "TitleBar",
                 50038: "Separator", 50039: "SemanticZoom", 50040: "AppBar"}
CLICKABLE = {"Button", "SplitButton", "CheckBox", "RadioButton", "Hyperlink", "MenuItem", "TabItem", "ListItem",
             "TreeItem", "ComboBox", "DataItem", "HeaderItem"}
EDITABLE = {"Edit", "ComboBox", "Document"}
LEAVES = {"Button", "CheckBox", "RadioButton", "Hyperlink", "Edit", "Text", "Image", "Slider", "ScrollBar",
          "ProgressBar", "Separator", "Thumb"}  # never worth walking into
# Words J says for a kind of control -> the control types that count as it.
KIND_WORDS = {"button": {"Button", "SplitButton"}, "toggle": {"Button", "CheckBox"}, "switch": {"Button", "CheckBox"},
              "checkbox": {"CheckBox"}, "check box": {"CheckBox"}, "tick box": {"CheckBox"},
              "radio button": {"RadioButton"}, "search box": EDITABLE, "text box": EDITABLE, "box": EDITABLE,
              "field": EDITABLE, "search bar": EDITABLE, "tab": {"TabItem"}, "menu": {"MenuItem", "Menu"},
              "link": {"Hyperlink"}, "option": {"RadioButton", "ListItem", "MenuItem", "ComboBox", "CheckBox"},
              "item": {"ListItem", "TreeItem", "MenuItem", "DataItem"}, "dropdown": {"ComboBox"},
              "drop down": {"ComboBox"}, "slider": {"Slider"}, "setting": set()}
KIND_SAID = {"Button": "button", "SplitButton": "button", "CheckBox": "check box", "RadioButton": "option",
             "Hyperlink": "link", "MenuItem": "menu item", "TabItem": "tab", "ListItem": "item", "TreeItem": "item",
             "ComboBox": "drop-down", "Edit": "box", "Document": "box", "DataItem": "row", "Slider": "slider"}
ACTIONS = ("click", "on", "off", "toggle", "type", "select", "expand", "collapse")
TOGGLE_STATES = {0: "off", 1: "on", 2: "mixed"}


@dataclass
class Node:
    """One control, as read from the tree (``ref`` is the backend's own handle on it)."""

    name: str
    kind: str = "Custom"
    automation_id: str = ""
    rect: tuple = (0, 0, 0, 0)  # left, top, right, bottom (physical screen pixels)
    enabled: bool = True
    offscreen: bool = False
    password: bool = False
    patterns: frozenset = frozenset()  # invoke, toggle, value, selection, expand
    readonly: bool = False
    runtime_id: tuple = ()
    ref: Any = None
    depth: int = 0
    context: str = ""  # the nearest named container ("Voice Channels"), to tell two "Join" buttons apart

    @property
    def said(self) -> str:
        return KIND_SAID.get(self.kind, "control")

    def where(self) -> str:
        return f"{self.name} in {self.context}" if self.context and self.context.lower() != self.name.lower() else self.name


@dataclass
class Found:
    node: Node | None = None
    choices: list[Node] = field(default_factory=list)  # more than one fits: ask which
    problem: str = ""  # nothing to act on, and why


# ---- the walk ------------------------------------------------------------------------------------------------------

def walk(backend, root, *, max_depth: int = MAX_DEPTH, max_nodes: int = MAX_NODES, budget_s: float = BUDGET_S,
         clock: Callable[[], float] = time.monotonic) -> tuple[list[Node], bool]:
    """Every control under ``root``, breadth-first (the toolbar before the 400th row of a list). (nodes, cut short)."""
    out: list[Node] = []
    queue = deque([(root, 0, "")])
    end = clock() + budget_s
    while queue:
        if len(out) >= max_nodes or clock() > end:
            return out, True
        ref, depth, context = queue.popleft()
        try:
            kids = backend.children(ref)
        except Exception:  # a control that went away mid-walk, or an app that stopped answering
            log.debug("uia: children failed", exc_info=True)
            continue
        for n in kids:
            n.depth, n.context = depth + 1, context
            out.append(n)
            if depth + 1 < max_depth and n.kind not in LEAVES:
                queue.append((n.ref, depth + 1, n.name if n.name and n.kind not in CLICKABLE else context))
    return out, False


# ---- understanding what J said -------------------------------------------------------------------------------------

def norm(text: str) -> str:
    t = (text or "").lower().replace("&", "")
    t = re.sub(r"(?:\.\.\.|…)", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def parse_target(text: str) -> tuple[str, set[str]]:
    """"the Dark mode toggle" -> ("dark mode", {Button, CheckBox}); "Export" -> ("export", set())."""
    t = norm(text)
    t = re.sub(r"^(?:on |the |a |that |this )+", "", t)
    for word in sorted(KIND_WORDS, key=len, reverse=True):
        if t.endswith(" " + word) and len(t) > len(word) + 1:
            return t[:-len(word) - 1].removesuffix(" the").strip(), set(KIND_WORDS[word])
    return t, set()


def name_score(query: str, name: str) -> float:
    q, n = norm(query), norm(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    qw, nw = q.split(), n.split()
    extra = max(0, len(nw) - len(qw))
    if nw[:len(qw)] == qw:  # "export" -> "Export Media"
        return max(0.7, 0.9 - 0.03 * extra)
    if set(qw) <= set(nw):  # "dark mode" -> "Choose your mode: dark"
        return max(0.65, 0.85 - 0.03 * extra)
    if f" {q} " in f" {n} ":
        return 0.7
    return 0.8 * difflib.SequenceMatcher(None, q, n).ratio()


def can(node: Node, action: str) -> bool:
    """Whether this control can take that action at all."""
    p = node.patterns
    if action in ("on", "off", "toggle"):
        return "toggle" in p
    if action == "type":
        return ("value" in p and not node.readonly) or node.kind in EDITABLE
    if action in ("expand", "collapse"):
        return "expand" in p
    return bool(p & {"invoke", "toggle", "selection", "expand"}) or node.kind in CLICKABLE


def match(nodes: list[Node], target: str, action: str = "click", kinds: set[str] | None = None,
          quiet: bool = False) -> Found:
    """The control J means, or the few he might mean, or why there's none. ``quiet`` (live on stream): a reason names
    the control by what J said, never by the window's own text (a link, a page title, an email subject)."""
    query, said_kinds = parse_target(target)
    called = (lambda n: target) if quiet else (lambda n: n.name)
    kinds = kinds if kinds is not None else said_kinds
    if not query:
        return Found(problem="Which control? Say its name, like “click Export”.")
    scored = []
    for i, n in enumerate(nodes):
        s = name_score(query, n.name)
        if s < MATCH_AT - 0.2:
            continue
        if kinds:
            s += 0.05 if n.kind in kinds else -0.2
        if n.offscreen:
            s -= 0.1
        scored.append((s, i, n))
    scored.sort(key=lambda x: (-x[0], x[1]))
    fits = [x for x in scored if x[0] >= MATCH_AT]
    if not fits:
        near = [] if quiet else [n.name for s, _i, n in scored[:3] if s >= MATCH_AT - 0.1]
        return Found(problem=f"I can't see anything called “{target}” there." +
                     (f" Closest: {', '.join(near)}." if near else ""))
    if fits[0][2].password:
        return Found(problem="That's a password field, and I never touch those.")
    usable = [x for x in fits if can(x[2], action) and not x[2].password]
    if not usable:
        n = fits[0][2]
        if not n.enabled:
            return Found(problem=f"{called(n)} is greyed out right now.")
        return Found(problem=f"{called(n)} isn't something I can {_verb(action)}.")
    live = [x for x in usable if x[2].enabled]
    if not live:
        return Found(problem=f"{called(usable[0][2])} is greyed out right now.")
    best = live[0][0]
    close, seen = [], set()
    for s, _i, n in live:
        if s < best - TIE:
            break
        key = n.runtime_id or (n.name, n.kind, n.rect)
        if key not in seen:
            seen.add(key)
            close.append(n)
    if len(close) == 1:
        return Found(node=close[0])
    return Found(choices=close[:3])


def _verb(action: str) -> str:
    return {"on": "turn on", "off": "turn off", "type": "type into"}.get(action, action)


def listing(nodes: list[Node], limit: int = 60) -> list[dict]:
    """What there is to click, named and usable, once each (for "what can I click here")."""
    out, seen = [], set()
    for n in nodes:
        if not n.name.strip() or n.password or not n.enabled or not can(n, "click") and not can(n, "type"):
            continue
        key = (norm(n.name), n.kind)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": n.name.strip()[:60], "kind": n.said})
        if len(out) >= limit:
            break
    return out


# ---- acting ----------------------------------------------------------------------------------------------------------

def perform(backend, node: Node, action: str, text: str = "", typer: Callable[[str], bool] | None = None) -> dict:
    """Do it through a pattern, a mouse click at the centre only as the last resort. A tool-style result."""
    p = node.patterns
    if action in ("on", "off"):
        want = 1 if action == "on" else 0
        state = backend.toggle_state(node)
        if state == want:
            return {"ok": True, "already": True, "state": TOGGLE_STATES[want], "how": "toggle"}
        for _ in range(2):  # a three-state box goes off -> on -> mixed: at most two steps to land
            backend.toggle(node)
            state = backend.toggle_state(node)
            if state == want:
                break
        return {"ok": state == want, "state": TOGGLE_STATES.get(state, "?"), "how": "toggle",
                **({} if state == want else {"error": f"{node.name} wouldn't stay {action}."})}
    if action == "toggle":
        backend.toggle(node)
        return {"ok": True, "state": TOGGLE_STATES.get(backend.toggle_state(node), "?"), "how": "toggle"}
    if action == "type":
        if "value" in p and not node.readonly:
            backend.set_value(node, text)
            return {"ok": True, "how": "value"}
        if typer is None:
            return {"ok": False, "error": f"I can't type into {node.name}."}
        backend.focus(node)
        ok = bool(typer(text))
        return {"ok": ok, "how": "typed",
                **({} if ok else {"error": "Windows blocked the typing (an app running as administrator?)."})}
    if action in ("expand", "collapse"):
        backend.expand(node, action == "expand")
        return {"ok": True, "how": "expand"}
    if action == "select" and "selection" in p:
        backend.select(node)
        return {"ok": True, "how": "select"}
    for pattern, fn in (("invoke", backend.invoke), ("toggle", backend.toggle), ("selection", backend.select)):
        if pattern in p:
            fn(node)
            return {"ok": True, "how": pattern}
    if "expand" in p:
        backend.expand(node, backend.expand_state(node) == 0)
        return {"ok": True, "how": "expand"}
    return click_centre(backend, node)


def click_centre(backend, node: Node) -> dict:
    left, top, right, bottom = node.rect
    if right <= left or bottom <= top or node.offscreen:
        return {"ok": False, "error": f"{node.name} isn't on screen, so I can't click it."}
    x, y = (left + right) // 2, (top + bottom) // 2
    if node.runtime_id and node.runtime_id not in backend.at_point(x, y):
        return {"ok": False, "error": f"Something is covering {node.name}, so I didn't click blind."}
    backend.click(x, y)
    return {"ok": True, "how": "click"}


FRAME_HOSTS = {"applicationframehost", "systemsettings", ""}  # Store apps' windows: the title names the app


def app_name(window) -> str:
    """"premiere pro", "discord"; "Settings" for a Store app (whose process is just the frame around it)."""
    app = getattr(window, "app", "") or ""
    return (getattr(window, "title", "") or app)[:40] if app in FRAME_HOSTS else app


def pin(node: Node, window) -> dict:
    """What a yes is for: the exact control, checked again before acting."""
    return {"hwnd": int(getattr(window, "handle", 0) or 0), "app": app_name(window), "name": node.name,
            "kind": node.kind, "runtime_id": list(node.runtime_id), "rect": list(node.rect), "context": node.context}


def refind(backend, target: dict) -> Node | None:
    """The pinned control again, if it's still there with the same name and kind."""
    rid = tuple(target.get("runtime_id") or ())
    root = backend.root(int(target.get("hwnd") or 0))
    if root is None:
        return None
    node = backend.find(root, rid, tuple(target.get("rect") or ())) if rid else None
    if node is None:
        nodes, _cut = walk(backend, root)
        node = next((n for n in nodes if rid and tuple(n.runtime_id) == rid), None)
    if node is None or node.name != target.get("name") or node.kind != target.get("kind"):
        return None
    return node


# ---- Windows: UI Automation over COM ---------------------------------------------------------------------------------

PROPS = {"name": 30005, "kind": 30003, "automation_id": 30011, "rect": 30001, "enabled": 30010, "offscreen": 30022,
         "password": 30019, "runtime_id": 30000, "invoke": 30031, "expand": 30028, "selection": 30036,
         "toggle": 30041, "value": 30043, "readonly": 30046}
PATTERN_IDS = {"invoke": 10000, "value": 10002, "expand": 10005, "selection": 10010, "toggle": 10015}
TREE_SCOPE_CHILDREN = 0x2


class ComBackend:  # pragma: no cover - Windows only (the Windows CI job imports it; real apps are checked by hand)
    """UIA through comtypes. One client per thread: COM objects don't cross threads."""

    def __init__(self, connection_ms: int = 1500, transaction_ms: int = 3000):
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U

        self.U, self.timeouts = U, (connection_ms, transaction_ms)
        self._local = threading.local()

    def _client(self):
        c = getattr(self._local, "client", None)
        if c is not None:
            return c
        import comtypes
        import comtypes.client

        try:
            comtypes.CoInitialize()
        except OSError:
            pass  # already initialised on this thread
        U = self.U
        try:
            c = comtypes.client.CreateObject(U.CUIAutomation8, interface=U.IUIAutomation2)
            c.ConnectionTimeout, c.TransactionTimeout = self.timeouts  # a hung app answers "timeout", not never
        except Exception:
            c = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
        cache = c.CreateCacheRequest()
        for pid in PROPS.values():
            cache.AddProperty(pid)
        self._local.client, self._local.cache = c, cache
        self._local.cond = c.ControlViewCondition
        return c

    def _node(self, el) -> Node:
        def get(key):
            return el.GetCachedPropertyValue(PROPS[key])
        r = el.CachedBoundingRectangle
        return Node(name=str(el.CachedName or ""), kind=CONTROL_TYPES.get(el.CachedControlType, "Custom"),
                    automation_id=str(get("automation_id") or ""), rect=(r.left, r.top, r.right, r.bottom),
                    enabled=bool(get("enabled")), offscreen=bool(get("offscreen")), password=bool(get("password")),
                    patterns=frozenset(k for k in PATTERN_IDS if get(k)), readonly=bool(get("readonly")),
                    runtime_id=tuple(get("runtime_id") or ()), ref=el)

    def root(self, hwnd: int):
        try:
            return self._client().ElementFromHandle(hwnd) if hwnd else None
        except Exception:
            return None

    def _point(self, x: int, y: int):
        """A POINT of the type comtypes generated for UIA (ctypes refuses wintypes.POINT in its place)."""
        return self.U.tagPOINT(int(x), int(y))

    def children(self, ref) -> list[Node]:
        self._client()
        found = ref.FindAllBuildCache(TREE_SCOPE_CHILDREN, self._local.cond, self._local.cache)
        return [self._node(found.GetElement(i)) for i in range(found.Length)] if found else []

    def find(self, root, runtime_id: tuple, rect: tuple = ()) -> Node | None:
        """The control with that runtime id, at the middle of where it was (one call). Not there (J went back to his
        game, the HUD covers it, the window moved): None, and refind() walks the window within its time budget. No
        FindFirst over all descendants: that's many calls inside UIA with no cap overall, on a huge tree (a web page,
        Premiere) for many seconds, and the yes waits on it."""
        c = self._client()
        if len(rect) == 4:
            try:
                el = c.ElementFromPointBuildCache(self._point((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2),
                                                  self._local.cache)
                if el and tuple(el.GetCachedPropertyValue(PROPS["runtime_id"]) or ()) == tuple(runtime_id):
                    return self._node(el)
            except Exception:
                pass
        return None

    def at_point(self, x: int, y: int) -> list[tuple]:
        """The runtime ids of the control under that point and a few of its parents."""
        c = self._client()
        out, walker = [], c.ControlViewWalker
        try:
            el = c.ElementFromPoint(self._point(x, y))
            for _ in range(4):
                if not el:
                    break
                out.append(tuple(el.GetRuntimeId() or ()))
                el = walker.GetParentElement(el)
        except Exception:
            pass
        return out

    def _pattern(self, node: Node, name: str, iface: str):
        return node.ref.GetCurrentPattern(PATTERN_IDS[name]).QueryInterface(getattr(self.U, iface))

    def invoke(self, node: Node) -> None:
        self._pattern(node, "invoke", "IUIAutomationInvokePattern").Invoke()

    def toggle(self, node: Node) -> None:
        self._pattern(node, "toggle", "IUIAutomationTogglePattern").Toggle()

    def toggle_state(self, node: Node) -> int:
        return int(self._pattern(node, "toggle", "IUIAutomationTogglePattern").CurrentToggleState)

    def set_value(self, node: Node, text: str) -> None:
        self._pattern(node, "value", "IUIAutomationValuePattern").SetValue(text)

    def value(self, node: Node) -> str:
        return str(self._pattern(node, "value", "IUIAutomationValuePattern").CurrentValue or "")

    def select(self, node: Node) -> None:
        self._pattern(node, "selection", "IUIAutomationSelectionItemPattern").Select()

    def expand(self, node: Node, open_it: bool) -> None:
        p = self._pattern(node, "expand", "IUIAutomationExpandCollapsePattern")
        p.Expand() if open_it else p.Collapse()

    def expand_state(self, node: Node) -> int:
        return int(self._pattern(node, "expand", "IUIAutomationExpandCollapsePattern").CurrentExpandCollapseState)

    def focus(self, node: Node) -> None:
        node.ref.SetFocus()

    def click(self, x: int, y: int) -> None:
        from .textio import INPUT, MOUSEINPUT, _INPUTUNION

        u = ctypes.WinDLL("user32", use_last_error=True)
        old = None
        try:  # UIA's rectangles are physical pixels: click in physical pixels too, whatever the display scaling
            u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            old = u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except AttributeError:
            pass
        try:
            u.SetCursorPos(int(x), int(y))
            down, up = 0x0002, 0x0004  # MOUSEEVENTF_LEFTDOWN / LEFTUP
            arr = (INPUT * 2)(*[INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(dwFlags=f))) for f in (down, up)])
            u.SendInput(2, arr, ctypes.sizeof(INPUT))
        finally:
            if old:
                u.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))


_backend: Any = None
_backend_lock = threading.Lock()


def default_backend():
    """The UIA backend on Windows (made once), None anywhere else or when UIA can't start."""
    global _backend
    if not IS_WINDOWS:
        return None
    with _backend_lock:
        if _backend is None:
            try:  # pragma: no cover - Windows only
                _backend = ComBackend()
            except Exception as exc:  # pragma: no cover
                log.warning("UI Automation unavailable: %s", exc)
                _backend = False
    return _backend or None  # pragma: no cover


def focused_is_password() -> bool | None:  # pragma: no cover - Windows only
    """UIA's IsPassword on the focused control (textio's password guard asks this too)."""
    b = default_backend()
    if b is None:
        return None
    try:
        el = b._client().GetFocusedElement()
        return bool(el.CurrentIsPassword) if el else None
    except Exception:
        return None
