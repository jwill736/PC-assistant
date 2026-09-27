"""Static checks on the HUD script (it has no build step, so nothing else catches these)."""

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "assistant" / "web" / "app.js"

# Non-configurable own properties of `window` in Chrome/Edge. A top-level
# const/let/class with one of these names is a SyntaxError that stops the whole
# script — the HUD then sits on "connecting…" forever. (Headless test shells
# lack window.chrome, which is how `const chrome` once slipped through.)
RESERVED = {"chrome", "document", "location", "top", "window"}


def test_no_top_level_names_that_clash_with_browser_globals():
    declared = re.findall(r"^(?:const|let|var|class|function|async function)\s+([A-Za-z_$][\w$]*)",
                          APP_JS.read_text(encoding="utf-8"), flags=re.M)
    assert declared, "parser found no declarations — did app.js move?"
    assert sorted(set(declared) & RESERVED) == []


def test_stylesheet_assets_are_bundled():
    """The HUD must work offline: every url() in the stylesheet is a file in the repo."""
    css = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")
    urls = re.findall(r'url\("?([^")]+)"?\)', css)
    assert urls, "expected bundled fonts"
    for url in urls:
        assert not url.startswith(("http:", "https:", "//")), url
        assert (APP_JS.parent / url).is_file(), url
