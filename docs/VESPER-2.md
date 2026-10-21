# Vesper 2.0 · every new feature, and how to switch each one on

This is the walkthrough for the big upgrade: 50+ new abilities. Most work the moment you update. A few need an
account connected once. Do the parts in order; each says how long it takes and what you should see.

**What's new at a glance**

| Area | What it does | Needs |
|---|---|---|
| Speaks first | Updates at the right moment (never on air, not mid-game), welcome-back summary, meeting warnings, break nudges, quiet mode | nothing |
| Reminders | "remind me at 3 / after the stream / when Dana emails me", timers with live rings, routines ("every weekday at 9 am check my email") | nothing |
| Memory | Learns about you from what you say, writes a profile of you each night, "what do you know about me", "forget that…", "search everything for…", off the record | nothing (Ollama for the nightly profile) |
| Weather | Now, the next hours, the week, air quality, "take an umbrella" | nothing |
| Gmail | Who's waiting on a reply (work + personal), search, read; briefing names them | Google sign-in (Part 7 of SETUP) |
| Replies | Replies you owe, people who owe you, promises you made in sent mail → tasks | Google sign-in |
| Meetings | A prep card 10 minutes before each meeting: attendees, last emails, docs, notes | Google sign-in |
| Reply drafts | "Draft reply" on any owed email: a Gmail draft in your own style, never sent | Google sign-in + the drafts box (Part D) |
| Meeting debrief | After a meeting: "anything to note?" → note, tasks with due dates, People notes | Google calendars |
| Google Tasks | "Vesper · Work" / "Vesper · Personal" lists kept in step both ways | the Tasks box (Part D) |
| Focus blocks | "Protect 2 hours tomorrow for the Q3 deck" → a busy 🔒 event, after your yes | the Calendar events box (Part D) |
| People | Who you deal with, last contact, "remember that Dana's daughter…", who's gone quiet | Google sign-in |
| Money | Subscriptions, bills due, free trials ending (from receipts) | Google sign-in |
| On the way | Packages (out for delivery) and flights (check-in opens) | Google sign-in |
| Day plan | Your day time-blocked from calendar + tasks, "am I overbooked", shut down for the day, weekly review | nothing |
| Focus | Focus sessions with a ring, one gentle nudge off YouTube/Reddit, daily stats | nothing |
| Habits | "track a habit: drink water 8 times a day", streaks | nothing |
| Journal | "journal: …", end-of-day prompt, mood over time; private, never on air or in the log | nothing |
| Pulse | 3–5 cards each morning from your goals, projects and news; 👍/👎 teaches it | nothing (better with Ollama) |
| Screen | "look at this", "read this error", "summarize this page" | Ollama with a vision model |
| Clipboard | History, "summarize / translate / fix what I copied", "copy that" | Ollama for the actions |
| Rewind | "where was I", "what was I doing at 3 pm yesterday", "how long was I on YouTube" | nothing |
| PC health | GPU heat/power alerts, full drives (your D:), what's filling a drive, tidy Downloads (with undo) | nothing |
| Stream co-pilot | Live stats, unanswered chat questions, chat mood, who to greet, recap + VOD chapters + post drafts after | Twitch (already set up) |
| Phone | Control Vesper from Telegram; urgent things pushed to your phone when you're away | a Telegram bot (Part E) |
| Music | Spotify now playing + control by voice; warns when a track may not be stream-safe | Spotify Premium (Part F) |
| Lights | Elgato Key Light, Home Assistant, an ON AIR light when you go live | the light (Part G) |
| YouTube | Subscribers, views, latest videos, growth | an API key (Part H) |
| Discord | Go-live post, "announce …" | a webhook (Part I) |
| Tool servers | Plug in Notion, Home Assistant, GitHub, a folder… as new abilities (MCP) | per server (Part J) |
| HUD 2.0 | New look: colour-coded orb that reacts to your voice, Ctrl+K command palette, notification bell, Life and PC tabs, motion settings | nothing |

---

## Part A · Update (3 minutes; waits on the merge)

1. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
2. Wait for the black window to finish. Vesper restarts and the HUD reloads by itself.
3. You should see six tabs across the top: **Command · Work · Life · Stream · PC · Setup**, and a bell icon at the
   top right.

## Part B · Try what needs no setup (10 minutes)

4. Press <kbd>Ctrl</kbd>+<kbd>K</kbd>. The command palette opens; type `weather` and press <kbd>Enter</kbd>.
5. Say “Vesper, what's the weather”. If it names the wrong town, say “Vesper, set my location to Atlanta” (your city).
6. Say “Vesper, remind me in 2 minutes to stretch”. The **Reminders & routines** panel (Command tab) shows it; in
   2 minutes Vesper says it (unless you're live or in a game; then it waits for a pause).
7. Say “Vesper, set a timer for 1 minute”. A ring counts down on the Command tab.
8. Say “Vesper, every weekday at 9 am check my email”. It shows under Reminders & routines with ↻.
9. Say “Vesper, plan my day”. The **Day plan** panel (Command tab) fills with time blocks and a moving “now” line.
10. Open the **Life** tab. Click **25 min** under **Focus**, then click **Stop** (just to see it work).
11. Say “Vesper, track a habit: drink water 8 times a day”, then “Vesper, I drank water”. The **Habits** panel
    (Life tab) counts 1 of 8.
12. Say “Vesper, journal: first day with the new Vesper”. The **Journal** panel saves it (and it never goes into
    the conversation log).
13. Say “Vesper, remember that my favourite game is Valorant”, then “Vesper, what do you know about me”.
14. Open the **PC** tab. **PC Health** shows each drive as a ring. Click the D: drive to see what's filling it.
15. Say “Vesper, where was I”.

**Send back:** a screenshot of the Command tab and the Life tab, and anything in red.

## Part C · Memory and vision on your PC (5 minutes, once)

Vesper's meaning search and its eyes use two small Ollama models next to your big one.

16. Press <kbd>Win</kbd>+<kbd>R</kbd>, type `cmd`, press <kbd>Enter</kbd>.
17. Paste this and press <kbd>Enter</kbd> (about 600 MB):
```
ollama pull embeddinggemma
```
18. Check that your main model can see images. Paste this and press <kbd>Enter</kbd>:
```
ollama show qwen3.6:35b-a3b
```
19. Look under **Capabilities**. If it lists `vision`, you're done. If not, paste this (about 6 GB):
```
ollama pull qwen2.5vl:7b
```
20. Back in the HUD: **Setup** tab → **Memory**: the top line should say “meaning search on (embeddinggemma)” a minute
    after Vesper next starts.
21. Open any error message on screen and say “Vesper, read this error”.

**Send back:** what Vesper said for step 21, and the Memory panel's top line.

## Part D · Gmail, drafts, Google Tasks and focus blocks (5 minutes; needs Google sign-in from SETUP Part 7)

Vesper 2.1 asks Google for three more permissions, each its own tickbox on Google's page:

| Box on Google's page | Called in the HUD | What Vesper does with it |
|---|---|---|
| “Manage drafts and send emails” | Gmail drafts | saves reply drafts in your Gmail Drafts. **It never sends**: you open the draft and press Send yourself |
| “Create, edit, organize, and delete all your tasks” | Google Tasks | keeps a “Vesper · Work” list (work account) and “Vesper · Personal” (personal account) in step with your tasks |
| “View and edit events on all your calendars” | Calendar events | puts a 🔒 focus block on your calendar, only after you said yes to the exact time |

Already connected? Each account shows **Partly: didn't allow Gmail drafts, Google Tasks, Calendar events** until
you do steps D1–D5. Every feature checks its own box and says which one is missing, so the others keep working.

**Once · Turn on Google Tasks in Google Cloud (2 minutes, once)**

D1. Open [the Google Tasks API page](https://console.cloud.google.com/apis/library/tasks.googleapis.com), check the
    top bar says **Vesper**, click **Enable**. (Gmail and Calendar are already on from SETUP Part 7.)

**Then · Reconnect each account (1 minute each)**

D2. **Setup** tab → **Google accounts**. An account showing **Partly** has a box still unticked.
D3. Click **Reconnect** next to it. Your browser opens Google.
D4. Pick the account, click **Continue**, tick **every** box, click **Continue**, close the tab. You should see
    **Connected** next to the account.
D5. Do D3–D4 for the other account.

**Try it (5 minutes)**

D6. **Command** tab → **Inbox**: who's waiting on you, work and personal side by side.
D7. **Work** tab: **Replies · owed, waiting, promised**, **Next meeting · prep** and **People** fill in within two
    minutes. **Life** tab: **Money** and **On the way** too (click **Read receipts** / **Check email** to start now).
D8. Say “Vesper, who do I owe a reply”, “Vesper, what did I promise”, “Vesper, prep me for my next meeting”.
D9. **Work** tab → **Replies** → **Owed**: click **Draft reply** next to someone. The **Reply drafts** panel shows the
    text; click **Open in Gmail** to check it and send it yourself. Or say:
```
Vesper, draft a reply to Dana saying Thursday works
```
D10. **Setup** tab → **Google Tasks**: one line per list, e.g. “Vesper · Work ↔ jon@talent-sdk.com”. Click **Sync now**.
    Open Google Tasks on your phone: the **Vesper · Work** list has your work tasks. Tick one off there; within
    5 minutes it's done in Vesper too.
D11. Say:
```
Vesper, protect 2 hours tomorrow for the Q3 deck
```
    Vesper says the exact day and time and waits. Say “yes” (or click **Confirm** on the HUD). A busy
    “🔒 Q3 deck” appears on your work calendar. The **Focus blocks** panel (Command tab) lists it.
D12. After a meeting with other people ends, Vesper asks “Anything to note from …?”. Just answer, e.g. “I'll send
    Dana the deck by Friday”: it becomes a note, a task due Friday and a note on Dana. Or type it in the
    **Meeting debrief** panel (Work tab), or say “Vesper, debrief my last meeting” any time that day.

If the work account says “Access blocked” or `admin_policy_enforced`: your company's Google admin blocks apps it
hasn't approved. The admin adds the Client ID in **admin.google.com → Security → Access and data control → API
controls → Manage third-party app access** as **Trusted**. Personal Gmail works either way.

**Send back:** a screenshot of the Work tab and of **Setup → Google accounts / Google Tasks**, what Vesper said in
step D11, and anything in red or amber.

## Part E · Phone: Telegram (5 minutes)

29. On your phone, open **Telegram**, tap search, type `@BotFather`, open the one with the blue tick, tap **Start**.
30. Send:
```
/newbot
```
31. Send a name, e.g. `Vesper`. Then send a username ending in `bot`, e.g. `jvesper_bot`.
32. BotFather replies with a token like `123456789:AA…`. Tap it to copy, and send it to yourself on the PC (or type it).
33. HUD → **Setup** → **Phone**: paste the token in step 1 → **Save**. You should see “Bot: @jvesper_bot ✓”.
34. Step 2 of the panel shows a 6-digit code. Click **Open in Telegram** (or send your bot `/start 123456` with your code).
35. Telegram answers “Paired with Vesper”. Click **Send test**: your phone gets “Vesper: Test from your PC”.
36. From your phone, send `what's on my calendar today`. Vesper answers there. Anything risky asks “Reply yes or no”.

## Part F · Spotify (5 minutes; Spotify Premium needed)

37. Open [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard), log in, click **Create app**.
38. **App name** `Vesper`, **App description** `My PC assistant`.
39. In **Redirect URIs** paste this, then click **Add**:
```
http://127.0.0.1:8765/api/skill/spotify/callback
```
40. Tick **Web API**, tick the terms, click **Save**.
41. Click **Settings**, copy the **Client ID**.
42. HUD → **Life** tab → **Now playing**: paste it → **Save** → **Connect Spotify** → **Agree** in the browser, close the tab.
43. Play a song. Say “Vesper, what's playing”, “Vesper, next song”, “Vesper, play lofi beats”.
44. For streams: play your stream-safe playlist, then click **Mark playlist stream-safe** (Stream tab → **Music**).
    While live, anything else gets a heads-up on the HUD.

## Part G · Lights and ON AIR (5 minutes per light)

45. Open **Elgato Control Center**, click the gear next to your Key Light, note its **IP address**.
46. HUD → **Life** tab → **Lights & home** → **Set up**: type the IP, e.g. `192.168.1.40` → **Add**.
47. Say “Vesper, turn on my key light”, then “Vesper, lights to 40 percent”.
48. Home Assistant (only if you use it): in Home Assistant click your name (bottom left) → **Security** →
    **Long-lived access tokens** → **Create token**, name it `Vesper`, copy it.
49. Back in **Set up**: paste your Home Assistant address (e.g. `http://homeassistant.local:8123`) and the token → **Save**.
50. ON AIR light: in **Set up → ON AIR when you go live**, paste the light's entity ID (Home Assistant → Settings →
    Devices & services → Entities → the light → gear → **Entity ID**) → **Save** → **Test: on** → **Test: off**.

## Part H · YouTube stats (5 minutes)

51. Open [console.cloud.google.com](https://console.cloud.google.com) and pick your **Vesper** project (top bar).
52. Open [the YouTube Data API page](https://console.cloud.google.com/apis/library/youtube.googleapis.com) → **Enable**.
53. Left menu **Credentials** → **+ Create credentials** → **API key**. Copy it (starts `AIza`).
54. HUD → **Stream** tab → **YouTube**: paste the key, type your channel handle (e.g. `@yourname`) → **Save**.

## Part I · Discord go-live posts (3 minutes)

55. In Discord, right-click your server → **Server Settings** → **Integrations** → **Webhooks** → **New Webhook**.
56. Name it `Vesper`, pick your announcements channel, click **Copy Webhook URL**, then **Save Changes**.
57. HUD → **Stream** tab → **Discord**: paste it → **Save** → **Test post** → **OK**. It appears in Discord.
58. From now on going live posts once per stream. “Vesper, announce giveaway at 9 tonight” asks you first.

## Part J · Tool servers (MCP) (5 minutes per server; optional)

Each server adds abilities; every action they take asks you first unless you lower it.

59. Most presets need **Node.js**: install the LTS version from [nodejs.org](https://nodejs.org), next-next-finish.
60. HUD → **Setup** → **Tool servers (MCP)**. Click a preset (**Files in a folder**, **Home Assistant**, **GitHub**,
    **Notion**); it fills the form.
61. Paste the token the preset asks for (GitHub: github.com → Settings → Developer settings → Personal access
    tokens; Notion: notion.so/my-integrations → New integration, then share your pages with it).
62. Click **Add & connect**. You should see a green dot and “N of N tools offered”.

## Part K · The new HUD (2 minutes)

63. Press <kbd>Ctrl</kbd>+<kbd>K</kbd> any time: jump to any panel, run any command, or type a question.
64. Click the **bell** (top right): everything Vesper brought up, newest first. 👍 / ✕ on the Updates panel teaches it
    what's worth saying out loud.
65. **Setup** tab → **Display**: **Motion** Full / Reduced / Off, and the theme.
66. Watch the orb while you talk: blue when it's listening to you, violet while it thinks, orange when it speaks.

## Part L · When something isn't working: diagnostics (1 minute)

67. **Setup** tab → **Diagnostics · report to send** → **Run diagnostics** (or say “Vesper, run diagnostics”). You
    should see pass / warn / fail counts within about 15 seconds, and the full path of the report under them.
68. Click **Open the file** to read it, or **Show in folder** to attach `diagnostics-<date>.md` to a message. Secrets
    are taken out and email addresses are partly hidden, so it's safe to send as it is.
69. **Setup** → **Skills** lists every ability with an on/off switch. A skill that keeps failing is switched off for
    the session (a red card says why); flip its switch to turn it back on.
70. Vesper closed? In a terminal in the Vesper folder: `.venv\Scripts\python -m assistant --diagnose` writes the
    same report.

## Things you can say (cheat sheet)

| Say | What happens |
|---|---|
| “what did I miss” / “catch me up” | the updates you didn't hear |
| “quiet mode” / “do not disturb for 30 minutes” / “you can talk again” | holds updates |
| “remind me at 3 pm to …” / “… after the stream” / “… when Dana emails me” | reminders |
| “25 minute timer called pasta” / “how long is left on the timer” | timers |
| “every friday at 4 pm recap my day” | routines |
| “plan my day” / “what should I work on now” / “am I overbooked” | day plan |
| “shut down for the day” / “weekly review” | evening and Friday |
| “start a 50 minute focus session on the Q3 deck” / “stop focus” | focus |
| “I drank water” / “how are my habits” | habits |
| “journal: …” / “end of day journal” / “how have I been feeling” | journal |
| “check my email” / “did Dana email me” / “search my email for the contract” | Gmail |
| “who do I owe a reply” / “what am I waiting on” / “what did I promise” | replies |
| “prep me for my next meeting” / “who's in my next meeting” | meetings |
| “draft a reply to Dana” / “draft a reply to Dana saying Thursday works” | a Gmail draft in your voice (never sent) |
| “debrief my last meeting” / “nothing to note” | meeting debrief → note, tasks, People notes |
| “sync my tasks with Google” | Google Tasks, both ways |
| “protect 2 hours tomorrow for the Q3 deck” / “block my afternoon for deep work” / “put my plan on my calendar” | 🔒 focus blocks (asks first) |
| “who is Dana” / “remember that Dana …” / “who haven't I talked to” | people |
| “what subscriptions do I have” / “what bills are due” | money |
| “where's my package” / “when's my flight” | on the way |
| “what's my pulse” / “run pulse now” | Pulse |
| “look at this” / “read this error” / “what's on my screen” | screen |
| “summarize what I copied” / “translate my clipboard to Spanish” / “copy that” | clipboard |
| “where was I” / “what was I doing at 3 pm yesterday” | rewind |
| “how's my PC” / “why is my D drive full” / “clean up my downloads” | PC health |
| “how's the stream going” / “how's chat” / “any questions in chat” / “stream recap” | stream co-pilot |
| “what do you know about me” / “forget that …” / “search everything for Acme” | memory |
| “off the record” / “back on the record” | nothing kept meanwhile |
| “what's playing” / “next song” / “play lofi beats” | Spotify |
| “turn on my key light” / “lights to 50 percent” / “movie scene” | lights |
| “how's my YouTube” | YouTube |

**Send back** after each part: a screenshot of the panel you set up, and the exact words of anything in red.
