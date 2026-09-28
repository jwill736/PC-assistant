# PC Assistant — voice control + HUD for your PC

A local, voice-driven assistant for a Windows PC, in the spirit of JARVIS. Say its
name, then tell it what you need: it opens programs and Chrome tabs, tells you
where you are on the machine, switches OBS scenes, runs your "work mode" and "stream
mode" setups, reads all of your calendars, tracks where your day went, and plans
your morning around the goal you actually care about. Anything bigger ("research
X", "have Claude add tests to my repo") runs in the background while you keep working.

The HUD dashboard has three views: **Command** (everything at once), **Work** and
**Stream**.

> It ships as **Vesper**. `assistant.name` in `config.yaml` is both what it calls itself
> and the wake word, so choose a name nobody says by accident: Vesper scored about 3.5×
> fewer sound-alikes in everyday English than "Jarvis" (which also matches "jars"), while
> names like Atlas ("at last"), Nova ("know the") or Juno ("you know") fire all the time.
> Re-run calibration after renaming.

## What works today

| Area | What it does |
|---|---|
| Voice | Local speech-to-text (Parakeet, on your PC), any wake word, follow-up window, push-to-talk hotkey (`Ctrl+Alt+J`). Replies in a natural local voice that starts about 0.2 s after the text is ready, speaks Claude's answer sentence by sentence as it arrives, and stops the instant you say "stop". Ignores its own voice coming back through your speakers |
| Your voice only | A one-minute calibration sets the mic threshold for your room, learns how Whisper spells the name in your voice, and enrolls your voice. After that, other voices (Discord, stream audio, the TV) are ignored |
| Trigger phrases | One phrase runs a whole sequence: "brb" switches to the BRB scene, mutes the mic and confirms. Define them in `config.yaml`, run them by voice, from the HUD, or let Claude pick one |
| Always on | Runs silently in the system tray and starts when you sign in. A watchdog restarts any part that crashes or stalls, and everything is logged to `data/logs/` |
| Apps & windows | Open any installed program or game by name (Start Menu and Desktop shortcuts are found automatically), close apps (after you confirm), bring windows to the front, "where am I" |
| System controls | Exact volume ("volume 30"), per-app volume ("Discord volume 20"), brightness, Windows Settings pages, virtual desktops |
| Safety | Every action has a risk tier; the risky ones wait for your "yes" (voice, HUD or a Windows notification button). A kill switch (`Ctrl+Alt+K`, the tray, or "stop everything") stops it all. Every action is logged |
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

1. Install **Python 3.12** from python.org, ideally [3.12.10](https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe),
   the last 3.12 with a Windows installer (tick *Add python.exe to PATH*). Step by step, with every click:
   the [setup guide](docs/SETUP.md).
2. Double-click **`setup.bat`**. It creates `.venv`, installs everything, and copies
   `config.example.yaml` to `config.yaml` and `.env.example` to `.env`.
3. Edit **`.env`**. Add `ANTHROPIC_API_KEY` for the Claude brain and your calendar links.
4. Edit **`config.yaml`**. Set your name and your **goals**. Apps, games, OBS scenes and repos are found automatically (see below).
5. Double-click **`start.bat`**. On first launch it scans the PC, then the HUD opens as a Chrome app window.
   Open the **Setup** tab to see what's connected and what still needs you.
6. In the Setup tab, click **Calibrate my voice** (about a minute; see [Voice](#voice)). Then say *"Vesper, good morning."*

To start it every time you sign in, run
`powershell -ExecutionPolicy Bypass -File scripts\install-startup.ps1`. It launches with `pythonw`: no
console window, just the tray icon (open the HUD, push-to-talk, mute the mic, pause tracking, rescan, open
the log folder, quit). Add `-ShowHud` to also open the HUD at sign-in, or `-Remove` to undo. Starting a
second copy just opens the HUD of the one already running.

Command-line flags: `python -m assistant --no-voice --no-window --no-tray --port 8765 --debug --config path\to\config.yaml`,
plus `--scan` (scan the PC and exit), `--doctor` (scan, run a live health check of every connection, and exit)
and `--calibrate` (voice calibration in the console, then exit).

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
| "Vesper, good morning" / "give me the rundown" | Morning briefing: spoken summary, full plan on the HUD |
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
`requirements-voice.txt` installs sherpa-onnx, livekit-wakeword, sounddevice, pyttsx3, pynput and faster-whisper (fallback).

How it hears, in order: **Silero VAD** finds where speech starts and ends → a **speech-to-text engine** turns
it into text → the echo guard drops its own voice → the wake word is matched → the speaker check. The first
launch downloads the models (~100 MB, from the sherpa-onnx releases on GitHub); after that it runs offline.

- **`voice.stt_engine`** (default `auto` = Parakeet). Measured on 80 commands in 3 voices, with and without a
  second person talking underneath, on a 4-core laptop-class CPU:

  | engine | word errors (clean / with chatter) | wake word caught (clean / chatter) | time to text |
  |---|---|---|---|
  | `parakeet` (Parakeet TDT 110M) | 8.2% / 22.5% | 16/20 / 12/20 | 64 ms |
  | `moonshine` (Moonshine base) | 13.2% / 60.4% | 15/20 / 8/20 | 62 ms |
  | `parakeet-large` (0.6B) | 9.3% / 72.5% | 16/20 / 5/20 | 219 ms |
  | `whisper` (faster-whisper `base.en`, the old default) | 16.5% / 36.3% | 8/20 / 9/20 | 273 ms |

  Synthetic voices, not yours. **Run `python -m assistant --bench-voice`**: it records 10 commands in your
  voice, tests every installed engine on this PC and prints the table; add `--apply` to switch to the winner.
  `whisper` is the only engine that uses `stt_model`/`stt_device` (set `stt_device: cuda` for an NVIDIA GPU).
- **Speech detection:** `voice.vad: auto` (Silero). With game sound effects between commands, the old
  loudness gate glued the effects onto the command and handed it over ~2.2 s after you stopped; Silero hands
  it over after ~0.4 s and produced no false segments. `voice.endpoint_ms` (400) is the pause that ends a
  command — raise it if long pauses cut you off. `voice.vad: energy` goes back to the loudness gate.
- **`voice.corrections`** fixes words the recogniser keeps getting wrong, e.g. `{vrb: BRB, "stream labs": Streamlabs}`.
- **Calibrate once** (Setup tab → *Calibrate my voice*, or `python -m assistant --calibrate`):
  1. 5 s of quiet measures your room and sets `voice.min_rms`.
  2. Say the name 5 times. Every way the recogniser spells it in your voice ("Fesper", "Vespa") becomes an extra wake word.
  3. Read 3 short lines. Those clips become your voice profile.

  Results go to `data/calibration.yaml`, which sits under `config.yaml` (your own settings still win),
  and take effect immediately. Re-run it after changing mics or rooms.
- **Choices made in the HUD win over `config.yaml`.** The voice picker, the *Only answer me* switch and
  `--bench-voice --apply` save to `data/settings.yaml`, which is merged on top of `config.yaml` (whose
  example spells most of these settings out, so it would otherwise undo them on every restart). Delete a line
  there to fall back to `config.yaml`.
- **Only your voice.** After calibration, `voice.speaker_check: strict` ignores commands whose voice
  doesn't match yours (WeSpeaker ResNet34 through sherpa-onnx: a 26 MB model downloaded once, ~50 ms per
  command on CPU). `log` scores voices without blocking, which is useful for checking the threshold; `off` answers anyone.
  Push-to-talk and the HUD mic button skip the check, because pressing them is proof enough. The profile
  is a set of voice fingerprints, encrypted with Windows DPAPI. It never leaves the PC, and *Delete voice profile*
  removes it. This is a convenience filter, not security: a recording of you can pass it, which is why risky
  actions still need a spoken "yes".
- The name and "hey &lt;name&gt;" are always wake words; add your own under `assistant.wake_words`. A near-miss
  only counts if it's nearly as long as the name, so everyday words that share letters ("jars" for "Jarvis") don't.
- The assistant ignores its own replies when your speakers feed them back into the mic (anything matching
  what it just said, within 4 s). Short answers like "yes" or "stop" are never mistaken for its echo.
  Headphones still work best.
- **Follow-up without the name** only happens after a question or a "say yes" confirmation, so a false wake
  can't turn into a back-and-forth. Spoken replies are stripped of markdown, links and emoji.
- Getting cut off mid-sentence? Raise `voice.endpoint_ms`. Triggering on TV or music? Raise `voice.vad_threshold` (0.6).

### Speaking voice
Replies are spoken by a local neural voice through [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx): no
cloud, no API key, and it works offline. Pick one in **Setup → Speaking voice** (*Preview* plays a sample
through your speakers) or set `voice.tts` in `config.yaml`. Measured on 2 CPU threads, from the reply text
being ready until its first sound:

| `voice.tts.engine` | first words after | download | voices | notes |
|---|---|---|---|---|
| `supertonic` (default via `auto`) | 140–220 ms | 85 MB | 10 (`m1`–`m5`, `f1`–`f5`) | MIT licence file in the model package; ~11× faster than real time |
| `kokoro` | 410–1200 ms | 320 MB | 11, incl. British `bm_george`, `bm_lewis` | Apache-2.0; its int8 build was 2.5× *slower*, so the full model is used |
| `pyttsx3` | instant | none | Windows SAPI voices | robotic; the fallback when the voice packages aren't installed |
| `browser` | — | none | Edge/Chrome natural voices | only speaks while the HUD is open |

How it stays fast and interruptible:

- **Sentence streaming.** Claude's reply is streamed, and each sentence is spoken as soon as it's complete,
  so a long answer starts talking after its first sentence instead of after its last. Text Claude writes before
  a tool call ("Checking your calendar.") is spoken while the tool runs. `voice.tts.stream: false` waits for the
  whole reply.
- **Pipelined playback.** Each sentence plays while the next one is synthesised, and the audio device stays
  open between sentences.
- **Stops within ~10 ms.** "stop" (or the name, or the push-to-talk hotkey) fades the audio out over 10 ms
  instead of clicking, and silences the rest of that reply, including sentences Claude hasn't finished sending.
- **Turns.** Each voice command starts a new turn; if you move on before a reply finishes, the rest of it is
  dropped rather than spoken late. Commands now run off the mic thread, so it keeps listening (for "stop",
  for the next command) while Claude thinks.
- Initialisms are spelled out ("BRB" → "B R B", "91%" → "91 percent"). Other voice settings: `voice.tts.speed` (0.6–1.6),
  `voice.tts.output_device`, `voice.tts.threads` (2). The health check times the chosen voice.

The Windows CI job speaks a sentence with both local voices and checks that Parakeet hears the same words back.
Nobody has listened to them on your speakers yet, so judge them by ear with *Preview*.

### Profiles: work vs stream
`profiles.<name>.apps` and `title_keywords` decide how each minute gets
categorized. Title keywords win, so Chrome on GitHub counts as work and Chrome
on twitch.tv counts as stream. `launch` is the routine that runs for
"start <name> mode". `close_apps` are offered for closing when you switch modes.

### Trained wake words and instant triggers (Colab, ~2 hours, once)
Out of the box the name is matched on the transcript. That works, but every sentence gets transcribed, and
saying "Vesper" to chat mid-sentence can wake it. A trained acoustic model fixes both:

1. Open [`training/wake_words.ipynb`](training/wake_words.ipynb) in Google Colab (*File → Open notebook → GitHub*),
   pick *Runtime → Change runtime type → T4 GPU*, then *Run all*. It trains `vesper.onnx` and `stop.onnx`
   (and optionally a phrase like "clip that") from synthetic voices with
   [livekit-wakeword](https://github.com/livekit/livekit-wakeword). The last cell prints each model's recall,
   false triggers per hour and a threshold, and downloads the `.onnx` files. Nothing of yours is uploaded.
2. In the HUD: **Setup → Trigger words → Add trained model**, pick the file, type the threshold.

What each model does, by file name:

| file | effect |
|---|---|
| `vesper.onnx` (the name) | `wake_mode` becomes `acoustic`: speech-to-text runs only after the model fires in the first ~2 s of what you say. The name mid-sentence doesn't count, and a mangled transcript ("Desperate, open Discord") still works because the model already heard the name. `voice.wake_mode: hybrid` accepts the transcript too. |
| `stop.onnx` | stops a spoken reply immediately |
| any other, e.g. `clip_that.onnx` | runs that phrase as a command the moment it's heard: no speech-to-text, no Claude (`voice.hard_triggers: {clip_that: "save the replay"}` to map it to something else) |

The detector streams its features (≈1.3 ms of CPU per 80 ms of audio), so it costs about 2% of one core.

**Talking over a reply (barge-in):** while it speaks, the mic stays on. Say "stop" / "cancel" / "never mind",
or the name plus a new command, and it stops mid-sentence (local, SAPI and browser voices; only plain pyttsx3 can't be cut off).
With a voice profile enrolled, the speaker check ignores the assistant's own voice coming out of your
speakers; without one, a "stop" that's part of the reply itself is ignored. `voice.barge_in: false` turns it off.

### PC control: what it may do, and the kill switch
Every action goes through one checkpoint that knows its risk tier (the research's plan, `assistant/brain/policy.py`):

| tier | examples | what happens |
|---|---|---|
| T0 read-only | where am I, calendar, system status | runs; works even while paused |
| T1 reversible | volume, brightness, open apps and sites, switch scenes, lock | runs |
| T2 may lose work | close an app | waits for a "yes" |
| T3 can't be undone, or public | shut down or restart, go live or end the stream, stop recording, delete temp files, let Claude Code change a repo | waits for a "yes" for that one action, with its exact target read back; by voice, only in *your* voice |

- **Yes from anywhere, for that action only.** Say "yes", click *Yes, do it* in the HUD, or (Windows) click *Yes* on
  the notification. Each waiting action has an id; a slow click on an old notification never confirms whatever
  replaced it. With a voice profile in `LOG` mode, a T3 "yes" in a voice that doesn't match yours is refused.
- **Kill switch:** `Ctrl+Alt+K` (`pc_control.kill_hotkey`), *Stop everything* in the tray or HUD, or say "stop
  everything" (no name needed; "hands off" and "freeze" work with the name). It stops the voice mid-word, drops
  anything waiting on a yes, cancels background jobs, cuts off Claude mid-reply and refuses every action until you
  say "resume control" or click *Resume*. Reads and answers keep working. Spoken, it skips the command queue, so it
  takes effect while a long request is still running. (In acoustic wake mode, without the name it's heard only
  while a reply is playing.)
- **Budget:** one request may take at most 25 actions and stops after 3 failures in a row (`pc_control.max_steps`,
  `max_failures`), so a confused tool loop can't run away.
- **Audit log:** every action, refusal and kill is one line in `data/logs/actions-YYYY-MM.jsonl`: time, tool,
  arguments (secrets redacted, long text clipped), tier, who asked (voice, typed, HUD) and the words they used,
  how it was confirmed, and the result. The last 25 are in **Setup → PC control**.
- **System controls** use Windows' own APIs: Core Audio via `pycaw` for exact master and per-app volume (the volume
  keys are the fallback), `screen-brightness-control` for brightness (laptop panels; desktop monitors need DDC/CI
  turned on in their menu), `ms-settings:` pages, and virtual desktops via Ctrl+Win+arrows or `pyvda` for "desktop 3".
  Say "volume 30", "Discord volume 20", "brightness 70", "dimmer", "open Bluetooth settings", "next desktop".
- **Not yet:** clicking and typing inside apps (Windows UI Automation) and screen-based computer use are Phase 4b;
  they build on this layer.

### Trigger phrases (macros)
One phrase, several actions, in order. Put them in `config.yaml`:

```yaml
macros:
  brb:
    say: ["brb", "be right back", "taking a break"]
    steps:
      - obs_switch_scene: {scene: BRB}
      - obs_set_mute: {source: mic, muted: true}
      - say: "Be right back is up. Mic's muted."
  wrap up stream:
    say: ["wrap it up", "end of stream"]
    steps:
      - obs_switch_scene: {scene: Ending}
      - wait: 20
      - obs_control: {action: stop_stream}
```

A step is any tool the assistant has (the same names Claude uses: `open_app`, `open_urls`, `obs_switch_scene`,
`obs_control`, `media_control`, `set_power_plan`…), or `say: text`, `wait: seconds` (max 30), or
`command: "anything you'd say out loud"`. A macro with a risky step, such as ending the stream,
asks for one "yes" before it starts. The phrase has to be the whole sentence ("Vesper, brb"), so
"be right back in five minutes with the new overlay" won't fire it. You can also say "run brb",
click **Run** in the Setup tab, or just describe what you want and let Claude choose the macro.
Unknown step names are logged when the assistant starts. `config.example.yaml` has four to start from.

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
mic ─► Silero VAD ─► Parakeet ─► echo guard ─► wake word ─► your voice? ─┐
HUD text box / buttons ──────────────────────────────────────────┼─► Assistant ─► macros / fast-path router ──► tools ─► Windows / Chrome / OBS / …
                                                                 │              └─► Claude (tool use) ────────┘
pollers (system 2s, OBS 3s, calendar 5m, news 15m, projects 2m, activity 60s) ─► event bus ─► WebSocket ─► HUD
watchdog: restarts any poller or service that crashes or stops reporting in ─► Setup tab + tray icon colour
```

- `assistant/brain/`: `router.py` (fast path), `macros.py` (trigger phrases), `tools.py` (every action, used by both paths), `assistant.py` (Claude loop, confirmations), `briefing.py`
- `assistant/integrations/`: `desktop`, `browser`, `system`, `obs`, `twitch`, `calendars`, `news`, `projects`, `activity`, `jobs`
- `assistant/voice/`: `listener.py` (mic, VAD, speech-to-text, wake word, echo guard), `stt.py`, `vad.py`, `wakeword.py`, `speaker_id.py` (voice profile + check), `calibrate.py` (the wizard), `neural.py` (local voices, sentence splitter, interruptible audio out), `tts.py` (turns, engines, fallbacks), `hotkey.py`
- `assistant/watchdog.py` (supervisor + log file), `assistant/tray.py` (tray icon), `assistant/discovery.py` + `doctor.py` (PC scan and health check)
- `assistant/web/`: the HUD (plain HTML, CSS and JS; no build step). Light and dark themes (follows Windows, or the ◐ button). Jost and Inter are bundled under the SIL Open Font License, so it looks the same offline
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

CI runs the suite on Windows (Python 3.11 and 3.12) and Linux. It has 255 tests, 6 of which only run on Windows, where they call the real window, idle-time, Start Menu, power-plan, tray-icon and system-control APIs. A separate Windows job downloads the real Silero and Parakeet models and transcribes a real recording, runs the wake-word runtime on real recordings, and speaks with both local voices and hears them back (`ASSISTANT_MODEL_TESTS=1 pytest tests/test_models_live.py` locally). Another runs `setup.bat` exactly as a new user would and checks the result. The suite covers the router, risk tiers, the kill switch and step budget, stale-confirmation protection, the audit log, the system controls (with fake Core Audio, brightness and desktop APIs), wake-word matching, VAD and speech-engine selection, model download (including a malicious archive), the echo guard and voice check, calibration (with a fake mic), trigger phrases, the watchdog, activity math,
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
- **No echo cancellation yet.** On speakers (not headphones), barge-in relies on the echo guard and the voice
  check to ignore the assistant's own voice. Real acoustic echo cancellation (WebRTC AEC3 against the speaker
  output) is the next voice step.
- **Until you train the name on Colab,** the wake word is matched on the transcript, so every utterance is
  transcribed (cheap: ~60 ms) and saying the name to chat can wake it. The notebook's models are trained on
  synthetic voices only; if one misses you, lower its threshold or retrain with `QUALITY = "best"`.
- **Talking over a second voice is still hard** for every engine tested (see the table above): with someone
  talking at -10 dB underneath, the best CPU engine still gets ~1 word in 5 wrong.
- **Barge-in on speakers without a voice profile** relies on the echo guard; enroll your voice (Setup tab)
  so the assistant's own voice can never interrupt itself.
- **Clicking and typing inside apps** isn't there yet. It can open, close, focus and switch apps, windows, tabs,
  OBS and media, but not press a button inside Photoshop. That's the next step (UI Automation, then Claude's
  computer-use tool as the fallback).
