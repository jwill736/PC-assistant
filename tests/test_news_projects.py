import json
import subprocess

from assistant.integrations.news import parse_feed
from assistant.integrations.projects import ClaudeSessions, find_repos, parse_session, repo_status

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Big &amp; news</title><link>https://e.com/1</link><pubDate>Sun, 27 Sep 2026 12:00:00 GMT</pubDate>
<description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Second</title><link>https://e.com/2</link></item></channel></rss>"""


def test_parse_feed_strips_html():
    items = parse_feed(RSS, "Src", "tech")
    assert items[0]["title"] == "Big & news"
    assert items[0]["summary"] == "Hello world"
    assert items[0]["published"] and items[1]["published"] is None


def write_session(path, prompts, cwd="/work/clipforge"):
    lines = [{"type": "queue-operation"}]
    for p in prompts:
        lines.append({"type": "user", "cwd": cwd, "gitBranch": "main", "timestamp": "2026-09-27T12:00:00Z",
                      "message": {"role": "user", "content": p}})
        lines.append({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}})
    lines.append({"type": "user", "cwd": cwd, "message": {"content": [{"type": "tool_result", "content": "x"}]}})
    lines.append({"type": "user", "cwd": cwd, "message": {"content": "<command-name>/clear</command-name>"}})
    path.write_text("\n".join(json.dumps(l) for l in lines))


def test_claude_sessions_group_by_project(tmp_path):
    proj = tmp_path / "projects" / "-work-clipforge"
    proj.mkdir(parents=True)
    write_session(proj / "a.jsonl", ["add highlight detection", "now export vertical clips"])
    write_session(proj / "b.jsonl", ["fix the ffmpeg crash"])
    info = parse_session(proj / "a.jsonl")
    assert info["prompts"] == 2 and info["first_prompt"] == "add highlight detection"
    assert info["last_prompt"] == "now export vertical clips"
    grouped = ClaudeSessions(str(tmp_path)).by_project()
    assert len(grouped) == 1 and grouped[0]["project"] == "clipforge" and grouped[0]["sessions"] == 2


def test_repo_scan_and_status(tmp_path):
    repo = tmp_path / "code" / "myrepo"
    repo.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    run("init", "-q")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "first commit")
    (repo / "new.txt").write_text("x")
    assert find_repos([str(tmp_path)]) == [repo]
    st = repo_status(repo)
    assert st["last_commit"]["message"] == "first commit"
    assert st["dirty_files"] == 1 and st["commits_today"] == 1
