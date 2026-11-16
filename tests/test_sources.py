"""What ActivityWatch and GitHub give: sessions, turns, pieces of your lane, calls, PRs"""
import re
import subprocess

import pytest

from timelog import config, model, timeline
from timelog.sources import activitywatch
from helpers import at, ev, turn, aw_event


def test_a_session_keeps_the_first_card_its_prompts_give(aw, monkeypatch):
    aw["agent.turn"] = [
        # opened on a card, then a PR of another card is looked at: still ABC-1
        turn("08:00", 2, "sessaaaa", "work on ABC-1"),
        turn("09:00", 2, "sessaaaa", "check https://github.com/o/r/pull/7"),
        # opened on a PR: its card, kept
        turn("09:00", 2, "sessbbbb", "fix https://github.com/o/r/pull/7"),
        turn("09:05", 2, "sessbbbb", "compare with ABC-9"),
        # tooling work, then a card mentioned in passing: still tooling
        turn("09:10", 2, "sesscccc", "fill my timelog"),
        turn("09:15", 2, "sesscccc", "analyze the timestamps of ABC-5"),
        # the card comes later: the whole session takes it; the branch says nothing
        turn("09:30", 2, "sessdddd", "rebase it", git_branch="DEF-2113-x"),
        turn("09:35", 2, "sessdddd", "the card is ABC-3"),
        turn("09:40", 2, "sessdddd", "now ABC-4"),
        # a /loop isn't work you did
        turn("09:50", 2, "sesseeee", "<command-message>loop</command-message><command-name>/loop</command-name>",
             trigger="command"),
        turn("09:55", 2, "sesseeee", "check ABC-1 again"),
        turn("09:56", 2, "sessffff", "/loop 5m check ABC-2", trigger="command", command="/loop"),
        # a pasted terminal names cards: tooling all the same; a pasted URL is typed text
        turn("10:00", 2, "sessgggg", '"""\n<pasted_content id="a1">\n$ timelog\n  1) ABC-7  0h20'
                                     '\n</pasted_content id="a1">\n"""'),
        turn("10:05", 2, "sesshhhh", "review https://github.com/o/r/pull/7"),
        # cut by an older watcher before its closing tag
        turn("10:10", 2, "sessiiii", 'see <pasted_content id="b2">\nABC-8 failed'),
        turn("10:15", 2, "sessiiii", "fix ABC-9"),
    ]
    monkeypatch.setattr(config, "TOOLING_CARD", "TOOL-68")
    monkeypatch.setattr(config, "TOOLING", re.compile("timelog", re.I))
    monkeypatch.setattr(model, "pr_info", lambda repo, n: {"headRefName": "ABC-2-fix", "author": {"login": "me"}})
    out, info = activitywatch.combine(at("08:30"), at("23:59"), re.compile(r"(?:ABC|DEF|TOOL)-\d+"), "me")
    assert sorted((e.stream, f"{e.when:%H:%M}", e.card) for e in out if e.kind == "claude") == [
        ("claude sessaaaa", "09:00", "ABC-1"),
        ("claude sessbbbb", "09:00", "ABC-2"), ("claude sessbbbb", "09:05", "ABC-2"),
        ("claude sesscccc", "09:10", "TOOL-68"), ("claude sesscccc", "09:15", "TOOL-68"),
        ("claude sessdddd", "09:30", "ABC-3"), ("claude sessdddd", "09:35", "ABC-3"),
        ("claude sessdddd", "09:40", "ABC-3"),
        ("claude sessgggg", "10:00", "TOOL-68"), ("claude sesshhhh", "10:05", "ABC-2"),
        ("claude sessiiii", "10:10", "ABC-9"), ("claude sessiiii", "10:15", "ABC-9"),
    ]
    assert info["sesseeee"]["loop"] and info["sessffff"]["loop"]
    assert not any(e.stream in ("claude sesseeee", "claude sessffff") for e in out)


def test_activitywatch_turns_take_the_card_of_their_session(aw):
    aw["agent.turn"] = [turn("09:00", 40, "s1", "work on A"),
                        aw_event("09:05", 50, session_id="s1", subagent_id="sub12345abc", description="research",
                                 trigger="subagent"),
                        turn("10:00", 31, "s2", "work on B")]
    out, _info = activitywatch.combine(at("00:00"), at("23:59"), re.compile(r"\b[AB]\b"), "me")
    assert sorted((e.kind, f"{e.when:%H:%M}", e.stream, e.card) for e in out) == [
        ("aw-agent", "09:00", "claude s1", "A"), ("aw-agent", "09:05", "claude s1/sub12345", "A"),
        ("aw-agent", "10:00", "claude s2", "B"), ("claude", "09:00", "claude s1", "A"), ("claude", "10:00", "claude s2", "B")]
    entries, _breaks, _notes = timeline.build(out, [], at("18:00"))
    assert [(e.task, f"{e.start:%H:%M}", f"{e.end:%H:%M}") for e in entries] == [
        ("A", "09:00", "09:55"), ("B", "10:00", "10:31")]
    assert timeline.agent_minutes(entries) == {"A": 55, "B": 31}


def test_activitywatch_afk_is_presence(aw, monkeypatch):
    monkeypatch.setattr(model, "pr_info", lambda repo, n: {"headRefName": "B-2-fix", "author": {"login": "me"}})
    aw["afkstatus"] = [aw_event("09:05", 40, status="not-afk"), aw_event("09:45", 30, status="afk")]
    aw["web.tab.current"] = [aw_event("09:30", 12, url="https://github.com/o/r/pull/7", title="fix"),
                             aw_event("09:50", 5, url="https://example.com", title="news")]
    aw["currentwindow"] = [aw_event("09:05", 25, app="term", title="zsh"), aw_event("09:30", 15, app="firefox"),
                           aw_event("09:45", 30, app="firefox")]
    out, _info = activitywatch.combine(at("00:00"), at("23:59"), re.compile(r"[AB]-\d+"), "me", [ev("09:00", "A")])
    entries, breaks, _notes = timeline.build(out, [], at("18:00"))
    # the terminal names no card and you have none yet: asked; after the PR, the tab with nothing
    # on a card folds into it; away from the keyboard, nothing
    assert [(e.task, f"{e.start:%H:%M}", f"{e.end:%H:%M}") for e in entries] == [
        ("A", "09:00", "09:05"), ("?", "09:05", "09:30"), ("B-2", "09:30", "09:45")]
    assert breaks == []


def test_activitywatch_down_stops(monkeypatch):
    def down(path, params=None):
        raise OSError("connection refused")
    monkeypatch.setattr(activitywatch, "fetch", down)
    with pytest.raises(SystemExit, match="ActivityWatch doesn't answer"):
        activitywatch.combine(at("00:00"), at("23:59"), re.compile("A"), "me", [])


def test_activitywatch_tabs_count_only_while_the_browser_has_focus(aw, monkeypatch):
    monkeypatch.setattr(model, "pr_info", lambda repo, n: {"headRefName": "B-2-fix", "author": {"login": "me"}})
    aw["web.tab.current"] = [aw_event("15:15", 30, url="https://github.com/o/r/pull/7", title="fix")]
    aw["currentwindow"] = [aw_event("15:15", 3, app="firefox"), aw_event("15:18", 20, app="Code"),
                           aw_event("15:38", 7, app="firefox")]
    out, _info = activitywatch.combine(at("00:00"), at("23:59"), re.compile(r"B-\d+"), "me", [])
    assert [(f"{e.when:%H:%M}", f"{e.until:%H:%M}") for e in out if e.kind == "browser"] == [
        ("15:15", "15:18"), ("15:38", "15:45")]


def test_focus_on_a_session_window_is_your_time(aw):
    aw["agent.turn"] = [turn("09:00", 10, "s1", "invoice export for A", title="Invoice export to the bank")]
    aw["afkstatus"] = [aw_event("09:00", 30, status="not-afk")]
    aw["currentwindow"] = [aw_event("09:00", 12, app="Code", title="Invoice export … - shop_odoo - Visual Studio Code"),
                           aw_event("09:12", 18, app="slack", title="Slack")]
    out, _info = activitywatch.combine(at("00:00"), at("23:59"), re.compile(r"\bA\b"), "me")
    entries, breaks, _notes = timeline.build(out, [], at("18:00"))
    # the turn 09:00-09:10 counts for the agent; your focus on its window counts once it's over, till
    # 09:12; Slack after it goes with A too: reading its answer made A your current card
    assert sorted((e.task, f"{e.start:%H:%M}", f"{e.end:%H:%M}") for e in entries) == [
        ("A", "09:00", "09:10"), ("A", "09:10", "09:30")]
    assert timeline.agent_minutes(entries)["A"] == 10


@pytest.mark.parametrize("info, expected", [
    ({"headRefName": "ghi-310-stock_report", "title": "Stock report"}, "GHI-310"),
    ({"headRefName": "master-fix-CA-cert", "title": "[BUILD] fix CA",
      "body": "screenshot [JKL-100](https://example.atlassian.net/browse/JKL-100)"}, "JKL-100"),
    ({"headRefName": "feature-x", "title": "Feature", "body": "see https://jira.example.com/browse/PQR-1894"},
     "PQR-1894"),
    ({"headRefName": "main", "title": "UTF-8 fix", "body": "nothing"}, None),
])
def test_card_of_pr(info, expected):
    assert model.card_of_pr(info, re.compile(r"\bGHI-\d+\b")) == expected


def test_a_call_is_a_tab_playing_sound_that_an_always_active_rule_matches(aw):
    meet = "https://meet.google.com/meet/abc-defg-hij?authuser=0"
    aw["web.tab.current"] = [aw_event("10:00", 1, url=meet, title="Meet - Weekly sync", audible=False),
                             aw_event("10:01", 20, url=meet, title="Meet - Weekly sync", audible=True),
                             aw_event("10:21", 1, url=meet, title="Meet - Weekly sync", audible=True),
                             aw_event("10:25", 2, url=meet, title="Meet - Weekly sync", audible=True),  # after another tab
                             aw_event("11:00", 10, url="https://www.youtube.com/watch?v=x", title="talk", audible=True)]
    out, _info = activitywatch.combine(at("00:00"), at("23:59"), re.compile("A"), "me", [])
    assert [(e.keys, f"{e.when:%H:%M}", f"{e.until:%H:%M}") for e in out if e.kind == "call"] == [
        (("Meet - Weekly sync",), "10:01", "10:27")]
    entries, _breaks, _notes = timeline.build(out, [], at("18:00"))
    assert [(e.task, f"{e.start:%H:%M}", f"{e.end:%H:%M}") for e in entries] == [
        ("? Meet - Weekly sync", "10:01", "10:27")]


def test_editing_takes_the_card_of_the_branch_or_is_named_after_the_repository(aw):
    aw["afkstatus"] = [aw_event("09:00", 30, status="not-afk")]
    aw["currentwindow"] = [aw_event("09:00", 30, app="Code", title="a.py - shop_odoo - Visual Studio Code")]
    aw["app.editor.activity"] = [
        aw_event("09:00", 10, repository="acme/shop_odoo", branch="ABC-5-export", project="/x/shop_odoo"),
        aw_event("09:10", 10, project="/x/erp_odoo", file="b.py"),  # aw-watcher-vscode: no branch
        aw_event("09:20", 10, project="unknown")]
    out, _info = activitywatch.combine(at("00:00"), at("23:59"), re.compile(r"ABC-\d+"), "me", [])
    assert [(e.kind, e.card, e.text) for e in out if e.stream.startswith("me ")] == [
        ("editor", "ABC-5", "acme/shop_odoo|ABC-5-export"), ("editor", None, "erp_odoo|"),
        ("focus", None, "a.py - shop_odoo - Visual Studio Code")]


def test_a_folder_of_folder_cards_gives_its_card_over_the_prompts_and_the_branch(aw, monkeypatch):
    monkeypatch.setattr(config, "FOLDER_CARDS", [("/x/tools/timelog", "TOOL-1"), ("/x/tools", "TOOL-2"),
                                                 ("skills", "TOOL-3: PR review")])
    aw["agent.turn"] = [
        # a card typed in a prompt doesn't move a session out of its folder's card; a subfolder's wins
        turn("08:00", 2, "sessaaaa", "group the worklogs like ABC-1", cwd="/x/tools/timelog"),
        turn("08:10", 2, "sessbbbb", "fix ABC-2", cwd="/x/tools/other/sub"),
        # a bare name: any clone of that repository
        turn("08:20", 2, "sesscccc", "review ABC-3", cwd="/y/skills"),
        turn("08:30", 2, "sessdddd", "fix ABC-4", cwd="/x/toolsmith"),
    ]
    aw["afkstatus"] = [aw_event("09:00", 20, status="not-afk")]
    aw["currentwindow"] = [aw_event("09:00", 20, app="Code", title="a.py - timelog - Visual Studio Code")]
    aw["app.editor.activity"] = [
        aw_event("09:00", 10, repository="me/timelog", branch="ABC-5-x", file="/x/tools/timelog/a.py"),
        aw_event("09:10", 10, repository="skills", branch="ABC-6-x", project="unknown")]
    out, _info = activitywatch.combine(at("00:00"), at("23:59"), re.compile(r"(?:ABC|TOOL)-\d+"), "me", [])
    assert sorted((e.stream, e.card, e.review) for e in out if e.kind == "claude") == [
        ("claude sessaaaa", "TOOL-1", False), ("claude sessbbbb", "TOOL-2", False),
        ("claude sesscccc", "TOOL-3", True), ("claude sessdddd", "ABC-4", False)]
    assert [(e.card, e.review) for e in out if e.kind == "editor"] == [("TOOL-1", False), ("TOOL-3", True)]


def test_in_a_browser_the_tab_comes_before_the_title_and_tells_a_review(aw, monkeypatch):
    monkeypatch.setattr(model, "pr_info", lambda repo, n: {"headRefName": "x", "author": {"login": "alice"}})
    title = "DEF-2152: Install payment_provider by alice · Pull Request #638"
    aw["afkstatus"] = [aw_event("11:00", 10, status="not-afk")]
    aw["currentwindow"] = [aw_event("11:00", 5, app="firefox", title=f"{title} — Mozilla Firefox")]
    aw["web.tab.current"] = [aw_event("11:00", 5, url="https://github.com/c/erp_odoo/pull/638", title=title)]
    out, _info = activitywatch.combine(at("00:00"), at("23:59"), re.compile(r"DEF-\d+"), "me", [])
    assert [(e.kind, e.card, e.review) for e in out if e.stream.startswith("me ")] == [("browser", "DEF-2152", True)]


def test_without_gh_or_its_login_nothing_breaks_and_nothing_is_a_review(monkeypatch):
    def missing(*args, **kw):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(subprocess, "run", missing)
    assert model.gh("api", "user") is None
    assert not model.is_review({"author": {"login": "someone"}}, "")
    assert model.is_review({"author": {"login": "someone"}}, "me")


def test_a_continued_turn_is_agent_work_not_a_new_prompt(aw):
    aw["agent.turn"] = [turn("09:00", 5, "s1", "fix ABC-1"),
                        turn("09:06", 10, "s1", "fix ABC-1", continued=True)]
    out, _info = activitywatch.combine(at("00:00"), at("23:59"), re.compile(r"ABC-\d+"), "me")
    assert sorted((e.kind, f"{e.when:%H:%M}", e.card) for e in out) == [
        ("aw-agent", "09:00", "ABC-1"), ("aw-agent", "09:06", "ABC-1"), ("claude", "09:00", "ABC-1")]


def test_sessions_in_an_ignored_folder_are_not_counted(aw, monkeypatch):
    monkeypatch.setattr(config, "IGNORED_FOLDERS", ["/home/me/projects/farm"])
    aw["agent.turn"] = [turn("09:00", 30, "s1", "work on ABC-1", cwd="/home/me/projects/farm"),
                        turn("10:00", 30, "s2", "work on ABC-2", cwd="/home/me/projects/farm/odoo"),
                        turn("11:00", 30, "s3", "work on ABC-3", cwd="/home/me/projects/farming")]
    out, _info = activitywatch.combine(at("00:00"), at("23:59"), re.compile(r"ABC-\d+"), "me")
    assert {e.card for e in out} == {"ABC-3"}
