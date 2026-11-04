# Vesper 2.3 · Voice: measured on your mic, not guessed

Three things were going wrong: Vesper ignored you ("didn't sound like your voice"), cut you off mid-sentence, and
misheard names. The settings behind all three (how much your voice must match, how long a silence ends a sentence,
how sure the name model must be) were sensible guesses, never measured on your mic, your room and your games. 2.3
measures them, and adds two fixes you can see working.

**What it does**

| | |
|---|---|
| Voice test | **Setup → Voice test · words I hear**. You read about 36 of your own recent commands (taken from what you've said to Vesper), then leave a few minutes of only background: the game, Discord, music, and Vesper talking. It replays all of it through the same chain the live mic uses (voice detector, end of sentence, speech-to-text with your word fixes and names, the name model, the name check on the text, the voice check) and shows: word errors, missed wakes, commands cut off, false wakes per hour of background, your voice scores against every other voice, and how long from when you stop talking to the text. Then it suggests settings from those numbers. **Nothing changes until you tick and click Apply.** Run it again on the same recordings after a change, or record again |
| Natural end of turn (opt-in) | Instead of "400 ms of silence ends the sentence", a small model (Smart Turn v3) listens at every short pause and decides whether you sounded finished. A thinking pause ("remind me to … call the dentist") doesn't end your turn; a finished "open Discord" is answered about 200 ms sooner. 1.5 s of silence always ends it. Off until the voice test shows it helps on your voice (Apply), or you click **Natural** under **End of a sentence** on the same panel |
| Words it learns | Say or type "you misheard Balatro as Belatro" (or "I said Niamh, not Niam"), and Vesper asks first: "Always write “Balatro” when I hear “belatro”? Say yes or no." Or retype on the HUD what you just said, with the word fixed: a card offers **Remember**. Saved only on your yes or click; listed on the panel with **Remove** |
| Names it expects | The game on screen, the Twitch category, recent chatters, the people you email most, your scenes, apps and words you taught it are passed to speech-to-text as names to listen for. Nothing to do; the **Your voice** panel says "names expected (N)" |

**Phrases**

| Say (or type) | What happens |
|---|---|
| "Vesper, you misheard Balatro as Belatro" | asks, then on "yes" always writes Balatro for belatro |
| "Vesper, you heard Shivon but I said Siobhan" | the same, the other way round |
| "Vesper, I said Kaelthas, not chaos" | the same |
| "it's Niamh, not Niam" | the same, but only right after it heard "Niam" and only for a near-spelling (so "it's Tuesday, not Monday" still changes your meeting) |

It never learns a fix for words that steer it (yes, no, stop, cancel, its name) or tiny words (to, the, it): a fix
applies to every sentence, and a "no" turned into "yes" would answer a question you refused.

**Settings** (`config.yaml` → `voice:`; the Apply button writes the same keys to `data/settings.yaml`, which wins)

| Key | Default | |
|---|---|---|
| `end_of_turn` | `silence` | `smart` = natural end of turn (downloads a 9 MB model once) |
| `turn_pause_ms` | `200` | smart: the pause it checks at |
| `turn_max_silence_ms` | `1500` | smart: the silence that always ends your turn |
| `endpoint_ms` | `400` | silence: the silence that ends a sentence (and smart's fallback when the model is missing) |
| `vad_threshold` | `0.6` | how sure the voice detector must be that a sound is speech |
| `wake_thresholds` | from the model | per trained name model, e.g. `{vesper: 0.6}` |
| `hotwords_score` | `2.0` | how hard Parakeet listens for the names; `0` turns it off |
| `corrections` | `{}` | words it keeps mishearing; learned ones are saved to `data/settings.yaml` |

The voice match threshold lives in your voice profile; Apply writes it there.

**What was measured here, and what wasn't**

- *Names*: 34 synthetic commands full of names (Kokoro voices; Balatro, Silksong, Zentreya, Siobhan, Helldivers,
  Niamh…). Parakeet with the names as hotwords: word errors 22.9% → 14.9%, exact transcripts 9 → 16. On 36 everyday
  commands with no names: no change (0.6%). About 12 ms more per sentence. Hence on by default; the voice test checks
  it on your voice and suggests turning it off if it hurts.
- *End of turn*: on 70 synthetic commands Smart Turn called finished sentences finished 97–99% of the time; phrases
  that trail off ("Open Spotify and") were held open about half the time. In a replay with a 0.8 s pause spliced into 12
  commands, silence (400 ms) cut 11 of them, Smart Turn 8, and Smart Turn answered the rest in a median 212 ms instead
  of 416 ms. Synthetic voices end every phrase like a sentence, so that's a weak test: your own voice decides. Hence
  opt-in, with the voice test as the judge (it weighs each cut-off as about 4 seconds of repeating yourself against
  the wait after every command).
- *Not measured here*: your mic, your voice, your games, Windows. That's what the voice test is for.

Files: recordings in `%USERPROFILE%\Vesper\data\bench\voice-test\` (WAV, your voice and whatever played in the
background, about 10 MB), results in `%USERPROFILE%\Vesper\data\bench\voice-test.json`. They stay on this PC;
**Delete recordings** removes the audio and keeps the results.

---

## Part A · Update (3 minutes; waits on the merge)

1. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
2. Wait for the black window to finish. Vesper restarts and the HUD reloads by itself.
3. Click the **Setup** tab. Under **Your voice** and **Speaking voice** there's a new wide panel: **VOICE TEST · WORDS
   I HEAR**, with **Start the voice test** on the left and **Words I hear right now** on the right.
4. In the **Your voice** panel there's a new line, **End of a sentence**, saying `after a silence · names expected (…)`.
   If it says `names not expected` with a reason, send me that line.

## Part B · The voice test (about 10 minutes)

Do this at your normal desk, with your normal mic and headset or speakers, the way you play.

1. On **Setup**, in **VOICE TEST · WORDS I HEAR**, click **Start the voice test**.
2. A box appears with about 36 lines: your own recent commands first, then everyday ones, and four with a `…` in them.
   Change or delete any line that's wrong (one command per line). Leave the `…` lines in.
3. Next to **Start recording**, leave **3 min of background** selected.
4. Start your game (in a window or fullscreen), join Discord if your friends are on, and start music if you usually
   have it. Then come back to the HUD.
5. Click **Start recording**. The panel shows **YOUR COMMANDS · 1/36**, a red dot and the line to say, e.g.
   `Say: “Vesper, open Discord”`, with a level bar that moves when you talk.
6. Say each line the way you'd normally say it to Vesper, once, when the red dot shows. On the `…` lines, pause for
   about a second at the dots, as if you were thinking. Vesper doesn't act on anything you say during the test.
7. After the last line the panel shows **BACKGROUND ONLY · 1/6** and
   `Don't talk to me now: play, chat on Discord, let the music run. I'll say a few things too.`
8. Switch to your game and play for the 3 minutes. Talk to Discord if you like, not to Vesper. Vesper says four short
   lines out loud during this part; that's meant (its own voice is part of your background).
9. Come back to the HUD. The panel shows **WORKING IT OUT** for about a minute. Vesper doesn't listen for commands
   while it does that.
10. The results appear: five tiles (**word errors**, **missed wakes**, **cut off**, **false wakes / hour**,
    **speech → text**), a strip with your voice scores on one row and other voices on the other, and under it either
    **Suggested from your numbers** or **Nothing to change**.

If a step goes wrong: **Cancel** stops the test and gives the mic back. A red line `The test failed: …` means
something broke; send me that line.

## Part C · Read the results and Apply (3 minutes)

1. Take a screenshot of the whole panel before you change anything.
2. Read each suggestion. Each has a tick box, what you have now → what it suggests, and why in numbers, e.g.
   `Voice match: 0.6 → 0.52 · At 0.6: 7 of your 36 commands turned away, 0 of 14 other voices let in. At 0.52: 1 and 0.`
3. Untick any you don't want. (The two **End of turn** / **Silence that ends a sentence** lines go together.)
4. Click **Apply ticked**. A toast says `Applied N settings. Listening again with them.` and each applied line says
   `· applied`.
5. Click **Run again on these recordings**. After about a minute the tiles update with the new settings on the same
   audio. The numbers you applied for (cut off, turned away, false wakes) should be the same or better.
6. Click **What I got wrong (…)** under the suggestions to see each command it misheard: you said / I heard.

## Part D · Teach it a word by voice (2 minutes)

1. Pick a name from **What I got wrong** that it misheard, for example it wrote `Belatro` for `Balatro`.
2. Say:
   ```
   Vesper, you misheard Balatro as Belatro
   ```
   (use your own two words: what you meant first, then what it wrote).
3. Vesper asks `Always write “Balatro” when I hear “belatro”? Say yes or no.` and a card appears on the HUD.
4. Say:
   ```
   yes
   ```
5. It says `Got it. When I hear belatro, I'll write Balatro.` The word appears under **Words I hear right now** as
   `belatro → Balatro` with **Remove**.
6. If it answers `I heard both of those as …`, it misheard the right word too. Type the fix instead: in **Words I hear
   right now**, put the word it writes in the first box, the right word in the second, and click **Add**.

## Part E · Teach it a word by retyping (1 minute)

1. Say a command with a name it gets wrong, for example:
   ```
   Vesper, set the category to Balatro
   ```
2. If it shows `set the category to Belatro` on the HUD, click the command box at the top of the **Command** tab
   within a minute and type what you meant:
   ```
   set the category to Balatro
   ```
   then press <kbd>Enter</kbd>.
3. A card appears: `You typed “Balatro” where I heard “belatro”. Always write it that way?` with **Remember** and
   **No**. Click **Remember**. (It also waits on the **Setup** panel if the card closes.)

## Part F · Natural end of turn by hand (optional, 1 minute)

Only if the voice test didn't suggest it and you want to try it anyway.

1. On **Setup**, in **VOICE TEST · WORDS I HEAR**, find **End of a sentence** under the test's buttons.
2. Click **Natural**. The first time, Vesper downloads a 9 MB model, which takes a few seconds.
3. On **Your voice**, **End of a sentence** should say `natural (Smart Turn)`. If it says
   `silence (Smart Turn unavailable: …)`, the model didn't download; send me that line.
4. Try a command with a thinking pause in the middle:
   ```
   Vesper, remind me to … call the dentist tomorrow
   ```
   It should wait through the pause and take the whole sentence.
5. To go back, click **After a silence**.

---

## What to send back

- The screenshot from Part C step 1 (the results before you applied anything), and one after step 5.
- Which suggestions you applied, and whether you now get cut off or ignored less over the next day of streaming.
- From Part D or E: the exact words Vesper said back.
- Anything in red on the **VOICE TEST · WORDS I HEAR** or **Your voice** panel, word for word.
- If anything felt worse: press <kbd>Win</kbd>+<kbd>R</kbd>, paste the line below, press <kbd>Enter</kbd>, and send
  the file it opens.
  ```
  notepad %USERPROFILE%\Vesper\data\bench\voice-test.json
  ```
