# Vesper 2.2 · Conversations that evolve

In 2.1 a conversation was one sitting: Vesper kept a rolling summary while you talked and forgot it afterwards. In 2.2
every conversation is kept as a **thread** you can come back to, Vesper learns from what you say in them, picks up
open decisions days later, talks in different **modes**, and adapts its **style** to your feedback.

| What | How it works | Needs |
|---|---|---|
| Threads | When a talk ends, the local model writes its notes: a title, a summary, key points, your opinions and decisions, what's still open, and anything Vesper promised to come back to | the local model (without one: the last few lines, word for word) |
| Resume | "Let's continue our talk about the move", "what were we saying about the new PC", "pick up where we left off": Vesper opens with a one-line recap and carries on with the notes | a kept thread |
| Linking | A new talk that sounds like an old one ("let's talk about my new PC build") gets an offer: "last week you were leaning towards the 9800X3D. Still?" Say "pick that up" or just keep going | embeddings if memory has them, else matching words |
| Memory | What you say that's worth keeping goes into long-term memory (its usual secret filter and "this replaces that" rules), tagged with the thread | the local model |
| Forget | "Forget this conversation" deletes the thread, its lines in the conversation log and everything it taught Vesper (what those facts had replaced comes back) | nothing |
| Off the record | "Let's talk off the record" (or "off the record" mid-talk): nothing from that talk is kept, logged or learned | nothing |
| Follow-ups | An open decision comes back once, a day or more later, at a good moment: "On Monday you were deciding between two GPUs: which did you pick?" At most one a day, never while you're live or mid-conversation; your answer updates the thread | the local model |
| Modes | brainstorm · devil's advocate / debate · coach · interview me · teach me … · rubber duck · plan with me · story / just chat | the local model |
| Style | "Too long", "more detail", "be funnier", "stop asking me questions", "be more formal" tune a small profile used in every conversation; editable on the HUD | nothing |
| Turn-taking | "Hmm, let me think" holds the quiet minute; "go on" / "tell me more" continues the last answer; "wait, back up" re-explains it; a lone "hmm" keeps the talk open | nothing |
| Look into it | "Look into that while we talk" starts a background job about the topic and the conversation carries on | the job runner (Claude key) or the tasks engine |

Threads are private: never kept from a talk on stream, and the list is hidden on the HUD while you're live.

## Things you can say (new in 2.2)

| Say | What happens |
|---|---|
| "let's continue our talk about the move" · "what were we saying about the new PC" · "pick up where we left off" | resumes a thread with a one-line recap |
| "pick that up" (after an offer) | links this talk to the earlier one |
| "let's brainstorm names for my channel" · "play devil's advocate" · "let's debate X" · "coach me on X" · "interview me" · "teach me chess" · "let's rubber duck this" · "plan the move with me" · "tell me a story" | starts a conversation in that mode |
| "switch to coach mode" · "devil's advocate mode" · "back to normal" | changes mode mid-talk |
| "too long" · "shorter" · "more detail" · "be funnier" · "be more serious" · "stop asking me questions" · "fewer questions" · "be more formal" · "be more casual" | adjusts the style (in a conversation) |
| "what's my conversation style" · "reset my conversation style" | reads it out / back to normal |
| "hmm, let me think" · "give me a second" | holds the quiet minute for 3 minutes |
| "go on" · "tell me more" · "wait, back up" · "you lost me" | continues / re-explains the last answer |
| "add them" (plan mode) | adds the tasks on the plan card |
| "look into that while we talk" · "research AM5 boards while we talk" | a background job, and the talk goes on |
| "forget this conversation" · "forget our conversation about the move" · "forget the last conversation" | deletes the thread and what it taught |
| "let's talk off the record" | nothing kept |
| "don't follow up on that" · "stop asking about the new PC" | follow-ups off for that thread, for good |
| "what have we talked about lately" | the threads, newest first |

## On the HUD (Command tab → Conversation mode)

- **Mode chips** under the header: click one to start in that mode, or to switch while talking (the current one is orange).
- The header shows the mode and the thread you're on (purple, "resumed" when it continues an earlier talk), and
  **Off the record** when nothing will be kept.
- A purple strip offers **Pick it up** / **Keep it new** when the talk sounds like an earlier one.
- An amber **Plan · N tasks to add?** card in plan mode: untick any, click **Add tasks** (or say "add them"). Nothing is
  added before that.
- When it ends: **What we covered** (summary, key points, your views, what's still open, what Vesper will come back to)
  with **Forget it**.
- At the bottom, **Threads** (title, when, how many talks, open questions; **Resume**, **Follow-ups** on/off, **Forget**
  which asks twice) and **Style** (length, humour, questions, tone; **Reset**).

Settings (optional, in `config.yaml` under `conversation:`): `think_minutes` (3), `followup_after_hours` (18),
`followup_within_days` (14). `skills.conversation.followups: false` turns follow-ups off entirely.

---

## Walkthrough for J

### Part A · Update (3 minutes; waits on the merge)

1. Press <kbd>Win</kbd>, type `Update Vesper`, press <kbd>Enter</kbd>.
2. Wait for the black window to finish. Vesper restarts and the HUD reloads by itself.
3. On the **Command** tab, the **Conversation mode** panel now has a row of chips under the header: **Brainstorm**,
   **Devil's advocate**, **Coach**, **Interview me**, **Teach me**, **Rubber duck**, **Plan with me**, **Story**.
4. At the bottom of the panel there are two tabs: **Threads 0** and **Style**.

If the chips are missing and the panel says "Needs a model to talk with": click **Setup**, scroll to
**Brain · what answers you**, and check the **Local model** line says qwen3.6 is ready. Then come back.

### Part B · A conversation that's kept (4 minutes)

1. Say:
   ```
   Vesper, let's talk
   ```
2. Without saying "Vesper", say:
   ```
   I'm building a new PC and I'm torn between the 9800X3D and the 9950X3D
   ```
3. Answer whatever it asks back, once or twice (for example "mostly games, and I stream").
4. Say:
   ```
   I'm leaning towards the 9800X3D but I haven't decided
   ```
5. Say:
   ```
   Thanks Vesper, that's it
   ```
6. Within about 20 seconds a **What we covered** box appears in the panel, with a title like "New PC build", a short
   summary, **Your views** and **Still open**, and a green **Saved to threads** label.
7. Click the **Threads** tab at the bottom. The new thread is listed with "just now" and "1 open".

### Part C · Pick it back up (2 minutes)

1. Say:
   ```
   Vesper, let's continue our talk about the new PC
   ```
2. You should hear a one-line recap starting "Picking up New PC build from earlier today…" and ending with what's still
   open. The panel header shows the thread name in purple with "resumed".
3. Say:
   ```
   What would you pick?
   ```
   The answer should build on what you said before (the two CPUs, gaming and streaming) without you repeating it.
4. Say:
   ```
   That's it
   ```
   In the **Threads** tab the thread now says "2 talks".

### Part D · An old topic in a new talk (2 minutes)

1. Say:
   ```
   Vesper, let's talk about my new PC build
   ```
2. You should hear something like "Sounds like our talk about New PC build: earlier today you were leaning towards the
   9800X3D. Still? Say pick that up and I'll carry on from there."
3. Say:
   ```
   Pick that up
   ```
4. You hear the recap; the header shows the thread. Say "that's it".

### Part E · Modes (5 minutes)

1. Say:
   ```
   Vesper, play devil's advocate
   ```
   The panel shows an orange **Devil's advocate** chip. Say "remote work is better than the office" and it should
   argue the other side.
2. Say:
   ```
   Switch to coach mode
   ```
   It now answers with questions, not advice.
3. Say:
   ```
   Teach me chess
   ```
   It explains one idea, then asks you a short check question.
4. Say "that's it". Then say:
   ```
   Vesper, plan my move with me
   ```
5. Answer its questions for a minute (when, where, what's hard). Then say:
   ```
   That's the plan
   ```
6. An amber **Plan · N tasks to add?** card appears. Nothing is in your tasks yet. Untick one, then click **Add tasks**.
   The ticked ones appear in **Goals & tasks**.
7. Say "that's it".

### Part F · Style (2 minutes)

1. Say:
   ```
   Vesper, let's talk
   ```
2. Ask:
   ```
   Tell me about the history of pizza
   ```
3. Say:
   ```
   Too long
   ```
   You hear "Got it, shorter." Ask another question: the answers are now a line or two.
4. Say:
   ```
   Stop asking me questions
   ```
5. Say "that's it". Click the **Style** tab at the bottom of the panel. **Length** shows **Brief** and **Questions**
   shows **Never** (orange), with "Last change: “stop asking me questions”".
6. Click **Balanced** under Length and **Sometimes** under Questions to put them back, or click **Reset**.

### Part G · Thinking time, go on, back up (2 minutes)

1. Say "Vesper, let's talk", then ask something with a long answer, for example:
   ```
   How does a CPU cache work?
   ```
2. Say:
   ```
   Hmm, let me think
   ```
   You hear "Take your time." and the header shows **Thinking time**. Stay quiet for 90 seconds: the conversation
   doesn't end.
3. Say:
   ```
   Wait, back up
   ```
   It explains the last part again, more simply.
4. Say:
   ```
   Go on
   ```
   It carries on from where it stopped. Say "that's it".

### Part H · Off the record and forgetting (2 minutes)

1. Say:
   ```
   Vesper, let's talk off the record
   ```
   The header shows **Off the record**. Say anything, then "that's it". The box says **Off the record: nothing kept**
   and no new thread appears.
2. Click **Threads**. Next to any test thread, click **Forget**, then **Sure?**. It's gone, with a toast saying how many
   things it taught Vesper were forgotten too.
3. Or by voice:
   ```
   Vesper, forget our conversation about the new PC
   ```

### Part I · Follow-ups (nothing to do now; check over the next days)

1. A day or more after a talk that left a decision open, at a quiet moment (never while you're live, in a game or in a
   conversation), Vesper asks once, for example "Yesterday you were deciding between the 9800X3D and the 9950X3D: which
   did you pick?". It's in the bell with **why** and a **Pick it up** button.
2. Just answer out loud. The thread updates (open question closed, your decision noted).
3. To stop follow-ups for that talk: say "don't follow up on that", or click **Follow-ups** next to it in **Threads**
   (grey dot = off).

### What to send back

- A screenshot of the **Conversation mode** panel after Part B step 7 (What we covered + the Threads list).
- The exact recap line you heard in Part C step 2.
- A screenshot of the **Style** tab after Part F step 5.
- Anything in red in the panel or a toast, word for word, and anything that didn't happen as described (which part and
  step).
