# Vesper 2.3 · Phone and memory

Two things you asked for, "phone hand-off" and "smarter memory". Most of both was already there: the Telegram bot
runs anything you'd type and answers, and memory already finds facts by words and by meaning, writes your profile
each night and tidies itself on Sunday. This closes the gaps that were left.

**What's new**

| | |
|---|---|
| Voice messages | Hold the mic button in your bot's chat and talk. The PC turns it into text with its own speech-to-text (nothing goes to a cloud) and runs it exactly like a typed message. The answer starts with **Heard: “…”** so you see if it misheard. Anything that needs a yes still asks with **Yes** / **No** buttons, and a spoken "yes" or "no" answers it like a typed one |
| Limits | Voice messages up to 2 minutes (and 2 MB), at most 6 in 10 minutes. Only your paired chat: anyone else's voice message isn't even downloaded. The audio file is deleted as soon as it's read |
| Answers later | Something long you start from the phone answers there when it's done, if you're still away from the PC (no mouse or keyboard for 5 minutes): a background job ("run the morning prep", "research …"), the notes from a recording you stopped or started from the phone, notes from a file. At the PC you get the usual card instead |
| Model test | If **Test my models** finishes while you're away, the result comes to your phone (only if **Push when away** is on) |
| Memory finds less junk | A question about something Vesper doesn't know ("who won the football last night", "remind me to call the bank") no longer drags in unrelated facts just because they share a word like "night" or "called" |
| Newest wins | When a fact changed and both versions are still on file, the newer one comes first, even if you asked about the old one a lot, and the model is told the later date is the one that's true now |
| Forget means forget | "Forget that I like pineapple" now also takes it out of your profile (the paragraphs on **Setup → Memory**), not only out of the facts. Before, the nightly profile could keep it |
| Only your words | Memory learns only from what you say, type in the HUD, or send from your paired phone. Never from routines, the planner or jobs (Vesper's own words), never from a voice the speaker check marked as someone else's, never from chat, email or other people's messages, never while live |

**Phrases** (from the phone, typed or as a voice message): anything you'd say to Vesper. New ones worth knowing:
"run the morning prep" (a job that answers on the phone), "stop recording" (the notes come to the phone),
"approve the inbox triage job" (when a job's message says it's waiting on your OK), "add the recording tasks".

**Settings:** nothing new to set. **Setup → Phone → Push when away** also covers the model test. `skills.phone.away_after_s`
(300) in `config.yaml` is how long without mouse or keyboard counts as away. Voice messages need ffmpeg (Part B).

### How memory was measured

`evals/memory_recall.py` is a small offline test: 41 facts a streamer with a day job would tell Vesper (learned over
two months, two of them changed later: a new manager, the stream moved to Fridays), 32 questions in your style
("who's my boss", "when's leah's birthday", "who mods my chat") and 15 that nothing answers ("what's the weather
tomorrow", "what day is it"). It scores what Vesper hands the model with each question (the top 3 facts). "Stand-in
meaning" is a fake embedding model that knows a few dozen synonyms: it checks the logic, not how good
`embeddinggemma` is (Part E measures that on your PC).

| | right fact in top 3 | junk for a question nothing answers | newest version first |
|---|---|---|---|
| words only, before | 84% | 27% | 50% |
| words only, after | 84% | **0%** | 50% |
| stand-in meaning, before | 91% | 33% | 75% |
| stand-in meaning, after | **94%** | **7%** | **100%** |

What it found and what changed: (1) single everyday words ("night", "day", "call", "long") matched facts that merely
contained them, so they no longer count as search words; (2) every fact Vesper showed the model counted as "used
lately", which pushed a changed fact's old version above the new one, so freshness is now when a fact was learned.
Words only still misses true paraphrases ("who's my boss" for "manager"): that's what the meaning model is for, and
why its line on **Setup → Memory** should say it's on.

---

## Part A · Update (3 minutes; waits on the merge)

1. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
2. Wait for the black window to finish. Vesper restarts and the HUD reloads by itself.

## Part B · ffmpeg, for voice messages (5 minutes; skip if you did the Recorder's Part I)

1. In the HUD, click **Setup**, then **Health check**, then **Run check**.
2. Find the row **Phone voice messages**. If it says **ffmpeg found**, skip to Part C.
3. If it says **need ffmpeg**: press <kbd>Win</kbd>+<kbd>R</kbd>, type `cmd`, press <kbd>Enter</kbd>, paste this and
   press <kbd>Enter</kbd>:
   ```
   winget install Gyan.FFmpeg
   ```
   Answer `Y` if asked. It ends with `Successfully installed`.
4. Close the black window. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
5. **Setup → Health check → Run check** again: **Phone voice messages** now says **ffmpeg found**. If it still says
   **need ffmpeg**, restart the PC once and check again.

## Part C · A voice message (2 minutes)

1. On your phone, open **Telegram** and the chat with your Vesper bot.
2. Hold the **microphone** button at the bottom right, say "what's on my calendar today", and let go.
3. Within a few seconds you should get an answer that starts with **Heard: “What's on my calendar today?”** and then
   your calendar. If the Heard line is wrong, that's the speech-to-text; send it again a bit slower.
4. Hold the microphone again and say "take a screenshot". The answer says screenshots don't go to the phone (voice
   follows the same rules as typing).
5. If instead you get "the PC needs ffmpeg to play it", do Part B.

## Part D · Long work answers on the phone (10 minutes; mostly waiting)

1. Step away from the PC: don't touch the mouse or keyboard for 5 minutes.
2. From your phone, send (typed or as a voice message):
   ```
   run the morning prep
   ```
3. Vesper answers that it started the job.
4. Keep away from the PC. When the job is done (a minute or two), your phone gets **Morning prep is done.** and a
   short summary. If the job needs your OK for something, the message says what and ends with
   “approve the … job”: send that line back to say yes.
5. Back at the PC, the same job is on the **Jobs** panel with Copy, as before.

## Part E · Memory checks (5 minutes)

1. Click in the command box at the top of the HUD, type this and press <kbd>Enter</kbd>:
   ```
   remember that I like pineapple on pizza
   ```
2. Wait a minute, then click **Setup**, then **Memory**. **I like pineapple on pizza** is in the list of what it knows.
3. In the command box, type this and press <kbd>Enter</kbd>:
   ```
   forget that I like pineapple on pizza
   ```
   Vesper says **Forgotten: I like pineapple on pizza.**
4. On **Setup → Memory**, it's gone from the list, and none of the three profile paragraphs at the top mention pineapple.
5. Optional, the real meaning model: press <kbd>Win</kbd>+<kbd>R</kbd>, type `cmd`, press <kbd>Enter</kbd>, and paste
   these two lines one at a time, each followed by <kbd>Enter</kbd> (Ollama must be running; it's in the tray):
   ```
   cd %USERPROFILE%\Vesper
   ```
   ```
   .venv\Scripts\python -m evals.memory_recall --ollama embeddinggemma
   ```
   You should see a table with a row for **embeddinggemma**. It takes under a minute.

**Send back:** a screenshot of the answer in Part C step 3, the phone message from Part D step 4, a screenshot of
**Setup → Memory** after Part E step 4, the table from Part E step 5 (copy the text), and anything in red.
