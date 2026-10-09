# Setting up Vesper, click by click

Every step in order, with exactly where to click. Parts 1–4 are once only.
Part 1 takes about 15 minutes, mostly waiting; Part 2 about 5; Part 3 about 2 hours, mostly waiting.
You need Windows 10 or 11, a microphone and a Google account (for Part 3).

**Which computer?** Vesper controls the one it's installed on: it opens apps, reads the active window and
runs OBS there. Your streaming PC is its real home. A laptop is a fine place to try it first, and from there it
can still use the Llama on your main PC (step 2, then step 19).

## Part 1 · Get Vesper onto your PC

1. **Open PowerShell.** Press <kbd>Win</kbd>, type `powershell`, press <kbd>Enter</kbd>. A blue or black
   window opens. You don't need "Run as administrator".
2. **Paste the install line** and press <kbd>Enter</kbd> (right-click pastes in that window):

   ```
   irm https://raw.githubusercontent.com/jwill736/PC-assistant/main/install.ps1 | iex
   ```

   It checks Windows lets desktop apps use the microphone (and opens the right setting if not), installs
   Python 3.12 if you don't have it (from python.org, just for you), downloads Vesper into
   `C:\Users\<you>\Vesper` and installs its packages (5–10 minutes). Then it asks three questions:
   - **What should Vesper call you?**
   - **Your main goal,** as a number and a date, e.g. `$5k/month from streaming by June 2027`. Every plan is
     ranked against it.
   - **Which model answers your questions?** If Ollama or LM Studio is running on this computer with a model,
     it's found and nothing is asked. Otherwise:
     - **1 · another PC on my home network:** Vesper on a laptop, your Llama on the main PC. Type that PC's
       name (on it: **Settings → System → About → Device name**) or its IP address. If it can't connect yet,
       it's saved anyway: do step 19 on that PC and Vesper connects within a minute.
     - **2 · Ollama or LM Studio on this computer:** start it before Vesper; Part 5 has which model to get.
     - **3 · a Claude API key:** optional and paid, billed separately from a Claude subscription
       ([console.anthropic.com → API keys](https://console.anthropic.com/settings/keys) → **Create Key**,
       add a few dollars under **Billing**). Paste it when asked; it stays hidden.
     - **4 · decide later:** apps, volume, OBS, Twitch, tasks, notes and "what did I say about…" all work
       with no model. Open questions wait until you connect one.

   It finishes with "Vesper is installed", puts **Vesper** on the desktop and in the Start menu, and starts it.
   If it stops with red text, that line says why: fix it and paste the line again. Nothing is lost.
3. **First start.** A black log window (keep it open), then the **Vesper HUD** with tabs
   **Command · Work · Stream · Setup**; top left says "Listening for “vesper”". The first start scans the PC
   and downloads the speech models and the voice (about 200 MB), 1–3 minutes. From now on, start it from the
   **Vesper** icon.

To change an answer later: **Setup → Brain** in the HUD, or in the Vesper folder click Explorer's address
bar, type `cmd`, press Enter and run `.venv\Scripts\python -m assistant.firstrun`. Rather do it all by
hand? See the README's *Quick start*.

## Part 2 · Teach it your voice (about 5 minutes)

4. **Pick the speech engine that hears you best.** Close Vesper (right-click the ring icon by the clock →
   **Quit**). In the Vesper folder (`C:\Users\<you>\Vesper`), click Explorer's address bar, type `cmd`,
   press Enter, then run:

   ```
   .venv\Scripts\python -m assistant --bench-voice --apply
   ```

   Read each command it shows out loud. It prints a table with "← best" on the winner and saves it. Start
   Vesper again.
5. **Calibrate.** HUD → **Setup** → **Your voice** → **Calibrate my voice**: quiet for 5 s, say “Vesper”
   5 times, read 3 lines. For the first day set **Only answer me** to **LOG** (scores voices, never ignores
   you); switch to **STRICT** once it's reliable. The switch is remembered.
6. **Pick its voice.** **Setup → Speaking voice**. **Supertonic** starts talking in about 0.2 s;
    **Kokoro** has British voices (George, Lewis) but starts 0.4–1 s later and downloads 320 MB the first
    time. Choose a voice, press **Preview**, adjust **Speed**. Also remembered.
7. **Try it.** “Vesper, where am I?” · “Vesper, open Notepad.” · “Vesper, volume 30.” · “Vesper, good
    morning.” — then say “stop” while it's talking. The emergency brake is <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>K</kbd>
    (or say “stop everything”): it stops every action until you say “resume control”. Replies show in the **Conversation** panel on the **Command** tab; if it
    didn't wake, what it heard shows under the command box as “(ignored) …”.

## Part 3 · Train the wake words on Colab (once, ~2 hours)

8. [colab.research.google.com](https://colab.research.google.com/) → **File → Open notebook → GitHub**,
    search `jwill736/PC-assistant`, open `training/wake_words.ipynb`.
9. **Runtime → Change runtime type → T4 GPU → Save.**
10. **Runtime → Run all** (<kbd>Ctrl</kbd>+<kbd>F9</kbd>) → **Run anyway**. Stay until the Google Drive box
    appears (up to ~20 minutes) → **Connect to Google Drive** → **Allow**. Keep the tab open and the PC awake.
11. Section **6 · Results and download** shows a threshold per model: write them down. `vesper.onnx` and
    `stop.onnx` land in **Downloads** (and in Drive, `vesper-wake-words`). A name like `vesper (1).onnx`
    works too.
12. HUD → **Setup → Trigger words → Add trained model (.onnx)**: pick `vesper.onnx`, type its threshold;
    same for `stop.onnx`. The text above them now starts with “Acoustic:”.
13. Test: “Vesper, open Discord” works; “…and Vesper is my assistant” (name mid-sentence) is ignored;
    “stop” during a reply cuts it off. Misses you → re-add with a threshold 0.05 lower; fires on its own →
    0.05 higher.

## Part 4 · Connect Twitch (5 minutes)

14. **Register the app.** [dev.twitch.tv/console](https://dev.twitch.tv/console) → log in → **Register Your
    Application**. Twitch first asks you to turn on two-factor authentication if it's off. Fill in:
    **Name** `Vesper <your name>` · **OAuth Redirect URLs** `http://localhost` → **Add** · **Category**
    Application Integration · **Client Type** **Public** (can't be changed later) · tick the captcha →
    **Create**.
15. **Copy the Client ID.** In the list, click **Manage** next to the app, copy **Client ID**. In the Vesper
    folder, right-click `.env` → **Open with → Notepad**, paste it straight after `TWITCH_CLIENT_ID=` and save.
    Leave `TWITCH_CLIENT_SECRET=` empty.
16. **Log in.** Restart Vesper (close the black window, open **Vesper** from the desktop). Say “Vesper, connect
    Twitch” or click **Connect Twitch** on the **Stream** tab. Click **Open Twitch** (or type the code at
    twitch.tv/activate) → **Authorize**. Vesper says “Twitch is connected as …”. Try “Vesper, any new
    followers?”.

## Part 5 · Your own model (optional, 5 minutes)

Skip this part if the installer said “Found … on this computer” or “Connected to …”. Do 17–18 on the PC
that will run the model: this one, or your main PC.

17. **Install Ollama** from [ollama.com](https://ollama.com/download) (Windows installer, next-next-finish).
    Already have Ollama or LM Studio with a model? Go to step 19 if it's on another PC, step 20 if it's here.
18. **Download a model.** Win+R, `cmd`, Enter, then `ollama pull llama3.1:8b` (about 5 GB; wants a GPU with
    8 GB). On a laptop, or a GPU with less: `ollama pull llama3.2:3b` (2 GB; answers take several seconds
    without a GPU).
19. **Model on your main PC, Vesper on a laptop: share it on your home network.** On the main PC: press
    <kbd>Win</kbd>, type `powershell`, Enter, run `setx OLLAMA_HOST 0.0.0.0`. Right-click the Ollama icon by the
    clock → **Quit**, then start **Ollama** from the Start menu. If Windows Firewall asks, tick **Private
    networks** → **Allow access**. (LM Studio instead: **Developer** tab → **Settings** → **Serve on Local
    Network**.) Check from the laptop: open `http://<main PC name>:11434` in a browser; it should say “Ollama is
    running”. No answer by name? Use the main PC's IP (`ipconfig` on it → **IPv4 Address**). Didn't give the
    installer that PC's name? On the laptop run `.venv\Scripts\python -m assistant.firstrun` in the Vesper
    folder and choose **1**. While shared, anyone on your home network can use the model; don't share it on
    public Wi-Fi.
20. **Check it's connected.** HUD → **Setup → Brain**: “Answering with … on Ollama”. About a minute after the first
    start, Vesper tests each of your models by itself (it says which one won); **Test my models** runs it again, and
    each model in the list says how it did. Click **Test**: it should end with “took the right action”. Then
    say “Vesper, what's connected?” and “Vesper, remember that the new overlay colours are orange and black”, and
    a minute later “Vesper, which colours did I pick for the overlay?”.

## Part 6 · Your documents (2 minutes, then it reads by itself)

21. **See what it reads.** HUD → **Work** tab → **Library · your documents**. Under **Folders it reads** you'll
    see your OneDrive (with Documents and Desktop in it), each Google Drive's **My Drive** and Dropbox, the ones
    on this PC. The top line counts up while it reads: “Reading your documents… 340 looked at”. The first read
    can take a while; Vesper works normally meanwhile.
22. **Add your work folders.** Under **Suggested** are the folders you pinned in File Explorer: click **Add** on
    the ones with work in them. Anything else: open it in File Explorer, click the address bar, copy the path
    (<kbd>Ctrl</kbd>+<kbd>C</kbd>), paste it into **Add a folder**, click **Add**. **Remove** takes a folder out.
23. **Try it.** Type a few words you know are in one of your files into the search box, e.g. a client's name,
    and press **Search**: you should see the file, the matching passage and an **Open** button. Then say
    “Vesper, search my documents for …” or, with your model connected, ask a question about your work and
    “check my documents”.

## Part 7 · Optional

24. **Start at sign-in:** in the Vesper folder's address bar type `powershell`, then run
    `powershell -ExecutionPolicy Bypass -File scripts\install-startup.ps1`. Vesper then lives as a ring icon
    by the clock (click <kbd>^</kbd> if hidden): right-click for **Open HUD**, **Mute microphone**, **Quit**.
25. **Updates:** Start menu → **Update Vesper** (or paste the install line again). It closes Vesper, downloads
    the new version, updates the packages and starts it again. `config.yaml`, `.env` and everything in `data`
    are kept. The old black log window closes by itself, and the HUD window you had open reloads onto the new
    version (no second window).

## Where everything lives

| what | where | for |
|---|---|---|
| Vesper folder | `C:\Users\<you>\Vesper` | everything below; turn on **View → Show → File name extensions** to see `.env` and `.yaml` |
| `start.bat` / `update.bat` | top of the folder | start with a log window / update (same as the Start menu entries) |
| `.env` | top of the folder | Claude key, Twitch Client ID, calendar links (private) |
| `config.yaml` | top of the folder | your name, goals, apps, scenes, macros, `brain.local.url` |
| `data\settings.yaml` | `data` folder | what you chose in the HUD (wins over `config.yaml`) |
| HUD | `http://127.0.0.1:8765` | the dashboard |
| Log | `data\logs\assistant.log` | what went wrong |
| Models | `data\models\` | speech, voices, trained wake words |
| Twitch login | `data\twitch_token.json` | delete it (or **Disconnect**) to log out |
| Highlights | `data\highlights\` | one file per day: moments with the time into the stream |
| Library index | `data\library.db` | the word index of your documents (delete it to start the reading over) |

## If something's off

- **Never hears you:** <kbd>Win</kbd>+<kbd>I</kbd> → **Privacy & security → Microphone**: turn on **Microphone
  access** and **Let desktop apps access your microphone**. Then **Setup → Health check → Run check** and read
  the Microphone row.
- **Nothing happens when you start it:** it's probably already running in the tray; a second copy opens the HUD.
- **Answers other people:** finish step 5 and set **STRICT**; headphones stop it hearing its own voice.
- **Twitch clips or markers say “only while live”:** that's Twitch's rule. Markers also need **Creator
  Dashboard → Settings → Stream → Store past broadcasts** on; ads and polls need affiliate or partner.
- **“Connect Twitch” asks for TWITCH_CLIENT_ID:** step 15, then restart Vesper. A login Twitch refuses usually
  means the app isn't a **Public** client: register a new one (the type can't be changed).
- **Voice sounds wrong or starts late:** **Setup → Speaking voice**; the Health check's "Speaking voice" row
  times it on your PC.
- **Colab "GPU not available" / disconnected:** try again later; finished models are already in Drive.
- **Install says Python was installed but can't be found:** close PowerShell, open a new one, paste the line
  again.
- **The main PC's model won't connect:** both PCs on the same home network; step 19 done and Ollama restarted
  after `setx`; the firewall allowed on **Private** networks; `http://<main PC name>:11434` opens on the
  laptop (else use the IP). **Setup → Brain** shows the address it's trying. Away from home, Vesper falls back
  to Claude if you added a key, else to the commands that need no model.
- **“Windows protected your PC”** on a `.bat` you downloaded by hand: **More info → Run anyway**.
