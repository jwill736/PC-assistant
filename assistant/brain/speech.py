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
    if tool == "set_volume":
        if result.get("toggled_mute"):
            return "Toggled mute."
        if "volume" not in result:
            return "Volume adjusted."
        if result.get("muted"):
            return f"Muted. Volume is at {result['volume']} percent underneath."
        about = "about " if result.get("approximate") else ""
        return f"Volume {about}{result['volume']}." if args else f"Volume is at {result['volume']} percent."
    if tool == "app_volume":
        parts = [a["app"].title() + (" muted" if a["muted"] else f" at {a['volume']}") for a in result.get("apps", [])]
        return (", ".join(parts) + ".") if parts else "Done."
    if tool == "set_brightness":
        levels = sorted(set(result.get("brightness") or []))
        return f"Brightness {'/'.join(str(b) for b in levels)}." if levels else "Brightness set."
    if tool == "open_settings":
        return f"Opening {result.get('page', args.get('page'))} settings."
    if tool == "virtual_desktop":
        if "desktop" in result:
            return f"Desktop {result['desktop']} of {result['count']}."
        return f"{str(result.get('moved', 'next')).title()} desktop."
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
            health = result.get("health") or {}
            bad = [f"{c} {v['pct']} percent" for c, v in (health.get("classes") or {}).items()
                   if v.get("level") in ("warning", "critical")]
            verdict = f" Problems in the last minute: {', '.join(bad)}." if bad else (
                " Healthy over the last minute." if health.get("classes") else "")
            return (f"Live for {live_for} on {scene}. {st['dropped_pct']} percent dropped frames{kbps}, "
                    f"{result['stats']['fps']} FPS.{verdict}")
        rec = " Recording." if result["recording"]["active"] else ""
        return f"Not live. Scene is {scene}.{rec}"
    if tool == "obs_source":
        return f"{result.get('source')} {'showing' if result.get('visible') else 'hidden'}."
    if tool == "prestream_check":
        return result.get("spoken") or "Checked."
    if tool == "recall":
        return _recall_summary(args, result)
    if tool == "connections":
        return _connections_summary(result)
    if tool.startswith("twitch_") and tool != "twitch_status":
        return _twitch(tool, args, result)
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


def _twitch(tool: str, args: dict, result: dict) -> str:
    if tool == "twitch_connect":
        code = result.get("user_code") or ""
        return (f"Go to twitch dot tv slash activate and enter {' '.join(code)}. The code and a link are on the HUD."
                if code else "Twitch login started. The code is on the HUD.")
    if tool == "twitch_clip":
        return "Clipped on Twitch" + (f": {result['title']}." if result.get("title") else ".")
    if tool == "twitch_marker":
        pos = result.get("position_s")
        if pos is None:
            return "Marker added."
        h, rem = divmod(int(pos), 3600)
        return f"Marker at {h}:{rem // 60:02d}:{rem % 60:02d}." if h else f"Marker at {rem // 60} minutes {rem % 60} seconds."
    if tool == "twitch_set_channel":
        parts = []
        if result.get("title"):
            parts.append("Title updated")
        if result.get("category"):
            parts.append(f"category is now {result['category']}")
        return (", ".join(parts) + ".").capitalize() if parts else "Updated."
    if tool == "twitch_ad":
        return f"Running a {result.get('length') or args.get('length') or 60}-second ad."
    if tool == "twitch_shoutout":
        return f"Shouted out {result.get('user')}."
    if tool == "twitch_poll":
        return f"Poll's up for {result.get('seconds')} seconds."
    if tool == "twitch_chat_send":
        return "Sent."
    if tool == "twitch_events":
        return _events_summary(result.get("events") or [])
    if tool == "twitch_highlights":
        hs = result.get("highlights") or []
        if not hs:
            return "No highlights yet today."
        top = "; ".join(f"{h.get('reason')}" + (f" at {_stamp(h['uptime_s'])}" if h.get("uptime_s") else "") for h in hs[:3])
        return f"{len(hs)} highlight{'s' if len(hs) != 1 else ''} today. {top}."
    return "Done."


def _stamp(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}"


def _events_summary(events: list[dict]) -> str:
    if not events:
        return "Nothing new on Twitch yet."
    by: dict[str, list[dict]] = {}
    for e in events:
        by.setdefault(e["kind"], []).append(e)
    parts = []
    if by.get("raid"):
        parts += [f"raid from {e.get('user')} with {e.get('amount')}" for e in by["raid"][:2]]
    subs = by.get("sub", []) + by.get("resub", [])
    if subs:
        names = ", ".join(e.get("user") or "someone" for e in subs[:3])
        parts.append(f"{len(subs)} sub{'s' if len(subs) != 1 else ''} ({names})")
    if by.get("gift"):
        total = sum(int(e.get("amount") or 0) for e in by["gift"])
        parts.append(f"{total} gifted sub{'s' if total != 1 else ''}")
    if by.get("cheer"):
        bits = sum(int(e.get("amount") or 0) for e in by["cheer"])
        parts.append(f"{bits} bits")
    if by.get("follow"):
        names = ", ".join(e.get("user") or "" for e in by["follow"][:3])
        n = len(by["follow"])
        parts.append(f"{n} follow{'s' if n != 1 else ''}, latest {names}")
    if by.get("redemption"):
        parts.append(f"{len(by['redemption'])} redemption{'s' if len(by['redemption']) != 1 else ''}")
    return ("Recently: " + "; ".join(parts) + ".") if parts else "Nothing new on Twitch yet."


def _recall_summary(args: dict, result: dict) -> str:
    hits = result.get("hits") or []
    query = (args.get("query") or "").strip()
    if not hits:
        return f"I don't have anything about {query} yet." if query else "No notes yet. Say remember, then anything."
    verb = {"note": "you noted", "task": "you added a task", "said": "you said", "replied": "I told you"}
    top = hits[0]
    day = (top.get("when") or "").split(",")[0]
    line = f"{day + ', ' if day else ''}{verb.get(top['kind'], 'you said')}: {top['text'].rstrip('.')}."
    more = len(hits) - 1
    return line[0].upper() + line[1:] + (f" And {more} more on the HUD." if more else "")


def _connections_summary(result: dict) -> str:
    findings = result.get("findings") or []
    brain = result.get("brain") or {}
    active = brain.get("active")
    local = brain.get("local") or {}
    head = ("I'm thinking with Claude." if active == "claude" else
            f"I'm thinking with {local.get('model')} on {local.get('server')}." if active == "local" else
            "No AI model is connected, so only built-in commands work.")
    if not findings:
        return head + " Run the PC scan on the Setup tab to see the rest."
    done = [f["name"] for f in findings if f["status"] == "connected"]
    todo = [f for f in findings if f["status"] == "action"]
    parts = [head]
    if done:
        parts.append(f"Connected: {', '.join(done[:6])}{' and more' if len(done) > 6 else ''}.")
    if todo:
        parts.append("Needs you: " + "; ".join(f"{f['name']}, {f['fix'] or f['detail']}".rstrip(".") for f in todo[:3]) + ".")
    else:
        parts.append("Nothing needs you.")
    return " ".join(parts)

