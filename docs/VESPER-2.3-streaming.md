# Vesper 2.3 · Streaming: live captions, YouTube Live next to Twitch, and how each stream compares

Three things for streams:

- **Live captions**: what you say shows as captions on stream (an OBS Browser source, or an OBS Text source). It runs on
  your PC with the speech-to-text Vesper already uses. Vesper's own voice and anything you say *to* Vesper stay out.
- **YouTube Live**: if you stream to YouTube too, its chat joins the co-pilot (questions, chat mood, who to greet,
  clip-worthy moments), and "set the title" can change Twitch and YouTube together. Without YouTube, everything stays
  Twitch-only, exactly as before.
- **Stream recap & growth**: after each stream, the recap says how it went next to your last 5 streams (average and peak
  viewers, follows, chat messages an hour, length, category), and once there are enough streams, what went with your
  better ones (the game, the start time, the length).

**What it does**

| | |
|---|---|
| Captions on / off | "captions on", "captions off", "turn on captions", "stop captions", "subtitles off"; or the switch on the **Stream** tab → **Live captions**. Off until you turn them on |
| When captions show | while you're live, and for 3 minutes after you turn them on (so you can line them up in OBS before going live). Each line stays up about 7 seconds |
| What's left out | Vesper's own voice coming back through your speakers; anything with "Vesper, …" in it; your answers to Vesper's questions and conversation turns; voices the speaker check turned away; "hmm"/"uh"; everything while you're **off the record**. Passwords, keys and card numbers become `[secret]` |
| Where they go | the OBS overlay's **Captions** panel (a Browser source), and, if you name one, an OBS **Text** source |
| YouTube chat | read while you're live on YouTube, into the co-pilot: questions show "· YouTube" after the name, mood and greetings count it, the recap shows **YouTube msgs**. Viewers wrote it: Vesper never reads it out or acts on it |
| YouTube quota | YouTube allows 10,000 units a day (reset at midnight Pacific). A chat read costs 5. Vesper keeps 1,500 for everything else and spreads the rest over a 5-hour stream: about one read every 11 seconds. The **YouTube Live** panel shows today's use |
| Titles | "set the title to …" asks once "Twitch and YouTube, or just Twitch?" and remembers. Then the usual yes question, as for Twitch today. "set the YouTube title to …", "set the YouTube description to …": YouTube only. "titles on Twitch only" / "titles on both": change your choice |
| Recap & growth | **Stream** tab → **Co-pilot** → **Last stream**: a card **Next to your last 5 streams** with each number, better or worse and by how much. "how did that stream compare", "stream growth" |

**Phrases**

| Say | What happens |
|---|---|
| "Vesper, captions on" / "Vesper, captions off" | live captions on or off |
| "Vesper, set the title to Ranked grind" | the first time: "Twitch and YouTube, or just Twitch?"; then the yes question |
| "both" / "just Twitch" | your answer (no "Vesper" needed right after the question) |
| "Vesper, set the YouTube title to …" / "… YouTube description to …" | YouTube only, after a yes |
| "Vesper, titles on Twitch only" / "Vesper, titles on both" | where "set the title" goes from now on |
| "Vesper, how's YouTube chat" | connected? live there? how many chat messages so far |
| "Vesper, how did that stream compare" / "Vesper, stream growth" | the comparison with your last 5 streams |
| "Vesper, stream recap" | as before, now ending with the biggest change (e.g. "Average viewers up 21% on your last 5") |

**Settings** (optional, `config.yaml` → `skills:`)

- `stream_captions:` `on` (false), `obs_source` (the OBS Text source's name; the HUD box sets it), `keep_s` (7: seconds a
  line stays up), `preview_s` (180), `hide` (a list of words shown as `***`, e.g. your real name).
- `stream_youtube:` `account` (which Google account, if more than one allowed YouTube), `title_on` (both / twitch; set by
  your answer), `daily_quota` (10000), `reserve` (1500), `stream_hours` (5: how long a stream the chat budget must last),
  `min_chat_s` (3).

**What it costs mid-game.** Captions add no new model. Vesper's speech-to-text (Parakeet 110M, on the CPU) already runs
on every sentence when your wake mode is "transcript"; with a trained wake word ("acoustic" mode) it normally skips
sentences without the name, and while captions are showing it hears them all. Measured: about 0.05 CPU-core seconds per
second of speech (150 ms of 2 threads for a 7-second sentence). Talking half the time, that is about 2.5% of one core,
well under 1% of your whole CPU, and nothing on the GPU. YouTube chat is one small web request every ~11 seconds, only
while you're live.

**Good to know**

- Captions show what your mic hears as speech. With a voice profile (Setup → Voice), sentences in other voices are
  left out only when they were meant for Vesper; a friend talking into your mic would be captioned. Use `hide` for words
  you never want on screen.
- A caption appears when you finish a sentence (Vesper hears sentences, not single words), usually within a second.
- Nothing is kept: captions live in memory for a few seconds, never in a log or the database.
- The recap's viewer numbers are Twitch's. YouTube chat counts in "chat messages an hour".
- Patterns ("Evening starts averaged 42 viewers, 35% more than the rest") are what happened so far, not proof: a
  handful of streams can't tell a good game from a good night.

---

## Part A · Update (3 minutes; waits on the merge)

1. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
2. Wait for the black window to finish. Vesper restarts and the HUD reloads by itself.
3. Click the **Stream** tab. You should see two new panels: **Live captions** (with a switch that says **Off**) and
   **YouTube Live** (next to **YouTube**).

## Part B · Captions in OBS (5 minutes)

1. On the **Stream** tab, in **Live captions**, click **Copy captions link for OBS**. A message says "Captions link
   copied".
2. Open OBS. In the **Sources** box at the bottom, click **+**, then **Browser**.
3. Choose **Create new**, type `Vesper captions`, click **OK**.
4. In **URL**, delete what's there and press <kbd>Ctrl</kbd>+<kbd>V</kbd>. The link starts with
   `http://127.0.0.1:8765/overlay?panels=captions`.
5. Set **Width** to `1920` and **Height** to `300`, then click **OK**.
6. Drag the new source to the bottom of your canvas (it's see-through, so you'll only see it once there's a caption).
7. Back in the HUD, click the **Live captions** switch. It turns red, and the panel's corner says **PREVIEW 180s**.
8. Say a sentence without "Vesper", for example "testing the captions, one two three". Within a second it shows in the
   panel and in OBS at the bottom, then fades after about 7 seconds.
9. Say:
   ```
   Vesper, what time is it
   ```
   Neither your question nor Vesper's answer appears in the captions.

Optional, a plain OBS Text source instead (or as well):

10. In OBS, **Sources** → **+** → **Text (GDI+)** → **Create new**, type `Captions`, click **OK**, pick a font and size,
    click **OK**.
11. In the HUD's **Live captions** panel, click the box that says **OBS Text source name (optional)**, type `Captions`,
    click **Save**. The next sentence you say shows in that source too.
    - If an amber line says "OBS has no source called …", the names don't match: check the spelling in OBS.

## Part C · Connect YouTube Live (5 minutes; only if you stream to YouTube)

This uses the Google sign-in you set up for Gmail and Calendar, plus one more box.

1. Open the Google Cloud console's API library: press <kbd>Win</kbd>+<kbd>R</kbd>, paste the line below, press
   <kbd>Enter</kbd>.
   ```
   https://console.cloud.google.com/apis/library/youtube.googleapis.com
   ```
2. At the top, check the project is the one with your Vesper sign-in key (the same one as for Gmail). Click **Enable**
   (if it says **Manage**, it's already on).
3. In the HUD, **Stream** tab → **YouTube Live**, click **Connect YouTube Live**. Your browser opens Google's page.
4. Pick the Google account that owns your YouTube channel. If Google asks which channel, pick yours.
5. If it says "Google hasn't verified this app", click **Continue** (same as for Gmail).
6. Tick **Manage your YouTube account**, then click **Continue**. The page says you're connected; close it.
7. The **YouTube Live** panel now shows your account, **Not live.**, and a **Quota today** bar.
8. Say:
   ```
   Vesper, how's YouTube chat
   ```
   You should hear "YouTube Live is connected; you're not live there right now."

## Part D · Your next stream (5 minutes of checks; waits on your next stream)

Before going live:

1. Say:
   ```
   Vesper, set the title to Testing Vesper 2.3
   ```
2. You hear "Twitch and YouTube, or just Twitch?" (only this first time). The **YouTube Live** panel shows the same
   question with two buttons.
3. Say `both` (or click **Twitch + YouTube**). If you said it, you then hear "Set the stream title on Twitch and YouTube
   to “Testing Vesper 2.3”? Say yes or no."; say `yes`. A click on the button is already your yes.
4. You hear "Title updated on Twitch and YouTube." If YouTube has no stream yet, you hear "Title updated on Twitch.
   YouTube didn't take it: There's no YouTube stream to rename yet…": that's fine, schedule one in YouTube Studio or set
   it again once you're live.

Once you're live on both:

5. Within a minute, the **YouTube Live** panel shows a red **LIVE** badge and your YouTube title, and a notice says
   "Live on YouTube too: its chat joins the co-pilot."
6. From your phone, type `is this working?` in your YouTube chat. Within about 15 seconds it shows in **Co-pilot** →
   **Questions in chat**, with **· YouTube** after the name.
7. Say:
   ```
   Vesper, captions on
   ```
   The **Live captions** corner says **ON AIR**. Talk to chat for a minute and watch the captions in OBS's preview.
8. After the stream, wait about two minutes. **Co-pilot** → **Last stream** shows the recap with a new card: **This
   stream** (fewer than 5 streams so far: just the numbers) or **Next to your last 5 streams**.
9. Say:
   ```
   Vesper, how did that stream compare
   ```

## Part E · What to send back

- A screenshot of the **Stream** tab during your next stream, showing **Live captions**, **YouTube Live** and the
  **Co-pilot** questions.
- A screenshot of OBS with a caption showing.
- The exact words Vesper said in Part C step 8 and Part D steps 2 and 4.
- Anything in amber or red in the **Live captions** or **YouTube Live** panels (copy the text).
- After the stream: a screenshot of the **Next to your last 5 streams** (or **This stream**) card, and whether a caption
  ever showed something you said *to Vesper*, or Vesper's own voice. That must never happen.
