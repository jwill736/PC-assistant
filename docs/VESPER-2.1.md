# Vesper 2.1 · conversation mode and 34 upgrades, and how to try each one

Vesper can now hold a real conversation: say "Vesper, let's talk" and keep talking, no name needed between turns.
Around that, 34 upgrades: dictation anywhere, actions on selected text, faster replies, a "why did you say that",
smarter updates, stream tools (clip-worthy moments, raid planner, privacy while live), a life review, a mini HUD,
the HUD on your phone, an OBS overlay, low-power mode in games, and a security review.

Do Part A first. After that, the parts can be done in any order; each says how long it takes and what you should see.
Part 1 of every section in [VESPER-2.md](VESPER-2.md) still applies (Google, Telegram, Spotify and so on).

**What's new at a glance**

| Area | What it does | Needs |
|---|---|---|
| Conversation mode | "Let's talk": every sentence is a turn, talk over it to interrupt, ends on "that's it" or a quiet minute | the local model (Setup → Brain) |
| Follow-ups | After any reply, ~7 seconds to keep talking without saying "Vesper" | nothing |
| Dictation | Hold Ctrl+Alt+D, talk, let go: typed where your cursor is; "comma", "new line" work | nothing (tidier with the model) |
| Selected text | Select text, say "fix this / make it shorter / translate to Spanish / explain this / reply to this" | the local model |
| Spoken yes/no | "Close Notepad? Say yes or no." Answer without the name, for 10 seconds | nothing |
| Faster replies | Built-in replies are spoken before anything else; Setup shows your real "you stop → it speaks" time | nothing |
| Why | Every update says why: the rule and what it saw. "Vesper, why did you say that" | nothing |
| Look-around | Every 45 minutes, at a natural pause, the model looks over your day and speaks up only if it's worth it | the local model |
| Replies close themselves | When someone you're waiting on replies, they drop off and reminders about them cancel | Gmail |
| Memory tidy-up | Weekly: duplicates merged, clashes asked about, stale facts retired (never deleted) | nothing |
| Your rhythm | Learns your work hours, lunch, stream nights, bedtime; one click sets quiet hours from it | a few days of use |
| Week in review | Hours, focus, habits, money, streams, wins, one suggestion; a shareable card | nothing |
| Subscription audit | What you pay for but haven't opened in 60 days, with the saving | 60 days of activity tracking |
| Travel day | Leave-by time, check-in, weather there and a packing list, from the day before a flight | Gmail |
| Category switch | "Switch category to Hades II?" when your game and Twitch category differ | Twitch |
| Clip-worthy moments | Chat spikes, you getting loud, raids → a marker and a "Clip that?" card; highlights list after | Twitch + OBS |
| Raid planner | Who's live in your category, ranked (similar size, friends, not raided this week) | Twitch, reconnected (Part K) |
| Privacy while live | Inbox, Replies, People, Money, Journal, Memory… blur while you're live | nothing |
| Layout | Drag, resize and hide panels on every tab | nothing |
| Mini HUD | A small always-there window: orb, last thing said, next event, timers | nothing (PowerToys for on-top) |
| Phone HUD | The HUD on your phone over home Wi-Fi, behind a PIN | your phone on the same Wi-Fi |
| OBS overlay | Now playing, goals, chat mood, countdown, follower ticker as a browser source | OBS |
| Low-power mode | In a game (or live) background checks slow down or pause | nothing |
| Security | Foreign web pages blocked, phone pairing lockout, secrets never kept, clipboard privacy | nothing |
| Model test | The model test now scores everyday jobs too; a 61-question quality test you can run | Ollama |

---

## Part A · Update (3 minutes; waits on the merge)

1. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
2. Wait for the black window to finish. Vesper restarts and the HUD reloads by itself.
3. On the **Command** tab you should see a new panel at the top: **CONVERSATION MODE** with a **Talk** button.
4. In the top bar, between the bell and the light/dark button, there's a new grid button (Edit layout).

If the Conversation mode panel says "Needs a model to talk with", click **Setup**, scroll to **Brain · what answers
you**, and check the **Local model** line says your model (qwen3.6) is ready. Then come back.

---

## Part B · Have a conversation (3 minutes)

1. Say:
   ```
   Vesper, let's talk
   ```
2. You should hear something like "I'm listening. What's on your mind?" The orb label reads **IN CONVERSATION**.
3. Without saying "Vesper", say:
   ```
   What do you think about pineapple on pizza?
   ```
4. You should hear a short answer, and both lines appear as bubbles in the Conversation mode panel (yours blue,
   Vesper's orange).
5. While it's still answering, talk over it:
   ```
   Actually, tell me about mushrooms instead
   ```
6. It should stop at once and answer the new question.
7. Click in the panel's **Say something…** box, type this and press <kbd>Enter</kbd>:
   ```
   quiz me on three capital cities
   ```
8. It answers out loud, in the same conversation. Answer its quiz question out loud.
9. Say:
   ```
   Thanks Vesper, that's it
   ```
10. You should hear "Okay. I'm here when you need me." The panel shows **Talk** again.

Good to know:
- A quiet minute also ends it, and so does going live. On stream it only starts if you say "let's talk on stream", and
  then it never mentions anything private.
- Commands still work mid-conversation ("what's on my calendar", "add a task…").
- Updates and nudges wait until the conversation ends.
- "Stop" or "wait" over a reply just stops it; "stop talking" stops it and ends the conversation.

## Part C · Follow-ups without the name (1 minute)

1. Say:
   ```
   Vesper, what's the weather?
   ```
2. Within about 4 seconds of the answer ending, say:
   ```
   And tomorrow?
   ```
3. It answers without you saying "Vesper".
4. Wait 10 seconds, then say:
   ```
   And Saturday?
   ```
5. Nothing should happen: the window has closed. (It never opens while you're live.)

---

## Part D · Dictation (2 minutes)

1. Press <kbd>Win</kbd>+<kbd>R</kbd>, type this and press <kbd>Enter</kbd>:
   ```
   notepad
   ```
2. Click inside Notepad.
3. Hold <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>D</kbd>, say "um this is a test comma new line second line period", then
   let go. A **Dictating** chip shows at the bottom of the HUD while you hold the keys. Notepad should show:
   ```
   This is a test,
   Second line.
   ```
4. With Notepad still in front, say "Vesper, dictate", wait for "Go ahead.", then say "See you at eight." It's typed
   into Notepad.

It refuses password fields and password managers. Note: Word uses Ctrl+Alt+D for endnotes; if that bothers you, the
keys can be changed (`skills.dictation.hotkey` in config).

## Part E · Selected text (2 minutes; needs the local model)

1. In Notepad, type this line, then select all of it with <kbd>Ctrl</kbd>+<kbd>A</kbd>:
   ```
   teh meeting is moved to thursday sorry
   ```
2. Say "Vesper, fix this". The line is replaced with a corrected version and Vesper says "Fixed."
3. Select the line again and say "Vesper, explain this". The answer shows in the **Selected text** panel on the **PC**
   tab, with a **Copy** button.

Also: "make it shorter", "make it more professional", "translate this to Spanish", "summarise this", "reply to this".
A long rewrite asks "Replace your selection with it?" first.

## Part F · Spoken yes/no and speed (2 minutes)

1. Say "Vesper, close notepad". Vesper asks "Close notepad? Say yes or no." The HUD card shows **Listening for yes or
   no**.
2. Say "no" (no name). Vesper says "Cancelled."
3. Say "Vesper, close notepad" again and stay silent. After about 10 seconds the card says "Waiting until …" and Vesper
   says nothing more. Click **Cancel** on the card.
4. Click **Setup**, find **Speaking voice**. The row **You stop → it speaks** shows a time in ms (built-in replies vs
   the model).

---

## Part G · Why, and look-around (2 minutes)

1. On the **Command** tab, in **Updates**, click **why** next to any update. A purple box shows the rule and what it saw
   (for example "Calendar: Client sync at 2 PM").
2. Click the bell at the top right. Under any notification, click **▸ WHY?**.
3. After Vesper says something by itself, say:
   ```
   Vesper, why did you say that
   ```
4. At the bottom of **Updates** there's "Look-around every 45 min". Click **Look now**. You should see either
   "Look-around: nothing worth saying." (the usual answer) or a new update with a **why**.
5. Over the next days, click 👍 or ✕ on its updates; it reads them before every look.

## Part H · Memory tidy-up and your rhythm (3 minutes)

1. Click **Setup**, then find **Memory**. Click **Tidy now**.
2. If two facts clash, an amber **Confirm** box asks (for example "Is your manager Sam Lee or Priya Shah?"). Click the
   right answer, or **Both** / **Not sure**.
3. Scroll to **Your rhythm**. It shows a day strip: Work, Lunch, Streams, Off the PC. With under 3 days of activity it
   says it needs more time; leave Vesper running.
4. When the line next to **Use these** looks right, click **Use these**. It sets quiet hours and break timing from your
   real day. **Undo** puts it back.
5. Say:
   ```
   Vesper, what's my routine
   ```

## Part I · Replies that close themselves (nothing to do; check over a few days; needs Gmail)

1. **Work** tab → **Replies** → **Waiting on**: when someone replies, they drop off, and the bell gets a quiet line like
   "Dana replied about “Contract redlines”."
2. **Promised**: when you've emailed what you promised, a **LOOKS DONE** card asks "Did you send Dana the revised
   deck?". Click **✓ Done** (it ticks the task too) or **✕ Not yet**.

---

## Part J · Life: week in review, subscription audit, travel day (5 minutes)

1. Say:
   ```
   Vesper, how was my week
   ```
   You hear your hours, focus sessions, habits, tasks done and streams, then one suggestion for next week.
2. On the **Life** tab, in **Week in review**, click **Open card**. A tall card opens. If no window opens, click the
   pop-up icon at the right end of the address bar, choose **Always allow**, and click **Open card** again.
3. In the card, click **Hide private** before sharing (mood and money disappear), then **Print / PDF** or snip it with
   <kbd>Win</kbd>+<kbd>Shift</kbd>+<kbd>S</kbd>.
4. Say:
   ```
   Vesper, audit my subscriptions
   ```
   Until Vesper has 60 days of activity it says "Nothing to cut yet: I've only tracked N days…". That's expected.
   Later, flagged rows in **Money** have **Remind me to cancel** (a reminder 2 days before renewal) and **Keep**.
5. Travel day: from the day before a flight that's in your Gmail, a **Travel day** card sits at the top of the Life tab
   with **Leave by …**, **Check in**, the weather there and a packing list. 30 minutes before you need to leave, Vesper
   says so. Try now:
   ```
   Vesper, what's the weather in Denver
   ```
6. Optional, for an exact drive time to the airport: press <kbd>Win</kbd>+<kbd>R</kbd>, paste this, press
   <kbd>Enter</kbd>:
   ```
   notepad %USERPROFILE%\Vesper\config.yaml
   ```
   Go to the very end of the file and paste these three lines (35 = your usual minutes to the airport; change it).
   If the file already has a line that is exactly `skills:`, paste only the last two lines straight under it instead.
   ```
   skills:
     travel:
       drive_min: 35
   ```
   Save (<kbd>Ctrl</kbd>+<kbd>S</kbd>), then restart Vesper: press <kbd>Win</kbd>, type `Update Vesper`, press
   <kbd>Enter</kbd>.

---

## Part K · Stream upgrades (3 minutes now, the rest happens on your next stream)

Raids need one more Twitch permission, so reconnect once:

1. Click **Setup**. In **Channel**, click **Disconnect**, then **Connect Twitch**.
2. Open the link it shows, enter the code, click **Authorize**. Twitch's page lists the new **Start raids** permission.
3. Click the **Stream** tab. Under the Co-pilot you should see **Clip-worthy moments**, **Raid planner** and **Privacy
   while live** (switch on, red).

On your next stream:

4. With OBS live, **Privacy while live** says **HIDING**. On the **Work** tab, Inbox, Replies and People show "Hidden
   while live · click to peek". A click shows one for 10 seconds.
5. Play a game that isn't your Twitch category for a minute: a card at the top right asks "Switch category to …?".
   Click it to switch.
6. When chat pops off, you get loud, or a raid lands, a red **Clip that?** card appears for 50 seconds. Click **Clip it**.
7. Near the end of your stream block (or when you ask), the raid planner fills in:
   ```
   Vesper, who should I raid
   ```
   Click **Raid**, then **Confirm raid**.
8. About 90 seconds after you go offline, **Clip-worthy moments** shows the highlights list. **Copy for promo video**
   copies it ready to paste to Claude for a promo cut.

---

## Part L · Your HUD, your way: layout, mini HUD, phone, OBS overlay (15 minutes)

**Layout (2 minutes)**
1. Click the grid button in the top bar (or <kbd>Ctrl</kbd>+<kbd>K</kbd>, type `Edit layout`, press <kbd>Enter</kbd>).
2. Drag any panel by its orange grip to a new place. Click **½** on a panel's bar to make it half width. Click the eye
   button to hide one.
3. Click **+ Add panel** at the bottom to bring a hidden panel back.
4. Click **Done**, then press <kbd>F5</kbd>. Your layout stays. **Reset tab** puts a tab back to normal.

**Mini HUD (2 minutes)**
1. Right-click the Vesper ring icon by the clock, click **Open mini HUD**. A small window opens at the top right.
2. Optional, to keep it on top: press <kbd>Win</kbd>, type `Microsoft Store`, press <kbd>Enter</kbd>, search
   `PowerToys`, click **Get**. Then click the mini window and press <kbd>Win</kbd>+<kbd>Ctrl</kbd>+<kbd>T</kbd>.

**Phone (5 minutes)**
1. **Setup** → **Phone · the HUD over Wi-Fi** → click **On**.
2. If Windows asks about Python and networks: tick **Private networks**, untick **Public networks**, click **Allow
   access**.
3. The panel says **Listening on your home network** with a QR code and a 6-digit PIN.
4. On your phone (same Wi-Fi), point the camera at the QR code and tap the link. Type the PIN, tap **Unlock**. The HUD
   opens with tabs along the bottom.
5. **New PIN** or **Sign out all phones** ends every phone session.

**OBS overlay (5 minutes)**
1. **Setup** → **OBS overlay**. Click the panels you want (orange = on). Type `10` under **Sub goal**, click **Save
   goals**.
2. Click **Copy OBS link**.
3. In OBS: **Sources** → **+** → **Browser** → name it `Vesper overlay` → **OK**. Paste into **URL**, set **Width**
   `600`, **Height** `900`, click **OK**.
4. Back in the HUD, click **5 min** under **Countdown**. A "Starting soon 4:59" card appears in OBS.

The overlay link has its own key: it shows only what's on the overlay and can't reach anything else. **New link** retires
the old one.

---

## Part M · Low power in games, and fixed phrases (5 minutes)

1. **Setup** → **Running parts (watchdog)**. Top right says **all healthy**; there's a new **MS** column.
2. Start any game fullscreen and play for 20 seconds, then <kbd>Alt</kbd>+<kbd>Tab</kbd> to the HUD. The panel says
   **low-power: in a game**; Clipboard says **Paused**, news and weather say **Slower**.
3. Quit the game and wait 20 seconds; the rows go back to **OK**.
4. Phrases that used to go wrong and now work:
   ```
   Vesper, turn off the PC
   ```
   It asks "Shutdown the PC? Say yes or no." Say `no`.
   ```
   Vesper, what's my GPU temperature
   ```
   You get the PC's health, not the weather.
   ```
   Vesper, every day at 8 am give me the weather
   ```
   It appears under **Reminders & routines** with ↻.
   ```
   Vesper, turn on do not disturb
   ```
   Vesper goes quiet for an hour. Say "Vesper, you can talk again" to undo it.

## Part N · Security (nothing to do)

Full write-up: [SECURITY.md](SECURITY.md). In short:
- Web pages in your browser can no longer talk to Vesper's port.
- 5 wrong phone pairing codes lock pairing for 10 minutes (longer each time).
- Passwords, PINs, codes, keys and card numbers are never remembered or logged.
- The clipboard skips banking, payment and private windows, and anything a password manager marks private.
- Calendar invites are treated like email: text from other people, never instructions.

## Part O · Test your models on everyday jobs (5 minutes per model; needs Ollama)

1. **Setup** → **Brain · what answers you** → **Test my models**. When it's done, each model in **Local model** reads
   like "passed, 7/8 jobs right, first word 0.4 s".
2. Optional, the full 61-question quality test: press <kbd>Win</kbd>+<kbd>R</kbd>, type `cmd`, press <kbd>Enter</kbd>,
   then paste these one at a time:
   ```
   cd /d %USERPROFILE%\Vesper
   ```
   ```
   .venv\Scripts\python -m evals.run --model qwen3.6:35b-a3b --k 3
   ```
   You should see lines like `[12/61] pass  tool-pizza …`, then a table starting with `### Vesper eval`.

---

## Things you can say (new in 2.1)

| Say | What happens |
|---|---|
| "let's talk" · "let's talk about my week" · "that's it" | conversation mode on / off |
| "dictate" · "dictate: see you at eight" | types your next words / these words |
| "fix this" · "make it shorter" · "translate this to Spanish" · "explain this" · "reply to this" | acts on the selected text |
| "why did you say that" | the rule and evidence behind the last update |
| "what's my routine" | your learned rhythm |
| "how was my week" · "life review" | week in review |
| "audit my subscriptions" | what you don't use |
| "when do I need to leave for my flight" · "what's the weather in Denver" | travel day |
| "who should I raid" | raid planner |
| "turn off privacy while live" | stop blurring panels on stream |
| "turn on do not disturb" | quiet for an hour |
