# Vesper 2.3 · Day job & life: a week that fits, and meetings that take their own notes

Two things, both built on what was already there (the Day plan, Habits, Focus blocks and the Recorder), not new
panels:

1. **A week that fits.** "Plan my week" lays out work and meetings, your stream nights, gym sessions toward your weekly
   target, and bedtime against your sleep target. "Am I overbooked this week" names the day that breaks
   ("Thursday: meeting till 6 PM, stream at 7 PM, no gym slot, bed after 1 AM (6.5 h sleep)") and gives one fix. On
   Sunday evening a short preview of the coming week arrives as a nudge.
2. **Meetings that take their own notes.** When a calendar meeting with a call link starts (or Zoom, Teams, Meet,
   Webex or Discord is up during one), Vesper asks once: "Record notes for Weekly sync? Say yes or no." It never
   records without your yes, and never asks while you're live. When the meeting is over and the call has gone quiet,
   it stops by itself and says so. The summary goes to your phone if you paired one, otherwise to a card on the HUD.

**What it does**

| | |
|---|---|
| Plan my week | One line a day on the **Day plan** panel (Command tab): work, meetings that run late, the stream, gym, bedtime. Days that break are marked amber, and there's one **Fix** line |
| Which day breaks | A meeting that runs into the stream or its 30 minutes of prep; a night more than 15 minutes short of your sleep target; a day with no room for gym when the week is short of sessions |
| The fix | One concrete change for the worst day: "End Thursday's stream by midnight to get 7.5 hours of sleep before your 9 AM start", "Start Wednesday's stream at 7:45 PM, after the meeting…", or "Fit a 45-minute gym session in on Monday at 5 PM" |
| Book it | "book it" (or the **Book Gym** button) puts the gym block on your personal Google calendar. You hear the exact day and time first and say yes (like every calendar change) |
| Gym target | "gym 3 times a week" sets it (a weekly habit on the **Habits** panel). "I went to the gym" logs one, and logged sessions count |
| Stream nights | Your stream calendar (your Twitch schedule's calendar link) when you have one; otherwise the nights you set in `config.yaml`, otherwise the nights Vesper learned from your PC use ("Your rhythm") |
| Sleep | Bedtime comes from **Your rhythm** (Setup → Memory) or `bedtime`. Wake-up is an hour before work or your first meeting |
| Sunday preview | Sunday from 6 PM: "Next week: 3 streams, gym 3 of 3, and 7.5 hours of sleep fits every night." It goes through the usual nudge rules: never spoken on stream, held in a game or during quiet hours |
| Record offer | Once per meeting, as it starts: a spoken question with a few seconds to say yes, plus the HUD card and a Windows toast. If you're presenting fullscreen or gaming, it asks only by card and toast |
| Always record | "always record the weekly sync" records that meeting series every time, without asking. "stop always recording the weekly sync" undoes it. Other meetings are still asked |
| Auto-stop | 5 minutes after the meeting's end time, once the call audio has been quiet for 2 minutes. If you're still talking half an hour after the end, one reminder appears instead (it never cuts you off) |
| The notes | To your phone (Telegram or ntfy, if paired), otherwise a HUD card with the summary. Action items still wait for your click on **Add tasks** |

**How it works it out:** every time and every "breaks" is calculated by Vesper itself, never guessed by the model,
and DST nights count as their real length (the night the clocks go back is an hour longer).

**Phrases**

- "plan my week", "plan next week", "preview next week", "balance my week"
- "am I overbooked this week", "am I overbooked next week", "is my week too packed"
- "gym 3 times a week", "set my gym target to 4 times a week", "book it" (right after a week plan)
- "always record the weekly sync", "always take notes of this meeting", "stop always recording the weekly sync"
- Unchanged: "take notes of this meeting", "stop recording", "add the recording tasks"

**Settings** (all optional, `config.yaml` → `skills:`)

- `planner:` `sleep_hours` (7.5), `bedtime` ("": from your rhythm), `wake` ("": from your rhythm, else 08:00, for days
  with nothing in the morning), `ready_minutes` (60: from waking up to work), `wind_down_minutes` (30: from the end of
  the stream to bed), `gym_minutes` (75, with the trip), `gym_window` ("06:30-21:30"), `stream_nights` (for example
  `{days: [Tue, Thu, Sat], start: "19:00", end: "23:30"}`), `stream_prep_minutes` (30), `week_preview` ("18:00" on
  Sunday; `""` turns it off).
- `recorder:` `auto_offer` (true; also the **Offer to record** button on the Recorder panel), `auto_stop` (true),
  `stop_grace_minutes` (5), `quiet_minutes` (2), `push_notes` (true).

Work hours stay where they were: `assistant.work_hours`, Monday to Friday.

---

## Part A · Update (3 minutes; waits on the merge)

1. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
2. Wait for the black window to finish. Vesper restarts and the HUD reloads by itself.
3. Click the **Command** tab. The **Day plan** panel now has a **Plan week** button at the bottom, next to
   **Shut down day** and **Weekly review**.

## Part B · Your gym target (1 minute)

1. Say:
   ```
   Vesper, gym 3 times a week
   ```
2. You should hear "Tracking gym, 3 times a week." The **Habits** panel shows **gym** with **0/3 this week**.
   - If you already track a workout habit (for example "workout 2 times a week"), it changes that one instead of
     adding a second.

## Part C · Your first week plan (2 minutes)

1. Say:
   ```
   Vesper, plan my week
   ```
2. You hear something like "This week: 3 work days, 2 streams (Tuesday, Thursday), gym 3 of 3, Monday at 6 PM, …".
   If a day breaks, you hear that day and one fix.
3. On the **Day plan** panel, a new card lists one line per day. Amber lines are the days that break. Under them:
   **Fix** and a button like **Book Gym · Monday 6:00 PM**.
4. Read the grey line at the bottom of that card. If it says **No stream nights found**, do Part D now. If it says
   **Stream nights from your rhythm**, check the nights are right; if not, do Part D.
5. Say:
   ```
   Vesper, am I overbooked this week
   ```
   You hear either "This week works: …" or "1 day breaks. Thursday: …" with one fix.

## Part D · Tell Vesper your stream nights (3 minutes; only if Part C said so)

Use this when your stream nights aren't on a calendar Vesper reads.

1. Press <kbd>Win</kbd>+<kbd>R</kbd>, paste this, press <kbd>Enter</kbd>:
   ```
   notepad %USERPROFILE%\Vesper\config.yaml
   ```
2. Go to the very end of the file and paste these three lines. Change the days and times to yours (days are Mon, Tue,
   Wed, Thu, Fri, Sat, Sun; times are 24-hour, and an end after midnight like "00:30" is fine). If the file already has
   a line that is exactly `skills:`, paste only the last two lines straight under it instead.
   ```
   skills:
     planner:
       stream_nights: {days: [Tue, Thu, Sat], start: "19:00", end: "23:30"}
   ```
3. Press <kbd>Ctrl</kbd>+<kbd>S</kbd> to save.
4. Restart Vesper: press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
5. Say "Vesper, plan my week" again. The streams are on your nights, and the grey line says
   **Stream nights from your settings**.

If your streams are already on a calendar Vesper reads as your stream calendar (the "Stream schedule" one under
`calendars:` in `config.yaml`, with `profile: stream`), its events win: a night without one counts as a night off.

## Part E · Book a gym slot (1 minute)

1. Within half an hour of Part C, say:
   ```
   Vesper, book it
   ```
2. You hear the exact slot, for example "Put “🔒 Gym” on your personal calendar Monday (Mon Oct 12), 6–7:15 PM,
   shown as busy? Say yes or no."
3. Say:
   ```
   yes
   ```
4. You hear "Done: 🔒 Gym is on your personal calendar …". The block is in Google Calendar. The next "plan my week"
   counts it as booked.
   - If you hear "No Google account is connected" or a line about the **Calendar events** box, connect it on
     **Setup → Google accounts** first (tick **Calendar events**), then repeat this part.

## Part F · Meeting autopilot (the next time you have a call on your calendar; nothing to set up)

1. Have a meeting on your calendar with a Zoom, Meet or Teams link (or open Zoom, Teams, Meet or Discord during a
   calendar meeting). Within a minute of the start, you hear:
   ```
   Record notes for Weekly sync? Say yes or no.
   ```
   An amber card with **Confirm** shows on the HUD too, and a Windows toast after a few seconds.
2. Say "yes" (or click **Confirm**). You hear "Recording Weekly sync. The call audio too." and the orb shows the red
   **REC** ring. Say "no", or ignore it, and nothing records. It won't ask again for this meeting.
3. Hold the meeting as usual. When it ends and everyone has hung up, within about 8 minutes of the end time you hear
   "Weekly sync is over, so I stopped recording. Notes are coming."
4. When the notes are ready:
   - with a phone paired (Setup → Phone): a message "Notes from Weekly sync: …" arrives, with "2 action items wait
     for your OK on the PC" if there are any;
   - without one: a green card on the HUD with the summary.
5. If there were action items, the amber **Add 2 tasks?** card waits on the **Work** tab as before. Nothing is added
   until you click **Add**.

## Part G · Always record one meeting (1 minute; optional)

1. Say (use your meeting's name):
   ```
   Vesper, always record the weekly sync
   ```
2. You hear "Okay: I'll record Weekly sync every time it's on, without asking. Let the people on it know." The
   Recorder panel's bottom row shows **Always: Weekly sync** with a **×**.
3. Next time that meeting starts, you hear a chime and see the **REC** ring with no question. Other meetings are still
   asked.
4. To undo it, say:
   ```
   Vesper, stop always recording the weekly sync
   ```
   or click the **×** next to **Always: Weekly sync**.
5. To stop the offers altogether, click **Offer to record: on** on the Recorder panel's bottom row; it changes to
   **Offer to record: off**.

## Part H · Sunday preview (Sunday from 6 PM; nothing to do)

1. On Sunday evening a nudge arrives: "Next week: … Say “plan my week” for the rest." with a **Plan my week** button.
2. It isn't spoken while you're live, in a game or in quiet hours; it waits on the HUD instead.

---

**What to send back**
- A screenshot of the **Day plan** panel's week card after Part C step 3.
- The exact words Vesper said in Part C steps 2 and 5, and in Part E step 2.
- After your next calendar meeting (Part F): did the question come within a minute of the start (yes/no), did it stop
  by itself, and roughly how long after the meeting ended?
- Anything in red or amber you didn't expect, with the text it shows.
