"""Click anything by name: UI Automation matching, the tier-2 yes pinned to the exact control, "which one?", the
password / HUD / sign-in refusals, the capped tree walk, patterns before mouse clicks, and undoing a switch. A fake UIA
tree stands in for Windows (the COM side, ComBackend, is checked by hand on J's PC)."""

import itertools
from types import SimpleNamespace

import pytest

from assistant.brain.assistant import Assistant
from assistant.integrations import uia
from assistant.integrations.desktop import WindowInfo
from assistant.skills.click import Click

_ids = itertools.count(1)


def ctl(name, kind="Button", patterns=("invoke",), kids=(), **kw):
    n = uia.Node(name=name, kind=kind, patterns=frozenset(patterns), runtime_id=(next(_ids),),
                 rect=kw.pop("rect", (10, 10, 50, 30)), **kw)
    n.ref, n.kids, n.toggled, n.text = n, list(kids), 0, ""
    return n


class FakeUIA:
    """The UIA backend's methods over a tree of Nodes: records what was done to which control."""

    def __init__(self, roots):
        self.roots, self.done, self.calls, self.covered = roots, [], 0, set()

    def root(self, hwnd):
        return self.roots.get(hwnd)

    def children(self, ref):
        self.calls += 1
        return list(ref.kids)

    def _all(self, n):
        for k in n.kids:
            yield k
            yield from self._all(k)

    def find(self, root, rid, rect=()):
        return next((n for n in self._all(root) if n.runtime_id == tuple(rid)), None)

    def at_point(self, x, y):
        """The ids under the point: the control itself, unless something covers it."""
        return [] if self.covered else [(i,) for i in range(1, next(_ids))]

    def invoke(self, n):
        self.done.append(("invoke", n.name))

    def toggle(self, n):
        n.toggled = 1 - n.toggled
        self.done.append(("toggle", n.name))

    def toggle_state(self, n):
        return n.toggled

    def set_value(self, n, text):
        n.text = text
        self.done.append(("value", n.name, text))

    def value(self, n):
        return n.text

    def select(self, n):
        self.done.append(("select", n.name))

    def expand(self, n, open_it):
        self.done.append(("expand" if open_it else "collapse", n.name))

    def expand_state(self, n):
        return 0

    def focus(self, n):
        self.done.append(("focus", n.name))

    def click(self, x, y):
        self.done.append(("click", x, y))


PREMIERE = WindowInfo("Untitled - Adobe Premiere Pro 2025", "adobe premiere pro", 11, 100)
DISCORD = WindowInfo("#general - Discord", "discord", 12, 200)
SETTINGS = WindowInfo("Settings", "systemsettings", 13, 300)
HUD = WindowInfo("Friday HUD", "msedge", 14, 400)
BITWARDEN = WindowInfo("Bitwarden", "bitwarden", 15, 500)


@pytest.fixture
def world():
    dark = ctl("Dark mode", "Button", ("toggle",))
    roots = {
        100: ctl("Premiere", "Window", (), [ctl("File", "MenuItem", ("expand",)), ctl("Export", "Button"),
                                             ctl("Export As", "Button"), ctl("Password", "Edit", ("value",), password=True)]),
        200: ctl("Discord", "Window", (), [
            ctl("General", "Group", (), [ctl("Join", "Button")]),
            ctl("Gaming", "Group", (), [ctl("Join", "Button")]),
            ctl("Music", "Group", (), [ctl("Join", "Button")]),
            ctl("Stage", "Group", (), [ctl("Join", "Button")]),
            ctl("Search", "Edit", ("value",)), ctl("Mute", "Button", ("toggle",)), ctl("Avatar", "Image", ())]),
        300: ctl("Settings", "Window", (), [dark, ctl("Bluetooth", "Button", ("toggle",), enabled=False),
                                            ctl("Open site", "Hyperlink", ())]),
        400: ctl("HUD", "Window", (), [ctl("Confirm", "Button")]),
        500: ctl("Bitwarden", "Window", (), [ctl("Unlock", "Button")]),
    }
    return SimpleNamespace(uia=FakeUIA(roots), windows=[DISCORD, PREMIERE, SETTINGS, BITWARDEN], front=[DISCORD],
                           dark=dark, typed=[])


@pytest.fixture
def click(svc, world):
    sk = Click(svc, backend=world.uia, windows=lambda: [HUD, *world.windows], front=lambda: world.front[0],
               typer=lambda text: world.typed.append(text) or True)
    svc.skills.all["click"] = sk
    return sk


@pytest.fixture
def vesper(svc, click):
    a = Assistant(svc)
    svc.skills["undo"].attach(a.tools.history)
    click.runtime = SimpleNamespace(assistant=a)
    return a


# ---- matching (pure) ------------------------------------------------------------------------------------------------

def test_names_match_exactly_first_then_by_words_then_fuzzily():
    assert uia.name_score("export", "Export") == 1.0
    assert uia.name_score("export", "Export...") == 1.0  # an ellipsis and an accelerator don't count
    assert uia.name_score("save as", "&Save As") == 1.0
    assert uia.name_score("export", "Export Media") > uia.name_score("export", "Quick Export Settings") > 0.6
    assert uia.name_score("exprot", "Export") > 0.6  # a misheard word still lands
    assert uia.name_score("export", "Import") < 0.6
    assert uia.parse_target("the Dark mode toggle") == ("dark mode", {"Button", "CheckBox"})
    assert uia.parse_target("search box")[0] == "search"
    assert uia.parse_target("Export") == ("export", set())


def test_the_best_match_wins_and_a_near_tie_asks_which(world):
    nodes, cut = uia.walk(world.uia, world.uia.roots[100])
    assert not cut
    assert uia.match(nodes, "export").node.name == "Export"  # "Export As" is close, not tied
    joins, _ = uia.walk(world.uia, world.uia.roots[200])
    found = uia.match(joins, "join")
    assert found.node is None and len(found.choices) == 3  # four fit: three are offered
    assert [n.where() for n in found.choices] == ["Join in General", "Join in Gaming", "Join in Music"]


def test_it_never_offers_a_password_field_and_says_why_it_cant(world):
    nodes, _ = uia.walk(world.uia, world.uia.roots[100])
    assert "password field" in uia.match(nodes, "password", "type").problem
    settings, _ = uia.walk(world.uia, world.uia.roots[300])
    assert "greyed out" in uia.match(settings, "bluetooth", "on").problem
    assert "isn't something I can turn on" in uia.match(settings, "open site", "on").problem
    assert uia.match(settings, "frobnicate").problem == "I can't see anything called “frobnicate” there."


def test_the_walk_is_capped_by_depth_count_and_time():
    class Deep(FakeUIA):
        def children(self, ref):
            self.calls += 1
            return [ctl(f"Row {self.calls}-{i}", "Group", ()) for i in range(10)]  # endless
    deep = Deep({})
    nodes, cut = uia.walk(deep, ctl("root", "Window", ()), max_nodes=200)
    assert cut and len(nodes) <= 210
    ticks = itertools.count()
    nodes, cut = uia.walk(Deep({}), ctl("root", "Window", ()), budget_s=5, clock=lambda: next(ticks))
    assert cut and len(nodes) < 100  # the clock ran out, whatever the tree held
    nodes, cut = uia.walk(Deep({}), ctl("root", "Window", ()), max_depth=2, max_nodes=10_000)
    assert not cut and max(n.depth for n in nodes) == 2


def test_patterns_come_first_and_a_mouse_click_only_on_a_bare_control(world):
    b = world.uia
    assert uia.perform(b, ctl("Export"), "click")["how"] == "invoke"
    assert uia.perform(b, ctl("Mute", patterns=("toggle",)), "click")["how"] == "toggle"
    assert uia.perform(b, ctl("Tab", "TabItem", ("selection",)), "click")["how"] == "selection"
    assert uia.perform(b, ctl("File", "MenuItem", ("expand",)), "click")["how"] == "expand"
    bare = ctl("Canvas", "Custom", ())
    assert uia.perform(b, bare, "click") == {"ok": True, "how": "click"} and b.done[-1] == ("click", 30, 20)
    b.covered.add(1)  # something on top of it: no blind click
    assert "covering" in uia.perform(b, bare, "click")["error"]
    assert "isn't on screen" in uia.perform(b, ctl("Gone", "Custom", (), offscreen=True), "click")["error"]


def test_on_and_off_land_on_the_state_asked_for(world):
    b, sw = world.uia, ctl("Wi-Fi", "Button", ("toggle",))
    assert uia.perform(b, sw, "on") == {"ok": True, "state": "on", "how": "toggle"}
    assert uia.perform(b, sw, "on")["already"] is True
    assert uia.perform(b, sw, "off")["state"] == "off"


# ---- the skill: tiers, the pin, refusals ------------------------------------------------------------------------------

def test_clicking_in_another_app_waits_for_a_yes_then_presses_that_exact_button(vesper, world):
    out = vesper.handle("click export in premiere")
    assert out["kind"] == "pending" and "Click the Export button in adobe premiere pro" in out["reply"]
    assert world.uia.done == []  # nothing pressed before the yes
    vesper.handle("yes")
    assert world.uia.done == [("invoke", "Export")]


def test_the_yes_acts_on_the_control_found_when_asked_never_a_new_one(vesper, world):
    vesper.handle("click export in premiere")
    root = world.uia.roots[100]
    old = next(k for k in root.kids if k.name == "Export")
    root.kids = [k for k in root.kids if k is not old] + [ctl("Export", "Button")]  # redrawn: a different control
    out = vesper.handle("yes")
    assert "isn't there any more" in out["reply"] and world.uia.done == []


def test_which_one_lists_three_and_the_answer_still_needs_a_yes(vesper, world):
    out = vesper.handle("press join")
    assert out["reply"] == "Which one: Join in General, Join in Gaming, or Join in Music?"
    out = vesper.handle("the second one")
    assert out["kind"] == "pending" and "Click the Join button under Gaming in discord?" in out["reply"]
    vesper.handle("yes")
    gaming = next(k for k in world.uia.roots[200].kids if k.name == "Gaming").kids[0]
    assert world.uia.done == [("invoke", "Join")] and gaming.runtime_id  # the one J picked


def test_never_in_a_password_app_the_hud_or_a_password_field(vesper, world, click):
    world.front[0] = BITWARDEN
    assert "password app" in vesper.handle("click unlock")["reply"]
    world.front[0] = HUD  # typed into the HUD: the window behind it, never the HUD itself
    a = {"target": "confirm", "action": "click"}
    click._resolve(a)
    assert "Confirm" not in str(a.get("_target")) and a.get("_problem")
    assert "password field" in vesper.handle("type hunter2 into the password box in premiere")["reply"]
    assert world.uia.done == []


def test_turning_a_switch_on_and_typing_into_a_box(vesper, world):
    world.front[0] = SETTINGS
    assert vesper.handle("turn on the dark mode toggle")["kind"] == "pending"
    assert vesper.handle("yes")["reply"] == "Dark mode is on."
    world.front[0] = DISCORD
    out = vesper.handle("type Lo-fi beats into the search box")
    assert "Type “Lo-fi beats” into the Search box in discord?" in out["reply"]
    vesper.handle("yes")
    assert ("value", "Search", "Lo-fi beats") in world.uia.done


def test_what_can_i_click_lists_the_controls_without_a_yes(vesper, world, click):
    out = vesper.handle("what can i click here")
    assert out["kind"] == "tool" and "In discord I can see" in out["reply"]
    names = [c["name"] for c in click.found["controls"]]
    assert "Join" in names and names.count("Join") == 1 and "Avatar" not in names  # an image isn't clickable
    assert vesper.tools.tools["ui_list"].untrusted  # names can be other people's words (a chat message)


def test_live_on_stream_the_list_stays_off_the_hud_and_out_loud(vesper, svc, click):
    svc.live = lambda: True
    out = vesper.handle("what can i click here")
    assert out["reply"].startswith("I can see ") and out["reply"].endswith(" things to click in discord.")
    assert click.status()["found"]["controls"] == [] and click.status()["found"]["hidden"] > 0


def test_an_unknown_app_or_control_just_says_so(vesper, world):
    assert "can't see anything called" in vesper.handle("click the frobnicate button")["reply"]
    assert vesper.handle("click export in blender")["kind"] != "pending"


def test_undo_puts_a_switch_back_and_asks_first(vesper, world):
    world.front[0] = SETTINGS
    vesper.handle("turn on the dark mode toggle")
    vesper.handle("yes")
    assert world.dark.toggled == 1
    out = vesper.handle("undo that")
    assert out["kind"] == "pending" and "Dark mode back to off" in out["reply"]  # another app's switch: tier 2 again
    vesper.handle("yes")
    assert world.dark.toggled == 0


def test_a_click_is_listed_as_one_that_cant_be_undone(vesper, world):
    vesper.handle("click export in premiere")
    vesper.handle("yes")
    out = vesper.handle("undo that")
    assert "can't undo click the Export button" in out["reply"] and world.uia.done == [("invoke", "Export")]


def test_elsewhere_than_windows_it_says_windows_only(svc):
    sk = Click(svc, backend=None)
    sk.backend = None
    assert sk.list_controls() == {"ok": False, "error": uia.WINDOWS_ONLY}
    a = {"target": "export"}
    assert sk._tier(a) == 0 and sk.control(**a)["error"] == uia.WINDOWS_ONLY
    assert sk.selftest()[0]["status"] in ("skip", "fail")
