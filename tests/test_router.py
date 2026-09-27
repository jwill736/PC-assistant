import pytest

from assistant.brain.router import RouterContext, clean, route

CTX = RouterContext(
    is_site=lambda t: t in {"gmail", "youtube", "twitch"} or "." in t,
    scene_match=lambda t: {"brb": "BRB", "just chatting": "Just Chatting", "gameplay": "Gameplay"}.get(t),
    profiles=["work", "stream"],
)


@pytest.mark.parametrize("text,kind,tool,args", [
    ("Good morning", "briefing", None, {}),
    ("give me the rundown", "briefing", None, {}),
    ("recap my day", "recap", None, {}),
    ("how did my day go?", "recap", None, {}),
    ("yes", "confirm", None, {}),
    ("Go ahead.", "confirm", None, {}),
    ("never mind", "cancel", None, {}),
    ("what time is it", "time", None, {}),
    ("what should I work on next", "next", None, {}),
    ("where am I?", "tool", "where_am_i", {}),
    ("open discord", "tool", "open_app", {"name": "discord"}),
    ("could you launch spotify please", "tool", "open_app", {"name": "spotify"}),
    ("open gmail", "tool", "open_urls", {"targets": ["gmail"]}),
    ("go to github.com", "tool", "open_urls", {"targets": ["github.com"]}),
    ("open a new tab with youtube", "tool", "open_urls", {"targets": ["youtube"]}),
    ("switch to BRB", "tool", "obs_switch_scene", {"scene": "BRB"}),
    ("change the scene to just chatting", "tool", "obs_switch_scene", {"scene": "Just Chatting"}),
    ("switch to the starting soon scene", "tool", "obs_switch_scene", {"scene": "starting soon"}),
    ("switch to chrome", "tool", "focus_window", {"query": "chrome"}),
    ("go to youtube", "tool", "open_urls", {"targets": ["youtube"]}),
    ("start stream mode", "tool", "run_routine", {"profile": "stream"}),
    ("let's work", "tool", "run_routine", {"profile": "work"}),
    ("go live", "tool", "obs_control", {"action": "start_stream"}),
    ("end the stream", "tool", "obs_control", {"action": "stop_stream"}),
    ("start recording", "tool", "obs_control", {"action": "start_recording"}),
    ("clip that", "tool", "obs_control", {"action": "save_replay"}),
    ("mute my mic", "tool", "obs_set_mute", {"source": "mic", "muted": True}),
    ("unmute desktop audio", "tool", "obs_set_mute", {"source": "desktop audio", "muted": False}),
    ("how's the stream", "tool", "obs_status", {}),
    ("volume up", "tool", "media_control", {"action": "volume_up", "times": 5}),
    ("pause the music", "tool", "media_control", {"action": "play_pause"}),
    ("next song", "tool", "media_control", {"action": "next"}),
    ("mute", "tool", "media_control", {"action": "mute"}),
    ("lock the pc", "tool", "power", {"action": "lock"}),
    ("optimize my pc", "tool", "optimize_pc", {"streaming": False}),
    ("close chrome", "tool", "close_app", {"name": "chrome"}),
    ("search youtube for lofi beats", "tool", "web_search", {"query": "lofi beats", "engine": "youtube"}),
    ("google best capture card 2026", "tool", "web_search", {"query": "best capture card 2026", "engine": "google"}),
    ("add task Email Acme the invoice", "tool", "add_task", {"title": "Email Acme the invoice"}),
    ("remind me to call mom", "tool", "add_task", {"title": "call mom"}),
    ("mark invoice follow up as done", "tool", "complete_task", {"title": "invoice follow up"}),
    ("remember that Acme wants two milestones", "tool", "remember", {"text": "Acme wants two milestones"}),
    ("what's my next meeting", "tool", "calendar", {"days": 2, "next_only": True}),
    ("what's on my calendar tomorrow", "tool", "calendar", {"days": 2, "when": "tomorrow"}),
    ("read me the news", "tool", "news", {}),
])
def test_routes(text, kind, tool, args):
    intent = route(text, CTX)
    assert intent is not None, text
    assert (intent.kind, intent.tool, intent.args) == (kind, tool, args)


@pytest.mark.parametrize("text", [
    "write me a haiku about OBS",
    "how many hours did I spend on Discord this week compared to last",
    "draft a reply to Sam about moving our 1:1",
])
def test_open_ended_goes_to_claude(text):
    assert route(text, CTX) is None


def test_clean_strips_filler_and_politeness():
    assert clean("Hey, can you open Discord for me please?") == "open discord"
