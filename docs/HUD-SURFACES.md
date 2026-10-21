# Your layout, the mini HUD, the HUD on your phone, and the OBS overlay

Four things in Vesper 2.1 that change where and how you see the HUD. Each part says how long it takes.

## Part 1 · Arrange the panels your way (2 minutes)

1. In the HUD's top bar, click the **grid** button (between the bell and the light/dark button), or press
   <kbd>Ctrl</kbd>+<kbd>K</kbd> and pick **Edit layout**.
   You should see dashed outlines round every panel and an **Editing … layout** bar at the bottom.
2. Drag a panel by its orange **grip** (top-left of the panel) to where you want it. The other panels slide out of
   the way and a dashed box shows where it will land.
   No mouse? Click the grip and use the arrow keys, or click the **↑ ← → ↓** buttons.
3. Set a panel's width with **⅓ ½ ⅔ Full** on its bar.
4. Hide a panel with the **eye** button. Bring it back from **+ Add panel** in the bottom bar.
5. Click **Done** (or press <kbd>Esc</kbd>).

Each tab keeps its own layout. It's saved in `data\settings.yaml` (`hud.layout`), so clearing the browser doesn't
lose it. **Reset tab** puts this tab back; **Reset all** puts every tab back.

## Part 2 · The mini HUD in a corner (1 minute)

1. Right-click the Vesper ring icon by the clock and click **Open mini HUD**
   (or in the HUD: **Setup → Mini HUD → Open mini HUD**, or <kbd>Ctrl</kbd>+<kbd>K</kbd> → **Open mini HUD**).
   A small window opens in the top-right corner: the orb, what you and Vesper just said, your next event, the
   latest update, music, timers and the mic buttons.
2. To keep it above a game or OBS, install Microsoft PowerToys once: press <kbd>Win</kbd>, type `Microsoft Store`,
   press <kbd>Enter</kbd>, search **PowerToys**, click **Get**.
3. Click the mini window, then press <kbd>Win</kbd>+<kbd>Ctrl</kbd>+<kbd>T</kbd>. A coloured border means it now stays
   on top. Press the same keys again to let it go.

While you're live, the mini HUD blurs what was said, the event name and the latest update (hover to read them),
in case the stream catches that corner.

## Part 3 · The HUD on your phone (5 minutes)

Your phone and the PC must be on the same home Wi-Fi. Nothing is reachable from outside your home.

1. In the HUD, click **Setup**, scroll to **Phone · the HUD over Wi-Fi**, and click **On**.
2. Windows may show **Windows Defender Firewall has blocked some features of this app** for Python. Tick
   **Private networks**, untick **Public networks**, click **Allow access**.
   The panel should now say **Listening on your home network** and show a QR code, a link and a 6-digit PIN.
3. On your phone, open the camera and point it at the QR code; tap the link that appears.
   (Or type the link shown under the code into the phone's browser, for example `http://192.168.1.23:8766/`.)
4. Type the 6-digit PIN and tap **Unlock**. The HUD opens, with the tabs along the bottom.

Your phone stays signed in for 30 days. Five wrong PINs lock that phone out for 15 minutes (you get a card on the
PC). **New PIN** or **Sign out all phones** signs every phone out at once. To stop it, click **Off**.

If the phone can't open the page: check both are on the same Wi-Fi, then repeat step 2's firewall choice from
<kbd>Win</kbd> → type `Allow an app through Windows Firewall` → find **Python** → tick **Private** → **OK**.

## Part 4 · The OBS overlay (5 minutes)

1. In the HUD: **Setup → OBS overlay**. Click the panels you want shown (**Now playing**, **Stream goals**,
   **Chat mood**, **Countdown**, **Follower ticker**, **Timers**), a **Style** and a **Side**.
2. Optional: type a **Sub goal** / **Follower goal** target and click **Save goals**.
3. Click **Copy OBS link**.
4. In OBS: **Sources** → **+** → **Browser** → name it `Vesper overlay` → **OK**.
5. Paste the link into **URL**, set **Width** `600` and **Height** `900`, leave **Shutdown source when not visible**
   off, click **OK**. Drag and resize it on the canvas.

The overlay only ever shows music, goals, the chat mood, a countdown, new followers/subs/raids and timers (time
left only). Never your calendar, email, chat messages or anything Vesper says. Its link has its own key, which
opens the overlay and nothing else; **New link** retires the old one (paste the new link into OBS).
**5 min / 10 min / 15 min** start a "Starting soon" countdown; without one, it counts down to the next event on
your stream calendar.

## Where it lives

| what | where |
|---|---|
| Layouts, phone on/off, overlay goals | `data\settings.yaml` (`hud:`) |
| Phone PIN | `data\phone_access.json` (delete it for a new PIN; every phone signs in again) |
| Overlay key | `data\overlay_token` (delete it, or **New link**, to retire every old OBS link) |
| Mini HUD window | its own Chrome profile in `data\chrome-mini` |
