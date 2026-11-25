"""The day: building it, fixing it with set and add, what an entry is made of, default comments"""
import datetime as dt
import json
import re

import pytest

from timelog import cli, config, daily, jira, model, sources, store, sync, timeline
from helpers import DATE, at, ev, mine, prompt, ns, worklogs


def test_day_add_set_and_worklogs(days, monkeypatch, capsys):
    events = [ev("09:00", "ABC-1"), ev("09:00", None, "claude s2")]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    cli.cmd_add(ns(start="14:00", end="15:02", task="ADMIN-5", comment="sprint review"))
    with pytest.raises(SystemExit):
        worklogs()  # the unattributed entry first
    cli.cmd_set(ns(entry=["1=ABC-2"], comment=["ABC-1=invoice export"]))
    cli.cmd_day(ns())  # replays the rename of the unattributed entry
    capsys.readouterr()
    worklogs()
    rows = [r.split("\t") for r in capsys.readouterr().out.splitlines()]
    assert [(r[0], r[1], r[2], r[4]) for r in rows] == [
        ("ABC-1", "09:00-09:05", "5m", "invoice export"),
        ("ABC-2", "09:00-09:05", "5m", ""),
        ("ADMIN-5", "14:00-15:00", "60m", "sprint review"),
    ]
    assert re.fullmatch(r"2026-10-20T09:00:00\.000[+-]\d{4}", rows[0][3])
    assert len(list((days / "backups").iterdir())) == 3


def test_set_refuses_an_unknown_task_or_entry(days, monkeypatch):
    monkeypatch.setattr(sources, "day_events", lambda date, cards: [ev("09:00", "ABC-1")])
    cli.cmd_day(ns())
    for kw in (dict(task=["nope=x"]), dict(entry=["9=x"]), dict(drop=[9])):
        with pytest.raises(SystemExit):
            cli.cmd_set(ns(**kw))


def test_card_regex_reads_the_config_and_the_pushed_worklogs(days, monkeypatch):
    days.mkdir()
    # a pushed worklog's card was accepted by Jira; an entry's task can be a look-alike
    (days / f"{DATE}.json").write_text(json.dumps({"entries": [{"task": "UTF-8"}], "pushed": [{"card": "ABC-1"}]}))
    monkeypatch.setattr(config, "CARD_PREFIXES", ["DEF"])
    monkeypatch.setattr(config, "TOOLING_CARD", "TOOL-68")
    cards = model.card_regex()
    assert [model.card_of_text(x, cards) for x in ("ABC-2", "DEF-3", "TOOL-4", "UTF-8")] == [
        "ABC-2", "DEF-3", "TOOL-4", None]


def test_rebuild_replays_renames_and_drops(days, monkeypatch):
    events = [ev("09:00", "A", "claude s1"), ev("10:00", "B", "claude s2"),
              ev("11:00", None, "claude s3")]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    cli.cmd_set(ns(task=["A=A: PR review"], drop=[2]))
    events.append(ev("11:30", "A", "claude s1"))  # more work since: the rebuild sees it
    cli.cmd_day(ns())
    day = store.load_day(DATE)
    assert [(e.task, f"{e.start:%H:%M}") for e in day["entries"]] == [
        ("A: PR review", "09:00"), ("?", "11:00"), ("A: PR review", "11:30")]


def test_worklogs_refuse_a_retired_card(days, monkeypatch):
    monkeypatch.setattr(sources, "day_events", lambda date, cards: [ev("09:00", "TOOL-7")])
    monkeypatch.setattr(config, "RETIRED_CARDS", {"TOOL-7"})
    cli.cmd_day(ns())
    with pytest.raises(SystemExit, match="retired cards: TOOL-7"):
        sync.worklog_lines(ns(), store.load_day(DATE))


def test_rebuild_replays_corrections_in_order():
    entry = model.Entry("?", at("09:00"), at("09:10"), "auto", ("me focus",))

    def override(task, to):
        return {"task": task, "start": at("09:00").isoformat(), "end": at("09:10").isoformat(), "to": to}
    # set --entry 1=ABC-5, then set --drop 1
    assert timeline.replay([entry], {}, [override("?", "ABC-5"), override("ABC-5", "-")]) == []
    # set --entry 1=X, then set --task X=Y
    assert [e.task for e in timeline.replay([entry], {"X": "Y"}, [override("?", "X")])] == ["Y"]


def test_agent_time_follows_its_entry_through_corrections(days, monkeypatch):
    turn = model.Event(at("11:00"), None, False, "claude s1", "aw-agent", "activitywatch agents", "", until=at("11:20"))
    monkeypatch.setattr(sources, "day_events", lambda date, cards: [turn])
    cli.cmd_day(ns())
    cli.cmd_set(ns(entry=["1=ABC-5"]))
    cli.cmd_day(ns())
    assert timeline.agent_minutes(store.load_day(DATE)["entries"]) == {"ABC-5": 20}


def test_show_n_tells_what_an_entry_is_made_of(days, monkeypatch, capsys):
    confluence = mine("09:00", None, text="Release notes - Confluence", minutes=9)
    events = [confluence._replace(keys=("Confluence - Release notes",)),
              mine("09:09", None, text="Gmail", minutes=3)._replace(keys=("Gmail",)),
              prompt("10:00", "TOOL-1", "claude s9", text="fix the export"), ev("10:00", "ABC-1", minutes=5)]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    capsys.readouterr()
    cli.cmd_show(ns(entry=1))
    out = capsys.readouterr().out
    assert "09:00-09:12 12m  ?" in out and "9m  me focus" in out and "Confluence - Release notes" in out
    assert "3m  me focus  Gmail" in out


def test_show_leaves_out_what_an_entry_of_another_card_counts(days, monkeypatch, capsys):
    # the agent reviews B while you read A's page: your page is A's, not the review's
    events = [ev("09:00", "B", "claude s2", minutes=20), mine("09:00", "A", "browser", text="A page", minutes=10),
              mine("09:20", "B", "browser", text="B page", minutes=10)]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    capsys.readouterr()
    cli.cmd_show(ns(entry=3))  # yours, apart from the agent's entry 2
    out = capsys.readouterr().out
    assert out.startswith("09:20-09:30 10m  B") and "B page" in out and "A page" not in out
    # moved by hand to another card, the entry still shows what it's made of
    cli.cmd_set(ns(entry=["3=C"]))
    capsys.readouterr()
    cli.cmd_show(ns(entry=3))
    assert "B page" in capsys.readouterr().out


def test_a_task_without_comment_gets_its_summary_and_prs_until_you_write_one(days, monkeypatch, capsys):
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    monkeypatch.setattr(model, "pr_info", lambda repo, n: {"title": "t"})
    events = [prompt("09:00", "ABC-1", text="fix https://github.com/acme/shop/pull/5"), ev("09:00", "ABC-1"),
              prompt("10:00", "ABC-2", "claude s2", text="review https://github.com/acme/erp/pull/9")._replace(review=True),
              ev("10:00", "ABC-2", "claude s2", review=True)]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    jira.save(DATE, {"known": {"ABC-1": {"summary": "Invoice export"}, "ABC-2": {}}, "candidates": {}})
    cli.cmd_day(ns())
    day = store.load_day(DATE)
    assert day["comments"] == {}
    assert day["defaults"] == {"ABC-1": "Invoice export, shop#5", "ABC-2: PR review": "PR review of erp#9"}
    cli.cmd_set(ns(comment=["ABC-1=export to the bank"]))
    events.append(prompt("11:00", "ABC-2", "claude s2", text="also https://github.com/acme/erp/pull/10")._replace(review=True))
    cli.cmd_day(ns())  # the defaults follow the day, yours stay
    capsys.readouterr()
    worklogs()
    assert [r.split("\t")[4] for r in capsys.readouterr().out.splitlines()] == [
        "export to the bank", "PR review of erp#9, erp#10"]


def test_a_card_you_reviewed_a_pr_of_on_github_says_so(days, monkeypatch, capsys):
    # the session was asked to review the Jira card: its prompts name only a PR mentioned in passing
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    prs = {"acme/erp#287": {"title": "ABC-1: hide attributes", "author": {"login": "other"}},
           "acme/argocd#9336": {"title": "chore: add a key", "author": {"login": "other"}}}
    monkeypatch.setattr(model, "pr_info", lambda repo, n: prs.get(f"{repo}#{n}", {"title": "t"}))
    monkeypatch.setattr(sources, "reviews", lambda: [
        (at("09:20"), "acme/erp#287"), (at("10:10"), "acme/argocd#9336"),
        (at("09:20") - dt.timedelta(days=1), "acme/old#1")])  # another day's
    events = [prompt("09:00", "ABC-1", text="review https://acme.atlassian.net/browse/ABC-1"), ev("09:00", "ABC-1", minutes=30),
              prompt("09:25", "ABC-1", text="could we reuse https://github.com/OCA/pos/pull/971 ?"),
              prompt("10:00", "ABC-2", "claude s2", text="review https://acme.atlassian.net/browse/ABC-2"),
              ev("10:00", "ABC-2", "claude s2", minutes=20)]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    jira.save(DATE, {"known": {"ABC-1": {"summary": "Hide attributes"}, "ABC-2": {"summary": "WMS key"}},
                     "candidates": {}})
    cli.cmd_day(ns())
    # 9336 names no card: it goes to the card of the agent working when the review was posted
    assert store.load_day(DATE)["defaults"] == {"ABC-1": "PR review of erp#287", "ABC-2": "PR review of argocd#9336"}


def test_the_tooling_card_says_what_was_done_not_its_title(days, monkeypatch):
    # its sessions' titles, the longest first, then your windows of 3 minutes or more; a name
    # already said is left out, also when its window clipped it
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    monkeypatch.setattr(config, "TRACKER", {**config.TRACKER, "tooling_card": "TOOL-68"})
    def session(hhmm, stream, title, minutes):
        return [prompt(hhmm, "TOOL-68", stream, text="go")._replace(keys=(title,)),
                ev(hhmm, "TOOL-68", stream, minutes=minutes)._replace(keys=(title,))]
    events = (session("09:00", "claude s1", "Timelog review editor loop and lpass login", 20)
              + session("09:30", "claude s2", "Including odoo skills in c2c-ai-skills", 5)
              + session("09:40", "claude s3", "Skills PR", 2)
              + [mine("09:50", "TOOL-68", text="timelog", minutes=6)._replace(keys=("timelog",)),
                 mine("09:56", "TOOL-68", text="Gemini", minutes=2)._replace(keys=("Gemini",)),
                 mine("09:58", "TOOL-68", text="Skills …", minutes=4)._replace(keys=("Skills …",))])
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    jira.save(DATE, {"known": {"TOOL-68": {"summary": "2025 TRAIN - Self Training"}}, "candidates": {}})
    cli.cmd_day(ns())
    assert store.load_day(DATE)["defaults"] == {
        "TOOL-68": "Timelog review editor loop and lpass login, Including odoo skills in c2c-ai-skills, Skills PR"}


def test_the_admin_card_says_timesheeting_for_tempo_however_short(days, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    monkeypatch.setattr(config, "TRACKER", {**config.TRACKER, "admin_card": "ADMIN-5"})
    events = [mine("09:24", "ADMIN-5", text="Tempo", minutes=1)._replace(keys=("Tempo",)),
              mine("15:36", "ADMIN-5", text="Tempo", minutes=1)._replace(keys=("Tempo",))]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    jira.save(DATE, {"known": {"ADMIN-5": {"summary": "admin_business_fr_2024_2025"}}, "candidates": {}})
    cli.cmd_day(ns())
    assert store.load_day(DATE)["defaults"] == {"ADMIN-5": "timesheeting"}


def test_a_card_an_agent_was_asked_to_review_says_so_without_a_pr(days, monkeypatch):
    # no review on GitHub: the session's first prompt tells; a later "review" in a session doesn't
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    events = [prompt("09:00", "ABC-1", text="Review https://acme.atlassian.net/browse/ABC-1"), ev("09:00", "ABC-1"),
              prompt("10:00", "ABC-2", "claude s2", text="fix ABC-2"), ev("10:00", "ABC-2", "claude s2"),
              prompt("10:05", "ABC-2", "claude s2", text="review the diff"), ev("10:05", "ABC-2", "claude s2")]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    jira.save(DATE, {"known": {"ABC-1": {"summary": "Payment reminder"}, "ABC-2": {"summary": "Export"}},
                     "candidates": {}})
    cli.cmd_day(ns())
    assert store.load_day(DATE)["defaults"] == {"ABC-1": "Review: Payment reminder", "ABC-2": "Export"}


def test_summary_rebuilds_the_day_up_to_now(days, monkeypatch, capsys):
    events = [ev("09:00", "ABC-1", minutes=30)._replace(tokens=(3000, 4000, 9000, 99999))]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    events.append(ev("11:00", "ABC-1", minutes=30))  # worked since the day was saved
    capsys.readouterr()
    cli.cmd_summary(ns())
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["time", "agent", "you", "rounded", "in", "out", "task"]
    assert "1h00  total logged" in out
    # the turn's tokens: input and cache writes in, output out; cache reads left out
    assert re.search(r"\n  1h00 .*  12k     4k  .*ABC-1\n", out)
    assert "   16k  agent tokens: 12k in (cache writes included), 4k out; cache reads left out" in out


def test_set_names_only_what_is_wrong(days, monkeypatch):
    monkeypatch.setattr(sources, "day_events", lambda date, cards: [ev("09:00", "ABC-1")])
    cli.cmd_day(ns())
    with pytest.raises(SystemExit, match=r"entry on .*: 9, x$"):
        cli.cmd_set(ns(entry=["1=ABC-2", "x=ABC-3"], drop=[9]))
    with pytest.raises(SystemExit, match="--comment TASK=TEXT, not ABC-1"):
        cli.cmd_set(ns(comment=["ABC-1"]))


def test_a_break_answered_to_the_minute_covers_its_gap_given_to_the_second(days, monkeypatch):
    # the review shows the gap 09:05:29-10:00:31 as 09:05-10:00 and keeps the break as shown
    events = [mine("09:00", "ABC-1")._replace(until=at("09:05") + dt.timedelta(seconds=29)),
              mine("10:00", "ABC-1")._replace(when=at("10:00") + dt.timedelta(seconds=31))]
    _day, gaps, _notes = daily.build_day(DATE, events)
    assert [(f"{g0:%H:%M}", f"{g1:%H:%M}") for g0, g1 in gaps] == [("09:05", "10:00")]
    store.save_day(DATE, {**_day, "breaks": [[at("09:05").isoformat(), at("10:00").isoformat()]]}, quiet=True)
    assert daily.build_day(DATE, events)[1] == []


def test_logged_over_the_wall_clock_tells_what_overlaps():
    entries = [model.Entry("ABC-1", at("09:00"), at("10:00"), "auto", ("claude s1",)),
               model.Entry("ABC-1", at("09:30"), at("10:00"), "auto", ("me focus",)),  # you, with its agent
               model.Entry("DEF-2", at("09:50"), at("10:10"), "auto", ("claude s2",))]  # in parallel
    assert daily.logged_rows(entries) == [("logged", "1h50", "over 1h10 of wall clock"),
                                          ("same card", "0h30", "in parallel lanes"),
                                          ("other cards", "0h10", "at the same time")]
    assert daily.logged_rows(entries[:1]) == [("logged", "1h00", "over 1h00 of wall clock")]


def test_a_card_without_pr_gets_the_comment_you_wrote_last_time(days, monkeypatch):
    for date, comments in (("2026-10-01", {"ABC-1": "older"}), ("2026-10-15", {"ABC-1": "implementation"}),
                           ("2026-10-16", {"ABC-2": "other card"}), ("2026-10-21", {"ABC-1": "after"})):
        store.save_day(date, {"entries": [], "comments": comments}, quiet=True)
    cards = model.card_regex()
    assert daily.earlier_comments(DATE, cards) == {"ABC-1": "implementation", "ABC-2": "other card"}
    monkeypatch.setattr(daily, "EARLIER_DAYS", 4)
    assert daily.earlier_comments(DATE, cards) == {"ABC-2": "other card"}


def test_the_comment_you_wrote_last_time_isnt_followed_by_the_prs_it_names(days, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    monkeypatch.setattr(model, "pr_info", lambda repo, n: {"title": "t"})
    store.save_day("2026-10-15", {"entries": [], "comments": {"ABC-1": "implementing shop#5"}}, quiet=True)
    events = [prompt("09:00", "ABC-1", text="fix https://github.com/acme/shop/pull/5"),
              prompt("09:01", "ABC-1", text="and https://github.com/acme/shop/pull/6"), ev("09:00", "ABC-1")]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    assert store.load_day(DATE)["defaults"] == {"ABC-1": "implementing shop#5, shop#6"}


def test_summary_rounds_as_the_push_does_one_worklog_per_card(days, monkeypatch, capsys):
    monkeypatch.setattr(sources, "day_events", lambda date, cards: [ev("09:00", "ABC-1", minutes=7)])
    cli.cmd_day(ns())
    cli.cmd_add(ns(start="10:00", end="10:07", task="ABC-1: meeting", comment=None))
    capsys.readouterr()
    cli.cmd_summary(ns())
    # 14 min on ABC-1 make one worklog of 15, not two of 5
    assert "0h14  total logged, 0h15 once each worklog is rounded" in capsys.readouterr().out


def pushed_day(monkeypatch, events):
    """the day built from `events`, then pushed"""
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    day = store.load_day(DATE)
    day["pushed"] = [{"id": "1", "card": "ABC-1", "started": "", "minutes": 30, "comment": ""}]
    store.save_day(DATE, day, quiet=True)


def test_a_pushed_day_keeps_its_entries_when_a_later_timelog_would_count_it_differently(days, monkeypatch, capsys):
    events = [mine("09:00", "ABC-1", minutes=30)]
    pushed_day(monkeypatch, events)
    events[0] = events[0]._replace(card="ABC-2")  # what a later timelog would make of the same window
    for build in (cli.cmd_day, cli.cmd_summary):
        build(ns())
        assert "ABC-2" not in capsys.readouterr().out
    assert [e.task for e in store.load_day(DATE)["entries"]] == ["ABC-1"]
    cli.cmd_day(ns(rebuild=True))  # unless you ask for it
    assert [e.task for e in store.load_day(DATE)["entries"]] == ["ABC-2"]
    assert "rebuilt, against what was pushed: ABC-1 30m -> 0m, ABC-2 0m -> 30m" in capsys.readouterr().out


def test_a_pushed_day_gets_the_work_after_the_push_of_a_quarter_hour_or_more(days, monkeypatch):
    events = [mine("09:00", "ABC-1", minutes=30)]
    pushed_day(monkeypatch, events)
    events += [mine("11:00", "ABC-2", minutes=20), mine("14:00", "ABC-3", minutes=5)]
    cli.cmd_day(ns())
    assert [(e.task, f"{e.start:%H:%M}", f"{e.end:%H:%M}") for e in store.load_day(DATE)["entries"]] == [
        ("ABC-1", "09:00", "09:30"), ("ABC-2", "11:00", "11:20")]


def test_the_admin_card_doesnt_say_the_windows_its_time_was_spent_in(days, monkeypatch):
    # a window's title says where you were, not what you did: your last comment, else none to write
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    monkeypatch.setattr(config, "TRACKER", {**config.TRACKER, "admin_card": "ADMIN-5"})
    monkeypatch.setattr(sources, "day_events", lambda date, cards: [
        mine("11:47", "ADMIN-5", text="Claude Code", minutes=4)._replace(keys=("Claude Code",))])
    jira.save(DATE, {"known": {"ADMIN-5": {"summary": "admin_business_fr_2024_2025"}}, "candidates": {}})
    cli.cmd_day(ns())
    assert store.load_day(DATE)["defaults"] == {}  # nor its Jira summary, which never changes
    store.save_day("2026-10-15", {"entries": [], "comments": {"ADMIN-5": "mail and Slack"}}, quiet=True)
    cli.cmd_day(ns())
    assert store.load_day(DATE)["defaults"] == {"ADMIN-5": "mail and Slack"}
