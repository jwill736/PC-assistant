# Vesper (PC-assistant)

A voice assistant for J's Windows PC: Python (FastAPI) backend in `assistant/`, a vanilla-JS HUD in
`assistant/web/`, tests in `tests/` (`pytest -q`). Setup for the user lives in `docs/SETUP.md` and `install.ps1`.

## Giving J steps to take

Whenever J has to do anything (install, update, change a setting, run a command, check a result), lay it out as a
simple numbered walkthrough:

- One action per step, in order, with exactly where to click (menu names as they appear on screen).
- Every command or line to type goes in its own code block, ready to copy and paste, with nothing in it to edit
  unless the step says so (then show a complete example line).
- Say what J should see after a step when it matters, so a wrong turn is caught at once.
- Group steps into short parts with how long each takes, and say which part waits on something (a merge, CI).
- End with what to send back (screenshot, the exact text it said, anything in red).

Keyboard paths on Windows: Win+R opens Run, Win then typing opens Start search; the Vesper folder is
`%USERPROFILE%\Vesper`, and **Update Vesper** in the Start menu runs the installer as an update.
