"""The command line: arguments, dates, the config, what is printed"""
import datetime as dt
import io
import os
import sys

import pytest

from timelog import cli, config, detect, reviewing, style
from helpers import MONDAY, PARSE_DATE


def test_config_merges_the_defaults_and_reports_problems():
    user = {"timing": {"break_gap": 15}, "tracker": {"site": "x.atlassian.net", "nope": 1},
            "tooling_card": "TOOL-68"}
    settings = config.merged(user)
    assert settings["timing"]["break_gap"] == 15 and settings["timing"]["join_gap"] == 30
    assert settings["tracker"]["site"] == "x.atlassian.net" and settings["tracker"]["retired_cards"] == []
    assert config.problems(user) == ["unknown key: tooling_card", "unknown key: tracker.nope"]
    assert config.problems({"tracker": {"kind": "linear"}}) == ["tracker.kind: linear is not supported, only jira"]
    assert config.problems({"your_time_during_agent_turns": False}) == [
        "your_time_during_agent_turns: False is not one of other_cards, calls"]
    assert config.problems({"timing": {"round_mode": "down"}}) == ["timing.round_mode: down is not one of nearest, up"]
    assert config.problems({}) == []


def test_the_setup_check_names_what_a_blank_config_lacks(monkeypatch):
    monkeypatch.setattr(detect, "missing_watchers", lambda: [])
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.setattr(config, "TRACKER", config.DEFAULTS["tracker"])
    found = " | ".join(detect.warnings(config.merged({})))
    for part in ("tracker.site", "tracker.email", "an API token", "tracker.tooling_card nor tracker.admin_card",
                 "card_prefixes is empty"):
        assert part in found


def test_a_config_that_isnt_json_stops_instead_of_running_on_the_defaults(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"card_prefixes": ["ABC"],}')
    user, error = config.load_config(str(path))
    assert user == {} and "isn't valid JSON" in error
    assert config.load_config(os.devnull) == ({}, None)  # empty or missing: the defaults
    assert config.load_config(str(tmp_path / "none.json")) == ({}, None)
    monkeypatch.setattr(config, "ERROR", error)
    monkeypatch.setattr(config, "CONFIG", str(path))
    for argv in (["day"], ["config", "--write"]):
        monkeypatch.setattr("sys.argv", ["timelog", *argv])
        with pytest.raises(SystemExit, match="isn't valid JSON"):
            cli.main()
    assert path.read_text() == '{"card_prefixes": ["ABC"],}'


def test_the_date_can_be_given_with_an_equal_sign_and_is_checked(days, monkeypatch, capsys):
    monkeypatch.setattr(reviewing, "cmd_review", lambda a: print(f"review {a.date}"))
    monkeypatch.setattr("sys.argv", ["timelog", "--date=2026-10-19"])
    cli.main()
    assert capsys.readouterr().out == "review 2026-10-19\n"
    monkeypatch.setattr("sys.argv", ["timelog", "show", "--date", "someday"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "'someday' isn't a date" in capsys.readouterr().err


@pytest.mark.parametrize("value, order, expected", [
    ("2026-11-14", "dmy", "2026-11-14"),  # ISO, whatever the order
    ("20261114", "mdy", "2026-11-14"),
    ("2026/11/14", "dmy", "2026-11-14"),
    ("14-11-2026", "dmy", "2026-11-14"),
    ("14/11/2026", "dmy", "2026-11-14"),
    ("14.11.2026", "dmy", "2026-11-14"),
    ("14.11.26", "dmy", "2026-11-14"),
    ("14. 11. 26", "dmy", "2026-11-14"),  # fr_CH
    ("1.2.26", "dmy", "2026-02-01"),
    ("14.11", "dmy", "2026-11-14"),
    ("14.11.", "dmy", "2026-11-14"),
    ("30.12", "dmy", "2025-12-30"),  # the last one, not a day to come
    ("11/14/26", "mdy", "2026-11-14"),
    ("11/14", "mdy", "2026-11-14"),
    ("26-11-14", "ymd", "2026-11-14"),
    ("11-14", "ymd", "2026-11-14"),
    ("today", "dmy", "2026-11-16"),
    ("Yesterday", "dmy", "2026-11-15"),
    ("-2", "dmy", "2026-11-14"),
    ("friday", "dmy", "2026-11-13"),
    ("sat", "dmy", "2026-11-14"),
    ("monday", "dmy", "2026-11-16"),  # today is the last Monday
])
def test_parse_date(value, order, expected):
    assert cli.parse_date(value, MONDAY, order) == expected


@pytest.mark.parametrize("value", ["11/14/2026", "14.11.026", "32.1", "14", "14.11.26.1", "fr", "tomorrow", ""])
def test_parse_date_refuses(value):
    with pytest.raises(ValueError):
        cli.parse_date(value, MONDAY, "dmy")


@pytest.mark.parametrize("order, forms", [
    ("dmy", "14.11.2026, 14/11/26, 14-11, 2026-11-14"),
    ("mdy", "11.14.2026, 11/14/26, 11-14, 2026-11-14"),
])
def test_the_forms_shown_follow_the_order(order, forms):
    assert cli.date_forms(order).startswith(forms)


@pytest.mark.parametrize("d_fmt, order", [
    ("%d/%m/%y", "dmy"), ("%m/%d/%Y", "mdy"), ("%d. %m. %y", "dmy"), ("%Y-%m-%d", "ymd"), ("%x", "ymd"),
])
def test_the_order_of_a_locale(d_fmt, order):
    assert config.order_of(d_fmt) == order


def test_a_wrong_date_order_is_reported():
    assert config.problems({"date_order": "dd/mm/yyyy"}) == ["date_order: dd/mm/yyyy is not one of dmy, mdy, ymd"]


@pytest.fixture
def run(monkeypatch):
    """main() on argv, with the commands replaced: the arguments each one would get"""
    called = []
    for name in ("review", "day", "show", "config"):
        monkeypatch.setattr(reviewing if name == "review" else cli, f"cmd_{name}", lambda a: called.append(a))
    monkeypatch.setattr(config, "DATE_ORDER", "dmy")
    monkeypatch.setattr(cli, "parse_date", lambda value: PARSE_DATE(value, MONDAY))

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["timelog", *argv])
        cli.main()
        return called.pop()
    return run


@pytest.mark.parametrize("argv, cmd, date", [
    ((), "review", "2026-11-16"),
    (("friday",), "review", "2026-11-13"),
    (("-2",), "review", "2026-11-14"),
    (("--date", "2026-11-14"), "review", "2026-11-14"),
    (("day", "2026-11-14"), "day", "2026-11-14"),
    (("day", "20261114", "--events"), "day", "2026-11-14"),
    (("day", "--date", "20261114"), "day", "2026-11-14"),
    (("14/11/26",), "review", "2026-11-14"),
    (("day", "14.11"), "day", "2026-11-14"),
])
def test_the_date_is_an_argument_or_an_option(run, argv, cmd, date):
    a = run(*argv)
    assert (a.cmd, a.date) == (cmd, date)


@pytest.mark.parametrize("argv, date, entry", [
    (("show",), "2026-11-16", None),
    (("show", "3"), "2026-11-16", 3),
    (("show", "sat"), "2026-11-14", None),
    (("show", "sat", "3"), "2026-11-14", 3),
    (("show", "20261114", "3"), "2026-11-14", 3),
    (("show", "14.11", "3"), "2026-11-14", 3),
])
def test_show_tells_an_entry_from_a_date(run, argv, date, entry):
    a = run(*argv)
    assert (a.date, a.entry) == (date, entry)


def test_config_takes_no_date(run):
    assert run("config").cmd == "config"


@pytest.mark.parametrize("argv, message", [
    (("typo",), "'typo' is neither a command nor a date"),
    (("day", "2026-13-01"), "'2026-13-01' isn't a date. Dates: 14.11.2026, 14/11/26, 14-11, 2026-11-14"),
    (("--date", "x"), "'x' isn't a date"),
    (("day", "friday", "--date", "2026-11-14"), "two dates"),
])
def test_a_wrong_date_says_which_forms_work(run, capsys, argv, message):
    with pytest.raises(SystemExit):
        run(*argv)
    assert message in capsys.readouterr().err


def test_help_starts_with_the_review(run, capsys):
    with pytest.raises(SystemExit):
        run("--help")
    out = capsys.readouterr().out
    assert out.startswith("usage: timelog [DATE]")
    assert "  show      the saved day" in out


def test_colors_only_in_a_terminal(monkeypatch):
    from timelog import style
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert style.task("ABC-1: PR review") == "\033[36mABC-1\033[0m: PR review"
    assert style.pad("ABC-1", style.task("ABC-1"), 7).endswith("\033[0m  ")
    monkeypatch.setenv("NO_COLOR", "")
    assert style.task("ABC-1") == "ABC-1"
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert style.paint("x", "red") == "x" and style.columns() is None  # piped: the skill reads it


def test_sources_of_an_entry_are_short():
    from timelog import model
    at = MONDAY.isoformat()
    entry = model.Entry("ABC-1", model.at(at, "09:00"), model.at(at, "10:00"), "auto",
                        ("claude 8b36b346", "claude 8b36b346/a2378f66", "claude 66692592", "me browser (folded)",
                         "me focus", "call"))
    assert cli.sources_of(entry) == "focus, call, agent 8b36b346 66692592"
    folded_only = entry._replace(streams=("me browser (folded)",))
    assert cli.sources_of(folded_only) == "browser"


def test_the_bar_splits_agent_and_you():
    hour = dt.timedelta(hours=1)
    assert cli.bar(30, 60, hour) == "▒▒▒▒▒█████"
    assert cli.bar(0, 3, hour) == "█" + " " * 9  # a few minutes still show
    assert cli.bar(0, 0, hour) == " " * 10


def test_token_counts_are_short():
    assert [cli.count(n) for n in (0, 850, 12_400, 999_499, 999_500, 1_420_000)] == [
        "-", "850", "12k", "999k", "1.0M", "1.4M"]


def test_working_shows_the_outer_step_only(monkeypatch):
    err = io.StringIO()
    err.isatty = lambda: True
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setenv("NO_COLOR", "1")
    with style.working("reading the day's worklogs in Jira"):
        with style.working("asking Jira"):
            pass
    assert err.getvalue() == "# reading the day's worklogs in Jira…\r\033[K"
