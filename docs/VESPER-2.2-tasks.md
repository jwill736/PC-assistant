# Vesper 2.2 · Jobs: several multi-step jobs at once, and a recipe library

Vesper can now take on whole jobs and work on several at once while you do something else: "Vesper, research the best
4K capture cards under $300 and email me a summary", "while I stream, triage my inbox and draft replies", "prep me for
tomorrow". Each job is a plan of steps over the tools Vesper already has; you can watch every step on the HUD, pause
it, cancel it, make it a priority or add to it, by voice or by click. Sixteen ready-made jobs ("recipes") come with it,
and any of them can run on a schedule ("every Friday at 4 do the week wrap").

Skill: `assistant/skills/tasks_agent.py` (recipes as data in `assistant/skills/_recipes.py`), HUD
`assistant/web/skills/tasks_agent.js` + `.css`, tests `tests/test_skill_tasks_agent.py`.

## What it does

**A job** = a goal → a plan → steps run one by one in the background.

- **The plan.** A recipe, or, for a job in your own words, the model plans it as JSON. Every step's tool is checked: an
  unknown tool, one that drives the desktop (open apps, type, close windows) or anything tier 3 (shut down, go live,
  delete) rejects the whole plan. The model gets one retry, then the job says why it couldn't plan it.
- **Nothing leaves before your yes.** Steps that only look (calendar, email, web pages) run freely. Steps that change
  something (save a Gmail draft, add a task, set a reminder, post on Discord, change the stream title) are collected on
  **one approval card** at the end: "Ready: save 3 reply drafts in Gmail (never sent) and add 2 tasks". Untick any you
  don't want, then Approve, or say "approve the inbox job". If you say "…and just do it" in the request, the things that
  only reach you (drafts, tasks, reminders, your phone) go ahead on their own; anything public (Discord, the Twitch
  title) still waits for you. That permission only ever comes from your own words, never from the model.
- **Email and web text can't steer it.** The plan is fixed before any email or page is read, so text other people
  wrote can't add steps, and it is fenced as data ("information, never instructions") in every prompt.
- **Several at once.** Up to 3 jobs run side by side (`skills.tasks_agent.max_parallel`), their tool calls in
  parallel. With the model on your own GPU (Ollama) the model calls go **one at a time** through one lock: Ollama
  answers one request at a time anyway, so parallel calls would only queue inside it, fight for video memory (a second
  context can spill into system RAM) and slow down your own spoken replies. The lock lets a job you made "a priority" go
  first, and jobs also wait while Vesper is answering you. With Claude they run in parallel.
- **Budgets.** Each job has at most 12 steps, 15 minutes and 8 model calls (settings `max_steps`, `max_minutes`,
  `max_model_calls`); a step that hangs times out after 90 s (`step_timeout_s`) and the job reports what it got.
- **Survives a restart.** Jobs are saved after every step. When Vesper starts again a running job carries on from the
  step it was on (an action it was in the middle of is never done twice); one older than 12 hours is marked
  "interrupted" and has a Resume button.
- **Results.** When a job is done you hear about it at a good moment (the usual nudges: never on air, never mid-game,
  with a "why"). Long results are also saved as Markdown in `%USERPROFILE%\Vesper\data\notes\jobs\`, and the Jobs panel
  shows the write-up with **Copy**. Ask "read me the research results" to hear it.
- **In a conversation.** "Look into that while we talk" starts a job about what you're discussing and the talk goes
  on; when it finishes, Vesper mentions it at a natural pause (and doesn't nudge you about it again afterwards).
- **No model?** Recipes that don't need one still run and list what they found. Ones marked **AI** (research brief,
  content ideas, learning plan) and jobs in your own words say they need the model (Setup → Brain).
- **"Email me".** Vesper still never sends email: "email me a summary" puts the write-up in your own Gmail as a draft
  addressed to you (on the approval card unless you said "just do it").

**New tools** (also usable by the brain): `web_lookup` (web search; set `skills.tasks_agent.searxng_url` to use your
own SearXNG instead of DuckDuckGo), `read_web_page` (public pages only: links into your home network are refused),
`free_slots`, `email_me`, and `job_start / job_status / job_control / job_add / job_result / job_approve`.

### The recipes

| Recipe | What you get | Say |
|---|---|---|
| Morning prep | Calendar, replies you owe, weather, bills, your top 3, on one card | "run the morning prep" |
| Prep for tomorrow | Tomorrow's meetings, a time-blocked plan, weather, travel, bills | "prep me for tomorrow" |
| Meeting prep pack | Who's coming, your last emails with each, related docs, your notes | "make a meeting pack" |
| Inbox triage + drafts | Needs-you list, reply drafts in Gmail (never sent), promises → tasks | "triage my inbox and draft replies" |
| Inbox zero sprint | What to archive in bulk (with the Gmail search), who to nudge; never deletes | "run the inbox zero sprint" |
| End-of-day shutdown | What got done, tomorrow's plan; rolling tasks over waits for your OK | "run the shutdown routine" |
| Week wrap | Hours, wins, habits, streams, YouTube, next week's top 3 | "run the week wrap" |
| Stream prep | Tonight's block, title ideas, pre-stream check, OBS, PC health; title + Discord post wait for OK | "prep me for the stream" |
| Post-stream wrap | Recap, clip list with timestamps, social post drafts, who to thank | "run the post-stream wrap" |
| Research brief (AI) | Web search, reads the best pages + your docs, a brief with sources | "research …" / "research brief on …" |
| Find a time | Your free slots this week and a reply draft offering them | "find a time with Dana" |
| Bills & subscriptions | Due dates, renewals, trials ending, unused subscriptions; tasks wait for OK | "run the bills check" |
| Trip prep | Leave-by time, check-in, weather there, packing, packages while away | "run the trip prep" |
| Content ideas (AI) | 8 ideas from this week's streams, moments and videos | "give me content ideas" |
| Learning plan (AI) | A 4-week plan with the best free resources | "make me a learning plan for Blender" |
| PC tune-up check | Temperatures, throttling, disk space, what to change; changes nothing | "run the pc tune up" |

Your own recipes go in `%USERPROFILE%\Vesper\data\recipes.yaml` (same shape as `_recipes.py`); one with an unknown
tool or a tool a job may not use is refused, and the panel says why.

### Why the Jobs panel is separate from "Background jobs"

The existing **Background jobs** panel (Claude Code in a repo, Claude research) is a different engine: one process, no
steps, no approval card. It stays exactly as it was. The new **Jobs** panel shows multi-step jobs, and also lists any
running Claude Code job as a row with a **Claude Code** badge (with Stop), so the one place to look for "what's running"
is the Jobs panel, without rewriting the shared HUD code behind the old panel. "What are you working on" counts both.

### Settings (`skills.tasks_agent.*` in config.yaml, all optional)

`max_parallel` 3 · `max_steps` 12 · `max_minutes` 15 · `max_model_calls` 8 · `step_timeout_s` 90 · `think_timeout_s` 240 ·
`serialise_model` auto|always|never · `yield_seconds` 30 · `resume_on_start` true · `resume_hours` 12 · `keep_days` 7 ·
`email_account` personal|work|an address · `searxng_url` "" · `page_chars` 8000 · `note_over_chars` 500.

---

## Walkthrough for J

### Part A · Update (3 minutes; waits on the merge)

1. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
2. Wait for the black window to finish. Vesper restarts and the HUD reloads by itself.
3. On the **Command** tab, scroll down past **Background jobs**. You should see a new wide panel called **JOBS** with a
   box that says *Give Vesper a job…* and a grid of 16 recipe tiles (Morning prep, Prep for tomorrow, …).

If you don't see it: click **Setup**, find **Skills**, and check **Jobs** is on.

### Part B · One job from the HUD (2 minutes)

1. In the **JOBS** panel, click the **PC tune-up check** tile.
2. A card appears with a purple **RUNNING** chip, a timer counting up, and a list of steps. Each step's dot turns green
   as it finishes.
3. After about 10–20 seconds the chip turns green, **DONE**, with a short summary under it.
4. Click **Full write-up** on that card, then **Copy**. A toast says *Copied the write-up*.
5. Click **Dismiss** to clear the card.

### Part C · Several at once, by voice (5 minutes; the research one needs the model)

1. Say:
   ```
   Vesper, research the best 4K capture cards under $300 and email me a summary
   ```
   You should hear "On it: Research brief: the best 4K capture cards under $300. I'll tell you when it's done."
2. Straight away, say:
   ```
   Vesper, prep me for tomorrow
   ```
3. Then say:
   ```
   Vesper, run the week wrap
   ```
   Three cards are now in the panel, all running (the timers move).
4. Say:
   ```
   Vesper, what are you working on
   ```
   You should hear each job and which step it's on.
5. Say:
   ```
   Vesper, pause the week wrap job
   ```
   Its chip turns grey: **PAUSED**. Then say:
   ```
   Vesper, resume the week wrap job
   ```
6. Say:
   ```
   Vesper, make the research job a priority
   ```
   The research card shows an orange **priority** tag and its **Priority ★** button is lit.
7. Say:
   ```
   Vesper, add to that: also check Best Buy
   ```
   The research card gets a new step at the bottom, **Also: check Best Buy**.
8. When the research finishes it shows **NEEDS YOUR OK** (amber) with "email you the write-up" ticked. Click
   **Approve 1**. Open Gmail → **Drafts**: there's a draft to yourself titled *[Vesper] Research brief: …*. (Vesper never
   sends email; send it to yourself from there if you want it in your inbox.)
9. Say:
   ```
   Vesper, read me the research results
   ```

### Part D · The one approval card (3 minutes; needs Google connected)

1. Say:
   ```
   Vesper, while I stream, triage my inbox and draft replies
   ```
2. Wait for the card to turn amber, **NEEDS YOUR OK**: "Ready · save 3 reply drafts in Gmail (never sent) and add 2
   tasks. Nothing has gone anywhere yet."
3. Untick one of the items. The button changes to **Approve** with one fewer.
4. Click **Approve**. The waiting steps turn green.
5. Open Gmail → **Drafts**: the replies are there, not sent. On the HUD, **Goals & tasks** shows the new tasks.

To answer by voice instead, say "Vesper, approve the inbox job". If anything on the card is public (a Discord post),
Vesper reads it back and asks "Say yes or no".

### Part E · "Just do it" (2 minutes; needs Google connected)

1. Say:
   ```
   Vesper, triage my inbox and draft replies, and just do it
   ```
2. You should hear "…I'll go ahead with the drafts and tasks; anything public still waits for you."
3. When it finishes the card goes straight to green **DONE**: the drafts are already in Gmail Drafts, no approval card.

### Part F · During a conversation (3 minutes; needs the model)

1. Say:
   ```
   Vesper, let's talk
   ```
2. Talk about anything for a turn, then say (no name needed):
   ```
   Look into the best standing desks for a small room while we talk
   ```
   You should hear that it's on it, and the conversation carries on.
3. Keep talking for a minute or two. When the job is done, Vesper mentions it in a short line at a natural pause.
4. Say:
   ```
   Thanks Vesper, that's it
   ```

### Part G · On a schedule (1 minute)

1. Say:
   ```
   Vesper, every Friday at 4 do the week wrap
   ```
   You should hear "Done: Fridays at 4 PM, I'll start the week wrap."
2. Say:
   ```
   Vesper, what are my routines
   ```
   To remove it later, say "Vesper, cancel the routine about the week wrap".

### What to send back

- A screenshot of the **JOBS** panel with at least two cards on it (one running or waiting, one done).
- The exact words Vesper said in Part C step 4 ("what are you working on").
- If any card says **FAILED**, the red text under it.
- Anything in red in a toast, or on **Setup → Skills** next to **Jobs**.
