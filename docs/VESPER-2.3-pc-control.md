# Vesper 2.3 · PC control: click anything by name, and "undo that"

Two new things for the PC:

- **Click anything by name.** "Vesper, click Export in Premiere", "press Join in Discord", "turn on the Night light
  toggle in Settings", "type lo-fi beats into the search box". Vesper reads the app's buttons, switches, tabs and boxes
  through Windows UI Automation (the accessibility layer every Windows app has), finds the one you named, tells you
  exactly which one it found, and presses it after your yes.
- **"Vesper, undo that."** Vesper keeps a list of what it changed this session. "Undo that" puts the last change back
  the way it was, "undo the last 3" does the last three, and the HUD lists them with an **Undo** button on each.

Skills: `assistant/skills/click.py` (the UI Automation part is `assistant/integrations/uia.py`) and
`assistant/skills/undo.py` (the history is `assistant/brain/undo.py`, kept by `ToolBox.run`). HUD panels on the **PC**
tab: **Undo** and **Click by name**. Tests: `tests/test_skill_click.py`, `tests/test_undo.py`.

## Click anything by name

**What to say**

| You say | What happens |
| --- | --- |
| "Vesper, click Export in Premiere" | Finds the Export button in Premiere's window and asks "Click the Export button in adobe premiere pro? Say yes or no." |
| "Vesper, press Join in Discord" | The same for Discord. If three "Join" buttons fit, it asks "Which one: Join in General, Join in Gaming, or Join in Music?" Say "the second one", then yes. |
| "Vesper, turn on the Night light toggle in Settings" | Flips that switch on (says so if it's already on). "turn off …" turns it off. |
| "Vesper, type lo-fi beats into the search box" | Puts the text in that box (in the window in front, or "… in Spotify"). |
| "Vesper, what can I click here?" | Lists what it can see in the window in front ("… in Discord" for another app). The full list is on the HUD. No yes needed: it only looks. |

Without an app name it uses the window in front. If you typed into the HUD, it uses the window behind the HUD.

**What it will and won't do**

- Pressing anything in another app needs your **yes** (tier 2). The yes is for the exact control it named: if that
  button has gone or changed by the time you say yes, nothing is pressed and it tells you.
- It presses buttons the proper way first (the same way a screen reader does). Only a control with no other way in gets
  a real mouse click at its centre, and only if nothing is covering it.
- Never in a password field, a password manager, a sign-in or Windows security (UAC) prompt, or Vesper's own HUD.
- A huge window (a long chat, a big timeline) is read for at most 2.5 seconds. If what you want is further down, scroll
  it into view and ask again.
- Apps running as administrator can't be pressed from Vesper (Windows blocks it); it says so.

## Undo that

**What to say**

| You say | What happens |
| --- | --- |
| "Vesper, undo that" (or "take that back") | Puts back the last thing Vesper changed. |
| "Vesper, undo the last 3" | The last three, newest first. |
| "Vesper, what can you undo?" | Says how many of its recent actions can be undone; the list is on the HUD. |
| HUD → **PC** tab → **Undo** panel → **Undo** next to any line | Undoes that one. |

**What can be undone:** master volume and mute (also after "louder" / "mute"), an app's volume, screen brightness, a
window minimised, maximised or brought to the front, a virtual desktop switch, the OBS scene, an OBS source shown or
hidden, an OBS mute, Spotify's volume, a task added or ticked off, a reminder, timer or routine you set, a focus block
put on your calendar, a Downloads tidy, and a switch or text box Vesper changed in another app.

**What can't:** a sent chat or Discord message, a post, a raid, a shoutout, closing an app, shutting down, a button
press. These are listed as "can't undo", and "undo that" says so instead of pretending.

**It never fights you.** If you changed it yourself since (you turned the volume up again, ticked the task off, went to
another scene), Vesper says what it is now, for example "The volume is 60% now, not 40%, so I left it", and leaves it.

**It still asks when it should.** An undo goes through the same rules as anything else: if PC control is paused ("stop
everything"), it waits; taking a focus block off your calendar or flipping a switch back in another app asks for a yes,
just as doing it did. The list is for this session only: it starts empty each time Vesper starts.

## Settings

- **Setup → Skills**: **Click by name** and **Undo** can each be turned off.
- Nothing else to set. Clicking by name works on Windows only; on another computer it says "Windows only".
- **Setup → Health check** shows a **UI Automation** line: "ready" on your PC.

## Walkthrough

### Part A · Update Vesper (about 5 minutes)

1. Press <kbd>Win</kbd>, type `Update Vesper`, and press <kbd>Enter</kbd>.
2. Wait for the window to say it's done and Vesper to start again. You should see the Vesper ring icon by the clock.
3. Open the HUD (right-click the ring icon → **Open HUD**) and click the **PC** tab. You should see two new panels:
   **Undo** ("Nothing changed yet…") and **Click by name**.

### Part B · Undo (about 3 minutes)

1. Say "Vesper, set volume to 30". The volume goes to 30.
2. Say "Vesper, undo that". You should hear "Undone: the volume back to …" with the level you had before, and the
   volume goes back to it.
3. Say "Vesper, set volume to 30" again, then turn the volume up yourself with the volume keys or the taskbar slider.
4. Say "Vesper, undo that". You should hear "The volume is … now, not 30%, so I left it", and it stays where you put it.
5. Say "Vesper, add a task to test undo". Then say "Vesper, undo that". The task should disappear from the task list.
6. In the HUD, **PC** tab, look at the **Undo** panel: every step above is listed, newest first, with "undone" or
   "you changed it since" on each.

### Part C · Click by name in Settings (about 5 minutes)

1. Press <kbd>Win</kbd>+<kbd>I</kbd> to open Settings, then click **System** → **Display**.
2. Leave Settings in front and say "Vesper, what can I click here?". You should hear "In Settings I can see …",
   and the **Click by name** panel lists the controls, with **Night light** among them.
3. Say "Vesper, turn on the Night light toggle in Settings". You should hear "Turn on the Night light switch in
   Settings? Say yes or no."
4. Say "yes". Night light turns on (the screen goes warmer), and you hear "Night light is on."
5. Say "Vesper, undo that". It asks again (a switch in another app needs a yes). Say "yes". Night light turns off.
6. Click **Bluetooth & devices** in the left list, then say "Vesper, click System in Settings", then "yes". Settings
   should go back to the System page.

### Part D · A real app (about 5 minutes, optional)

1. Open Discord and go to a server with voice channels.
2. Say "Vesper, what can I click in Discord?". Look at the list in the **Click by name** panel.
3. Pick a harmless button from that list (for example a channel name) and say "Vesper, click <that name> in Discord",
   for example:

   ```
   Vesper, click general in Discord
   ```

4. If it asks "Which one: …?", say "the first one", then "yes". Discord should open that channel.

### Part E · What to send back

- A screenshot of the HUD's **PC** tab showing the **Undo** and **Click by name** panels after Part C.
- The exact words Vesper said at Part C step 2 and step 3.
- If anything didn't work: what you said, what Vesper said back (word for word), and anything shown in red. If it said
  "I can't see anything called …", send the list from the **Click by name** panel too.
