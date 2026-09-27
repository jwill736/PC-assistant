# PC Assistant — voice control + HUD for your PC

A local, voice-driven assistant for a Windows PC, in the spirit of JARVIS. Say its
name, then tell it what you need: it opens programs and Chrome tabs, tells you
where you are on the machine, switches OBS scenes, runs your "work mode" and "stream
mode" setups, reads all of your calendars, tracks where your day went, and plans
your morning around the goal you actually care about. Anything bigger ("research
X", "have Claude add tests to my repo") runs in the background while you keep working.

The HUD dashboard has three views: **Command** (everything at once), **Work** and
**Stream**.

> Name it whatever you want: `assistant.name` in `config.yaml` is both what it
> calls itself and the wake word. It ships as "Jarvis".

## What works today

| Area | What it does |
|---|---|
| Voice | Local speech-to-text (Whisper, on your PC), any wake word, follow-up window, push-to-talk hotkey (`Ctrl+Alt+J`), spoken replies |
| Apps & windows | Open any installed program or game by name (Start Menu and Desktop shortcuts are found automatically), close apps (after you confirm), bring windows to the front, "where am I" |
| Chrome | Open sites, several tabs at once, and Google/YouTube/GitHub/Twitch searches |
| OBS | Switch scenes (fuzzy-matched), go live or end the stream (after you confirm), record, clip the replay buffer, mute sources, live bitrate, dropped frames, FPS |
| Calendars | Any number of Google, Outlook or iCloud calendars merged into one agenda; free blocks; next-meeting countdown |
| Day tracking | Samples the active window every 5 s and sorts it into work, stream, other or idle. Shows hours per category, top apps, deep-work sessions and context switches. Stays on your PC |
| Briefings | "Good morning": a plan for the day built from your calendars, yesterday's numbers, projects, tasks, news and your goals. "Recap my day": where the time went and what shipped |
| Projects | Your Claude Code sessions (from `~/.claude`), local git repos (uncommitted work, commits today), GitHub PRs |
| Background jobs | Hand Claude Code a task in a repo, or start a web-research brief; you get a spoken alert when it finishes |
| PC optimizer | CPU, GPU and NVENC, RAM, disks, network; flags memory hogs, full drives, temp bloat, GPU heat and the wrong power plan, with one-click fixes |
| Tasks & notes | "add task …", "mark … done", "remember that …". Goals and tasks feed every plan |
| News | Your RSS feeds, filterable by topic |

## Quick start (Windows)

1. Install **Python 3.11+** from python.org (tick *Add to PATH*).
2. Double-click **`setup.bat`**. It creates `.venv`, installs everything, and copies
   `config.example.yaml` to `config.yaml` and `.env.example` to `.env`.
3. Edit **`.env`**. Add `ANTHROPIC_API_KEY` for the Claude brain and your calendar links.
4. Edit **`config.yaml`**. Set your name and your **goals**. Apps, games, OBS scenes and repos are found automatically (see below).
5. Double-click **`start.bat`**. On first launch it scans the PC, then the HUD opens as a Chrome app window.
   Open the **Setup** tab to see what's connected and what still needs you, then say *"Jarvis, good morning."*

To start it every time you sign in, run
`powershell -ExecutionPolicy Bypass -File scripts\install-startup.ps1`.

Command-line flags: `python -m assistant --no-voice --no-window --port 8765 --debug --config path\to\config.yaml`,
plus `--scan` (scan the PC and exit) and `--doctor` (scan, run a live health check of every connection, and exit).

## PC scan & health check

The assistant searches the PC on first launch and once a day after that, or on
demand with `--scan` or **Rescan PC** in the Setup tab. It finds:

| What | Result |
|---|---|
| ~40 common apps (browsers, OBS, Streamlabs, Stream Deck, Discord, Slack, Spotify, VS Code, Adobe, DaVinci…) | Open and close by name with correct process names; work/stream apps sharpen activity tracking |
| Steam and Epic games | "open baldur's gate 3" launches the game |
| OBS: WebSocket settings, scene collection, streaming service | Scene names with emoji/symbols get spoken aliases ("🔴 Starting Soon" → "starting soon"); the platform (Twitch/YouTube/Kick) is detected. The WebSocket password is read from OBS directly, so there's nothing to copy. The stream key is never read |
| Chrome / Edge / Brave bookmarks bar | Each bookmark opens by voice by its name |
| Git repos under the usual folders + every repo Claude Code has worked in | Project tracking plus your GitHub username |
| Mics, NVIDIA GPU + CUDA, Claude Code CLI, keys you've set | Setup to-dos; Whisper moves to the GPU when CUDA is available |

Results go to `config.discovered.yaml`, which loads **underneath** `config.yaml`.
Anything you set yourself wins. Nothing secret is written into it.

`--doctor` (or **Run check** in the Setup tab) then tests each connection live:
the Claude key and model, OBS, every calendar link, news feeds, Chrome, the
Claude Code CLI, a mic level test and the speech model. Each problem prints its fix.

## Things to say

| Say | Does |
|---|---|
| "Jarvis, good morning" / "give me the rundown" | Morning briefing: spoken summary, full plan on the HUD |
| "recap my day" / "what did I get done today" | End-of-day recap |
| "what should I work on next" | One highest-leverage move (uses Claude) |
| "where am I" | Active window and everything that's open |
| "open discord" / "launch photoshop" / "open gmail" | Apps or sites |
| "open a new tab with twitch" / "search youtube for …" | Chrome |
| "switch to BRB" / "change the scene to just chatting" | OBS scene |
| "go live" / "end the stream" / "clip that" / "mute my mic" | OBS control (going live and ending ask "yes?" first) |
| "how's the stream" | Bitrate, dropped frames, FPS |
| "start stream mode" / "let's work" | Runs that profile's routine: apps, tabs, OBS scene, offers to close distractions |
| "close chrome" → "yes" | Anything destructive waits for a yes |
| "what's my next meeting" / "what's on my calendar tomorrow" | Calendar |
| "add task email Acme the invoice" / "mark invoice as done" | Tasks |
| "optimize my PC" / "clean temp files" / "lock the PC" | System |
| "volume up" / "pause the music" / "next song" | Media keys |
| "research the best capture cards under $200" | Background research brief |
| "have Claude add unit tests in clipforge" | Background Claude Code job in that repo (asks first) |

Common commands take the **fast path**: they're matched locally in about a
millisecond, cost nothing, and work offline. Anything else goes to Claude, which
has the same actions available as tools. After a reply, you have 8 seconds to keep
talking without saying the name.

## Setup details

### Claude (the brain)
Get a key at console.anthropic.com and put it in `.env` as `ANTHROPIC_API_KEY`.
The default model is `claude-opus-5`. Voice commands run at low effort for speed;
briefings run at high effort for depth (`claude.command_effort` and
`claude.briefing_effort`). Without a key, everything in the fast-path table above
still works, and briefings use a local template instead of a real plan.

**Privacy:** when Claude handles a request, it receives that request, the active
window title, and the results of the tools it calls (for example calendar events
or task titles). Fast-path commands and the activity log never leave your PC.

### Calendars (plural)
Each calendar is a private iCal link. Store the link in `.env` and reference it
from `config.yaml` with `url_env`:

- **Google:** Calendar settings → *(the calendar)* → Integrate calendar → **Secret address in iCal format**
- **Outlook / Microsoft 365:** Settings → Calendar → Shared calendars → **Publish a calendar** → ICS link
- **iCloud:** Share the calendar → Public Calendar → copy the `webcal://` link (it's converted automatically)

Tag each calendar with a `profile` (`work`, `stream`, `personal`, …) so the Work
and Stream views show the right ones. Calendar access is **read-only** for now.

### OBS
OBS 28+ has WebSocket built in: **Tools → WebSocket Server Settings → Enable**.
The password is read from OBS's own settings, so leave `OBS_PASSWORD` empty
unless OBS runs on another machine. Scene names are fuzzy-matched, the scan
adds aliases for scenes with emoji, and `obs.scene_aliases` maps anything else
you say to exact names ("be right back" → `BRB`).

### Voice
`requirements-voice.txt` installs faster-whisper, sounddevice, pyttsx3 and keyboard.

- `voice.stt_model`: `tiny.en` is fastest, `base.en` is the default, `small.en`
  is most accurate. With an NVIDIA GPU and CUDA installed, set `stt_device: cuda`.
  The model downloads once on first launch (about 150 MB for `base.en`); after that, speech recognition runs offline.
- If Whisper mishears the name, add what it hears to `assistant.wake_words`
  (e.g. `travis`). Your OBS scene and app names are fed to Whisper as hints.
- `voice.tts.engine: browser` uses Edge/Chrome's natural voices (e.g. *Microsoft
  Ryan Online (Natural)*). They sound far better than SAPI, but only speak while
  the HUD is open.
- Noisy room? Raise `voice.min_rms`. Getting cut off mid-sentence? Raise `voice.silence_ms`.

### Profiles: work vs stream
`profiles.<name>.apps` and `title_keywords` decide how each minute gets
categorized. Title keywords win, so Chrome on GitHub counts as work and Chrome
on twitch.tv counts as stream. `launch` is the routine that runs for
"start <name> mode". `close_apps` are offered for closing when you switch modes.

### Projects
- Claude Code sessions are read from `~/.claude/projects`, so there's nothing to set up.
- `projects.scan_dirs`: folders whose git repos are tracked.
- `projects.github.user` (+ optional `GITHUB_TOKEN` for private repos and PRs).
- Background Claude Code jobs need the `claude` CLI on PATH. They run with
  `--permission-mode acceptEdits`, so they can edit files in that repo but not run
  arbitrary commands. Review the diff when they finish.

### Twitch (optional)
Create an app at dev.twitch.tv, put `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`
in `.env`, and set `twitch.enabled: true` and `twitch.channel`.

## How it's built

```
mic ─► VAD ─► Whisper ─► wake word ─┐
HUD text box / buttons ─────────────┼─► Assistant ─► fast-path router ──► tools ─► Windows / Chrome / OBS / …
                                    │              └─► Claude (tool use) ─┘
pollers (system 2s, OBS 3s, calendar 5m, news 15m, projects 2m, activity 60s) ─► event bus ─► WebSocket ─► HUD
```

- `assistant/brain/`: `router.py` (fast path), `tools.py` (every action, used by both paths), `assistant.py` (Claude loop, confirmations), `briefing.py`
- `assistant/integrations/`: `desktop`, `browser`, `system`, `obs`, `twitch`, `calendars`, `news`, `projects`, `activity`, `jobs`
- `assistant/voice/`: `listener.py` (mic, VAD, Whisper, wake word), `tts.py`, `hotkey.py`
- `assistant/web/`: the HUD (plain HTML, CSS and JS; no build step)
- Data lives in `data/assistant.db` (SQLite): activity, tasks, notes, conversation, jobs

**Security:** the server listens on `127.0.0.1` only. Every API and WebSocket call
needs a per-install token (`data/api_token`, injected into the HUD page) and a
localhost `Host` header. Without that, any website open in your browser could POST
to `localhost:8765` and drive your PC. Closing apps, going live or ending the
stream, restart and shutdown, and background Claude Code jobs always need a spoken
"yes" or a confirmed click in the HUD.

## Development

```
pip install -r requirements-dev.txt
pytest
```

CI runs the suite on Windows (Python 3.11 and 3.12) and Linux. It has 102 tests, 4 of which only run on Windows, where they call the real window, idle-time, Start Menu and power-plan APIs. The suite covers the router, wake-word matching, VAD, activity math,
calendar merging (recurring, all-day and cancelled events), feed and session
parsing, the Claude tool loop (with a fake client), confirmation gating,
briefings, the PC scan (against a simulated Windows folder layout), the
health check, and the API's token and Host checks.

## Known limits

- **CI has no desktop.** Window listing, idle time, Start Menu discovery and
  power plans run on the Windows CI job. Microphone capture, media keys, SAPI
  voices and OBS can only be checked on a real PC with a signed-in user.
- **Calendars are read-only.** Creating events by voice needs Google or Microsoft
  OAuth; that's the natural next step.
- **Chrome tabs are opened, not read.** Listing and switching existing tabs needs
  Chrome's remote-debugging port or a small extension.
- **Streaming platform:** Twitch stats are built in; YouTube Live and Kick aren't yet.
- **Wake word runs through Whisper,** so there's about a second of latency after
  you stop talking. A dedicated wake-word model would be faster but ties you to one name.
