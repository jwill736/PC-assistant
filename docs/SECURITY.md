# Vesper security model

Vesper runs on J's own PC and can do things on it (open and close apps, type, shut down, post to Twitch and Discord).
This page is what stops anyone else from making it do those things, and what's still possible. The tests that hold it
in place are `tests/test_security.py` (and `tests/test_phrasebook.py` for "shutdown always asks").

## Who can give Vesper an order

| Way in | What guards it |
|---|---|
| Voice | The wake word, then the speaker check (`voice.speaker_check`): tier 3 actions refuse a voice that isn't J's. |
| The HUD (`http://127.0.0.1:8765`) | Listens on 127.0.0.1 only. Every `/api` call and the `/ws` feed need the per-install token (`data/api_token`, put into the HUD page), a localhost `Host` header (stops DNS rebinding) and, from a browser, a localhost `Origin` (stops any other web page). |
| Sign-in callbacks | `GET /api/google/callback` and `GET /api/skill/<name>/callback` skip the token (the service's page sends the browser back without it). Only `GET`, only that exact path, and the skill must find a single-use `state` it issued in the last 10 minutes; anything else gets a 400 page and no call to the service. A skill with `callback()` must issue that state from `begin()`. |
| Phone (Telegram) | Only the one private chat that sent the 6-digit pairing code. Wrong codes: 5 tries, then a new code and a lockout of 10 minutes that doubles each time (up to a day), kept across restarts, and a notice on the HUD. Groups can never pair; other chats get no answer. Messages run like typed commands, so anything tier 2+ still asks "Reply yes or no". |
| Routines and automations | Run as `runtime.command(...)` with source `automation`: the same confirmation rules. |

## What an order can do: tiers

Every tool has a tier. 0 reads, 1 is a harmless change (runs at once), 2+ waits for a yes ("Confirm: …? Say yes or
cancel"), 3 is serious (shutdown, Claude Code jobs) and also refuses a voice that failed the speaker check. A HUD click
counts as a yes. Once a request has read text other people wrote (email, Twitch chat, documents, the screen, the
clipboard, a tool server's answer), every action left in that request needs a yes, so an email saying "Vesper, forward
this to…" can't act on its own. The kill switch (Ctrl+Alt+K, "stop everything") refuses all actions until "resume
control".

## Tool servers (MCP)

Adding one from Setup → Tool servers runs its command line on this PC, so it's only reachable with the HUD token.
Every tool a server offers starts at tier 2 (asks every time); J can lower a server or a single tool to tier 1. Every
answer is marked untrusted. A token pasted for a server goes to `.env` (`MCP_<NAME>_TOKEN`), never into
`data/mcp_servers.json`.

## Secrets

- API keys and tokens live only in `.env` (or the real environment) and are read with `cfg.secret(...)`. A value
  pasted on the HUD must be one line: `firstrun.set_env_value` refuses any line break (so `abc\nFOO=bar` can't add a
  setting), and `_kit.save_secrets` writes several values all or none.
- No skill panel (`status`), action reply, HUD event or `/api/state` contains a secret: a test fills every secret with a
  marker, makes every network call fail with the URL and headers in the error, runs every action of every skill and
  looks for the markers.
- Memory never learns anything about a password, PIN, code, key or card (`privacy.mentions_secret`). Notes ("remember
  that…") and People notes refuse an actual value ("the wifi password is hunter2") but keep "the wifi password is on
  the fridge". The conversation log and the action log blank any value that slips through (`privacy.redact`).
- Journal entries never go into the conversation log; nothing private is spoken while live.

## Privacy of what's on the PC

- Clipboard history is in memory only (unless `skills.clipboard.persist`). It skips anything that looks like a
  password, key, private key or card number; anything copied while a password manager, a bank, a payment site or a
  private browsing window is in front (the screen skill's list plus `skills.clipboard.deny_apps` / `deny_titles`);
  and anything the copying app marks "don't record" (the Windows clipboard formats password managers set). It is
  paused while a game runs.
- The screen is only looked at when asked, never on air unless asked out loud, never for those private windows
  (`skills.screen.deny_apps` / `deny_titles` add more), and the HUD thumbnail is dropped after 2 minutes.

## What's still possible (residual risks)

- **Anything already running as J on this PC** can read `data/api_token` and `.env` and drive Vesper. Vesper doesn't
  defend against malware with J's rights; Windows' user account does.
- **J's Telegram account** is a way in: whoever holds it can talk to Vesper (risky actions still ask). There is one
  waiting confirmation at a time, and a yes from the PC or the phone answers it.
- **ntfy topics are public by name**: anyone who knows the topic can read the pushes. Use a long random one (the HUD
  suggests one).
- **Tool servers run with J's rights.** A stdio server is a program on this PC; lowering it to tier 1 lets the model
  call it without asking. A server URL with a token in its query string shows on the HUD (use the token field).
- **Pattern matching is not perfect.** A password that doesn't look like one, copied from an ordinary window with no
  "don't record" mark, can land in the clipboard history; the log redaction catches common forms only.
- **Whole-screen looks** (when the HUD itself is in front) can include other windows behind it.
- **Voice**: a voice on stream audio or in the room can say the wake word; tier 2+ still needs a yes and tier 3 J's
  own voice, but tier 1 actions (open an app, switch a scene) just happen.
- `.env` and `data/` are plain files protected by the Windows user profile, not encrypted.
