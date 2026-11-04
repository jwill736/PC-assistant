# Vesper 2.3 · Performance: what it costs your PC while you game and stream

Vesper runs beside a game and OBS on the same PC, with a 35B model on the same graphics card. This pass measured what
it spends (CPU, memory, video memory, wakeups, programs it launches, model calls) and cut what it didn't need to
spend. Then it added a card that measures the same things on your PC: **Setup → What Vesper is using**.

## The rule for the graphics card in a game

While a fullscreen game runs (Windows' own "a game is running" signal, live on stream or not):

- **Background work never loads a model onto the graphics card.** Inbox triage, bills, the journal's mood, memory,
  Pulse, Week in review, the morning briefing, stream titles: they only use a model that is already loaded, and ask it
  with a 30-second keep-alive, so it leaves the card 30 seconds later instead of staying 15 more minutes.
- **With nothing loaded, the work waits for the game to end.** Pulse and the morning briefing are made after the
  game; memory learns what waited. Smaller jobs use their built-in rules (as with no model at all).
- **Your own requests still get the full model.** A question you say, type, send from your phone or click on the
  HUD gets the model it gets at your desk (the 35B), loaded if it has to be. It then stays loaded 2 minutes for a
  follow-up, not 15, and the game gets its video memory back.
- **A game starting releases what's left from before.** Asked Vesper something, then launched the game: the model that
  answered used to hold its 24 GB up to 15 minutes into the game. It now leaves 2 minutes after the game starts.
- Embeddings (memory) and the screen's vision model follow the same rule.

Settings (`config.yaml` → `brain:` → `local:`):

| Key | Default | |
|---|---|---|
| `game_keep_alive` | `30s` | in a game: how long a model background work used stays loaded |
| `game_answer_keep_alive` | `2m` | in a game: how long the model that answered you stays loaded |
| `keep_alive` | `15m` | at the desk, as before |

`low_power: {enabled: false}` turns all of the game rules off (as it always has for the slowed-down pollers).

## What changed, measured

Measured with the audit's harness (the real Vesper with a fake Ollama, OBS, mic and GPU, on a 4-core Linux box shared
with other work), at 1571574 and with this pass. CPU is % of one core.

| | What was measured | Before | Now |
|---|---|---|---|
| Game from launch, 10 min | models Ollama loaded mid-game | the 4B (2.6 GB) at 47 s, kept 15 min, renewed at 3 min: held all game | none |
| Asked something, then a game | the 35B's 24 GB in the game | renewed for 15 min by a background summary: held the whole 9-minute window (and ~16 min by its keep-alive) | cut to 2 min when the game started, used once with 30 s: gone 1.8 min into the game |
| You talking to chat (not to Vesper) | CPU, % of one core | 8.97% | 4.65% |
| … with natural end of turn on | CPU | 20.3% | 6.2% |
| One Smart Turn check | CPU / wait | 188 ms / 48 ms | 44 ms / 45 ms |
| Speech-to-text, per second of speech | CPU / wait | 131 ms / 39 ms (2 threads) | 45 ms / 46 ms (1 thread) |
| OBS open, not live | CPU / wakeups a second | 2.76% / 41.3 | 0.34% / 18.9 |
| Start-up | imports / HUD answers / all skills started | 1.3-1.5 s / 2.3-2.6 s / 3.1-3.5 s | 0.4-0.6 s / 1.3-1.9 s / 1.6-2.2 s |
| Start-up | memory after start | 176 MB | 116-119 MB |
| GPU readings | nvidia-smi launches an hour: HUD open / idle / in a game | 960 / 180 / 102 | 0 / 0 / 0 (read in-process through NVML) |
| Projects panel, 12 repos | per refresh: git launches / CPU | 48 / 106 ms | 12 / 40 ms |
| Projects panel | refreshes an hour with no HUD open; a live 12 MB Claude Code session re-read | 30; 36 ms each | 4; 0 ms (only what was added) |
| Look-around | first 12.5 min after start | restarted 3 times, never ran, "stalled" | ran 8 times, no restarts |
| HUD open | data sent to it | 387 KB and 110 messages a minute | 128 KB and 76 a minute |
| You talking to chat | speaker callbacks a second (the output kept open) | 51 | 0 |
| Idle | database commits an hour | 66 | 6 |
| Idle, no voice | CPU / OS threads | 0.38% / 63 | 0.28% / 59 |
| Voice on, silence | CPU / OS threads | 2.48% / 72 | 2.32% / 66 |

The speaking voice keeps 2 threads: measured again, Supertonic's second thread does shorten the wait (90 → 66 ms
per second of audio; a short reply 230-250 → 176-197 ms), so it stays.

**Things you may notice**

- In a game, a background summary (Pulse, a triage note) is made after the game, or by its rules, instead of by the
  model: the model is not loaded for it.
- Your first question in a game, more than 2 minutes after the last one, waits for the model to load (a few seconds),
  as the first question of the day does. Before, it was often still loaded from before the game, holding 24 GB.
- Speech-to-text runs on one CPU thread: a 3-second command is transcribed about 20 ms later, for a third of the
  CPU (`voice.stt_threads: 2` puts it back).
- With OBS open but not live, Vesper no longer listens to OBS's mic meters. The pre-stream check turns them on for a
  minute when it runs, so it still says your mic's level.
- The first reply after you've been talking to chat for a while (not to Vesper) can start 50-100 ms later: the
  speakers are no longer kept open while you talk to chat.
- The Projects panel (git status of your repos) refreshes every 2 minutes while a HUD is open, every 15 when none is.
- Setup → Speaking voice now says Kokoro takes about 6× Supertonic's CPU for each sentence (and 350 MB).
- `nvidia-ml-py` is a new package (Update Vesper installs it). Without it, or without an NVIDIA driver, Vesper reads
  the card with nvidia-smi as before.

## What Vesper is using (the card)

**Setup → What Vesper is using**, beside **Diagnostics · report to send**. Nothing is measured until you click: it
costs nothing while it's closed. A click watches Vesper for 30 seconds and shows:

- the first line: Vesper's CPU, its memory, and what Ollama holds on the graphics card and when it unloads
  ("qwen3.6:35b-a3b 24 GB, unloads in 12 min — mid-game" if a game is running);
- CPU by part: the voice listener, the speaking voice, OBS's events, each poller, the web server, and the speech
  models' own threads;
- the graphics card: each Ollama model's video memory, the card's used/total, and Vesper's own (none: its speech runs
  on the CPU);
- memory per speech model (speech-to-text, voice detection, the speaking voice, wake words, the voice check);
- programs launched in the last hour (git, powercfg… nvidia-smi should be gone);
- model calls in the last hour: who asked (a skill, you, the briefing), which model, whether it had to load, and the
  keep-alive it asked for;
- the busiest pollers, with how often each runs.

**Measure in a game (starts in 1 min)** waits a minute first, so you can switch to the game: what Vesper uses
mid-game is the number that matters. **Run diagnostics** puts the same table in the report file.

### Walkthrough: measure it and send it back

**Part 1 · Update Vesper (5 minutes; waits until this change is merged)**

1. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
2. A window shows the update (a minute or two). Wait for the green line:

   ```
   Vesper is installed in C:\Users\J\Vesper.
   ```

   Vesper starts again by itself. Anything in red: stop and send a screenshot of the window.

**Part 2 · Measure at the desk (2 minutes)**

3. Press <kbd>Win</kbd>, type `Vesper`, press <kbd>Enter</kbd>. The HUD opens (with Vesper already running, this
   only brings its HUD up).
4. Click the **Setup** tab at the top.
5. Find the card **What Vesper is using** (next to **Diagnostics · report to send**). Click **Measure 30 s**.
   The button reads "Measuring… 6 of 30 s", then the card fills in. The first line starts with "Vesper:".
6. Take a screenshot of the whole card: <kbd>Win</kbd>+<kbd>Shift</kbd>+<kbd>S</kbd>, then drag over it.

**Part 3 · Measure in a game (3 minutes)**

7. On the same card, click **Measure in a game (starts in 1 min)**. A message says "Measuring starts in 60 s: switch
   to your game now."
8. Start your game (or switch to it) and play for 2 minutes. Stream as you normally would if you like.
9. Come back to the HUD (<kbd>Alt</kbd>+<kbd>Tab</kbd>) and the **Setup** tab. The card shows the new numbers; the
   first line should end with "mid-game" if Ollama still holds a model. Take a screenshot of the card again.

**Part 4 · Put it in the report (1 minute)**

10. On **Diagnostics · report to send**, click **Run diagnostics**. Wait until the chips show pass / warn / fail
    and a path under "The report to send".
11. Click **Open the file**. Notepad opens the report.
12. In Notepad press <kbd>Ctrl</kbd>+<kbd>F</kbd>, type the line below, press <kbd>Enter</kbd>:

    ```
    What Vesper is using
    ```

    That section has the same numbers as the card, as tables.
13. Click **Show in folder** on the card: Explorer opens with the report selected, ready to drag into a message.

**What to send back**

- The two screenshots of the card (desk, and in the game).
- The report file itself (`data\diagnostics-<date>.md`), or the **What Vesper is using** section copied out of it.
- Anything in red on the card or in the update window, word for word.
- Which game you measured in, and whether you were live.
