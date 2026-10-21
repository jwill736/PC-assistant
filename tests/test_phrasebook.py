"""The phrasebook: ~400 things J actually says, each with the (tool, args) the fast path must pick, or MODEL when it
should fall through to the brain. The router is built the way Vesper builds it in production: every skill loaded
(``build_services`` -> ``Skills(svc)``), the assistant's own ``router_context()``, OBS scenes, a contact, a habit,
a Key Light and a few Home Assistant entities.

When a phrase here changes route, decide on purpose: fix the pattern in the skill (or core) that is wrong, or change
the expectation with a comment saying why. The two property tests at the bottom keep new patterns honest: no two
skills may claim one phrase, and a skill may only take a phrase the core router also knows when that override is
listed in INTENDED_OVERRIDES.
"""

import pytest

from assistant.brain.assistant import Assistant
from assistant.brain.router import CANCEL, CONFIRM, KILL, RESUME, clean, route

MODEL = None  # falls through to the brain

# Spotify connected and playing, OBS scenes known, Dana Ortiz a contact, "drink water" a habit, one Key Light, and
# Home Assistant scenes "Gaming" and "Movie" plus a plug called "PC" (the world the BOOK is checked in).
BOOK = [
    # ---- apps & windows
    ("open discord", "open_app", {"name": "discord"}),
    ("open chrome", "open_app", {"name": "chrome"}),
    ("launch spotify", "open_app", {"name": "spotify"}),
    ("open obs", "open_app", {"name": "obs"}),
    ("start steam", "open_app", {"name": "steam"}),
    ("open sticky notes", "open_app", {"name": "sticky notes"}),  # an app, not a note
    ("open google slides", "open_app", {"name": "google slides"}),  # an app, not a deck
    ("close discord", "close_app", {"name": "discord"}),
    ("quit chrome", "close_app", {"name": "chrome"}),
    ("switch to discord", "focus_window", {"query": "discord"}),
    ("focus on chrome", "focus_window", {"query": "chrome"}),
    ("bring chrome to the front", "focus_window", {"query": "chrome"}),
    ("go to youtube.com", "open_urls", {"targets": ["youtube.com"]}),
    ("open github.com", "open_urls", {"targets": ["github.com"]}),
    ("take me to twitch", "open_urls", {"targets": ["twitch"]}),
    ("open a new tab with reddit", "open_urls", {"targets": ["reddit"]}),
    ("open the display settings", "open_settings", {"page": "display"}),
    ("open windows bluetooth settings", "open_settings", {"page": "bluetooth"}),
    # "play valorant": the game is an installed app, so not a Spotify search; the model launches it
    ("play valorant", MODEL),
    # "start a timer" without a length: an article means a thing to do, not an app called "a timer"
    ("start a timer", MODEL),
    # ---- the PC itself: shutdown/restart/sleep are tier 3 (always a spoken yes); never a smart plug called "PC"
    ("shut down the pc", "power", {"action": "shutdown"}),
    ("turn off the pc", "power", {"action": "shutdown"}),
    ("turn off the computer", "power", {"action": "shutdown"}),
    ("shut down my computer", "power", {"action": "shutdown"}),
    ("shut down the computer", "power", {"action": "shutdown"}),
    ("restart the pc", "power", {"action": "restart"}),
    ("restart my pc", "power", {"action": "restart"}),
    ("put the pc to sleep", "power", {"action": "sleep"}),
    ("lock the pc", "power", {"action": "lock"}),
    ("lock my computer", "power", {"action": "lock"}),
    # ---- volume & brightness
    ("volume up", "media_control", {"action": "volume_up", "times": 5}),
    ("volume down", "media_control", {"action": "volume_down", "times": 5}),
    ("turn it up", "media_control", {"action": "volume_up", "times": 5}),
    ("louder", "media_control", {"action": "volume_up", "times": 5}),
    ("quieter", "media_control", {"action": "volume_down", "times": 5}),
    ("set volume to 40", "set_volume", {"level": 40}),
    ("volume 30 percent", "set_volume", {"level": 30}),
    ("what's the volume", "set_volume", {}),
    ("mute", "media_control", {"action": "mute"}),
    ("unmute", "media_control", {"action": "mute"}),
    ("mute my mic", "obs_set_mute", {"source": "mic", "muted": True}),
    ("unmute the mic", "obs_set_mute", {"source": "mic", "muted": False}),
    ("mute desktop audio", "obs_set_mute", {"source": "desktop audio", "muted": True}),
    ("mute discord", "obs_set_mute", {"source": "discord", "muted": True}),
    ("discord volume 50", "app_volume", {"app": "discord", "level": 50}),
    ("spotify volume 20", "spotify_volume", {"level": 20}),  # Spotify's own volume, not the mixer's, while connected
    ("brightness 60", "set_brightness", {"level": 60}),
    ("dimmer", "set_brightness", {"change": -20}),
    ("brighter", "set_brightness", {"change": 20}),
    ("what's the brightness", "set_brightness", {}),
    # ---- music (Spotify connected and playing; see SPOTIFY_OFF and SPOTIFY_PAUSED for the other states)
    ("pause", "spotify_control", {"action": "pause"}),
    # a bare "play" while Spotify already plays stays with Spotify: the play/pause media key would pause it
    ("play", "spotify_control", {"action": "play"}),
    ("resume", "spotify_control", {"action": "play"}),
    ("pause the music", "spotify_control", {"action": "pause"}),
    ("stop the music", "spotify_control", {"action": "pause"}),
    ("stop the song", "spotify_control", {"action": "pause"}),
    ("play the music", "spotify_control", {"action": "play"}),
    ("play some music", "spotify_control", {"action": "play"}),
    ("start the music", "spotify_control", {"action": "play"}),
    ("next", "spotify_control", {"action": "next"}),
    ("skip", "spotify_control", {"action": "next"}),
    ("skip this", "spotify_control", {"action": "next"}),
    ("next song", "spotify_control", {"action": "next"}),
    ("skip this song", "spotify_control", {"action": "next"}),
    ("previous song", "spotify_control", {"action": "previous"}),
    ("last song", "spotify_control", {"action": "previous"}),
    ("previous", "media_control", {"action": "previous"}),
    ("go back", "media_control", {"action": "previous"}),
    ("what's playing", "spotify_now_playing", {}),
    ("what song is this", "spotify_now_playing", {}),
    ("now playing", "spotify_now_playing", {}),
    ("play lofi beats", "spotify_play", {"query": "lofi beats", "kind": "auto"}),
    ("put on some jazz", "spotify_play", {"query": "jazz", "kind": "auto"}),
    ("play my chill playlist", "spotify_play", {"query": "chill", "kind": "playlist"}),
    ("play songs by daft punk", "spotify_play", {"query": "daft punk", "kind": "artist"}),
    ("play something", MODEL),
    ("play it", MODEL),
    ("shuffle on", "spotify_shuffle", {"on": True}),
    ("turn the music down", "spotify_volume", {"change": -15}),
    ("music up", "spotify_volume", {"change": 15}),
    ("set the music to 30", "spotify_volume", {"level": 30}),
    ("youtube lofi", "web_search", {"query": "lofi", "engine": "youtube"}),
    # ---- OBS
    ("switch to gaming", "obs_switch_scene", {"scene": "Gaming"}),
    # Home Assistant has a "Gaming" scene too: "… scene" with an OBS scene's name is OBS's (home defers)
    ("switch to the gaming scene", "obs_switch_scene", {"scene": "Gaming"}),
    ("switch to the brb scene", "obs_switch_scene", {"scene": "BRB"}),
    ("go to just chatting", "obs_switch_scene", {"scene": "Just Chatting"}),
    ("scene starting soon", "obs_switch_scene", {"scene": "Starting Soon"}),
    ("cut to gaming", "obs_switch_scene", {"scene": "Gaming"}),
    ("go live", "obs_control", {"action": "start_stream"}),
    ("start streaming", "obs_control", {"action": "start_stream"}),
    ("start the stream", "obs_control", {"action": "start_stream"}),
    ("end stream", "obs_control", {"action": "stop_stream"}),
    ("stop the stream", "obs_control", {"action": "stop_stream"}),
    # "start recording" is the recorder's (audio → notes); OBS's when said by name, or while live
    ("start recording", "recording_start", {}),
    ("start obs recording", "obs_control", {"action": "start_recording"}),
    ("take notes of this meeting", "recording_start", {"system_audio": True}),
    ("record this", "recording_start", {}),
    ("record on stream", "recording_start", {"on_stream": True}),
    ("stop recording", "obs_control", {"action": "stop_recording"}),  # nothing being recorded: OBS's
    ("pause recording", "obs_control", {"action": "pause_recording"}),
    ("clip that", "obs_control", {"action": "save_replay"}),
    ("save replay", "obs_control", {"action": "save_replay"}),
    ("hide the webcam", "obs_source", {"source": "webcam", "visible": False}),
    ("show my camera", "obs_source", {"source": "camera", "visible": True}),
    ("turn off my camera", "obs_source", {"source": "camera", "visible": False}),
    ("turn off the overlay", "obs_source", {"source": "overlay", "visible": False}),
    ("toggle alerts", "obs_source", {"source": "alerts"}),
    ("hide the chat", "obs_source", {"source": "chat", "visible": False}),
    ("am i ready to stream", "prestream_check", {}),
    ("pre stream check", "prestream_check", {}),
    # "how's the stream" is the health check (frames, bitrate); "how's the stream going/doing" the co-pilot's stats
    ("how's the stream", "obs_status", {}),
    ("stream health", "obs_status", {}),
    ("am i live", "obs_status", {}),
    ("am i dropping frames", "obs_status", {}),
    ("search online for tips on fixing dropped frames", "web_search",  # a web question, not the OBS check
     {"query": "tips on fixing dropped frames", "engine": "google"}),
    ("how is the stream going", "stream_stats_now", {}),
    ("how's the stream doing", "stream_stats_now", {}),
    ("stream stats", "stream_stats_now", {}),
    ("brb", MODEL),  # not a scene switch on its own: too easy to say in passing
    # ---- Twitch
    ("make a clip", "twitch_clip", {}),
    ("clip it on twitch", "twitch_clip", {}),
    ("drop a marker", "twitch_marker", {}),
    ("mark that", "twitch_marker", {}),
    ("set the stream title to Ranked grind with chat", "twitch_set_channel", {"title": "Ranked grind with chat"}),
    ("change the category to Valorant", "twitch_set_channel", {"category": "valorant"}),
    ("run a 90 second ad", "twitch_ad", {"length": 90}),
    ("run an ad", "twitch_ad", {"length": 60}),
    ("shout out ninja", "twitch_shoutout", {"user": "ninja"}),
    ("give shroud a shoutout", "twitch_shoutout", {"user": "shroud"}),
    ("say hello everyone in chat", "twitch_chat_send", {"message": "hello everyone"}),
    ("tell chat I'll be right back", "twitch_chat_send", {"message": "I'll be right back"}),
    ("any new followers", "twitch_events", {}),
    ("who just raided", "twitch_events", {}),
    ("show highlights", "twitch_highlights", {}),
    ("connect my twitch account", "twitch_connect", {}),
    ("any questions in chat", "chat_pulse", {"focus": "questions"}),
    ("how's chat", "chat_pulse", {"focus": "mood"}),
    ("what's the chat mood", "chat_pulse", {"focus": "mood"}),
    ("vod chapters", "stream_recap", {"session": "last", "part": "chapters"}),
    ("stream recap", "stream_recap", {"session": "last"}),
    ("recap the stream", "stream_recap", {"session": "last"}),
    ("suggest a title", "suggest_title", {}),
    ("give me some titles for valorant ranked", "suggest_title", {"idea": "valorant ranked"}),
    # ---- briefing, time, calendar, news
    ("good morning", "briefing", {}),
    ("brief me", "briefing", {}),
    ("start my day", "briefing", {}),
    ("what's on my agenda today", "briefing", {}),
    ("recap my day", "recap", {}),
    ("how did my day go", "recap", {}),
    ("what did i do today", "recap", {}),
    # "what should I do/work on next" is the day plan's; "what's next" stays the general question (the model looks at
    # tasks, calendar and the stream together)
    ("what should i do next", "what_next", {}),
    ("what should i work on", "what_next", {}),
    ("what should i focus on now", "what_next", {}),
    ("what's next", "next", {}),
    ("what time is it", "time", {}),
    ("what day is it", "time", {}),
    ("what's my next meeting", "calendar", {"days": 2, "next_only": True}),
    ("when's my next meeting", "calendar", {"days": 2, "next_only": True}),
    ("next meeting", "calendar", {"days": 2, "next_only": True}),
    ("what's on my calendar", "calendar", {"days": 1, "when": "today"}),
    ("check my calendar", "calendar", {"days": 1, "when": "today"}),
    ("what's on my calendar tomorrow", "calendar", {"days": 2, "when": "tomorrow"}),
    ("my schedule this week", "calendar", {"days": 7, "when": "this week"}),
    ("news", "news", {}),
    ("what's in the news", "news", {}),
    ("read me the headlines", "news", {}),
    ("where am i", "where_am_i", {}),
    ("what's open", "where_am_i", {}),
    ("start work mode", "run_routine", {"profile": "work"}),
    ("let's stream", "run_routine", {"profile": "stream"}),
    ("start my stream setup", "run_routine", {"profile": "stream"}),
    ("next desktop", "virtual_desktop", {"action": "next"}),
    ("desktop 2", "virtual_desktop", {"action": "go", "number": 2}),
    # ---- tasks
    ("add a task buy milk", "add_task", {"title": "buy milk"}),
    ("ad a task call the bank", "add_task", {"title": "call the bank"}),
    ("new task fix the overlay", "add_task", {"title": "fix the overlay"}),
    # "put on my list" is a task even while Spotify is connected ("put on …" is otherwise music)
    ("put on my list renew passport", "add_task", {"title": "renew passport"}),
    ("remind me to call mom", "add_task", {"title": "call mom"}),  # no time: a to-do
    ("remember to buy eggs", "add_task", {"title": "buy eggs"}),  # "remember to …" is a to-do; "remember that …" a note
    ("mark the overlay task as done", "complete_task", {"title": "overlay task"}),
    ("finish the slides", "complete_task", {"title": "slides"}),
    ("check off buy milk", "complete_task", {"title": "buy milk"}),
    ("done with groceries", "complete_task", {"title": "groceries"}),
    ("list my tasks", "list_tasks", {}),
    ("what are my tasks", "list_tasks", {}),
    ("my to-dos", "list_tasks", {}),
    # ---- reminders, timers, routines (a time or a moment makes it a reminder, which beats the plain to-do)
    ("remind me to call mom at 5pm", "remind", {"text": "call mom", "when": "at 5pm"}),
    ("remind me in 10 minutes to stretch", "remind", {"text": "stretch", "when": "in 10 minutes"}),
    ("remind me tomorrow to pay rent", "remind", {"text": "pay rent", "when": "tomorrow"}),
    ("remind me tomorrow to play valorant", "remind", {"text": "play valorant", "when": "tomorrow"}),
    ("remind me to check the weather at 5pm", "remind", {"text": "check the weather", "when": "at 5pm"}),
    ("remind me to stretch after the stream", "remind", {"text": "stretch", "trigger": "after_stream"}),
    ("after the stream remind me to post the vod", "remind", {"text": "post the vod", "trigger": "after_stream"}),
    ("remind me to reply when dana emails me", "remind", {"text": "reply", "trigger": "email_from:dana"}),
    ("remind me to check the oven when the job is done", "remind", {"text": "check the oven", "trigger": "job_done"}),
    ("set a timer for 10 minutes", "set_timer", {"seconds": 600}),
    ("timer 5 minutes", "set_timer", {"seconds": 300}),
    ("5 minute timer", "set_timer", {"seconds": 300}),
    ("set a 25 minute timer for pizza", "set_timer", {"seconds": 1500, "label": "pizza"}),
    ("start a timer for 1 hour", "set_timer", {"seconds": 3600}),
    ("cancel the timer", "cancel_automation", {"what": "timer"}),
    ("stop the timer", "cancel_automation", {"what": "timer"}),
    ("what timers do i have", "list_automations", {}),
    ("how long is left on the timer", "list_automations", {}),
    ("how long is left", "list_automations", {}),
    ("my reminders", "list_automations", {}),
    ("cancel the reminder about mom", "cancel_automation", {"what": "reminder about mom"}),
    ("delete my routine", "cancel_automation", {"what": "routine"}),
    ("every weekday at 9 am check my email", "add_routine", {"every": "weekday at 9 am", "command": "check my email"}),
    # the weather words inside a routine don't make it a weather question
    ("every day at 8 am give me the weather", "add_routine", {"every": "day at 8 am", "command": "give me the weather"}),
    ("every morning at 8 give me the weather", "add_routine", {"every": "morning at 8", "command": "give me the weather"}),
    ("every day at 9 play lofi beats", "add_routine", {"every": "day at 9", "command": "play lofi beats"}),
    # ---- notes & memory
    ("remember that the wifi password is on the fridge", "remember", {"text": "the wifi password is on the fridge"}),
    ("note that the meeting moved to thursday", "remember", {"text": "the meeting moved to thursday"}),
    ("make a note the router is in the closet", "remember", {"text": "the router is in the closet"}),
    # a note that starts with a known contact's name is about that person (People), anything else a plain note
    ("remember that dana's daughter is starting college", "remember_about",
     {"name": "dana", "text": "Dana's daughter is starting college"}),
    ("remember that dana likes coffee", "remember_about", {"name": "dana", "text": "Dana likes coffee"}),
    ("what did i say about the router", "recall", {"query": "the router"}),
    ("do you remember the wifi password", "recall", {"query": "wifi password"}),
    ("search my notes for router", "recall", {"query": "router"}),
    ("what are my notes", "recall", {"query": ""}),
    ("what did i write down today", "recall", {"query": ""}),
    ("what do i know about the q3 plan", "recall", {"query": "the q3 plan"}),
    ("what do you know about me", "what_you_know", {"about": ""}),
    ("what have you learned about me", "what_you_know", {"about": ""}),
    ("what do you know about kubernetes", "what_you_know", {"about": "kubernetes"}),
    # a contact: People's card (last contact, notes, things to ask) beats the fact list; memory defers to it
    ("what do you know about dana", "who_is", {"name": "dana"}),
    ("forget that the wifi password is on the fridge", "forget", {"what": "the wifi password is on the fridge"}),
    ("forget it", "cancel", {}),
    ("forget about it", "cancel", {}),  # "never mind", not "forget the fact called 'it'"
    ("go off the record", "off_the_record", {"minutes": 30}),
    ("off the record for 10 minutes", "off_the_record", {"minutes": 10}),
    ("back on the record", "off_the_record", {"minutes": 0}),
    ("search everything for invoice", "search_everything", {"query": "invoice"}),
    ("what do i have on acme", "search_everything", {"query": "acme"}),
    # ---- people
    ("who is dana", "who_is", {"name": "dana"}),
    ("who's dana ortiz", "who_is", {"name": "dana ortiz"}),
    ("tell me about dana", "who_is", {"name": "dana"}),
    ("when did i last talk to dana", "who_is", {"name": "dana"}),
    ("who is ninja", MODEL),  # not a contact: the model knows streamers
    ("next time i talk to dana ask her about the trip", "remember_about", {"name": "dana", "text": "ask about the trip"}),
    ("ask dana about the contract next time", "remember_about", {"name": "dana", "text": "ask about the contract"}),
    ("who haven't i talked to in a while", "people_overdue", {}),
    ("who should i reach out to", "people_overdue", {}),
    # ---- documents
    ("open the q3 deck", "open_document", {"name": "q3"}),
    ("pull up the q3 deck", "open_document", {"name": "q3"}),
    ("open the budget spreadsheet", "open_document", {"name": "budget"}),
    ("open my resume document", "open_document", {"name": "resume"}),
    ("open the file called roadmap", "open_document", {"name": "roadmap"}),
    ("find the document about pricing", "search_library", {"query": "pricing"}),
    ("find my pdf about taxes", "search_library", {"query": "taxes"}),
    ("search my documents for pricing", "search_library", {"query": "pricing"}),
    ("search my drive for onboarding", "search_library", {"query": "onboarding"}),
    ("what's connected", "connections", {}),
    ("what's missing", "connections", {}),
    # ---- email
    ("check my email", "check_email", {}),
    ("check my work email", "check_email", {"account": "work"}),
    ("read my email", "check_email", {}),
    ("any new emails", "check_email", {}),
    ("do i have any unread mail", "check_email", {}),
    ("who's waiting on me", "check_email", {}),
    ("what's in my inbox", "check_email", {}),
    ("search my email for invoice", "search_email", {"query": "invoice"}),
    ("any emails from dana", "search_email", {"query": "from:dana"}),
    ("did dana email me", "search_email", {"query": "from:dana"}),
    ("who do i owe a reply", "replies_owed", {}),
    ("who do i owe a reply at work", "replies_owed", {"account": "work"}),
    ("who am i waiting on", "waiting_on", {}),
    ("who hasn't replied", "waiting_on", {}),
    ("what did i promise", "promises", {}),
    ("my promises", "promises", {}),
    # ---- web search
    ("google best mechanical keyboards", "web_search", {"query": "best mechanical keyboards", "engine": "google"}),
    ("search for rtx 5090 benchmarks", "web_search", {"query": "rtx 5090 benchmarks", "engine": "google"}),
    ("search youtube for valorant tips", "web_search", {"query": "valorant tips", "engine": "youtube"}),
    ("search youtube for weather radar", "web_search", {"query": "weather radar", "engine": "youtube"}),
    # get_weather only knows J's own town, so another city's weather is a search (or the model)
    ("look up the weather in tokyo", "web_search", {"query": "the weather in tokyo", "engine": "google"}),
    # ---- the PC's health
    ("how's my pc", "pc_health", {}),
    ("how's my pc doing", "pc_health", {}),
    ("pc health", "pc_health", {}),
    ("is my pc ok", "pc_health", {}),
    ("how hot is my gpu", "pc_health", {"focus": "gpu"}),
    ("what's my gpu temp", "pc_health", {"focus": "gpu"}),
    ("what's my gpu temperature", "pc_health", {"focus": "gpu"}),  # "temperature" is the PC's here, not the weather
    ("what's the cpu temperature", MODEL),
    ("optimize my pc", "optimize_pc", {"streaming": False}),
    ("why is my pc slow", "optimize_pc", {"streaming": False}),
    ("system status", "optimize_pc", {"streaming": False}),
    ("why is my d drive so full", "disk_report", {"drive": "d"}),
    ("what's taking up space on c", "disk_report", {"drive": "c"}),
    ("free up disk space", "disk_report", {"drive": ""}),
    ("clean up my downloads", "tidy_downloads", {"dry_run": True}),
    ("tidy my downloads", "tidy_downloads", {"dry_run": True}),
    ("undo the downloads tidy", "undo_downloads_tidy", {}),
    ("clean temp files", "clean_temp", {}),
    # ---- speaking up (nudges)
    ("quiet mode", "quiet_mode", {"minutes": 60}),
    ("turn on quiet mode", "quiet_mode", {"minutes": 60}),
    ("quiet mode for 2 hours", "quiet_mode", {"minutes": 120}),
    ("do not disturb", "quiet_mode", {"minutes": 60}),
    ("turn on do not disturb", "quiet_mode", {"minutes": 60}),
    ("stop interrupting me", "quiet_mode", {"minutes": 60}),
    ("you can talk again", "quiet_mode", {"minutes": 0}),
    ("turn off quiet mode", "quiet_mode", {"minutes": 0}),
    ("turn off do not disturb", "quiet_mode", {"minutes": 0}),
    ("what did i miss", "what_did_i_miss", {}),
    ("catch me up", "what_did_i_miss", {}),
    ("any updates", "what_did_i_miss", {}),
    # ---- focus
    ("focus mode", "focus_start", {"minutes": 0}),
    ("start focus mode", "focus_start", {"minutes": 0}),
    ("start a focus session", "focus_start", {"minutes": 0}),
    ("let's focus", "focus_start", {"minutes": 0}),
    ("time to focus", "focus_start", {"minutes": 0}),
    ("focus for 25 minutes", "focus_start", {"minutes": 25}),
    ("25 minute focus session", "focus_start", {"minutes": 25}),
    # a "focus timer" is a focus session, not a plain timer (automations leaves it alone)
    ("25 minute focus timer", "focus_start", {"minutes": 25}),
    ("set a focus timer for 25 minutes", "focus_start", {"minutes": 25}),
    ("cancel the focus timer", "focus_stop", {}),
    ("stop the focus timer", "focus_stop", {}),
    ("deep work for 2 hours on the api", "focus_start", {"minutes": 120, "label": "the api"}),
    ("stop focus", "focus_stop", {}),
    ("end focus session", "focus_stop", {}),
    ("i'm done with focus", "focus_stop", {}),
    ("how much focus did i get today", "focus_stats", {}),
    ("my focus stats", "focus_stats", {}),
    ("how long is left in my focus session", "focus_stats", {}),
    # ---- habits
    ("track a habit drink water 8 times a day", "habit_add", {"name": "drink water", "target": 8, "period": "day"}),
    ("track a habit: read 20 pages a day", "habit_add", {"name": "read 20 pages", "target": 1, "period": "day"}),
    ("i drank water", "habit_log", {"name": "drink water", "count": 1}),
    ("i just drank water", "habit_log", {"name": "drink water", "count": 1}),
    ("i drank 2 glasses of water", "habit_log", {"name": "drink water", "count": 2}),
    ("how are my habits", "habit_status", {}),
    ("habit status", "habit_status", {}),
    ("stop tracking drink water", "habit_remove", {"name": "drink water"}),
    ("i finished the report", MODEL),  # past tense, but no habit called that
    ("i need water", MODEL),  # not past tense: not a log
    # ---- journal (private: never logged)
    ("journal", "journal_prompt", {}),
    ("let's journal", "journal_prompt", {}),
    ("journal today was rough but the stream went well", "journal_add",
     {"text": "today was rough but the stream went well"}),
    ("journal i need to call dana tomorrow", "journal_add", {"text": "i need to call dana tomorrow"}),
    ("dear diary i finally beat the boss", "journal_add", {"text": "i finally beat the boss"}),
    ("add to my journal I'm proud of the launch", "journal_add", {"text": "i'm proud of the launch"}),
    ("how has my mood been this week", "journal_mood", {"when": "this week"}),
    ("how's my mood", "journal_mood", {"when": ""}),
    ("what did i journal about yesterday", "journal_search", {"query": "", "when": "yesterday"}),
    ("show my journal", "journal_search", {"query": "", "when": ""}),
    ("read my journal", "journal_search", {"query": "", "when": ""}),
    ("open my journal", "journal_search", {"query": "", "when": ""}),  # Vesper's journal, not an app
    ("search my journal for burnout", "journal_search", {"query": "burnout", "when": ""}),
    # ---- the day plan
    ("plan my day", "plan_day", {"day": "today"}),
    ("plan tomorrow", "plan_day", {"day": "tomorrow"}),
    ("replan", "plan_day", {"day": "today"}),
    ("am i overbooked", "am_i_overbooked", {}),
    ("is my day too full", "am_i_overbooked", {}),
    ("shut down for the day", "shutdown_day", {}),  # the work day, not the PC
    ("i'm done for the day", "shutdown_day", {}),
    ("wrap up for the day", "shutdown_day", {}),
    ("end my day", "shutdown_day", {}),
    ("weekly review", "weekly_review", {}),
    ("how did my week go", "weekly_review", {}),
    # ---- money, packages, trips, pulse
    ("how much am i spending on subscriptions", "subscriptions", {"ask": "total"}),
    ("what subscriptions do i have", "subscriptions", {"ask": "list"}),
    ("what bills are due this week", "upcoming_bills", {"days": 7}),
    ("any trials ending", "upcoming_bills", {"days": 14}),
    ("what's due", "upcoming_bills", {"days": 14}),
    ("where's my package", "packages", {"when": "all"}),
    ("track my package", "packages", {"when": "all"}),
    ("any deliveries today", "packages", {"when": "today"}),
    ("what's on the way", "packages", {"when": "all"}),
    ("when's my next flight", "trips", {}),
    ("my trips", "trips", {}),
    ("what's my pulse", "pulse", {}),
    ("run my pulse", "run_pulse", {}),
    ("refresh pulse", "run_pulse", {}),
    # ---- weather
    ("what's the weather", "get_weather", {"day": "today"}),
    ("weather tomorrow", "get_weather", {"day": "tomorrow"}),
    ("will it rain today", "get_weather", {"day": "today"}),
    ("do i need an umbrella", "get_weather", {"day": "today"}),
    ("how cold is it", "get_weather", {"day": "today"}),
    ("what's the temperature outside", "get_weather", {"day": "today"}),
    ("what's the forecast for the weekend", "get_weather", {"day": "weekend"}),
    ("what's the weather in london", "weather_in", {"place": "london"}),  # travel.py: any town's forecast
    ("what's the temperature in my room", MODEL),  # a room sensor, not the forecast
    ("set my location to atlanta", "set_location", {"name": "atlanta"}),
    ("i live in atlanta", "set_location", {"name": "atlanta"}),
    ("i am in a meeting", MODEL),  # not a new home town
    # ---- meetings
    ("prep me for my next meeting", "meeting_prep", {}),
    ("meeting prep", "meeting_prep", {}),
    ("prep me for the contract review", "meeting_prep", {"title": "contract review"}),
    ("prep me for the stream", MODEL),
    ("who's in my next meeting", "next_meeting", {}),
    ("who am i meeting next", "next_meeting", {}),
    # ---- screen, clipboard, rewind
    ("look at this", "look_at_screen", {"question": "", "whole_screen": False, "explicit": True}),
    ("look at my screen", "look_at_screen", {"question": "", "whole_screen": True, "explicit": True}),
    ("what's on my screen", "look_at_screen", {"question": "what's on my screen", "whole_screen": True, "explicit": True}),
    # "what am I looking at" wants the picture described, so the screen skill beats the window list (where_am_i)
    ("what am i looking at", "look_at_screen", {"question": "what am i looking at", "whole_screen": False, "explicit": True}),
    ("read this error", "look_at_screen", {"question": "read this error", "whole_screen": False, "explicit": True}),
    ("what's this error", "look_at_screen", {"question": "what's this error", "whole_screen": False, "explicit": True}),
    ("explain this error", "look_at_screen", {"question": "explain this error", "whole_screen": False, "explicit": True}),
    ("what does this error mean", "look_at_screen",
     {"question": "what does this error mean", "whole_screen": False, "explicit": True}),
    ("summarize this page", "look_at_screen", {"question": "summarize this page", "whole_screen": False, "explicit": True}),
    ("translate this page to spanish", "look_at_screen",
     {"question": "translate this page to spanish", "whole_screen": False, "explicit": True}),
    ("what did i copy", "clipboard_history", {}),
    ("what's on my clipboard", "clipboard_history", {}),
    ("clipboard history", "clipboard_history", {}),
    ("summarize what i copied", "clipboard_action", {"action": "summarize", "index": 0}),
    ("summarize the clipboard", "clipboard_action", {"action": "summarize", "index": 0}),
    ("translate my clipboard to spanish", "clipboard_action", {"action": "translate", "index": 0, "language": "Spanish"}),
    ("fix the grammar of what i copied", "clipboard_action", {"action": "fix grammar", "index": 0}),
    ("copy that", MODEL),  # only a clipboard action right after Vesper made something to copy (CLIPBOARD_RESULT)
    ("where was i", "where_was_i", {}),
    ("what was i doing", "where_was_i", {}),
    ("what was i doing at 3 pm yesterday", "rewind", {"when": "at 3 pm yesterday"}),
    ("what was i doing yesterday", "rewind", {"when": "yesterday"}),
    ("how long was i on youtube today", "time_spent", {"target": "youtube", "when": "today"}),
    ("how much time did i spend in valorant this week", "time_spent", {"target": "valorant", "when": "this week"}),
    ("how long have i been away", MODEL),
    # ---- lights & home
    ("turn off the lights", "lights_off", {"target": "lights"}),
    ("turn on the lights", "lights_on", {"target": "lights"}),
    ("lights off", "lights_off", {"target": "lights"}),
    ("turn the lights on", "lights_on", {"target": "lights"}),
    ("turn on my key light", "lights_on", {"target": "key light"}),
    ("lights to 40 percent", "lights_brightness", {"target": "lights", "level": 40}),
    ("set the key light to 30 percent", "lights_brightness", {"target": "key light", "level": 30}),
    ("dim the lights", "lights_brightness", {"target": "lights", "change": -20}),
    ("brighten the lights", "lights_brightness", {"target": "lights", "change": 20}),
    ("make the lights warmer", "lights_temperature", {"target": "lights", "warmer": True}),
    ("key light to 5000k", "lights_temperature", {"target": "key light", "kelvin": 5000}),
    ("turn on the desk fan", "lights_on", {"target": "desk fan"}),  # a Home Assistant name, said exactly
    ("turn on the fan", MODEL),  # only part of a name: changes need the exact name
    ("turn off the tv", MODEL),
    ("movie mode", "home_scene", {"name": "movie"}),
    # ---- YouTube & Discord
    ("how's my youtube channel", "youtube_stats", {}),
    ("youtube stats", "youtube_stats", {}),
    ("how many subscribers do i have on youtube", "youtube_stats", {}),
    ("how are my latest videos doing", "youtube_latest", {"count": 3}),
    ("announce going live in 5 minutes", MODEL),  # no Discord webhook yet (see DISCORD_ON)
    ("post on discord stream starts at 8", "discord_announce", {"text": "Stream starts at 8"}),
    ("post my latest clip", "discord_post_clip", {}),
    ("share the clip to discord", "discord_post_clip", {}),
    # ---- conversations that evolve (2.2): threads, modes, style (in-conversation phrases: test_conversation_threads)
    ("let's talk", "conversation", {"on_stream": False}),
    ("let's talk about the new pc", "conversation", {"on_stream": False, "topic": "the new pc"}),
    ("let's talk off the record", "conversation", {"on_stream": False, "off_record": True}),
    ("let's continue our talk about the move", "conversation", {"on_stream": False, "resume": "the move"}),
    ("what were we saying about the new pc", "conversation", {"on_stream": False, "resume": "the new pc"}),
    ("pick up where we left off", "conversation", {"on_stream": False, "resume": ""}),
    ("let's brainstorm names for my channel", "conversation",
     {"on_stream": False, "mode": "brainstorm", "topic": "names for my channel"}),
    ("play devil's advocate", "conversation", {"on_stream": False, "mode": "devil"}),
    # a conversation mode, not a window called "devil's advocate"
    ("switch to devil's advocate", "conversation", {"on_stream": False, "mode": "devil"}),
    ("coach me on public speaking", "conversation", {"on_stream": False, "mode": "coach", "topic": "public speaking"}),
    ("interview me", "conversation", {"on_stream": False, "mode": "interview"}),
    ("teach me chess", "conversation", {"on_stream": False, "mode": "teach", "topic": "chess"}),
    ("let's rubber duck this", "conversation", {"on_stream": False, "mode": "duck"}),
    ("plan the move with me", "conversation", {"on_stream": False, "mode": "plan", "topic": "the move"}),
    ("tell me a story", "conversation", {"on_stream": False, "mode": "story"}),
    ("forget our conversation about the move", "conversation_forget", {"topic": "the move"}),
    ("don't follow up on that", "conversation_followups", {"on": False}),
    ("what have we talked about lately", "conversation_threads", {}),
    ("reset my conversation style", "conversation_style", {"action": "reset"}),
    ("shorter", MODEL),  # style feedback only in a conversation
    ("let me think", MODEL),
    ("tell me more", MODEL),
    ("let's plan my day", MODEL),  # the planner's "plan my day" needs no "let's"; never plan-with-me mode
    # ---- conversation control: always the core's, before any skill
    ("stop", "cancel", {}),  # the barge-in word: never "stop the music"
    ("cancel", "cancel", {}),
    ("never mind", "cancel", {}),
    ("no", "cancel", {}),
    ("yes", "confirm", {}),
    ("do it", "confirm", {}),
    ("stop everything", "kill", {}),
    ("resume control", "resume", {}),
    ("new conversation", "reset", {}),
    ("start over", "reset", {}),
    # ---- the model's
    ("what's the meaning of life", MODEL),
    ("write me a poem about cats", MODEL),
    ("how do i fix a memory leak in python", MODEL),
    ("explain quantum computing", MODEL),
    ("tell me a joke", MODEL),
    ("what's 15 times 23", MODEL),
    ("can you help me with my essay", MODEL),
    ("why is the sky blue", MODEL),
    ("what can you do", MODEL),
]

# Spotify not connected: the generic media keys and the model take music.
SPOTIFY_OFF = [
    ("pause", "media_control", {"action": "play_pause"}),
    ("play", "media_control", {"action": "play_pause"}),
    ("pause the music", "media_control", {"action": "play_pause"}),
    ("start the music", "media_control", {"action": "play_pause"}),  # not an app called "music"
    ("next", "media_control", {"action": "next"}),
    ("next song", "media_control", {"action": "next"}),
    ("skip this song", "media_control", {"action": "next"}),
    ("previous song", "media_control", {"action": "previous"}),
    ("spotify volume 20", "app_volume", {"app": "spotify", "level": 20}),  # the Windows mixer's
    ("what's playing", "spotify_now_playing", {}),  # still Spotify's: its answer says how to connect
    ("play lofi beats", MODEL),
    ("put on some jazz", MODEL),
    ("put on my list renew passport", "add_task", {"title": "renew passport"}),
    ("stop", "cancel", {}),
]

# Spotify connected with a track loaded but paused: a bare "pause" goes to the media key (a video may be playing).
SPOTIFY_PAUSED = [
    ("pause", "media_control", {"action": "play_pause"}),
    ("play", "spotify_control", {"action": "play"}),
    ("resume", "spotify_control", {"action": "play"}),
    ("pause the music", "spotify_control", {"action": "pause"}),
    ("next", "media_control", {"action": "next"}),
    ("next song", "spotify_control", {"action": "next"}),
]

# With a Discord webhook, "announce …" posts there.
DISCORD_ON = [("announce going live in 5 minutes", "discord_announce", {"text": "Going live in 5 minutes"})]

# Right after a clipboard action made something, "copy that" puts it on the clipboard.
CLIPBOARD_RESULT = [("copy that", "copy_to_clipboard", {}), ("copy it to my clipboard", "copy_to_clipboard", {})]

# Phrases the core router also knows that a skill deliberately answers instead (checked on BOOK, Spotify playing).
INTENDED_OVERRIDES = {
    # Spotify, while connected: its own control and volume instead of the media keys / mixer
    **{p: "spotify" for p in ["spotify volume 20", "pause", "play", "resume", "pause the music", "play the music",
                              "start the music", "next", "skip", "skip this", "next song",
                              "skip this song", "previous song", "last song"]},
    # the co-pilot's live numbers instead of the OBS health check
    **{p: "stream_copilot" for p in ["how is the stream going", "how's the stream doing", "stream stats"]},
    # the day plan instead of the model's "what next"
    **{p: "planner" for p in ["what should i do next", "what should i work on", "what should i focus on now",
                              "shut down for the day"]},
    # a time or a moment makes "remind me to …" a reminder, not a to-do
    **{p: "automations" for p in ["remind me to call mom at 5pm", "remind me to stretch after the stream",
                                  "remind me to reply when dana emails me",
                                  "remind me to check the oven when the job is done",
                                  "remind me to check the weather at 5pm"]},
    # a note about a contact goes on their card
    **{p: "people" for p in ["remember that dana's daughter is starting college", "remember that dana likes coffee",
                             "what do you know about dana"]},
    **{p: "memory" for p in ["what do you know about me", "what do you know about kubernetes",
                             "search everything for invoice"]},
    **{p: "janitor" for p in ["how's my pc", "how's my pc doing", "pc health"]},
    **{p: "focus" for p in ["focus mode", "start focus mode", "focus for 25 minutes"]},
    **{p: "journal" for p in ["search my journal for burnout", "open my journal"]},
    "when's my next flight": "travel",
    "run my pulse": "pulse",
    "prep me for my next meeting": "meetings",
    "who's in my next meeting": "meetings",
    "youtube stats": "youtube",
    # J asked for "Vesper, start recording" to record his audio for notes; OBS's recording is "start obs recording"
    "start recording": "recorder",
    "start obs recording": "recorder",  # the core read it as "open the app 'obs recording'"
    "what am i looking at": "screen",
    # a conversation mode, not a window to focus
    "switch to devil's advocate": "conversation",
}


# ---- the world -------------------------------------------------------------------------------------------------

def _world(svc, *, spotify="playing"):
    sk = svc.skills
    svc.storage.execute("INSERT INTO people_contacts(email, name, account, count) VALUES "
                        "('dana@client.com', 'Dana Ortiz', 'work', 3)")
    sk["habits"].add("drink water", 8, "day")
    sk["home"].cfg["key_lights"] = [{"ip": "192.168.1.40", "name": "Key Light"}]
    sk["home"]._states = [{"entity_id": "scene.gaming", "attributes": {"friendly_name": "Gaming"}},
                          {"entity_id": "scene.movie", "attributes": {"friendly_name": "Movie"}},
                          {"entity_id": "switch.pc", "attributes": {"friendly_name": "PC"}},
                          {"entity_id": "fan.desk", "attributes": {"friendly_name": "Desk fan"}}]
    svc.cfg["apps"]["valorant"] = "C:/Riot Games/VALORANT/live/VALORANT.exe"
    svc.obs.enabled, svc.obs.scenes = True, ["Gaming", "BRB", "Just Chatting", "Starting Soon"]
    sp = sk["spotify"]
    if spotify != "off":
        sp.tokens = {"access_token": "x", "refresh_token": "y"}
        sp.now = {"playing": spotify == "playing", "track": {"name": "Midnight City"}}
    return Assistant(svc).router_context()


def _said(intent):
    if intent is None:
        return MODEL
    if intent.kind != "tool":
        return (intent.kind, intent.args)
    return (intent.tool, intent.args)


def _expected(row):
    return MODEL if row[1] is MODEL else (row[1], row[2] if len(row) > 2 else {})


def _check(svc, book, **world):
    ctx = _world(svc, **world)
    wrong = []
    for row in book:
        got, want = _said(route(row[0], ctx)), _expected(row)
        if got != want:
            wrong.append(f"{row[0]!r}: got {got}, want {want}")
    assert not wrong, "\n".join(wrong)


def test_the_book_is_big_and_has_no_duplicates():
    said = [r[0] for r in BOOK]
    assert len(said) >= 300 and len(said) == len(set(said)), [s for s in said if said.count(s) > 1]


def test_every_phrase_routes_as_expected(svc):
    _check(svc, BOOK)


def test_music_phrases_without_spotify(svc):
    _check(svc, SPOTIFY_OFF, spotify="off")


def test_music_phrases_with_spotify_paused(svc):
    _check(svc, SPOTIFY_PAUSED, spotify="paused")


def test_announce_goes_to_discord_once_it_has_a_webhook(svc, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    _check(svc, DISCORD_ON)


def test_copy_that_right_after_a_clipboard_result(svc):
    clip = svc.skills["clipboard"]
    clip.result = {"at": clip.clock(), "text": "Bonjour"}
    _check(svc, CLIPBOARD_RESULT)


def test_every_skill_is_loaded_like_production(svc):
    from assistant.skills import MODULES

    assert set(svc.skills.all) == set(MODULES) and not svc.skills.errors


def test_shutting_the_pc_down_always_waits_for_a_yes(svc, monkeypatch):
    from assistant.integrations import desktop

    done = []
    monkeypatch.setattr(desktop, "power_action", lambda action: done.append(action) or {"ok": True})
    _world(svc)
    a = Assistant(svc)
    for said in ("turn off the pc", "restart my pc", "put the pc to sleep"):
        out = a.handle(said)
        assert out["kind"] == "pending" and "Say yes or no" in out["reply"], (said, out)
        a.handle("cancel")
    assert done == []  # and never a Home Assistant plug called "PC"
    a.handle("shut down the computer")
    a.handle("yes")
    assert done == ["shutdown"]


# ---- properties ------------------------------------------------------------------------------------------------

def _claims(svc, t: str) -> list[str]:
    return [s.name for s in svc.skills if s.route(t) is not None]


ALL_BOOKS = [("playing", BOOK), ("off", SPOTIFY_OFF), ("paused", SPOTIFY_PAUSED)]


@pytest.mark.parametrize("state,book", ALL_BOOKS, ids=[s for s, _ in ALL_BOOKS])
def test_no_two_skills_claim_the_same_phrase(svc, state, book):
    _world(svc, spotify=state)
    clashes = {r[0]: c for r in book if len(c := _claims(svc, clean(r[0]))) > 1}
    assert not clashes, clashes


def test_skills_only_override_the_core_router_where_intended(svc):
    """A skill runs before the generic patterns, so any phrase both answer is a decision: listed, or a bug."""
    ctx = _world(svc)
    core = Assistant(svc).router_context()
    core.skill_route = lambda t: None
    overrides = {}
    for row in BOOK:
        t = clean(row[0])
        if any(p.match(t) for p in (CONFIRM, CANCEL, KILL, RESUME)):
            continue  # control words: the core answers before any skill is asked
        claims = _claims(svc, t)
        if claims and route(row[0], core) is not None:
            overrides[row[0]] = claims[0]
    assert overrides == INTENDED_OVERRIDES
    for phrase, skill in INTENDED_OVERRIDES.items():  # and the skill's answer is the one J gets
        assert _said(route(phrase, ctx)) == _expected(next(r for r in BOOK if r[0] == phrase)), phrase


def test_control_words_beat_every_skill(svc):
    """"stop", "yes", "never mind" mean the conversation, whatever a skill would make of them."""
    ctx = _world(svc)
    for said, kind in [("stop", "cancel"), ("yes", "confirm"), ("never mind", "cancel"), ("stop everything", "kill"),
                       ("forget it", "cancel"), ("forget about it", "cancel")]:
        assert route(said, ctx).kind == kind
