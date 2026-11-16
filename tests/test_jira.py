"""Jira: the token, fetch, the push plan and sending"""
import datetime as dt
import json
import re
import types

import pytest

from timelog import cli, config, jira, jira_rest, model, push, reviewing, sources, store, sync
from helpers import DATE, at, ev, ns, worklogs, wline, pushed, FakeJira, jira_worklog


def test_jira_flags_unknown_cards_and_offers_candidates(days, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    cards = re.compile(r"ABC-\d+")
    assert jira.unknown({"ABC-1"}, DATE, cards) is None  # Jira not asked
    (tmp_path / f"jira-{DATE}.json").write_text(json.dumps({
        "known": {"ABC-1": {"summary": "Invoices", "status": "Done"}},
        "candidates": {"GHI-357": "Display a warning on reject"}}))
    assert jira.unknown({"ABC-1", "ABC-2: PR review", "?"}, DATE, cards) == ["ABC-2"]
    events = [ev("09:00", "ABC-1"), ev("09:00", None, "claude s2")]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    out = capsys.readouterr().out
    assert "GHI-357 Display a warning on reject" in out and "not in Jira" not in out
    cli.cmd_set(ns(entry=["1=ABC-2"]))
    with pytest.raises(SystemExit, match="not in Jira: ABC-2"):
        worklogs()


def test_push_plan():
    lines = [wline("A-1", "09:00", 30, "x"), wline("A-1", "11:00", 10, "y"), wline("B-2", "10:00", 15), wline("C-3", "12:00", 5)]
    done = [pushed("1", "A-1", "09:00", 30, "x"),   # unchanged
            pushed("2", "B-2", "10:00", 20),        # shorter now
            pushed("3", "A-1", "14:00", 5, "y"),    # entry moved: its id is reused
            pushed("4", "D-4", "15:00", 5),         # entry dropped since
            pushed("5", "E-5", "16:00", 5)]         # deleted in Jira by hand: forgotten
    jira_now = [{k: v for k, v in p.items()} for p in done[:4]] + [pushed("9", "A-1", "08:00", 60, "by hand")]
    actions = push.plan(lines, done, jira_now)
    assert [(a.action, a.card, f"{a.started:%H:%M}", a.worklog_id) for a in actions] == [
        ("foreign", "A-1", "08:00", "9"), ("keep", "A-1", "09:00", "1"), ("update", "B-2", "10:00", "2"),
        ("update", "A-1", "11:00", "3"), ("add", "C-3", "12:00", None), ("delete", "D-4", "15:00", "4")]


def test_push_plan_without_jira_and_record():
    assert [a.action for a in push.plan([wline("A-1", "09:00", 30)], [], None)] == ["add"]
    done = push.record([], [{"id": 7, "card": "A-1", "started": "2026-10-20T09:00:00.000+0200", "minutes": 30}])
    assert [a.action for a in push.plan([wline("A-1", "09:00", 30)], done, None)] == ["keep"]
    assert push.parse_started("2026-10-20T09:00:00.000+0200") == dt.datetime.fromisoformat("2026-10-20T09:00:00+02:00")


def test_a_card_makes_one_worklog_a_day(monkeypatch):
    # you on A, an agent on A meanwhile, B, then A again under another task: one worklog for A from
    # its first entry, its lanes counted once (20 + 7 min), its tasks' comments joined, rounded
    monkeypatch.setattr(config, "ROUND", 5)
    monkeypatch.setattr(config, "ROUND_MODE", "nearest")
    entries = [model.Entry("A-1", at("09:00"), at("09:10"), "auto", ("me focus",)),
               model.Entry("A-1", at("09:05"), at("09:20"), "auto", ("claude s1",)),
               model.Entry("B-2", at("09:20"), at("09:31"), "auto", ("me focus",)),
               model.Entry("A-1: PR review", at("09:40"), at("09:47"), "auto", ("claude s2",))]
    day = {"comments": {"A-1": "invoice export"}, "defaults": {"A-1: PR review": "PR review of erp#1"}}
    lines = sync.day_worklogs(day, entries, re.compile(r"[A-Z]+-\d+"))
    assert [(x.card, f"{x.started:%H:%M}", x.minutes, x.comment) for x in lines] == [
        ("A-1", "09:00", 25, "invoice export; PR review of erp#1"), ("B-2", "09:20", 10, "")]


def test_a_cards_comments_join_without_repeating_a_part():
    assert sync.join_comments(["timelog dry-run, timelog#1, aw-watcher-agents#1", "timelog#1, aw-watcher-agents#1",
                               "", "timelog#1 review", "Timelog dry-run"]) == (
        "timelog dry-run, timelog#1, aw-watcher-agents#1; timelog#1 review")


def test_push_rounds_each_worklog_to_the_nearest_step():
    lines = [wline("A-1", "09:00", m) for m in (1, 4, 6, 8, 12, 13, 114, 80)]
    assert [x.minutes for x in push.round_lines(lines, 5)] == [5, 5, 5, 10, 10, 15, 115, 80]
    assert [x.minutes for x in push.round_lines(lines, 5, "up")] == [5, 5, 10, 10, 15, 15, 115, 80]
    assert [x.minutes for x in push.round_lines(lines, 1)] == [1, 4, 6, 8, 12, 13, 114, 80]


def test_push_plan_doesnt_take_a_worklog_cut_off_by_jira_for_deleted():
    done = [pushed("1", "A-1", "09:00", 30)]
    assert [a.action for a in push.plan([wline("A-1", "09:00", 30)], done, [])] == ["add"]
    assert [a.action for a in push.plan([wline("A-1", "09:00", 30)], done, [], truncated=["A-1"])] == ["keep"]


def test_the_token_comes_from_the_environment_or_a_command(monkeypatch):
    monkeypatch.setattr(config, "TRACKER", {**config.TRACKER, "token_command": "echo from-command"})
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    assert jira_rest.token() == "from-command"
    monkeypatch.setenv("JIRA_API_TOKEN", "from-env")
    assert jira_rest.token() == "from-env"


def test_fetch_reads_every_worklog_of_yours_card_by_card():
    logs = [jira_worklog("1", "ABC-1", "09:00", 30), jira_worklog("2", "ABC-1", "10:00", 15),
            jira_worklog("3", "ABC-1", "11:00", 5, comment="third page"),
            jira_worklog("4", "ABC-1", "12:00", 5, author="x")]
    fake = FakeJira({"ABC-1": {"summary": "Invoices", "status": {"name": "Done"}}}, logs)
    answer = jira_rest.fetch(fake, DATE, ["ABC-1", "NOPE-9"], "status = review")
    assert answer["known"] == {"ABC-1": {"summary": "Invoices", "status": "Done"}}  # NOPE-9 doesn't exist
    assert answer["candidates"] == {"GHI-1": "to review"}
    assert [(w["id"], w["minutes"], w["comment"]) for w in answer["worklogs"]] == [
        ("1", 30, "x"), ("2", 15, "x"), ("3", 5, "third page")]  # another author's isn't yours


def test_push_send_adds_updates_deletes_and_records(days, monkeypatch):
    events = [ev("09:00", "ABC-1", minutes=30), ev("10:00", "ABC-2", minutes=10)]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    day = store.load_day(DATE)
    day["pushed"] = [pushed("7", "ABC-1", "09:00", 20), pushed("8", "ABC-3", "11:00", 5)]
    store.save_day(DATE, day)
    fake = FakeJira({}, [jira_worklog("7", "ABC-1", "09:00", 20), jira_worklog("8", "ABC-3", "11:00", 5)])
    monkeypatch.setattr(jira_rest, "Client", lambda: fake)
    monkeypatch.setattr(jira_rest, "available", lambda: True)
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    jira.save(DATE, {"known": {"ABC-1": {}, "ABC-2": {}}, "candidates": {}})  # what `fetch` wrote
    sync.cmd_push(ns(record=None, json=False, dry_run=True))
    assert not [c for c in fake.calls if c[0] in ("PUT", "DELETE") or c[:2] == ("POST", "/issue/ABC-2/worklog")]
    sync.cmd_push(ns(record=None, json=False, dry_run=False))
    sent = [(m, p, b and b["timeSpentSeconds"], q) for m, p, b, q in fake.calls if m in ("POST", "PUT", "DELETE")
            and p != "/search/jql"]
    # no adjustEstimate: Jira lowers the remaining estimate by the time logged, as Tempo does
    assert sent == [("PUT", "/issue/ABC-1/worklog/7", 1800, None), ("POST", "/issue/ABC-2/worklog", 600, None),
                    ("DELETE", "/issue/ABC-3/worklog/8", None, None)]
    post = next(b for m, p, b, q in fake.calls if m == "POST" and p == "/issue/ABC-2/worklog")
    assert re.fullmatch(r"2026-10-20T10:00:00\.000[+-]\d{4}", post["started"])
    assert [(p["id"], p["card"], p["minutes"]) for p in store.load_day(DATE)["pushed"]] == [
        ("7", "ABC-1", 30), ("901", "ABC-2", 10)]


def test_push_send_keeps_what_jira_accepted_before_failing(days, monkeypatch):
    class Failing(FakeJira):
        def call(self, method, path, body=None, params=None):
            if method == "POST" and self.next_id == 901:
                raise SystemExit("# Jira answered 429")
            return super().call(method, path, body, params)
    days.mkdir()
    day = {"entries": [], "comments": {}, "pushed": []}
    actions = [push.Action("add", "ABC-1", at("09:00"), 10, ""), push.Action("add", "ABC-2", at("10:00"), 10, "")]
    with pytest.raises(SystemExit, match="429"):
        sync.send(DATE, day, Failing({}, []), actions)
    assert [p["id"] for p in store.load_day(DATE)["pushed"]] == ["901"]


def test_push_after_send_or_record_plans_from_what_was_sent(days, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sources, "day_events", lambda date, cards: [ev("09:00", "ABC-1", minutes=30)])
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    cli.cmd_day(ns())
    jira.save(DATE, {"known": {"ABC-1": {}}, "candidates": {}, "worklogs": []})  # what `fetch` read
    monkeypatch.setattr(jira_rest, "Client", lambda: FakeJira({}, []))
    monkeypatch.setattr(jira_rest, "available", lambda: True)
    sync.cmd_push(ns(record=None, json=False, dry_run=False))
    capsys.readouterr()
    monkeypatch.setattr(jira_rest, "available", lambda: False)  # the connector's way from here
    sync.cmd_push(ns(record=None, json=False, dry_run=True))
    assert capsys.readouterr().out.split()[0] == "keep"
    # the connector's way: the session sends, then records
    cli.cmd_set(ns(entry=["1=ABC-1"], comment=["ABC-1=export"]))
    sent = tmp_path / "sent.json"
    sent.write_text(json.dumps([{"id": "901", "card": "ABC-1", "started": f"{at('09:00'):%Y-%m-%dT%H:%M:%S.000%z}",
                                 "minutes": 30, "comment": "export"}]))
    sync.cmd_push(ns(record=str(sent), json=False, dry_run=True))
    capsys.readouterr()
    sync.cmd_push(ns(record=None, json=False, dry_run=True))
    assert capsys.readouterr().out.split()[0] == "keep"


def test_the_token_command_runs_once(monkeypatch):
    runs = []
    monkeypatch.setattr(jira_rest.subprocess, "run", lambda cmd, **kw: runs.append(cmd) or types.SimpleNamespace(
        returncode=0, stdout="secret\n"))
    monkeypatch.setattr(config, "TRACKER", {**config.TRACKER, "site": "x.atlassian.net", "email": "a@b.c",
                                            "token_command": "pass show jira-once"})
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    assert jira_rest.available() and jira_rest.Client().auth and jira_rest.token() == "secret"
    assert runs == [["pass", "show", "jira-once"]]


def test_push_sends_like_git_push_and_n_only_shows(days, monkeypatch, capsys):
    monkeypatch.setattr(sources, "day_events", lambda date, cards: [ev("09:00", "ABC-1", minutes=30)])
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    cli.cmd_day(ns())
    jira.save(DATE, {"known": {"ABC-1": {}}, "candidates": {}, "worklogs": []})
    monkeypatch.setattr(jira_rest, "available", lambda: False)
    with pytest.raises(SystemExit, match="nothing sent"):
        sync.cmd_push(ns(record=None, json=False, dry_run=False))
    for argv in (["push", "-n"], ["push", "--dry-run"]):
        monkeypatch.setattr("sys.argv", ["timelog", *argv, DATE])
        capsys.readouterr()
        cli.main()
        assert capsys.readouterr().out.split()[0] == "add"  # the plan, shown only
    fake = FakeJira({}, [])
    monkeypatch.setattr(jira_rest, "available", lambda: True)
    monkeypatch.setattr(jira_rest, "Client", lambda: fake)
    monkeypatch.setattr("sys.argv", ["timelog", "push", DATE])
    cli.main()
    assert [p for m, p, b, q in fake.calls if m == "POST" and p != "/search/jql"] == ["/issue/ABC-1/worklog"]
    fake.logs.append(jira_worklog("901", "ABC-1", "09:00", 30))  # Jira holds it now
    capsys.readouterr()
    cli.main()  # pushed already: nothing to send
    assert "Jira already has these worklogs" in capsys.readouterr().out


def test_an_empty_plan_says_so(days, monkeypatch, capsys):
    monkeypatch.setattr(sources, "day_events", lambda date, cards: [])
    monkeypatch.setattr(jira_rest, "available", lambda: False)
    cli.cmd_day(ns())
    capsys.readouterr()
    sync.cmd_push(ns(record=None, json=False, dry_run=True))
    assert capsys.readouterr().out == "# nothing to push: the day has no worklog\n"


def test_a_token_command_that_fails_says_so(days, monkeypatch, capsys):
    monkeypatch.setitem(config.TRACKER, "site", "acme.atlassian.net")
    monkeypatch.setitem(config.TRACKER, "email", "me@example.com")
    monkeypatch.setitem(config.TRACKER, "token_command", "false")
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    jira_rest.command_output.cache_clear()
    monkeypatch.setattr(jira_rest, "FAILED", {})
    assert jira_rest.unavailable() == "the Jira token didn't come: `false` failed: exit 1"
    # the review sends nothing and doesn't offer the lines for Tempo: Jira is there, try again later
    monkeypatch.setattr("builtins.input", lambda question: pytest.fail(question))
    reviewing.finish_review(ns(), {"entries": []})
    assert "run `timelog` again once it works" in capsys.readouterr().out
    jira_rest.command_output.cache_clear()


def test_the_review_reads_jira_once_and_sends_the_plan_shown(days, monkeypatch, capsys):
    monkeypatch.setattr(sources, "day_events", lambda date, cards: [ev("09:00", "ABC-1", minutes=30)])
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    cli.cmd_day(ns())
    jira.save(DATE, {"known": {"ABC-1": {}}, "candidates": {}, "worklogs": []})
    clients = []
    monkeypatch.setattr(jira_rest, "available", lambda: True)
    monkeypatch.setattr(jira_rest, "Client", lambda: clients.append(FakeJira({}, [])) or clients[-1])
    monkeypatch.setattr("builtins.input", lambda question: "y")
    reviewing.finish_review(ns(), store.load_day(DATE))
    assert len(clients) == 1
    calls = [(m, p) for m, p, _b, _q in clients[0].calls]
    assert calls.count(("GET", "/myself")) == 1 and calls[-1] == ("POST", "/issue/ABC-1/worklog")
    assert "add      ABC-1" in capsys.readouterr().out

