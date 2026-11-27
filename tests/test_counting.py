"""How time is counted: lanes, folding, the entries a day is made of"""
import datetime as dt
import re

import pytest


from timelog import cli, config, model, sources, store, timeline
from timelog.sources import activitywatch
from helpers import DATE, at, ev, mine, prompt, turn, entries_of, glance, ns, worklogs, chat, yours_of


def test_parallel_reviews_each_get_their_full_time():
    events = [ev("09:00", c, s, review=True, minutes=10) for c, s in (("A", "claude s1"), ("B", "claude s2"),
                                                                       ("C", "claude s3"))]
    entries, _breaks, _notes = entries_of(events)
    assert entries == [("A: PR review", "09:00", "09:10"), ("B: PR review", "09:00", "09:10"),
                       ("C: PR review", "09:00", "09:10")]


def test_an_agent_working_counts_while_the_user_prompts_another_card():
    events = [ev("09:00", "B", "claude s2", minutes=30), ev("09:10", "A", "claude s1", minutes=7)]
    entries, _breaks, _notes = entries_of(events)
    assert entries == [("B", "09:00", "09:30"), ("A", "09:10", "09:17")]


def test_an_agent_working_counts_while_the_user_is_away():
    away = model.Event(at("12:10"), None, False, "afk", "away", "activitywatch afk", "", until=at("13:00"))
    entries, breaks, _notes = entries_of([ev("12:00", "A", minutes=65), away])
    assert entries == [("A", "12:00", "13:05")] and breaks == []


def test_a_prompt_gives_no_time_its_turn_does():
    entries, _breaks, _notes = entries_of([prompt("09:00", "A"), prompt("10:00", "B"), ev("10:00", "B", minutes=10)])
    assert entries == [("B", "10:00", "10:10")]


def test_a_quiet_stretch_goes_to_no_card():
    entries, breaks, _notes = entries_of([ev("09:00", "A"), ev("09:20", "B", "claude s2")])
    assert entries == [("A", "09:00", "09:05"), ("B", "09:20", "09:25")] and breaks == []


def test_your_minutes_without_a_card_fold_into_your_current_card():
    entries, _breaks, _notes = entries_of([mine("09:00", "A"), mine("09:05", None, text="Slack", minutes=10),
                                           mine("09:15", "B"), mine("09:20", None)])
    assert entries == [("A", "09:00", "09:15"), ("B", "09:15", "09:25")]


def test_a_glance_at_a_card_folds_into_your_current_card():
    events = [mine("09:00", "A", minutes=4), glance("09:04", "B", 20, review=True), mine("09:04:20", "A", minutes=5)]
    entries, _breaks, _notes = entries_of(events)
    assert entries == [("A", "09:00", "09:09")]
    # 30 seconds or more is a look, not a glance: B gets its time
    events = [mine("09:00", "A", minutes=4), glance("09:04", "B", 40, review=True), mine("09:04:40", "A", minutes=5)]
    assert ("B: PR review", "09:04", "09:05") in entries_of(events)[0]


def test_a_glance_with_nothing_to_fold_into_counts_no_time():
    entries, _breaks, _notes = entries_of([glance("09:00", "B", 15), glance("09:00:15", "C", 10)])
    assert entries == []


def test_in_an_agents_window_you_count_between_its_turns():
    # you write a prompt to B in a terminal, watch B work, read Gmail, then read B's answer in the
    # terminal: writing and reading count for you on B, watching only for B's lane; Gmail is B's while
    # B works, and after: B is the card you're on since your prompt
    term = mine("09:00", None, text="terminal", minutes=2)._replace(keys=("terminal",))
    events = [term, prompt("09:01", "B", "claude s2"), ev("09:01", "B", "claude s2", minutes=3),
              mine("09:02", None, text="Gmail", minutes=3)._replace(keys=("Gmail",)),
              term._replace(when=at("09:05"), until=at("09:07"))]
    # yours on B, a minute apart, join (merge_gap)
    assert yours_of(events) == [("B", "09:00", "09:07")]


def test_watching_an_agent_counts_once():
    # the terminal you prompted from, in front of you for the whole turn: the agent's 10 min only
    events = [mine("09:00", None, text="terminal", minutes=10)._replace(keys=("terminal",)),
              prompt("09:00", "B", "claude s2"), ev("09:00", "B", "claude s2", minutes=10)]
    entries, _breaks, _notes = entries_of(events)
    assert entries == [("B", "09:00", "09:10")]


def test_your_minutes_follow_the_running_agent_you_prompted_last():
    away = model.Event(at("08:56"), None, False, "afk", "away", "activitywatch afk", "", until=at("08:59"))
    events = [prompt("08:50", "A", "claude s1"), prompt("08:55", "B", "claude s2"),
              ev("08:50", "A", "claude s1", minutes=30), ev("08:55", "B", "claude s2", minutes=10),
              away, mine("09:00", None, minutes=10)]
    # back from a break, no current card: your minutes go with B, prompted last, while it works, then
    # with A, still working
    assert yours_of(events) == [("A", "09:05", "09:10"), ("B", "09:00", "09:05")]


def test_kept_entries_are_not_counted_twice():
    kept = [model.Entry("A", at("09:00"), at("09:30"), "manual")]
    events = [ev("09:28", "A"), ev("09:20", "B", "claude s2")]
    entries, _breaks, _notes = entries_of(events, kept)
    assert entries == [("B", "09:20", "09:25"), ("A", "09:30", "09:33")]


def test_glances_minutes_apart_are_not_joined():
    def glance(start, end):
        when, until = (dt.datetime.fromisoformat(f"{DATE}T{x}").astimezone() for x in (start, end))
        return model.Event(when, "B-1", True, "browser", "browser", "activitywatch web", "pr", until=until)

    here = model.Event(at("17:30"), None, False, "afk", "presence", "activitywatch afk", "", until=at("17:45"))
    events = [here, glance("17:34:29", "17:34:36"), glance("17:39:11", "17:39:13")]
    entries, _breaks, notes = timeline.build(events, [], at("18:00"))
    assert [(e.task, f"{e.start:%H:%M}", f"{e.end:%H:%M}") for e in entries] == [("B-1: PR review", "17:34", "17:35")]
    assert "rounded to the minute: 1 bits" in notes[0]


def test_normalize_lets_a_manual_entry_win_over_an_auto_one_of_the_same_task():
    entries = [model.Entry("A", at("09:00"), at("10:00"), "auto", ("s1",)),
               model.Entry("A", at("09:30"), at("09:45"), "manual", ("manual",)),
               model.Entry("B", at("09:30"), at("09:45"), "auto", ("s2",))]
    assert [(e.task, f"{e.start:%H:%M}", f"{e.end:%H:%M}", e.source) for e in timeline.normalize(entries)] == [
        ("A", "09:00", "09:30", "auto"), ("A", "09:30", "09:45", "manual"), ("B", "09:30", "09:45", "auto"),
        ("A", "09:45", "10:00", "auto")]


def test_a_glance_doesnt_make_a_card_your_current_one():
    glance = model.Event(at("09:00"), "A", False, "me focus", "focus", "activitywatch window", "Re: A",
                         until=at("09:00") + dt.timedelta(seconds=6))
    entries, _breaks, _notes = entries_of([mine("08:55", "B"), glance, mine("09:01", None, text="Slack")])
    # the Slack minutes fold into B, the card you were working on, not into A glanced at in the inbox
    assert entries == [("B", "08:55", "09:06")]


def test_an_unattributed_call_stays_apart_with_what_folds_into_it():
    call = model.Event(at("09:10"), None, False, "call", "call", "activitywatch web", "Meet - Sync", until=at("09:40"))
    entries, _breaks, _notes = entries_of([mine("09:00", None, minutes=5), call, mine("09:15", None, text="Slack")])
    assert entries == [("?", "09:00", "09:05"), ("? Meet - Sync", "09:10", "09:40")]


def test_a_call_holds_your_lane_while_it_runs():
    # a meeting 09:00-09:30 on ADMIN-5: a page of B, then the window of an agent working on A between
    # its turns, count for the meeting while it runs, and for their own cards after
    call = model.Event(at("09:00"), "ADMIN-5", False, "call", "call", "activitywatch web", "Meet - All hands",
                       until=at("09:30"))
    events = [call, mine("09:05", "B", "browser", text="B page", minutes=10),
              ev("09:15", "A", "claude s1", minutes=2), mine("09:20", None, text="A session", minutes=15)._replace(keys=("A session",)),
              prompt("09:21", "A", "claude s1")]
    entries, _breaks, _notes = entries_of(events)
    assert sorted(entries) == [("A", "09:15", "09:17"), ("A", "09:30", "09:35"), ("ADMIN-5", "09:00", "09:30")]


def test_a_prompt_makes_its_card_your_current_one():
    # a page of A for 5 minutes, then a prompt about B: the Slack minutes go with B while it works,
    # and after, while you wait to give it the next prompt; a page of A for a minute brings A back
    events = [mine("09:00", "A"), prompt("09:05", "B", "claude s2"), ev("09:05", "B", "claude s2", minutes=2),
              mine("09:06", None, text="Slack", minutes=10), mine("09:16", "A", minutes=1),
              mine("09:17", None, text="Slack", minutes=3)]
    assert yours_of(events) == [("A", "09:00", "09:05"), ("A", "09:16", "09:20"), ("B", "09:06", "09:16")]


def test_a_cards_lanes_count_once_in_its_worklog(days, monkeypatch, capsys):
    # two agents on A for 30 min, and you reading A's page from 09:20 to 09:40: each agent keeps its
    # entry and yours starts when they stop, and A's worklog is the 40 min something worked on it
    events = [ev("09:00", "A", "claude s1", minutes=30), ev("09:00", "A", "claude s2", minutes=30),
              mine("09:20", "A", "browser", text="A page", minutes=20)]
    assert sorted(entries_of(events)[0]) == [("A", "09:00", "09:30"), ("A", "09:00", "09:30"), ("A", "09:30", "09:40")]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    cli.cmd_day(ns())
    worklogs()
    rows = [r.split("\t")[:3] for r in capsys.readouterr().out.splitlines() if "\t" in r]
    assert rows == [["A", "09:00-09:40", "40m"]]
    # moving your entry to B leaves the agents' on A, also once rebuilt
    day = store.load_day(DATE)
    yours = next(i for i, e in enumerate(day["entries"], 1) if model.lane_of(e.streams) == "you")
    cli.cmd_set(ns(entry=[f"{yours}=B"]))
    cli.cmd_day(ns())
    assert sorted((e.task, model.lane_of(e.streams)) for e in store.load_day(DATE)["entries"]) == [
        ("A", "agent s1"), ("A", "agent s2"), ("B", "you")]


def test_lane_of():
    assert model.lane_of("claude 8b36b346/a2378f66") == "agent 8b36b346"
    assert model.lane_of("copilot 8b36b346") == "agent 8b36b346"
    assert model.lane_of(("claude s1", "claude s1/sub")) == "agent s1"
    assert model.lane_of(("me focus", "me browser (folded)")) == "you"
    assert model.lane_of(("call",)) == model.lane_of(("manual",)) == "you"


def test_an_agent_waiting_for_your_approval_isnt_working(aw):
    # a 30 min turn that waited 10 min for your approval: the agent worked 20 min, in two stretches
    aw["agent.turn"] = [turn("09:00", 30, "s1", "deploy ABC-1",
                             waits=[[at("09:05").isoformat(), at("09:15").isoformat()]])]
    out, _info = activitywatch.combine(at("00:00"), at("23:59"), re.compile(r"ABC-\d+"), "me")
    entries, _breaks, _notes = timeline.build(out, [], at("18:00"))
    assert [(e.task, f"{e.start:%H:%M}", f"{e.end:%H:%M}") for e in entries] == [
        ("ABC-1", "09:00", "09:05"), ("ABC-1", "09:15", "09:30")]
    # a turn written before the watcher kept its waits counts whole
    aw["agent.turn"] = [turn("09:00", 30, "s1", "deploy ABC-1", waiting=600)]
    out, _info = activitywatch.combine(at("00:00"), at("23:59"), re.compile(r"ABC-\d+"), "me")
    assert [(e.start, e.end) for e in timeline.build(out, [], at("18:00"))[0]] == [(at("09:00"), at("09:30"))]


def test_chat_goes_with_the_agent_you_steer():
    # you prompt B, then chat and read a page while B works till 09:12: B's; then, B done, still B's:
    # the card you're on since your prompt
    events = [prompt("09:00", "B", "claude s2"), ev("09:00", "B", "claude s2", minutes=12), chat("09:01", 9),
              mine("09:10", None, text="news", minutes=5)._replace(keys=("news",))]
    assert yours_of(events) == [("B", "09:01", "09:15")]


@pytest.mark.parametrize("setting, yours", [
    ("other_cards", [("B", "09:15", "09:20"), ("B", "09:25", "09:40")]),  # the call holds you 09:20-09:25
    ("calls", [("B", "09:30", "09:40")]),
])
def test_your_time_during_an_agent_turn(monkeypatch, setting, yours):
    # A's agent works 09:00-09:30 while you read A's page, then its PR, then B's page, with a call on C
    # 09:20-09:25: the agent's time and the call count whatever the setting
    monkeypatch.setattr(config, "DURING_AGENTS", setting)
    call = model.Event(at("09:20"), "C", False, "call", "call", "activitywatch web", "Meet - C|x", until=at("09:25"))
    events = [ev("09:00", "A", "claude s1", minutes=30), mine("09:00", "A", "browser", text="A page", minutes=10),
              mine("09:10", "A", "browser", text="A PR", minutes=5)._replace(review=True),
              mine("09:15", "B", "browser", text="B page", minutes=25), call]
    assert sorted(entries_of(events)[0]) == sorted([("A", "09:00", "09:30"), ("C", "09:20", "09:25"), *yours])


def test_your_time_is_cut_by_the_agents_entry_as_built():
    # two turns 40 s apart make one entry 09:00-09:20 (merge_gap): your page of A between them doesn't count
    second = ev("09:10", "A", "claude s1", minutes=10)._replace(when=at("09:10") + dt.timedelta(seconds=40))
    events = [ev("09:00", "A", "claude s1", minutes=10), second, mine("09:05", "A", "browser", minutes=20)]
    assert sorted(entries_of(events)[0]) == [("A", "09:00", "09:20"), ("A", "09:20", "09:25")]


def test_a_session_without_a_card_ends_the_card_of_the_one_before_in_its_window():
    # A's review in the repo's window in the morning; after lunch, a release from that window by a
    # session that names no card: the files you edit after prompting it don't go to A
    repo = mine("09:00", None, text="pop_odoo", minutes=7)._replace(keys=("pop_odoo",))
    events = [repo, prompt("09:00", "A", "claude s1"), ev("09:00", "A", "claude s1", minutes=5),
              repo._replace(when=at("11:00"), until=at("11:02")), prompt("11:01", None, "claude s2"),
              ev("11:01", None, "claude s2", minutes=3),
              mine("11:04", None, text="releases.md", minutes=10)._replace(keys=("releases.md",))]
    assert {task for task, start, _end in yours_of(events) if start >= "11:01"} == {config.UNATTRIBUTED}


def test_an_answer_without_a_card_ends_the_card_of_the_one_before():
    # in the same window, s2 (no card) answers after s1 (card A): reading it, then Slack, aren't A's
    term = mine("09:05", None, text="terminal", minutes=1)._replace(keys=("terminal",))
    events = [term._replace(when=at("09:00"), until=at("09:01")), prompt("09:00:30", "A", "claude s1"),
              ev("09:01", "A", "claude s1", minutes=4), term,
              prompt("09:05:30", None, "claude s2"), ev("09:06", None, "claude s2", minutes=2),
              term._replace(when=at("09:08"), until=at("09:09")),
              mine("09:09", None, text="Slack - dev", minutes=5)._replace(keys=("Slack - dev",))]
    assert any(task == "A" and start <= "09:05" < end for task, start, end in yours_of(events))
    assert [task for task, start, _end in yours_of(events) if start >= "09:08"] == [config.UNATTRIBUTED]


def test_the_current_card_example_of_design_md():
    shop = mine("09:00", None, text="shop_odoo", minutes=1)._replace(keys=("shop_odoo",))
    events = [shop, prompt("09:00:30", "ABC-1", "claude s1"), ev("09:01", "ABC-1", "claude s1", review=True, minutes=9),
              shop._replace(when=at("09:10"), until=at("09:15")),
              shop._replace(when=at("10:00"), until=at("10:02")), prompt("10:01", None, "claude s2"),
              ev("10:02", None, "claude s2", minutes=8),
              shop._replace(when=at("10:10"), until=at("10:12")),
              mine("10:12", None, text="releases.md", minutes=8)._replace(keys=("releases.md",)),
              mine("10:20", None, text="Slack - dev", minutes=5)._replace(keys=("Slack - dev",))]
    review, unknown = "ABC-1: PR review", config.UNATTRIBUTED
    assert entries_of(events)[0] == [(review, "09:00", "09:01"), (review, "09:01", "09:10"), (review, "09:10", "09:15"),
                                     (review, "10:00", "10:02"), (unknown, "10:02", "10:10"), (unknown, "10:10", "10:25")]


def test_an_answer_holds_for_its_time_not_for_a_longer_entry_rebuilt_later():
    # you answered ADM-5 for 11:47-11:53; rebuilt, the entry runs to 11:58: the rest is asked about
    you = model.Entry(config.UNATTRIBUTED, at("11:47"), at("11:58"), "auto", ("me focus",))
    answer = {"task": config.UNATTRIBUTED, "start": at("11:47").isoformat(), "end": at("11:53").isoformat(),
              "to": "ADM-5", "lane": "you"}
    spans = [(e.task, f"{e.start:%H:%M}", f"{e.end:%H:%M}") for e in timeline.replay([you], {}, [answer])]
    assert spans == [("ADM-5", "11:47", "11:53"), (config.UNATTRIBUTED, "11:53", "11:58")]
    # a minute either side is the same entry, rebuilt a little differently
    shifted = you._replace(start=at("11:46"), end=at("11:54"))
    assert [e.task for e in timeline.replay([shifted], {}, [answer])] == ["ADM-5"]
