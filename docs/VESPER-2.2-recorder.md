# Vesper 2.2 · Recorder: record your audio, get notes

"Vesper, start recording" records what you say (and, in a call, what the others say), shows the transcript as it grows,
and when you stop, writes notes: a summary, key points, decisions, action items with who and when, open questions and
the people mentioned. The notes are a Markdown file in your notes folder, and you can ask about them later ("what did we
decide about the budget in Tuesday's call?").

**What it does**

| | |
|---|---|
| Start / stop | "start recording", "record this", "take notes", "take notes of this meeting", "stop recording"; "pause recording" / "resume recording"; <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd>; the round red button on the **Work** tab |
| While it records | a red ring and a **REC** timer on the orb (and in the mini HUD); the transcript fills in on the **Work** tab. Voice commands still work. "Vesper, mark that" drops a bookmark, "Vesper, note: the budget is 40k" adds a typed note. Both answer with a chime, not speech, so you don't talk over the meeting |
| Call audio | during a calendar meeting, or when you say "meeting" or "call", it also records the PC's own sound: your mic is **You**, the PC's sound is **Others**. Needs a way to hear the PC (Part F) |
| Notes | made by your local model (private). Long recordings are summarised in parts, then put together. With no model you still get the full transcript and your bookmarks, marked "No model: transcript only" |
| Tasks | action items become one card: **Add 4 tasks?** Nothing is added until you click **Add** or say "add them" |
| Meetings | a calendar meeting happening now names the notes and lists who was there; the Meeting debrief panel then shows the recording instead of asking "anything to note?" |
| Ask later | "what did we decide about the budget in Tuesday's call", "what did Dana say about pricing", "search my recordings for pricing"; "search everything for …" finds them too |
| From a file | "make notes from C:\Users\you\Downloads\call.m4a" (WAV always works; other kinds need ffmpeg, Part I) |
| Privacy | never starts while you're live unless you say "record on stream"; going live stops it (it says so). Off the record = no recording. The first recording with call audio reminds you to tell people. Audio is deleted after 30 days, notes stay. Transcripts and notes never go in the conversation log, and nothing from them is shown or said on stream |

**Why the Work tab:** recordings are mostly calls and meetings, so the Recorder sits with Next meeting · prep and
Meeting debrief, and the notes it makes feed the debrief.

Settings (optional, `config.yaml` → `skills:` → `recorder:`): `hotkey` (ctrl+alt+r, `""` turns it off), `notes_dir`,
`keep_audio_days` (30), `system_audio` (meetings / always / never; the HUD has buttons for it), `notes_budget_s` (300:
how long the model may spend on notes), `max_hours` (8), `chunk_minutes` (5).

Files: audio and the transcript in `%USERPROFILE%\Vesper\data\recordings\<date>-<time>\` (5-minute pieces, so hours-long
recordings are fine); notes in `%USERPROFILE%\Vesper\data\notes\` unless you choose a folder (Part G).

---

## Part A · Update (3 minutes; waits on the merge)

1. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
2. Wait for the black window to finish. Vesper restarts and the HUD reloads by itself.
3. Click the **Work** tab. Under **Meeting debrief** there's a new wide panel: **RECORDER · NOTES FROM YOUR AUDIO**, with
   a round red button, `00:00`, and **Start recording** on the right.
4. At the bottom of that panel, the **Notes** line shows where notes go, and **Audio** says `Opus` or `WAV (no ffmpeg)`.
   Either is fine for now.

## Part B · Your first recording, by voice (3 minutes)

1. Say:
   ```
   Vesper, start recording
   ```
2. You should hear "Recording." The orb gets a red ring and a red **REC 00:03** label, and the Recorder panel's border
   turns red with a running timer and a moving level meter.
3. Talk for about 30 seconds, as if to a colleague. For example: "We need to send the invoice to Acme by Friday. I'll
   do it. Dana still owes us the pricing." Within a few seconds of each sentence it appears in the panel under **You**.
4. Say:
   ```
   Vesper, mark that
   ```
   You should hear a short chime (no words), and a blue ★ **Bookmark** row appears in the transcript.
5. Say:
   ```
   Vesper, note: the budget is 40k
   ```
   Another chime; a blue row shows "the budget is 40k". Neither command shows up as a transcript line.
6. Say:
   ```
   Vesper, what time is it
   ```
   It answers as usual (commands still work). Its answer appears faded under **Vesper** and is left out of the notes.
7. Say:
   ```
   Vesper, stop recording
   ```
8. You should hear "Stopped after under a minute. Notes will be on the HUD." In the panel a purple spinner says
   **Writing notes**, then the notes appear on the right: a title, a summary, **Decisions**, **Action items** (with
   **You** or a name, and a date if you said one), **Bookmarks**.
   - If it says **No model: transcript only**, your local model isn't running: click **Setup**, check **Brain · what
     answers you**, then come back and click **Retry** under the notes.

## Part C · The task card (1 minute)

1. If your notes had action items, an amber card at the top right asks **Add 2 tasks?** with a tick box per item.
2. Untick any you don't want, then click **Add 2 tasks** (the number follows your ticks).
3. Click the **Command** tab. The tasks are in **Goals & tasks**.
4. Next time, you can also just say:
   ```
   Vesper, add them
   ```
   **Not now** closes the card without adding anything.

## Part D · Hotkey, button, pause (2 minutes)

1. Press <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd>. You hear a chime and the REC ring appears.
2. On the **Work** tab, click **Pause** in the Recorder. The ring turns amber and dashed, the label says **Paused**, and the
   timer stops.
3. Click **Resume**, say a sentence, then type `ship it Friday` in the **Type a note…** box and click **Add note**.
4. Press <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd> again to stop. The notes appear on the right.
5. In the notes, click **Copy**, open Notepad (<kbd>Win</kbd>+<kbd>R</kbd>, type `notepad`, <kbd>Enter</kbd>) and press
   <kbd>Ctrl</kbd>+<kbd>V</kbd>: the whole notes file, ending with the full transcript.
6. Click **Open notes**: File Explorer opens with the notes file selected.

Note: if the bottom line of the panel says **Hotkey … (not active)**, or another app already uses
<kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd>, use the button or the voice instead, and tell me which keys you'd like.

## Part E · A meeting from your calendar (when you next have one; nothing to set up)

1. During a calendar meeting with other people (Google calendar), say:
   ```
   Vesper, take notes of this meeting
   ```
2. You hear "Recording Client review. The call audio too. Let people on the call know." The first time only, a notice
   also reminds you to tell people they're being recorded. Please do.
3. The panel shows the meeting's name and **with Dana Ortiz, …**. Lines from the call show as **Others**, yours as **You**.
4. When you stop, the notes are named after the meeting. On the **Work** tab, **Meeting debrief** shows "Recorded · …"
   for that meeting and won't ask you about it.

If the panel shows a grey dashed **No call audio** chip, Vesper can't hear the PC's sound yet: do Part F.

## Part F · Let Vesper hear the call (5 minutes; optional)

The best way is a small add-on that records what Windows plays (WASAPI loopback).

1. Press <kbd>Win</kbd>+<kbd>R</kbd>, type this and press <kbd>Enter</kbd>:
   ```
   cmd
   ```
2. Paste this line and press <kbd>Enter</kbd>:
   ```
   cd /d %USERPROFILE%\Vesper
   ```
3. Paste this line and press <kbd>Enter</kbd>:
   ```
   .venv\Scripts\python -m pip install pyaudiowpatch
   ```
   You should see `Successfully installed pyaudiowpatch-…` at the end.
4. Close the black window. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd> (this restarts Vesper).
5. Play any YouTube video, then say "Vesper, take notes of this meeting". The panel shows a **Call audio** chip, and the
   video's words appear as **Others**. Say "Vesper, stop recording".

Without the add-on, Vesper also uses a **Stereo Mix** input if your sound card has one: press <kbd>Win</kbd>+<kbd>R</kbd>,
type `mmsys.cpl`, press <kbd>Enter</kbd>, click the **Recording** tab, right-click an empty spot, tick **Show Disabled
Devices**, then right-click **Stereo Mix** → **Enable**.

Headphones work best: with speakers, your mic hears the call too, so some of their words also show under **You**.

## Part G · Keep notes in Google Drive (2 minutes; optional)

1. On the **Work** tab, at the bottom of the Recorder, click **Change** next to **Notes**.
2. Paste the folder's full path (create the folder in File Explorer first), for example, and click **OK**:
   ```
   K:\My Drive\Vesper Notes
   ```
3. A green notice says **Notes go to K:\My Drive\Vesper Notes**. New notes are saved there (Google Drive syncs them).
   Audio stays in Vesper's own folder. To go back, click **Change**, clear the box, click **OK**.

## Part H · Ask about your recordings (1 minute)

1. Say (use a word from Part B):
   ```
   Vesper, what did we decide about the invoice in today's call
   ```
2. You hear a short answer that says which recording and roughly when in it.
3. Type `invoice` in the **Search recordings…** box on the panel: the matching recordings list, with the line that
   matched. Click one to open its notes.
4. Also works: "what did Dana say about pricing", "search my recordings for pricing", "search everything for invoice".

## Part I · Notes from a file you already have (5 minutes; .m4a / .mp3 need ffmpeg)

1. Press <kbd>Win</kbd>+<kbd>R</kbd>, type `cmd`, press <kbd>Enter</kbd>, paste this and press <kbd>Enter</kbd>:
   ```
   winget install Gyan.FFmpeg
   ```
   Answer `Y` if asked. It ends with `Successfully installed`.
2. Close the black window. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
3. On the **Work** tab, the Recorder's bottom line now says **Audio · Opus** (files about ten times smaller). If it still
   says **WAV (no ffmpeg)**, restart the PC once.
4. Click in the command box at the top of the HUD, type this with your own file's full path, and press <kbd>Enter</kbd>
   (tip: in File Explorer, <kbd>Shift</kbd>+right-click the file → **Copy as path**, then paste):
   ```
   make notes from C:\Users\J\Downloads\call.m4a
   ```
5. You get "Making notes from call.m4a. They'll be on the HUD." The panel shows **Reading call.m4a**, then
   **Transcribing call.m4a · 00:05:00** as it goes (the time is how far into the file it is), then the notes.

## Part J · Privacy checks (2 minutes; next time you stream)

1. Go live in OBS. Press <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd>. Nothing records, and a notice says
   "You're live, so I won't record. Say “record on stream” if you mean it."
2. Optional: say "Vesper, record on stream". The panel shows a red **On stream** badge, and the transcript and notes stay
   hidden on the HUD while you're live.
3. Off stream, start a recording, then go live: you hear "You're live, so I stopped recording." and the notes are made
   from what came before.
4. Say "Vesper, go off the record", then "Vesper, start recording": it refuses until "Vesper, back on the record".

---

**What to send back**
- A screenshot of the **Work** tab's Recorder right after Part B step 8 (the notes on the right).
- The exact words Vesper said in Part B step 8, and in Part H step 2.
- If anything showed in red or amber in the panel (for example **No call audio**, **Notes failed**, **(not active)**),
  hover it and send the text it shows.
- If you did Part F: does the call show as **Others**? (yes/no)
