"""Ready-made jobs ("recipes") for skills/tasks_agent.py: data, not code.

A recipe is a list of steps over tools that already exist (a test checks every name). Each step is one of:

- ``{"id", "title", "tool", "args"}``: call a tool. Reads (tier 0) run straight away; anything that changes something
  (``ACTION_TOOLS`` in tasks_agent.py) waits for J's yes on one approval card at the end, unless he pre-approved it.
- ``"each": {"from": <step id>, "path": "owed", "limit": 3, "where": {"kind": "due"}}``: the call once per item of an
  earlier step's result; ``{item.name}`` in the args is that item's field.
- ``"if": "steps.leave.plan"``: only when that earlier result has something there.

Args are templates: ``{topic}`` is one of the recipe's inputs, ``{steps.prep.title}`` a field of an earlier step's
result, ``{x|fallback}`` a default. An argument that comes out empty is left out.

``brief`` tells the model what to write at the end (with every step's result as data); without a model the job still
finishes with a plain digest of the results, except a recipe with ``needs_model`` (it says so instead of starting).
``phrases`` are what J says to start it (anchored regexes over the cleaned utterance; a named group fills an input),
``match`` finds it inside a free-form request ("while I stream, triage my inbox").

J can add his own in data/recipes.yaml (same shape); they're checked like these.
"""

from __future__ import annotations

RECIPES: list[dict] = [
    {
        "id": "morning_prep", "title": "Morning prep", "icon": "sun",
        "blurb": "Calendar, replies you owe, weather and your top 3 for today, on one card.",
        "phrases": [r"(?:my |the )?morning prep", r"(?:do|run|start) (?:my |the )?morning prep",
                    r"prep (?:me )?for (?:the |this )?morning"],
        "match": r"\bmorning prep\b|\bprep (?:me )?for (?:the |this )?morning\b",
        "steps": [
            {"id": "cal", "title": "Today's calendar", "tool": "calendar", "args": {"days": 1}},
            {"id": "owed", "title": "Replies you owe", "tool": "replies_owed", "args": {}},
            {"id": "weather", "title": "Weather", "tool": "get_weather", "args": {"day": "today"}},
            {"id": "plan", "title": "Day plan", "tool": "plan_day", "args": {"day": "today"}},
            {"id": "bills", "title": "Bills due in 3 days", "tool": "upcoming_bills", "args": {"days": 3}},
        ],
        "brief": "J's morning card. The shape of the day (meetings, free stretches), who is waiting on a reply and "
                 "about what, the weather in one phrase, anything due, then his top 3 moves for today ranked by "
                 "leverage. Under 160 words.",
    },
    {
        "id": "tomorrow_prep", "title": "Prep for tomorrow", "icon": "moon",
        "blurb": "Tomorrow's meetings, a time-blocked plan, the weather and anything travel or bills.",
        "phrases": [r"(?:prep|prepare|get) me (?:ready )?for tomorrow", r"tomorrow prep", r"(?:do|run|start) (?:the )?tomorrow prep"],
        "match": r"\b(?:prep|prepare|ready)\b.*\btomorrow\b",
        "steps": [
            {"id": "cal", "title": "Tomorrow's calendar", "tool": "calendar", "args": {"days": 2}},
            {"id": "plan", "title": "Plan for tomorrow", "tool": "plan_day", "args": {"day": "tomorrow"}},
            {"id": "weather", "title": "Tomorrow's weather", "tool": "get_weather", "args": {"day": "tomorrow"}},
            {"id": "trips", "title": "Travel", "tool": "trips", "args": {}},
            {"id": "bills", "title": "Bills due", "tool": "upcoming_bills", "args": {"days": 2}},
            {"id": "owed", "title": "Replies to clear first", "tool": "replies_owed", "args": {}},
        ],
        "brief": "Prep J for tomorrow: first meeting and when to start, the plan's blocks, weather, travel or bills "
                 "if any, and the one or two replies worth clearing before then. Under 160 words.",
    },
    {
        "id": "meeting_pack", "title": "Meeting prep pack", "icon": "people",
        "blurb": "For your next meeting: who's coming, your last emails with each, related docs and your notes.",
        "inputs": [{"name": "meeting", "label": "Which meeting", "placeholder": "empty: the next one", "optional": True}],
        "phrases": [r"(?:build|make|do|run|get) (?:me )?(?:a |the |my )?meeting (?:prep )?pack(?: for (?:the |my )?(?P<meeting>.+?)(?: meeting)?)?",
                    r"meeting pack"],
        "match": r"\bmeeting (?:prep )?pack\b",
        "steps": [
            {"id": "prep", "title": "The meeting", "tool": "meeting_prep", "args": {"title": "{meeting}"}},
            {"id": "who", "title": "Who's coming", "tool": "who_is", "each": {"from": "prep", "path": "attendees", "limit": 4},
             "args": {"name": "{item.name}"}},
            {"id": "docs", "title": "Related documents", "tool": "search_library", "if": "steps.prep.title",
             "args": {"query": "{steps.prep.title}", "limit": 4}},
            {"id": "notes", "title": "Your notes", "tool": "recall", "if": "steps.prep.title",
             "args": {"query": "{steps.prep.title}"}},
        ],
        "brief": "A one-page prep pack: the meeting (time, where), each person with the last thing you discussed and "
                 "anything to ask them, the documents worth opening, and 3 talking points.",
    },
    {
        "id": "inbox_triage", "title": "Inbox triage + drafts", "icon": "mail",
        "blurb": "Who needs you, reply drafts saved in Gmail (never sent), promises turned into tasks.",
        "phrases": [r"triage (?:my |the )?(?:inbox|e-?mails?|mail)(?: and draft (?:the |some )?replies)?",
                    r"(?:do|run|start) (?:an |the |my )?inbox triage", r"inbox triage"],
        "match": r"\btriage\b.*\b(?:inbox|e-?mails?|mail)\b|\binbox\b.*\bdraft",
        "steps": [
            {"id": "mail", "title": "New mail from people", "tool": "check_email", "args": {}},
            {"id": "owed", "title": "Replies you owe", "tool": "replies_owed", "args": {}},
            {"id": "waiting", "title": "Who you're waiting on", "tool": "waiting_on", "args": {}},
            {"id": "promises", "title": "Promises you made", "tool": "promises", "args": {}},
            {"id": "drafts", "title": "Reply drafts", "tool": "draft_reply", "each": {"from": "owed", "path": "owed", "limit": 3},
             "args": {"name": "{item.name}", "about": "{item.subject}", "account": "{item.account}"}},
            {"id": "tasks", "title": "Promises as tasks", "tool": "add_task", "each": {"from": "promises", "path": "promises", "limit": 4},
             "args": {"title": "{item.task}", "due": "{item.due}", "profile": "work"}},
        ],
        "brief": "A needs-you list: each person, what they need, and the suggested next step, most important first; "
                 "then who J is waiting on. Say the drafts are saved in Gmail Drafts and never sent.",
    },
    {
        "id": "inbox_zero_sprint", "title": "Inbox zero sprint", "icon": "mail",
        "blurb": "What to archive in bulk, who to nudge, what you promised. Suggestions only: nothing is deleted.",
        "phrases": [r"inbox zero(?: sprint)?", r"(?:do|run|start) (?:an |the |my )?inbox zero(?: sprint)?"],
        "match": r"\binbox zero\b",
        "steps": [
            {"id": "mail", "title": "Mail from people", "tool": "check_email", "args": {}},
            {"id": "waiting", "title": "Who you're waiting on", "tool": "waiting_on", "args": {}},
            {"id": "promises", "title": "Promises", "tool": "promises", "args": {}},
            {"id": "promo", "title": "Promotions (2 weeks)", "tool": "search_email",
             "args": {"query": "category:promotions newer_than:14d", "limit": 20}},
            {"id": "stale", "title": "Unread over a week old", "tool": "search_email",
             "args": {"query": "is:unread older_than:7d", "limit": 20}},
        ],
        "brief": "An inbox-zero plan: groups J can archive in one go in Gmail (with the search to use and a count), "
                 "the few that need a reply, who to nudge, and promises to keep. Never suggest deleting.",
    },
    {
        "id": "shutdown", "title": "End-of-day shutdown", "icon": "moon",
        "blurb": "What got done, what moves to tomorrow, and tomorrow's plan. Rolling tasks over waits for your OK.",
        "phrases": [r"(?:run|do|start) (?:the |my )?(?:workday |work day |end of (?:the )?day )?shutdown(?: routine| recipe)?",
                    r"(?:end of (?:the )?day|workday) shutdown"],
        "match": r"\bshutdown routine\b|\bend of (?:the )?day shutdown\b",
        "steps": [
            {"id": "act", "title": "Where today went", "tool": "activity", "args": {"day": "today"}},
            {"id": "tasks", "title": "Open tasks", "tool": "list_tasks", "args": {}},
            {"id": "cal", "title": "Tomorrow's calendar", "tool": "calendar", "args": {"days": 2}},
            {"id": "plan", "title": "Tomorrow's plan", "tool": "plan_day", "args": {"day": "tomorrow"}},
            {"id": "roll", "title": "Roll today's tasks to tomorrow", "tool": "shutdown_day", "args": {}},
        ],
        "brief": "A shutdown note: what got done today (with focus time), what moves to tomorrow, and tomorrow's "
                 "first three blocks. Warm, short, then 'you're done for the day'.",
    },
    {
        "id": "week_wrap", "title": "Week wrap", "icon": "chart",
        "blurb": "Hours, wins, habits, streams and money for the week, plus next week's top 3.",
        "phrases": [r"(?:the |my )?week wrap(?:-?up)?", r"(?:do|run|start) (?:the |my |a )?week(?:ly)? wrap(?:-?up)?",
                    r"wrap up (?:the|my|this) week"],
        "match": r"\bweek(?:ly)? wrap\b|\bwrap up (?:the|my|this) week\b",
        "steps": [
            {"id": "review", "title": "Work week", "tool": "weekly_review", "args": {}},
            {"id": "life", "title": "Life this week", "tool": "life_review", "args": {}},
            {"id": "habits", "title": "Habits", "tool": "habit_status", "args": {}},
            {"id": "stream", "title": "Last stream", "tool": "stream_recap", "args": {"session": "last"}},
            {"id": "yt", "title": "YouTube", "tool": "youtube_stats", "args": {}},
        ],
        "brief": "J's week wrap: the numbers that matter, 3 wins, what slipped and why, one pattern worth changing, "
                 "and next week's top 3. Under 220 words.",
    },
    {
        "id": "stream_prep", "title": "Stream prep", "icon": "stream",
        "blurb": "Tonight's block, a title idea, pre-stream check, gear and PC health; title and Discord post wait for your OK.",
        "phrases": [r"stream prep", r"(?:do|run|start) (?:the |my )?stream prep",
                    r"(?:prep|prepare) (?:me )?for (?:the |my |tonight'?s )?stream"],
        "match": r"\bstream prep\b|\bprep(?:are)? (?:me )?for (?:the |my |tonight'?s )?stream\b",
        "steps": [
            {"id": "cal", "title": "Stream on the calendar", "tool": "calendar", "args": {"days": 1, "profile": "stream"}},
            {"id": "title", "title": "Title ideas", "tool": "suggest_title", "args": {}},
            {"id": "twitch", "title": "Channel now", "tool": "twitch_status", "args": {}},
            {"id": "check", "title": "Pre-stream check", "tool": "prestream_check", "args": {}},
            {"id": "obs", "title": "OBS scenes and gear", "tool": "obs_status", "args": {}},
            {"id": "pc", "title": "PC health", "tool": "pc_health", "args": {}},
            {"id": "settitle", "title": "Set the stream title", "tool": "twitch_set_channel", "if": "steps.title.titles",
             "args": {"title": "{steps.title.titles.0}"}},
            {"id": "discord", "title": "Go-live post for Discord", "tool": "discord_announce", "if": "steps.title.titles",
             "args": {"text": "Going live soon: {steps.title.titles.0}"}},
        ],
        "brief": "A stream-prep card: when you go live, the title pick (and two alternatives), what the pre-stream check "
                 "flagged and how to fix it, scene and gear notes, PC headroom. Short lines.",
    },
    {
        "id": "post_stream", "title": "Post-stream wrap", "icon": "stream",
        "blurb": "Highlights, clip list with timestamps, social post drafts and who to thank.",
        "phrases": [r"post-? ?stream wrap(?:-?up)?", r"(?:do|run|start) (?:the |my |a )?post-? ?stream wrap(?:-?up)?",
                    r"stream wrap-?up"],
        "match": r"\bpost-? ?stream\b",
        "steps": [
            {"id": "recap", "title": "Stream recap", "tool": "stream_recap", "args": {"session": "last"}},
            {"id": "moments", "title": "Clip-worthy moments", "tool": "stream_moments", "args": {"session": "last"}},
            {"id": "highlights", "title": "Highlights", "tool": "twitch_highlights", "args": {}},
            {"id": "events", "title": "Raids, subs and cheers", "tool": "twitch_events", "args": {"limit": 50}},
            {"id": "clip", "title": "Post the latest clip on Discord", "tool": "discord_post_clip", "args": {}},
        ],
        "brief": "A post-stream wrap: 3-line recap with numbers, a clip list (timestamp and why), two social post "
                 "drafts (one for Discord, one for X, under 250 characters), and the thank-you list (raiders, new subs, "
                 "gifters, top cheers) by name.",
    },
    {
        "id": "research_brief", "title": "Research brief", "icon": "search", "needs_model": True,
        "blurb": "Web search, reads the best pages and your own docs, writes a brief with sources.",
        "inputs": [{"name": "topic", "label": "Topic", "placeholder": "the best 4K capture cards under $300"}],
        "phrases": [r"(?:do |run |make |write |start )?(?:me )?(?:a )?research brief (?:on|about|for) (?P<topic>.+)",
                    r"research (?P<topic>(?!brief\b|mode\b).+)",
                    r"(?:look|dig) into (?P<topic>(?!that\b|it\b|this\b).+?) (?:while we talk|in the background|for me)"],
        "match": r"\bresearch\b",
        "steps": [
            {"id": "web", "title": "Search the web", "tool": "web_lookup", "args": {"query": "{topic}"}},
            {"id": "pages", "title": "Read the best pages", "tool": "read_web_page", "each": {"from": "web", "path": "results", "limit": 3},
             "args": {"url": "{item.url}"}},
            {"id": "docs", "title": "Your documents", "tool": "search_library", "args": {"query": "{topic}", "limit": 3}},
            {"id": "notes", "title": "What you said before", "tool": "recall", "args": {"query": "{topic}"}},
        ],
        "brief": "A research brief in Markdown: the answer first (a clear recommendation when it's a buying question), "
                 "key findings with numbers, a comparison table when comparing options, caveats, then Sources as a "
                 "numbered list of links you actually read. Cite claims as [1], [2].",
    },
    {
        "id": "find_time", "title": "Find a time", "icon": "calendar",
        "blurb": "Free slots this week that suit you, and a reply draft offering them (never sent).",
        "inputs": [{"name": "person", "label": "With whom", "placeholder": "Dana"}],
        "phrases": [r"find (?:a |some )?time (?:with|to meet(?: with)?) (?P<person>.+?)(?: and draft (?:an |the )?invite)?",
                    r"when can i meet (?:with )?(?P<person>.+)"],
        "match": r"\bfind (?:a |some )?time\b",
        "steps": [
            {"id": "who", "title": "Who they are", "tool": "who_is", "args": {"name": "{person}"}},
            {"id": "slots", "title": "Your free slots", "tool": "free_slots", "args": {"days": 5, "minutes": 30}},
            {"id": "invite", "title": "Draft the invite", "tool": "draft_reply", "if": "steps.slots.text",
             "args": {"name": "{person}", "gist": "Suggest meeting at one of these times: {steps.slots.text}. Ask which works."}},
        ],
        "brief": "Three good times to meet (why each suits J's day), and note the invite draft waiting in Gmail Drafts.",
    },
    {
        "id": "bills_check", "title": "Bills & subscriptions", "icon": "money",
        "blurb": "What's due, renewals, trials ending and what you don't use; reminders wait for your OK.",
        "phrases": [r"(?:do |run )?(?:a |the |my )?bills? (?:and subscriptions? )?check", r"check my bills and subscriptions",
                    r"money check"],
        "match": r"\bbills?\b.*\bsubscriptions?\b|\bbills? check\b",
        "steps": [
            {"id": "due", "title": "Due in 2 weeks", "tool": "upcoming_bills", "args": {"days": 14}},
            {"id": "subs", "title": "Subscriptions", "tool": "subscriptions", "args": {"ask": "list"}},
            {"id": "audit", "title": "What you don't use", "tool": "subscription_audit", "args": {}},
            {"id": "pay", "title": "Tasks for bills due", "tool": "add_task",
             "each": {"from": "due", "path": "upcoming", "limit": 4, "where": {"kind": "due"}},
             "args": {"title": "Pay {item.merchant} ({item.when})", "due": "{item.date}", "profile": "personal", "priority": 1}},
            {"id": "trials", "title": "Decide on trials", "tool": "add_task",
             "each": {"from": "due", "path": "trials", "limit": 3},
             "args": {"title": "Keep or cancel the {item.merchant} trial", "due": "{item.ends}", "profile": "personal"}},
        ],
        "brief": "A money check: what's due and when with amounts, renewals this month, trials ending, and the "
                 "subscriptions J pays for but hasn't used, with the monthly saving.",
    },
    {
        "id": "trip_prep", "title": "Trip prep", "icon": "plane",
        "blurb": "Your next flight: when to leave, check-in, weather there, packing list and the week around it.",
        "phrases": [r"trip prep", r"(?:do|run|start) (?:the |my )?trip prep", r"get me ready for (?:my|the) (?:trip|flight)"],
        "match": r"\btrip prep\b|\bready for (?:my|the) (?:trip|flight)\b",
        "steps": [
            {"id": "trips", "title": "Flights and hotels", "tool": "trips", "args": {}},
            {"id": "leave", "title": "When to leave", "tool": "leave_for_flight", "args": {}},
            {"id": "parcels", "title": "Packages arriving", "tool": "packages", "args": {}},
            {"id": "cal", "title": "Your week", "tool": "calendar", "args": {"days": 7}},
            {"id": "pack", "title": "Packing task", "tool": "add_task", "if": "steps.leave.plan",
             "args": {"title": "Pack for {steps.leave.plan.dest|the trip}", "profile": "personal", "priority": 1}},
        ],
        "brief": "Trip prep: the flight, leave-by time, check-in, weather there, a packing list, packages that would "
                 "arrive while away, and meetings to move.",
    },
    {
        "id": "content_ideas", "title": "Content ideas", "icon": "spark", "needs_model": True,
        "blurb": "Ideas for clips, videos and posts from this week's streams and chat.",
        "phrases": [r"(?:give me |find |come up with |get me )?(?:some )?content ideas(?: from (?:this week|my streams?))?",
                    r"(?:do|run|start) (?:the )?content ideas"],
        "match": r"\bcontent ideas\b",
        "steps": [
            {"id": "recap", "title": "Last stream", "tool": "stream_recap", "args": {"session": "last"}},
            {"id": "moments", "title": "Moments", "tool": "stream_moments", "args": {"session": "last"}},
            {"id": "highlights", "title": "Highlights", "tool": "twitch_highlights", "args": {}},
            {"id": "yt", "title": "Latest videos", "tool": "youtube_latest", "args": {"count": 5}},
            {"id": "news", "title": "Gaming news", "tool": "news", "args": {"topic": "gaming"}},
        ],
        "brief": "Eight content ideas from this week: YouTube titles, Shorts and clip compilations, community posts. "
                 "For each: the idea, the moment or data it comes from, and why it would land.",
    },
    {
        "id": "learning_plan", "title": "Learning plan", "icon": "book", "needs_model": True,
        "blurb": "A week-by-week plan for a topic, with the best free resources found on the web.",
        "inputs": [{"name": "topic", "label": "Topic", "placeholder": "Blender for stream overlays"}],
        "phrases": [r"(?:make|build|give|write) (?:me )?a learning plan (?:for|on|about) (?P<topic>.+)",
                    r"learning plan (?:for|on|about) (?P<topic>.+)"],
        "match": r"\blearning plan\b",
        "steps": [
            {"id": "web", "title": "Roadmaps", "tool": "web_lookup", "args": {"query": "{topic} learning roadmap"}},
            {"id": "courses", "title": "Courses", "tool": "web_lookup", "args": {"query": "best free {topic} course"}},
            {"id": "pages", "title": "Read the best pages", "tool": "read_web_page", "each": {"from": "web", "path": "results", "limit": 2},
             "args": {"url": "{item.url}"}},
            {"id": "docs", "title": "Your documents", "tool": "search_library", "args": {"query": "{topic}", "limit": 3}},
            {"id": "notes", "title": "What you said before", "tool": "recall", "args": {"query": "{topic}"}},
        ],
        "brief": "A 4-week learning plan in Markdown: a goal for each week, 3-5 hours of concrete practice per week "
                 "fitted around a day job and streaming, the resources to use (with links), and a small project to "
                 "finish. Sources at the end.",
    },
    {
        "id": "pc_tuneup", "title": "PC tune-up check", "icon": "gear",
        "blurb": "Temperatures, throttling, disk space and what's slowing the PC. Looks only, changes nothing.",
        "phrases": [r"(?:the |a )?pc tune-? ?up(?: check)?", r"(?:do|run|start) (?:a |the |my )?pc tune-? ?up(?: check)?"],
        "match": r"\btune-? ?up\b",
        "steps": [
            {"id": "health", "title": "Health", "tool": "pc_health", "args": {}},
            {"id": "disk", "title": "Disk", "tool": "disk_report", "args": {}},
            {"id": "opt", "title": "Optimizations", "tool": "optimize_pc", "args": {}},
            {"id": "now", "title": "Load now", "tool": "system_status", "args": {}},
        ],
        "brief": "A PC tune-up report: what's fine, what's not (temperatures, throttling, space), and the 3 changes "
                 "worth making, each with the exact place to click in Windows.",
    },
]
