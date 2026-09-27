"""Turn tool results into one or two spoken sentences for the fast path."""

from __future__ import annotations

from datetime import datetime, tzinfo


def clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    h, m = divmod(seconds // 60, 60)
    if h and m:
        return f"{h} hour{'s' if h != 1 else ''} {m} minute{'s' if m != 1 else ''}"
    if h:
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{m} minute{'s' if m != 1 else ''}"


def _err(result: dict) -> str | None:
    if not result.get("ok", True) and result.get("error"):
        extra = ""
        if result.get("suggestions"):
            extra = " Did you mean " + " or ".join(result["suggestions"][:3]) + "?"
        return result["error"] + extra
    return None


def summarize(tool: str, args: dict, result: dict, tz: tzinfo, hints: dict | None = None) -> str:
    hints = hints or {}
    err = _err(result)
    if err:
        return err
    if tool == "open_app":
        return f"Opening {result.get('launched', args.get('name'))}."
    if tool == "close_app":
        return f"Closed {', '.join(result.get('names', [args.get('name')]))}." + (
            f" {result['still_running']} process{'es' if result['still_running'] != 1 else ''} didn't exit." if result.get("still_running") else "")
    if tool == "focus_window":
        return f"Switched to {result['window']['app']}." if result.get("ok") else "I found it but Windows wouldn't give it focus."
    if tool == "where_am_i":
        active = result.get("active")
        apps = [a for a in result.get("apps", {}) if a != "unknown"]
        here = f"You're in {active['app']}: {active['title'][:80]}." if active else "I can't read the active window."
        return f"{here} {result.get('open_windows', 0)} windows open across {len(apps)} apps."
    if tool == "open_urls":
        opened = result.get("opened", [])
        return "Opening that." if len(opened) == 1 else f"Opened {len(opened)} tabs."
    if tool == "web_search":
        return f"Searching {args.get('engine', 'google')} for {args.get('query')}."
    if tool == "media_control":
        return ""
    if tool == "power":
        return {"lock": "Locking up."}.get(args.get("action"), f"{args.get('action', '').title()} in ten seconds.")
    if tool == "optimize_pc":
        findings = result.get("findings", [])
        if len(findings) == 1 and findings[0]["severity"] == "ok":
            return f"System's healthy. CPU {result.get('cpu')} percent, memory {result.get('memory')} percent."
        top = findings[:2]
        return " ".join(f"{f['title']}. {f['detail']}" for f in top)
    if tool == "clean_temp":
        return f"Cleared {result.get('removed', 0)} temp files, freed {result.get('freed_mb', 0)} megabytes."
    if tool == "set_power_plan":
        return f"Power plan set to {args.get('plan')}."
    if tool == "obs_status":
        if not result.get("connected"):
            return result.get("error") or "OBS isn't connected."
        st = result["streaming"]
        scene = result.get("current_scene")
        if st["active"]:
            live_for = duration((st.get("duration_ms") or 0) / 1000)
            kbps = f", {st['kbps']} kilobits" if st.get("kbps") else ""
            return (f"Live for {live_for} on {scene}. {st['dropped_pct']} percent dropped frames{kbps}, "
                    f"{result['stats']['fps']} FPS.")
        rec = " Recording." if result["recording"]["active"] else ""
        return f"Not live. Scene is {scene}.{rec}"
    if tool == "obs_switch_scene":
        return f"{result.get('scene')}."
    if tool == "obs_control":
        return {"start_stream": "You're going live.", "stop_stream": "Stream ended.", "start_recording": "Recording.",
                "stop_recording": "Recording stopped.", "save_replay": "Clipped.", "pause_recording": "Recording paused.",
                "resume_recording": "Recording resumed."}.get(args.get("action"), "Done.")
    if tool == "obs_set_mute":
        return f"{result.get('source')} {'muted' if result.get('muted') else 'live'}."
    if tool == "calendar":
        return _calendar_speech(result, tz, hints)
    if tool == "news":
        heads = result.get("headlines", [])[:3]
        if not heads:
            return "No headlines — check the news feeds in config."
        return "Top headlines. " + " ".join(f"{h['source']}: {h['title']}." for h in heads)
    if tool == "list_tasks":
        tasks = result.get("tasks", [])
        if not tasks:
            return "Your list is clear."
        top = "; ".join(t["title"] for t in tasks[:3])
        return f"{len(tasks)} open task{'s' if len(tasks) != 1 else ''}. Top: {top}."
    if tool == "add_task":
        return f"Added: {result['task']['title']}."
    if tool == "complete_task":
        return f"Checked off {result['task']['title']}."
    if tool == "remember":
        return "Noted."
    if tool == "run_routine":
        parts = [f"{result.get('profile', '').title()} mode is up."]
        if result.get("launched"):
            parts.append("Launched " + ", ".join(result["launched"]) + ".")
        if result.get("tabs"):
            parts.append(f"{len(result['tabs'])} tabs open.")
        if result.get("failed"):
            parts.append("Couldn't start " + ", ".join(result["failed"]) + ".")
        return " ".join(parts)
    if tool == "start_job":
        return f"On it. Job {result.get('job_id')} is running in the background; I'll tell you when it lands."
    if tool == "twitch_status":
        if not result.get("enabled"):
            return "Twitch isn't configured."
        if not result.get("live"):
            return "You're offline on Twitch."
        return f"Live with {result.get('viewers')} viewers for {duration(result.get('uptime_s', 0))}."
    return "Done."


def _calendar_speech(result: dict, tz: tzinfo, hints: dict) -> str:
    now = datetime.now(tz)
    events = [e for e in result.get("events", []) if not e["all_day"]]
    if hints.get("next_only"):
        upcoming = [e for e in events if datetime.fromisoformat(e["start"]) > now]
        if not upcoming:
            return "Nothing else on the calendar today or tomorrow."
        e = upcoming[0]
        start = datetime.fromisoformat(e["start"])
        mins = int((start - now).total_seconds() // 60)
        when = f"in {duration(mins * 60)}" if mins < 180 else f"at {clock(start)}"
        day = "" if start.date() == now.date() else " tomorrow"
        return f"Next up: {e['title']}{day} {when}."
    when = hints.get("when", "today")
    if when == "tomorrow":
        target = now.date().toordinal() + 1
        events = [e for e in events if datetime.fromisoformat(e["start"]).date().toordinal() == target]
    elif when == "today":
        events = [e for e in events if datetime.fromisoformat(e["start"]).date() == now.date()]
    if not events:
        free = result.get("free_today") or []
        return f"Nothing scheduled {when}." + (f" You've got {duration(sum(b['minutes'] for b in free) * 60)} of open time." if when == "today" and free else "")
    listed = ", ".join(f"{clock(datetime.fromisoformat(e['start']))} {e['title']}" for e in events[:5])
    more = f", plus {len(events) - 5} more" if len(events) > 5 else ""
    return f"{len(events)} event{'s' if len(events) != 1 else ''} {when}: {listed}{more}."
