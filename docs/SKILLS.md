# Writing a Vesper skill

A skill is one feature in one file: `assistant/skills/<name>.py`, with its HUD side in
`assistant/web/skills/<name>.js` (+ optional `<name>.css`) and tests in `tests/test_skill_<name>.py`.
It is listed in `MODULES` in `assistant/skills/__init__.py` (order = order on the HUD and in the briefing).
Look at `weather.py`, `nudges.py` and `automations.py` as worked examples.

## The Python side

```python
from ..brain.router import Intent
from ..brain.tools import Tool, obj
from . import Skill

class Thing(Skill):
    name, title = "thing", "Thing"      # name = bus event, HUD panel, /api/skill/<name>
    poll_s = 300                         # tick() every 5 min (0 = never); delay_s before the first one

    def tools(self):                     # what the model may call (and the fast path routes to)
        return [Tool("thing_status", "When to use it, in one or two sentences.", obj({"q": {"type": "string"}}),
                     self._status, tier=0)]   # tier: 0 read, 1 harmless change, 2+ needs the user's yes
    def route(self, t):                  # t: cleaned lower-case utterance; anchored regexes only
        if re.fullmatch(r"how'?s (?:the )?thing", t):
            return Intent("tool", "thing_status", {})
    def say(self, tool, args, result):   # the spoken reply on the fast path (short, no lists, no URLs)
        return f"Thing is {result['state']}."
    def status(self):                    # the HUD panel's data (JSON); publish() sends it
        return {...}
    def actions(self):                   # HUD buttons -> POST /api/skill/thing/<action> {json}
        return {"reset": lambda body: {"ok": True}}
    def briefing(self):                  # a small JSON-able bit for the morning briefing, or None
        return None
    def tick(self):                      # default: self.publish()
        ...
    def start(self, runtime):            # once at startup, in the background; runtime.command(text) runs a command
        ...
    def selftest(self):                  # does the outside connection work now? (health check + diagnostics)
        return [check("Service", "pass", "answers")]   # from . import check; status pass/warn/fail/skip
```

A skill whose tick, route or `say` raises 5 times in a row is switched off for the session (a notice says why;
Setup → Skills turns it back on). Make the http client with `lazy_client(timeout=10)` (from `.`), not
`httpx.Client(...)`: building clients in `__init__` slowed startup. `selftest` must be quick, change nothing, and
never put a secret or a message's text in `detail`.

What a skill can use (`self.svc`): `cfg` (+ `self.setting(key, default)` for `skills.<name>.<key>`), `bus`, `storage`
(SQLite: `storage.execute/query`; create your own tables with `CREATE TABLE IF NOT EXISTS <name>_...`), `tz`,
`calendars.agenda(days)` (events with `attendees`), `gmail` (`needs_you()`, `search(q)`, `read(id)`, `thread(id)`,
`sent(days)`, `accounts()`), `google`, `library` (`search(q)`), `activity.segments(start, end)`, `obs`, `twitch`,
`system`, `jobs`, `live()` (streaming now), `speak(text)`, `think(prompt, schema=None)` (ask the model; returns a dict
for a schema, text otherwise, None with no model: always have a no-model fallback), and `self.nudge(text,
priority="urgent"|"normal"|"low", key=..., expires_in=...)` to speak up at the right moment (never on air).
Save files under `svc.cfg.data_dir`. Network calls go through `httpx` with a timeout, and the client is injectable
(`__init__(self, svc, http=None)`) so tests can use `httpx.MockTransport`.

Connecting to an outside service: a sign-in page that sends the browser back to Vesper (an OAuth redirect) lands on
`GET /api/skill/<name>/callback`, the one skill route that needs no token header (guard it with a single-use state),
and calls the skill's `callback(params) -> (ok, title, line)` for the page the browser shows. `skills/_kit.py` has
`pkce()`, `callback_url(svc, name)`, `save_secret(svc, NAME, value)` (a pasted key → .env), `save_setting(skill, key,
value)` and `LiveEdge(svc)` ("start"/"end" when the stream just went live or stopped). See `spotify.py`.

Rules:
- **Untrusted text** (email, web pages, chat, documents, clipboard, screen text): tools that return it set
  `untrusted=True, untrusted_label="an email", untrusted_note=...` so any action after reading it needs a yes.
- **On air**: when `svc.live()`, never put private details in spoken replies (`say` should give the gist and point
  at the HUD).
- **Never break Vesper**: no network at import or in `__init__`; catch errors in `tick` and show them in `status`.
- Secrets live in `.env` (read with `self.svc.cfg.secret("NAME")`), never in config.yaml or logs.
- Skills must work on Windows (the real target) and import fine on Linux CI (guard `ctypes.windll`, `winreg`).

## The HUD side

```js
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast, status, meter } = Vesper.ui;
  Vesper.skill('thing', {
    panels: [{ view: 'work', span: 4, title: 'Thing', after: 'library' }],   // views: command, work, stream, setup
    render(d) { for (const p of panels('thing')) { fill(body(p), ...); meta(p, '...'); } },
  });
})();
```
`Vesper.act('thing', 'reset', {...})` calls an action. `Vesper.data` is the HUD state. Build DOM with `h()` only
(never innerHTML with data). `toast(text)` also takes `{ level: 'good'|'warn'|'error', source: 'thing' }`; notices a
skill sends (`self.notify(...)`) land in the bell's notification center and in `Vesper.notices`.

Colours come from CSS variables, and each accent means one thing: `--ember` (Vesper itself, primary action; `--accent`
is an alias), `--ion` (listening / J's input), `--iris` (thinking, tools, "AI did this"), `--phosphor` (healthy),
`--amber` (needs J), `--alert` (error, on air); neutrals `--ink`, `--ink-2`, `--muted`, `--line`, `--panel`, `--inset`;
`--mono` for numbers and readouts. Charts use `--series-1..8`, never the accents. Animate only `transform`/`opacity`
and wrap loops in `:root[data-motion="full"]` (the Setup → Display setting, which also covers reduced motion).

## Tests

`tests/test_skill_<name>.py`: use the `svc`/`cfg` fixtures from `tests/conftest.py`, fake every network call, and
cover the routes (`route(text, RouterContext(skill_route=Skills(svc, ["thing"]).route))`), the tools, `say`, and
the edge cases. `pytest -q` and `ruff check assistant tests` must pass; `node --check` the JS.
