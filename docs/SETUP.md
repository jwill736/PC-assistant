# Setting up Vesper, click by click

Every step in order, with exactly where to click. Parts 1–3 are once only.
Parts 1–2 take about 30 minutes; Part 3 about 2 hours, mostly waiting.
You need Windows 10 or 11, a microphone and a Google account (for Part 3).

## Part 1 · Get Vesper onto your PC

1. **Let desktop apps use your microphone.** Press <kbd>Win</kbd>+<kbd>I</kbd> → **Privacy & security** →
   **Microphone**. Turn on **Microphone access** and **Let desktop apps access your microphone**. If this is
   off, Vesper runs but hears nothing, and nothing tells you why.
2. **Install Python 3.12.** Download
   [Python 3.12.10 for Windows (64-bit)](https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe)
   (the version Vesper is tested on; python.org's big yellow button gives a newer one some voice parts may
   not support yet). Open it, tick **Add python.exe to PATH** at the bottom of the first screen, click
   **Install Now**. Already have 3.11 or 3.12? Skip this.
3. **Download the code.** Install [GitHub Desktop](https://desktop.github.com/) and sign in. **File → Clone
   repository… → URL**, paste `https://github.com/jwill736/PC-assistant`, click **Clone**. Then
   **Repository → Show in Explorer**. In Explorer, turn on full file names: Windows 11 **View → Show → File
   name extensions**; Windows 10 **View** tab → tick **File name extensions**.
   The folder is `C:\Users\<you>\Documents\GitHub\PC-assistant`. Right-click it → **Pin to Quick access**.
4. **Run `setup.bat`** (double-click it). 5–10 minutes. It ends with "Done. Edit config.yaml and .env, then
   run start.bat". "Python was not found" means step 2's PATH box wasn't ticked: reinstall with it ticked.
5. **Add your Claude API key.** [console.anthropic.com → API keys](https://console.anthropic.com/settings/keys)
   → **Create Key**, copy it. The API is billed separately from a Claude subscription, so add a few dollars
   under **Billing**. Right-click `.env` → **Open with → Notepad**, paste the key straight after
   `ANTHROPIC_API_KEY=`, save. (Without a key, apps, OBS, media and tasks still work; open questions and the
   morning plan need it.)
6. **Set your name and goal.** Right-click `config.yaml` → **Open with → Notepad**. Change `user_name:`,
   `north_star:` (a number and a date, e.g. `"$5k/month from streaming by June 2027"`) and `this_week:`.
   Keep the two spaces at the start of each line. Leave `name: Vesper`. Save.
7. **Start it:** double-click `start.bat`. The first start scans the PC and downloads the speech models and
   the voice (about 200 MB), 1–3 minutes. A black log window, then the **Vesper HUD** with tabs
   **Command · Work · Stream · Setup**; top left says "Listening for “vesper”". Keep the black window open.

## Part 2 · Teach it your voice (about 5 minutes)

8. **Pick the speech engine that hears you best.** Close Vesper. In the PC-assistant folder, click Explorer's
   address bar, type `cmd`, press Enter, then run:

   ```
   .venv\Scripts\python -m assistant --bench-voice --apply
   ```

   Read each command it shows out loud. It prints a table with "← best" on the winner and saves it. Start
   Vesper again.
9. **Calibrate.** HUD → **Setup** → **Your voice** → **Calibrate my voice**: quiet for 5 s, say “Vesper”
   5 times, read 3 lines. For the first day set **Only answer me** to **LOG** (scores voices, never ignores
   you); switch to **STRICT** once it's reliable. The switch is remembered.
10. **Pick its voice.** **Setup → Speaking voice**. **Supertonic** starts talking in about 0.2 s;
    **Kokoro** has British voices (George, Lewis) but starts 0.4–1 s later and downloads 320 MB the first
    time. Choose a voice, press **Preview**, adjust **Speed**. Also remembered.
11. **Try it.** “Vesper, where am I?” · “Vesper, open Notepad.” · “Vesper, good morning.” — then say
    “stop” while it's talking. Replies show in the **Conversation** panel on the **Command** tab; if it
    didn't wake, what it heard shows under the command box as “(ignored) …”.

## Part 3 · Train the wake words on Colab (once, ~2 hours)

12. [colab.research.google.com](https://colab.research.google.com/) → **File → Open notebook → GitHub**,
    search `jwill736/PC-assistant`, open `training/wake_words.ipynb`.
13. **Runtime → Change runtime type → T4 GPU → Save.**
14. **Runtime → Run all** (<kbd>Ctrl</kbd>+<kbd>F9</kbd>) → **Run anyway**. Stay until the Google Drive box
    appears (up to ~20 minutes) → **Connect to Google Drive** → **Allow**. Keep the tab open and the PC awake.
15. Section **6 · Results and download** shows a threshold per model: write them down. `vesper.onnx` and
    `stop.onnx` land in **Downloads** (and in Drive, `vesper-wake-words`). A name like `vesper (1).onnx`
    works too.
16. HUD → **Setup → Trigger words → Add trained model (.onnx)**: pick `vesper.onnx`, type its threshold;
    same for `stop.onnx`. The text above them now starts with “Acoustic:”.
17. Test: “Vesper, open Discord” works; “…and Vesper is my assistant” (name mid-sentence) is ignored;
    “stop” during a reply cuts it off. Misses you → re-add with a threshold 0.05 lower; fires on its own →
    0.05 higher.

## Part 4 · Optional

18. **Start at sign-in:** in the folder's address bar type `powershell`, then run
    `powershell -ExecutionPolicy Bypass -File scripts\install-startup.ps1`. Vesper then lives as a ring icon
    by the clock (click <kbd>^</kbd> if hidden): right-click for **Open HUD**, **Mute microphone**, **Quit**.
19. **Updates:** GitHub Desktop → **Fetch origin → Pull origin**, run `setup.bat` again (settings kept), quit
    Vesper from the tray and start it again.

## Where everything lives

| what | where | for |
|---|---|---|
| `setup.bat` / `start.bat` | top of the folder | install or update / start with a log window |
| `.env` | top of the folder | Claude key, calendar links (private) |
| `config.yaml` | top of the folder | your name, goals, apps, scenes, macros |
| `data\settings.yaml` | `data` folder | what you chose in the HUD (wins over `config.yaml`) |
| HUD | `http://127.0.0.1:8765` | the dashboard |
| Log | `data\logs\assistant.log` | what went wrong |
| Models | `data\models\` | speech, voices, trained wake words |

## If something's off

- **Never hears you:** redo step 1, then **Setup → Health check → Run check** and read the Microphone row.
- **Nothing happens on `start.bat`:** it's probably already running in the tray; a second copy opens the HUD.
- **Answers other people:** finish step 9 and set **STRICT**; headphones stop it hearing its own voice.
- **Voice sounds wrong or starts late:** **Setup → Speaking voice**; the Health check's "Speaking voice" row
  times it on your PC.
- **Colab "GPU not available" / disconnected:** try again later; finished models are already in Drive.
- **“Windows protected your PC”:** **More info → Run anyway** (happens with ZIP downloads, not GitHub Desktop).
