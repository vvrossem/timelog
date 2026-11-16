"""Rules: the built-in ones, yours, and those your answers add"""
import re

import pytest

from timelog import attribution, cli, config, model, rules, sources
from helpers import at, ev, mine, prompt, written_rules, ns


@pytest.mark.parametrize("string, kind, name, flag", [
    ("dev-chat (Channel) - Acme - 1 new item - Slack", "focus",
     "Slack - dev-chat (Channel)", "side_activity"),
    ("Slack - Julien Coux (DM)", "focus", "Slack - Julien Coux (DM)", "side_activity"),  # the desktop app
    ("Meet - Weekly sync|https://meet.google.com/meet/abc-defg-hij?authuser=0", "browser",
     "Meet - Weekly sync", "always_active"),
    ("Meet - Sync|https://meet.google.com/abc-defg-hij", "browser", "Meet - Sync", "always_active"),
    ("Edit - Release notes - Product - Confluence", "focus", "Confluence - Release notes", None),
    ("Inbox (12) - me@example.com - Mail|https://mail.google.com/mail/u/0/#inbox", "browser", "Gmail", None),
    ("shop_odoo|ABC-1-export", "editor", "shop_odoo ABC-1-export", None),
    ("zsh - Terminal", "focus", "zsh", None),
])
def test_rules_name_pieces(string, kind, name, flag):
    rule, got, card = rules.match(string, [], kind)
    assert got == name and card is None
    assert (rule or {}).get(flag, False) if flag else not (rule or {}).get("always_active")


def test_a_rules_groups_fill_its_card_and_the_config_comes_first():
    mine_first = [{"regex": r"^([A-Z]+-\d+) ", "card": "$1", "name": "work on $1"}, {"regex": "Slack", "card": "X"}]
    docs = "ABC-7 notes - Google Docs|https://docs.google.com"
    assert rules.match(docs, mine_first)[1:] == ("work on ABC-7", "ABC-7")
    assert rules.match("a - Acme - Slack", mine_first)[2] == "X"


def test_rules_attribute_events_without_card():
    config_rules = [{"regex": "^timelog", "card": "TOOL-68"}, {"regex": r"\|/timelog$", "card": "-"},
                    {"regex": "^sales", "card": "ABC-644: PR review", "name": "Slack - sales (Channel)",
                     "side_activity": True}]
    sessions = {"s1": {"command": "/timelog"}}
    events = [mine("09:00", None, text="timelog - x"), prompt("09:01", None),
              mine("09:02", None, text="sales (Channel) - Acme - Slack"), mine("09:03", "ABC-1", text="y")]
    out = attribution.apply(events, sessions, config_rules)
    assert [(e.kind, e.card, e.review, e.side, e.keys) for e in out] == [
        ("focus", "TOOL-68", False, False, ("timelog",)), ("presence", None, False, False, ()),
        ("focus", "ABC-644", True, True, ("Slack - sales (Channel)",)), ("focus", "ABC-1", False, False, ("y",))]


def test_set_adds_a_rule_only_with_a_bang(days, monkeypatch, capsys):
    events = attribution.apply([mine("09:00", None, text="timelog - x"), ev("10:00", "ABC-1")], {}, [])
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    cli.cmd_set(ns(entry=["1=TOOL-68"]))  # that day only
    assert written_rules() == [] and "rule added" not in capsys.readouterr().out
    cli.cmd_set(ns(entry=["1=TOOL-68!"]))
    assert "rule added: ^timelog\\ \\-\\ x$ -> TOOL-68" in capsys.readouterr().out
    assert written_rules() == config.RULES == [{"regex": r"^timelog\ \-\ x$", "name": "timelog", "card": "TOOL-68"}]
    assert rules.match("timelog - x")[2] == "TOOL-68" and rules.match("timelog - y")[2] is None


def test_set_adds_a_rule_only_for_the_page_holding_most_of_the_entry(days, monkeypatch, capsys):
    def page(hhmm, minutes, name):
        return mine(hhmm, None, "browser", text=f"{name}|https://x", minutes=minutes)._replace(keys=(name,))
    events = [page("09:00", 9, "Confluence - Release notes"), page("09:09", 3, "Gmail"), page("09:12", 1, "Calendar"),
              page("10:00", 5, "Gmail"), page("10:05", 5, "Calendar"), page("10:10", 5, "Docs")]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    cli.cmd_set(ns(entry=["1=OPS-71!", "2=OPS-72!"]))
    assert "no rule added: no page or window without a card of its own holds more than half of 10:00-10:15" in (
        capsys.readouterr().out)
    assert [(r["name"], r["card"]) for r in written_rules()] == [("Confluence - Release notes", "OPS-71")]


def test_a_new_rule_comes_first_and_replaces_one_of_the_same_regex(days, monkeypatch, capsys):
    config.add_rule({"regex": "^Q4 planning\\.ods$", "card": "ABC-9"})
    config.add_rule({"regex": "^Q4", "card": "ABC-8"})
    events = attribution.apply([mine("09:00", None, text="Q4 planning.ods", minutes=30)], {})
    assert events[0].card == "ABC-8" and events[0].guessed
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    cli.cmd_set(ns(entry=["1=TOOL-69!"]))
    assert [(r["regex"], r["card"]) for r in written_rules()] == [
        ("^Q4\\ planning\\.ods$", "TOOL-69"), ("^Q4", "ABC-8"), ("^Q4 planning\\.ods$", "ABC-9")]


def test_what_a_page_shows_gets_no_rule(days, monkeypatch, capsys):
    events = attribution.apply([mine("09:00", "ABC-1", "browser", text="ABC-1 page|https://x", minutes=30)], {})
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    cli.cmd_set(ns(entry=["1=ABC-2!"]))
    assert "no rule added" in capsys.readouterr().out
    assert written_rules() == []


def test_the_scopes_offered_for_a_rule():
    def scopes(kind, text, keys=()):
        piece = model.Event(at("09:00"), None, False, f"me {kind}", kind, "", text, until=at("09:10"), keys=keys)
        return [(what, r) for what, r in rules.scopes(piece)]
    tab = scopes("browser", "Studio Code Server – Home Assistant|http://192.168.50.167:8123/a?b")
    assert [what for what, _r in tab] == ['this title: "Studio Code Server – Home Assistant"',
                                          "anything on 192.168.50.167:8123"]
    site = tab[1][1]["regex"]
    assert re.search(site, "x|https://192.168.50.167:8123") and not re.search(site, "x|http://192.168.50.1678/")
    # a call keeps being one: its rule keeps the name and always_active of the rule it matched; no site
    [(what, call)] = scopes("call", "Meet – Daily sync|https://meet.google.com/abc-defg-hij")
    assert call == {"regex": r"^Meet\ –\ Daily\ sync\|", "name": "Meet - Daily sync", "always_active": True}
    assert rules.match("Meet – Daily sync|https://meet.google.com/abc-defg-hij", [{**call, "card": "X"}])[0][
        "always_active"]
    # chat stays a side activity
    assert scopes("focus", "Slack - dev")[0][1]["side_activity"]
    assert [what for what, _r in scopes("editor", "shop_odoo|ABC-1-fix")] == ["shop_odoo on ABC-1-fix",
                                                                            "shop_odoo, any branch"]
    [(what, session)] = scopes("aw-agent", "a prompt", keys=("Invoice export",))
    assert re.search(session["regex"], "Invoice export - bank|/review") and what == 'this session title: "Invoice export"'


def test_tempo_is_admin_time(monkeypatch):
    monkeypatch.setitem(config.TRACKER, "admin_card", "TOOL-69")
    tab = "Tempo|https://acme.atlassian.net/plugins/servlet/ac/io.tempo.jira/tempo-app#!/my-work/week"
    assert rules.match(tab, [], "browser")[1:] == ("Tempo", "TOOL-69")
    assert rules.match("Tempo - Jira — Mozilla Firefox", [], "focus")[1:] == ("Tempo", "TOOL-69")  # its window
    # the file `timelog` opens in your editor
    assert rules.match("tmpnbgzy3ro.timelog - Visual Studio Code", [], "focus")[1:] == ("timelog review", "TOOL-69")
    monkeypatch.setitem(config.TRACKER, "admin_card", None)  # not set: it follows your agents, or is asked
    assert rules.match(tab, [], "browser")[1:] == ("Tempo", None)
