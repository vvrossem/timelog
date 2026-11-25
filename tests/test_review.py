"""The review: the questions, the editor file, the days not pushed yet, then the push"""
import datetime as dt
import json
import re

import pytest

from timelog import cli, config, jira, jira_rest, model, push, questions, review, reviewing, rules, sources, store, strip, sync, timeline
from helpers import DATE, at, ev, mine, written_rules, ns, review_day, on, asked_day


def test_review_applies_cards_drops_times_and_comments(days, monkeypatch, capsys):
    def fix(text):
        text = re.sub(r"09:30-10:00 +\?.*", "09:30-09:45  ABC-1", text)  # a gap: what left no trace
        text = re.sub(r"10:00-10:30 +\?", "10:00-10:30  TOOL-1: planning!", text)
        return re.sub(r"11:00-11:10 .*\n", "", text) + "TOOL-1: planning = planning, see PR #3\n"
    seen = review_day(days, monkeypatch, [fix])
    assert "\nagent s1  ABC-1" in seen[0] and re.search(r"\n10:00-10:30 +\? +# Q4 planning.ods\n", seen[0])
    assert [(e.task, f"{e.start:%H:%M}", f"{e.end:%H:%M}", e.source) for e in store.load_day(DATE)["entries"]] == [
        ("ABC-1", "09:00", "09:30", "auto"), ("ABC-1", "09:30", "09:45", "manual"),
        ("TOOL-1: planning", "10:00", "10:30", "auto")]
    assert store.load_day(DATE)["comments"] == {"TOOL-1: planning": "planning, see PR #3"}
    # the card with a ! makes a rule; the line deleted, without one, is dropped that day only
    assert [(r["name"], r["card"]) for r in written_rules()] == [("Q4 planning.ods", "TOOL-1: planning")]
    assert "ABC-1\t09:00-09:45\t45m" in capsys.readouterr().out  # no Jira: the worklogs to enter by hand


def test_worklogs_entered_in_tempo_make_the_day_done(days, monkeypatch):
    def fix(text):
        return re.sub(r"\n1[01]:\d\d-.*", "", text)  # only ABC-1 left
    for answer, pending in (("", [DATE]), ("y", [])):
        review_day(days, monkeypatch, [fix], answer=answer)
        events = sources.day_events(DATE, None)
        monkeypatch.setattr(sources, "day_events", lambda date, cards: events if date == DATE else [])
        assert [date for date, _day, _late in reviewing.pending_days(dt.date(2026, 10, 21))] == pending
    assert store.load_day(DATE)["entered"][0][::2] == ["ABC-1", 30]


def test_the_review_offers_the_days_not_handed_over_oldest_first(days, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATE_ORDER", "dmy")
    dates = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", DATE]
    by_date = {date: [on(date, "09:00", f"ABC-{i}")] for i, date in enumerate(dates, 1)}
    built = []
    monkeypatch.setattr(sources, "day_events", lambda date, cards: built.append(date) or by_date.get(date, []))
    # 13.10 pushed that day, nothing since; 14.10 entered in Tempo the day after; 15.10 pushed that
    # day, then 20 minutes more work; 16.10 reviewed but not pushed; 19.10 never seen
    for date, saved, how in (("2026-10-13", "09:40", "pushed"), ("2026-10-14", None, "entered"),
                             ("2026-10-15", "09:40", "pushed"), ("2026-10-16", None, None)):
        cli.cmd_day(ns(date=date))
        day = store.load_day(date)
        if how == "pushed":
            day["pushed"] = [{"id": "1", "card": "ABC-1", "started": "", "minutes": 30, "comment": ""}]
        elif how == "entered":
            day["entered"] = [["ABC-2", "", 30, ""]]
        store.save_day(date, day)
        if saved:  # as if saved that day: the file is copied with its time
            path = days / f"{date}.json"
            path.write_text(json.dumps({**json.loads(path.read_text()), "saved": model.at(date, saved).isoformat()}))
    by_date["2026-10-15"].append(on("2026-10-15", "10:00", "ABC-3", minutes=20))
    built.clear()
    pending = ["2026-10-15", "2026-10-16", "2026-10-19"]
    assert [date for date, _day, _late in reviewing.pending_days(dt.date.fromisoformat(DATE))] == pending
    assert "2026-10-14" not in built  # handed over after it ended: not even rebuilt

    reviewed = []
    monkeypatch.setattr(reviewing, "review_day", lambda a: reviewed.append(a.date) or a.date != pending[0] or kept)
    # Enter says yes; a day you cancel (kept False) stops the catch-up there
    for answer, kept, expected in (("", True, [*pending, DATE]), ("", False, pending[:1]), ("n", True, [DATE])):
        monkeypatch.setattr("builtins.input", lambda question: answer)
        capsys.readouterr()
        reviewing.cmd_review(ns(date_given=False))
        assert reviewed == expected
        out = capsys.readouterr().out
        assert "# not pushed yet: Fri 16.10 (0h30), Mon 19.10 (0h30)" in out
        assert "# pushed, with work after the push: Thu 15.10 (0h20 since)" in out
        reviewed.clear()
    monkeypatch.setattr("builtins.input", lambda question: pytest.fail("asked"))
    reviewing.cmd_review(ns(date_given=True))  # a day given: only that day
    assert reviewed == [DATE]


def test_review_sends_an_unreadable_file_back_to_the_editor_and_can_be_cancelled(days, monkeypatch, capsys):
    seen = review_day(days, monkeypatch, [lambda t: t.replace("TOOL", "") + "10:00-10:30 tool 1\n", lambda t: ""])
    assert seen[1].startswith("# ERROR line ") and "10:00-10:30 tool 1" in seen[1]
    assert "cancelled" in capsys.readouterr().out
    assert [e.task for e in store.load_day(DATE)["entries"]] == ["ABC-1", "?", "?"]


def test_the_review_asks_in_the_terminal(days, monkeypatch, capsys):
    # 09:30-10:00 a break; Q4 planning, typed in lower case, for good on that window (its only scope:
    # y); 10:30-11:00 was ABC-1; news dropped that day only (n); then a comment, and no Tempo
    done, _prompts, _seen = asked_day(days, monkeypatch, [
        "", "tool-1: planning", "y", "abc-1", "-", "n", "2", "planning, see PR #3", "", "n"])
    out = capsys.readouterr().out
    assert done and "─── 1 of 4 ── nothing active ── 09:30-10:00 · 0h30 ─" in out
    assert re.search(r"   1  ABC-1  just before\n   2  ADM-1  admin", out)  # the options of Q4 planning
    assert 'From now on too, for this window: "Q4 planning.ods"?   [y] yes · [n] no' in out
    assert "[1-2] or [ABC-1] card · [Enter] a break · [e] editor · [q] stop" in out
    assert "  Tue 20.10  09    10    11    12\n  you        ······???···?·····\n" in out  # the day strip
    assert "\n                   ^^^\n  what was open\n       30m  Q4 planning.ods\n" in out  # Q4 planning on it
    # redrawn with each answer: Q4 planning and 10:30-11:00 given cards, the break left as it was
    assert "─── 4 of 4 ── your windows ── 11:00-11:10 · 0h10 ─" in out.split("── 3 of 4")[1]
    assert "  you        ······▓▓▓▓▓▓?·····\n" in out.split("── 4 of 4")[1]
    # once more after the last question: news dropped, so the day ends at 11:00
    assert "  Tue 20.10  09    10    11\n  you        ······▓▓▓▓▓▓\n" in out.split("─── the day as answered ─")[1]
    assert [(e.task, f"{e.start:%H:%M}", f"{e.end:%H:%M}", e.source) for e in store.load_day(DATE)["entries"]] == [
        ("ABC-1", "09:00", "09:30", "auto"), ("TOOL-1: planning", "10:00", "10:30", "auto"),
        ("ABC-1", "10:30", "11:00", "manual")]
    assert store.load_day(DATE)["comments"] == {"TOOL-1: planning": "planning, see PR #3"}
    assert store.load_day(DATE)["breaks"] == [[at("09:30").isoformat(), at("10:00").isoformat()]]
    assert [(r["name"], r["card"]) for r in written_rules()] == [("Q4 planning.ods", "TOOL-1: planning")]


def test_the_review_goes_to_the_editor_or_stops_keeping_your_answers(days, monkeypatch, capsys):
    done, _prompts, seen = asked_day(days, monkeypatch, ["", "TOOL-1", "", "e"], [lambda t: ""])
    assert not done and re.search(r"\n10:00-10:30 +TOOL-1 ", seen[0])  # then cancelled in the editor
    assert [e.task for e in store.load_day(DATE)["entries"]] == ["ABC-1", "TOOL-1", "?"]
    # the 09:30 break and TOOL-1, kept, aren't asked again: 10:30-11:00 a break too, then stopped
    done, prompts, seen = asked_day(days, monkeypatch, ["", "q"])
    out = capsys.readouterr().out
    assert not done and not seen and len(prompts) == 2 and "your answers are kept" in out
    assert "─── 1 of 2 ── nothing active ── 10:30-11:00" in out
    # a wrong answer is asked again; a scope answer other than y, n or Enter too
    done, prompts, _seen = asked_day(days, monkeypatch, ["nope", "ADM-1", "x", "", "", "n"])
    out = capsys.readouterr().out
    assert "─── 1 of 1 ── your windows ── 11:00-11:10" in out and "a number from 1 to" in out and "y or n" in out and len(prompts) == 6


def test_review_says_why_a_file_is_unreadable_and_stops_if_you_say_so(days, monkeypatch, capsys):
    seen = review_day(days, monkeypatch, [lambda t: t + "10:00-10:30 tool 1\n"], answer="n")
    out = capsys.readouterr().out
    assert len(seen) == 1 and "ERROR line " in out and "cancelled" in out


def test_review_refuses_a_comment_for_a_task_not_in_the_day(days, monkeypatch):
    seen = review_day(days, monkeypatch, [lambda t: t + "ABC-2 = typo\n", lambda t: ""])
    assert seen[1].startswith("# ERROR a comment for a task that isn't in the day: ABC-2")


def test_review_reads_same_time_lines_by_task_calls_with_a_hash_and_refuses_a_wrong_time():
    rows = [review.Row("09:00", "10:00", "ABC-1", "entry", 1), review.Row("09:00", "10:00", "DEF-2", "entry", 2)]
    # the first of two entries sharing their times is deleted: the other isn't renamed
    assert review.parse("09:00-10:00 DEF-2\n", rows) == ({}, {1}, [], {}, [])
    assert review.parse("09:00-10:00 GHI-3\n09:00-10:00 DEF-2\n", rows) == ({1: "GHI-3"}, set(), [], {}, [])
    rows = [review.Row("09:00", "09:30", "? Meet - Standup #2", "entry", 1)]
    text = review.render(DATE, rows, [""], {}, [], "0h30", "0h30")
    assert review.parse(text, rows) == ({}, set(), [], {}, [])
    with pytest.raises(review.Invalid, match="line 1: no such time"):
        review.parse("25:00-26:00 ABC-1\n", [])


def test_a_bang_remembers_the_task_of_a_line_timelog_found():
    rows = [review.Row("", "", "ABC-1", "agent", 0, (1, 2), "s1"), review.Row("09:00", "10:00", "?", "entry", 3),
            review.Row("10:00", "10:30", "DEF-2", "entry", 4, (4, 5)), review.Row("11:00", "11:30", "?", "gap", 0)]
    text = "agent s1 TOOL-1!\n09:00-10:00 -!\n10:00-10:30 DEF-2 !\n11:00-11:30 ?\n"
    assert review.parse(text, rows) == ({1: "TOOL-1", 2: "TOOL-1", 3: "-"}, set(), [], {},
                                        [((1, 2), "TOOL-1"), ((3,), "-"), ((4, 5), "DEF-2")])
    for line in ("11:00-11:30 ABC-1!", "12:00-12:30 ABC-1!", "09:00-10:00 ?!", "ABC-1! = x"):
        with pytest.raises(review.Invalid, match="!"):
            review.parse(line + "\n", rows)


def test_the_review_sends_to_jira_only_on_a_yes(days, monkeypatch):
    monkeypatch.setattr(jira_rest, "available", lambda: True)
    monkeypatch.setattr(jira, "unknown", lambda tasks, date, cards: [])
    monkeypatch.setattr(sync, "push_plan", lambda a, day, client=None: [push.Action("add", "ABC-1", at("09:00"), 30, "")])
    monkeypatch.setattr(jira_rest, "Client", lambda: "client")
    sent = []
    monkeypatch.setattr(sync, "send", lambda date, day, client, actions: sent.append(True))
    for answer in ("", "n", "no", "Y es", EOFError):
        def reply(question):
            if answer is EOFError:
                raise EOFError  # no terminal: never a yes
            return answer
        monkeypatch.setattr("builtins.input", reply)
        reviewing.finish_review(ns(), {"entries": []})
        assert sent == []  # the plan, shown only
    for answer in ("y", "yes", "Y"):
        monkeypatch.setattr("builtins.input", lambda question: answer)
        reviewing.finish_review(ns(), {"entries": []})
        assert sent == [True]
        sent.clear()


def test_the_catch_up_starts_at_start_date(days, monkeypatch):
    built = []
    monkeypatch.setattr(sources, "day_events", lambda date, cards: built.append(date) or [on(date, "09:00", "ABC-1")])
    monkeypatch.setattr(config, "START_DATE", dt.date(2026, 10, 16))
    assert [date for date, _day, _late in reviewing.pending_days(dt.date.fromisoformat(DATE))] == [
        "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19"]
    assert min(built) == "2026-10-16"  # the days before aren't even built
    assert config.problems({"start_date": "16.10.2026"}) == ["start_date: 16.10.2026 is not a date like 2026-11-16"]
    assert config.problems({"start_date": "2026-10-16"}) == []


def test_the_review_asks_jira_about_the_day_it_just_built(days, monkeypatch, capsys):
    monkeypatch.setattr(sources, "day_events", lambda date, cards: [ev("09:00", "ABC-1", minutes=30)])
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    cli.cmd_add(ns(start="14:00", end="15:00", task="TOOL-1", comment="moved here"))  # the day file is stale
    monkeypatch.setattr(jira_rest, "available", lambda: True)
    asked = []

    def fetch(a):
        asked.append(sync.day_cards(a.date))
        jira.save(a.date, {"known": {c: {"summary": "Invoices"} for c in asked[-1]}, "candidates": {}, "worklogs": []})
    monkeypatch.setattr(sync, "cmd_fetch", fetch)
    seen = []
    monkeypatch.setattr(reviewing, "edit", lambda text: seen.append(text) or "")  # then cancelled
    capsys.readouterr()
    reviewing.review_day(ns())
    assert asked == [["ABC-1", "TOOL-1"]]
    assert "\nABC-1 = Invoices\n" in seen[0]  # its comment, there to check
    assert "written to" not in capsys.readouterr().out


def test_an_agent_line_moves_or_drops_all_its_turns(days, monkeypatch):
    events = [ev("09:00", "ABC-1", minutes=10), ev("10:00", "ABC-1", minutes=10), ev("09:00", "ABC-9", "claude s2")]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    monkeypatch.setattr(jira_rest, "available", lambda: False)
    monkeypatch.setattr("builtins.input", lambda question: "")
    for edit, expected in ((lambda t: t.replace("agent s1  ABC-1", "agent s1  ABC-2"), ["ABC-2", "ABC-2", "ABC-9"]),
                           (lambda t: re.sub(r"agent s1 .*\n", "", t), ["ABC-9"])):
        store.save_day(DATE, {"entries": [], "comments": {}}, quiet=True)  # a fresh day
        calls = []
        # once: an invalid file would come back to the editor; ABC-1's comment line stays, unchanged
        monkeypatch.setattr(reviewing, "edit", lambda t, edit=edit: calls.append(t) and len(calls) > 1 and pytest.fail(t)
                            or edit(t))
        reviewing.review_day(ns())
        assert sorted(e.task for e in store.load_day(DATE)["entries"]) == expected


def test_your_lines_of_a_card_group_while_close(days, monkeypatch):
    events = [mine("09:00", "ABC-1", minutes=10), mine("09:10", "ABC-2", minutes=5), mine("09:15", "ABC-1", minutes=10),
              mine("10:30", "ABC-1", minutes=5)]  # more than 30 min after: a line of its own
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    monkeypatch.setattr(jira_rest, "available", lambda: False)
    monkeypatch.setattr("builtins.input", lambda question: "")
    seen = []

    def edit(text):
        if seen:
            pytest.fail(text)  # sent back: the file didn't read
        seen.append(text)
        return text.replace("09:00-09:25  ABC-1", "09:00-09:25  ABC-3")
    monkeypatch.setattr(reviewing, "edit", edit)
    reviewing.review_day(ns())
    assert re.search(r"\n09:00-09:25  ABC-1 +# 0h20\n09:10-09:15  ABC-2\n.*\n10:30-10:35  ABC-1\n", seen[0])
    assert sorted((e.task, f"{e.start:%H:%M}") for e in store.load_day(DATE)["entries"]) == [
        ("ABC-1", "10:30"), ("ABC-2", "09:10"), ("ABC-3", "09:00"), ("ABC-3", "09:15")]


def test_a_gap_given_a_dash_in_the_editor_is_a_break(days, monkeypatch):
    review_day(days, monkeypatch, [lambda text: re.sub(r"09:30-10:00 +\?", "09:30-10:00  -", text)])
    assert store.load_day(DATE)["breaks"] == [[at("09:30").isoformat(), at("10:00").isoformat()]]
    seen = review_day(days, monkeypatch, [lambda text: ""])  # then cancelled
    assert "09:30-10:00" not in seen[0] and "10:30-11:00" in seen[0]


def test_the_cards_you_give_are_asked_to_jira(days, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", str(days))
    asked = []

    def fetch(a):
        asked.append(sync.day_cards(a.date))
        jira.save(a.date, {"known": {c: {"summary": "x"} for c in asked[-1]}, "candidates": {}, "worklogs": []})
    monkeypatch.setattr(sync, "cmd_fetch", fetch)
    monkeypatch.setattr(reviewing, "jira_rest", type("Up", (), {"available": staticmethod(lambda: True)}))
    asked_day(days, monkeypatch, ["NEW-9", "q"])
    assert asked == [["ABC-1"], ["ABC-1", "NEW-9"]]  # the day built, then with the card you gave


def test_a_card_given_to_a_session_goes_to_its_window_and_is_offered_after(monkeypatch, capsys):
    asked = [questions.Question("agent a2", "", [], [("ADM-1", "", "admin")], [], name="cosanum_odoo pull request 3851"),
             questions.Question("your windows", "09:20-09:26", [], [("ADM-1", "", "admin")], [], name="Slack"),
             questions.Question("your windows", "09:29-09:34", [], [], [], name="cosanum_odoo pull reques…")]
    answers = ["abc-7", ""]
    monkeypatch.setattr("builtins.input", lambda prompt: answers.pop(0))
    assert questions.ask(asked) == ({0: "ABC-7", 2: "ABC-7"}, [])
    out = capsys.readouterr().out
    assert "   1  ADM-1  admin\n   2  ABC-7  given above\n" in out  # the card typed, offered after
    assert "ABC-7  as [1], the same 'cosanum_odoo pull reques…'" in out
    assert not questions.same_piece("Slack", "Slack - dev…") and not questions.same_piece("", "")


def test_a_day_pushed_then_worked_on_asks_only_about_the_work_after(days, monkeypatch, capsys):
    # 10:00-10:30 left without a card before the push here: dropped, then the comments and Tempo
    done, prompts, _seen = asked_day(days, monkeypatch, ["ABC-2", "", "y", "", ""], late=[(at("11:00"), at("11:10"))])
    out = capsys.readouterr().out
    assert "─── 1 of 1 ── your windows ── 11:00-11:10" in out and len(prompts) == 5
    assert re.search(r"Comments   0h10 in worklogs\n  1  11:00  ABC-2  0h10", out) and "ABC-1  " not in out.split("Comments")[1]


def test_the_comments_show_what_jira_gets(days):
    you = model.Entry("ABC-1", at("09:00"), at("09:30"), "auto", ("me focus",))
    day = {"entries": [you, you._replace(streams=("claude s1",))], "comments": {}}
    assert reviewing.worklog_time(day, "ABC-1") == "0h30"  # an hour logged, the same card in two lanes


def test_the_plan_tells_the_time_its_worklogs_hold(capsys):
    sync.print_plan([push.Action(action, "ABC-1", at(hhmm), minutes, "", "1")
                     for action, hhmm, minutes in (("add", "09:00", 30), ("keep", "10:00", 15), ("delete", "11:00", 10),
                                                   ("foreign", "12:00", 20))])
    out = capsys.readouterr().out
    assert out.startswith("Jira   add 1 · delete 1 · keep 1 · foreign 1   → 0h45\n")  # what Jira will hold
    assert "  − delete   11:00-11:10  0h10  ABC-1" in out


def test_a_line_left_for_later_is_dropped_or_stops_before_the_comments(days, monkeypatch, capsys):
    # Q4 planning and news left for later, kept: no comments asked, nothing pushed
    done, prompts, _seen = asked_day(days, monkeypatch, ["", "", "", "", ""])
    out = capsys.readouterr().out
    assert done and "[Enter] later" in out and "Comments" not in out
    assert "Drop them (0h40) and go on? [y/N] " in prompts[-1] and "run `timelog 20.10` again" in out
    # then dropped: the comments, then the lines for Tempo
    done, prompts, _seen = asked_day(days, monkeypatch, ["", "", "y", "", "n"])  # the breaks aren't asked again
    assert "Comments" in capsys.readouterr().out
    assert [e.task for e in store.load_day(DATE)["entries"]] == ["ABC-1"]


def test_no_rule_is_offered_for_a_pr(days, monkeypatch, capsys):
    events = [mine("10:00", None, text="cosanum_odoo pull reques… - pop_odoo - Visual Studio Code", minutes=30)._replace(
        keys=("cosanum_odoo pull reques…",)), ev("11:00", None, "claude a2", minutes=10)._replace(
        keys=("cosanum_odoo pull request 3851",))]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    day, gaps, _notes = reviewing.daily.build_day(DATE, events)
    rows, labels = reviewing.review_rows(day, gaps, events)
    asked = [q for _row, q in reviewing.questions_of(DATE, day, rows, labels, events)]
    assert [(q.scopes, q.sections[0][1][0].split("  ", 1)[1]) for q in asked if not q.gap] == [
        ([], "cosanum_odoo pull request 3851"), ([], "cosanum_odoo pull request 3851")]  # the window's name made whole


def test_the_summaries_offered_are_whole_unless_a_width_is_set(monkeypatch):
    entries = [model.Entry("ABC-1", at("09:00"), at("09:30"), "auto", ("me focus",))]
    known = {"ABC-1": {"summary": "Address on reports does not fit in the window"}}
    for width, shown in ((None, "Address on reports does not fit in the window"), (12, "Address on …")):
        monkeypatch.setattr(config, "SUMMARY_WIDTH", width)
        assert reviewing.likely_cards(entries, at("09:30"), at("10:00"), known)[0] == ("ABC-1", shown, "just before")
    assert config.problems({"summary_width": 0}) == ["summary_width: 0 is not a number of characters above 0, or null"]
    assert config.problems({"summary_width": 60}) == config.problems({"summary_width": None}) == []


def test_the_comments_tell_when_and_where_each_task_was(days, monkeypatch, capsys):
    events = [mine("09:00", "ABC-1", text="shop_odoo", minutes=20)._replace(keys=("shop_odoo",)),
              mine("11:00", "ABC-1", text="Slack - dev", minutes=10)._replace(keys=("Slack - dev",))]
    day = {"entries": timeline.build(events, [], at("18:00"))[0], "comments": {}}
    where = reviewing.where_of(day, "ABC-1", events)
    assert where == "09:00-09:20, 11:00-11:10 · shop_odoo, Slack - dev"
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    questions.ask_comments([("ABC-1", "09:00", "0h30", "invoice export", where)], dt.timedelta(minutes=30))
    assert ("  1  09:00  ABC-1  0h30  invoice export\n" + " " * 25 + "09:00-09:20, 11:00-11:10 · shop_odoo, Slack - dev\n"
            in capsys.readouterr().out)


def test_the_questions_tell_what_little_there_is(days, monkeypatch):
    def glance(key, card, a, b):  # seconds after 10:00
        return mine("10:00", card, kind="browser")._replace(
            when=at("10:00") + dt.timedelta(seconds=a), until=at("10:00") + dt.timedelta(seconds=b), keys=(key,))
    events = [mine("09:00", "ABC-1", minutes=20)._replace(keys=("shop_odoo",)),
              ev("09:20", None, "afk", kind="away")._replace(until=at("10:00")),
              glance("Gmail", None, 0, 15), glance("Gmail", "XYZ-9", 15, 25), glance("Slack", None, 25, 45),
              mine("10:01", "ABC-2", minutes=30)._replace(keys=("stock_odoo",))]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    day, gaps, _notes = reviewing.daily.build_day(DATE, events)
    rows, labels = reviewing.review_rows(day, gaps, events)
    asked = {q.span.split(" ")[0]: {label: lines for label, lines in q.sections if lines}
             for _row, q in reviewing.questions_of(DATE, day, rows, labels, events)}
    assert asked["09:20-10:00"] == {"while it lasted": ["away from the keyboard the whole time"],
                                    "around it": ["until 09:20  shop_odoo (ABC-1)", "from  10:00  Gmail"]}
    # glances only: shown anyway, with the card a glance showed, which gives no time
    assert asked["10:00-10:01"] == {"what was open": [" 20s  Slack", " 15s  Gmail"],
                                    "cards shown": ["Gmail  XYZ-9 10s"],
                                    "around it": ["until 09:20  shop_odoo (ABC-1)", "from  10:01  stock_odoo (ABC-2)"]}
    assert reviewing.gist('"""\n<ide_selection>x</ide_selection>\ninvoice export') == "invoice export"
    assert reviewing.gist('<agent-message from="a1">report</agent-message>') == ""


def test_the_day_strip_shows_each_lane_and_the_question(monkeypatch):
    monkeypatch.setattr(strip.style, "columns", lambda: 100)
    entries = [model.Entry("ABC-1", at("09:00"), at("09:30"), "auto", ("me focus",)),
               model.Entry("BS-1", at("09:30"), at("09:50"), "auto", ("call",)),
               model.Entry("?", at("10:20"), at("10:21"), "auto", ("me focus",)),  # a minute to answer shows
               model.Entry("ABC-1", at("09:00"), at("09:20"), "auto", ("claude s1",))]
    away = [ev("09:50", None, "afk", kind="away")._replace(until=at("10:20"))]
    assert strip.lines("Tue 20.10", entries, away, [], mark=(at("10:20"), at("10:21"))) == [
        "Tue 20.10  09    10    11",
        "you        ▓▓▓██░░░?···",
        "agents     ▒▒··········",
        "                   ^"]


def test_the_comments_show_once_then_only_the_one_changed(monkeypatch, capsys):
    answers = ["1", "invoice export to the bank", ""]
    monkeypatch.setattr("builtins.input", lambda prompt: answers.pop(0))
    monkeypatch.setattr(questions, "prefilled", lambda prompt, text: answers.pop(0))
    changed = questions.ask_comments([("ABC-1", "09:00", "0h30", "invoice export", ""),
                                      ("DEF-2", "10:00", "1h00", "", "")], dt.timedelta(minutes=90))
    out = capsys.readouterr().out
    assert changed == {"ABC-1": "invoice export to the bank"}
    assert out == ("\nComments   1h30 in worklogs\n"
                   "  1  09:00  ABC-1  0h30  invoice export\n"
                   "  2  10:00  DEF-2  1h00  (none)\n"
                   "  1  09:00  ABC-1  0h30  invoice export to the bank  ✓ changed\n")


def test_the_day_strip_fits_the_terminal(monkeypatch):
    def strip_of(columns, start, end):
        monkeypatch.setattr(strip.style, "columns", lambda: columns)
        return strip.lines("Tue 20.10", [model.Entry("ABC-1", at(start), at(end), "auto", ("me focus",))], [], [])
    # an 11-hour day on 80 columns: 15 minutes a cell, and the last hour's label still on the line
    lines = strip_of(80, "08:00", "19:00")
    assert lines[0] == "Tue 20.10  08  09  10  11  12  13  14  15  16  17  18  19"
    assert max(len(line) for line in lines) + strip.INDENT <= 80
    # 30 minutes a cell: every second hour, so that the labels don't touch
    assert strip_of(60, "02:00", "22:00")[0] == "Tue 20.10  02  04  06  08  10  12  14  16  18  20  22"


def test_the_questions_name_each_window_s_app(days, monkeypatch):
    def piece(hhmm, minutes, key, app, card=None):
        return mine(hhmm, card, minutes=minutes)._replace(keys=(key,), text=key, app=app)
    events = [piece("09:20", 30, "ABC-1 board", "firefox", "ABC-1"),
              ev("09:50", None, "afk", kind="away")._replace(until=at("10:00")),  # no current card after it
              piece("10:00", 6, "Slack - dev", "slack"), piece("10:06", 4, "shop_odoo", "com.microsoft.VSCode"),
              piece("10:10", 20, "ABC-1 board", "firefox", "ABC-1")]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    day, gaps, _notes = reviewing.daily.build_day(DATE, events)
    rows, labels = reviewing.review_rows(day, gaps, events)
    [(_row, q)] = reviewing.questions_of(DATE, day, rows, labels, events)
    # Slack's name without "Slack - ", which its app says; the same page before and after, once, its
    # card left out as its name says it
    assert dict(q.sections) == {"what was open": ["  6m  Slack    dev", "  4m  VS Code  shop_odoo"],
                                "cards shown": [], "around it": ["until 09:50 and from 10:10  Firefox  ABC-1 board"],
                                "prompts": []}


def test_a_card_typed_is_offered_with_its_summary(monkeypatch, capsys):
    asked = [questions.Question("your windows", "09:00-09:10", [], [("ADM-1", "", "admin")], []),
             questions.Question("your windows", "10:00-10:10", [], [("ADM-1", "", "admin")], [])]
    answers = ["abc-7", ""]
    monkeypatch.setattr("builtins.input", lambda prompt: answers.pop(0))
    questions.ask(asked, summary=lambda task: {"ABC-7": "Invoice export"}.get(task, ""))
    assert "   2  ABC-7  Invoice export  given above\n" in capsys.readouterr().out


def test_the_tempo_lines_line_up_in_a_terminal_only(monkeypatch, capsys):
    lines = [push.Line("OPS-79", at("14:04"), 25, "daily"), push.Line("SHOP-2168", at("15:01"), 135, "review")]
    sync.print_worklogs(lines)
    assert capsys.readouterr().out.splitlines()[0].split("\t")[0] == "OPS-79"  # piped: only the value
    monkeypatch.setattr(sync.style, "on", lambda: True)
    sync.print_worklogs(lines)
    assert [line.split("\t")[0] for line in capsys.readouterr().out.splitlines()] == ["OPS-79   ", "SHOP-2168"]


def test_around_it_and_the_cards_shown_skip_what_says_nothing(monkeypatch):
    flash = mine("10:00", "XYZ-9", kind="browser")._replace(until=at("10:00") + dt.timedelta(seconds=0.5), keys=("Gmail",))
    assert reviewing.cards_shown([(at("10:00"), at("10:01"))], [flash]) == []  # under a second
    # a Meet tab before, its call after: the same page once
    tab = mine("09:50", "BS-1", kind="browser", minutes=10)._replace(keys=("Meet - Daily",), app="firefox")
    call = ev("10:10", "BS-1", "call", kind="call")._replace(until=at("10:30"), keys=("Meet - Daily",))
    assert reviewing.around(at("10:00"), at("10:10"), [tab, call]) == [
        "until 10:00 and from 10:10  Firefox  Meet - Daily (BS-1)"]


def test_a_call_s_page_open_as_a_tab_too_counts_its_time_once():
    call = ev("10:00", "BS-1", "call", kind="call")._replace(until=at("10:20"), keys=("Meet - Daily",))
    tab = mine("10:05", "BS-1", kind="browser", minutes=10)._replace(keys=("Meet - Daily",), app="firefox")
    entry = model.Entry("BS-1", at("10:00"), at("10:20"), "auto", ("call", "me browser"))
    assert reviewing.largest_pieces([entry], [entry], [call, tab])[0] == [("Meet - Daily", dt.timedelta(minutes=20))]


def test_a_card_is_left_out_only_when_the_name_says_it_as_a_word():
    page = mine("09:50", "ABC-1", kind="browser", minutes=10)._replace(keys=("ABC-12 follow-up",), app="firefox")
    assert reviewing.around(at("10:00"), at("10:10"), [page]) == ["until 10:00  Firefox  ABC-12 follow-up (ABC-1)"]
    assert reviewing.cards_shown([(at("09:50"), at("10:00"))], [page]) == ["ABC-12 follow-up  ABC-1 10m"]


def test_a_card_in_the_page_s_name_isn_t_repeated_and_a_rule_names_the_app(monkeypatch):
    jira_tab = mine("10:00", "SHOP-2168", kind="browser")._replace(until=at("10:00") + dt.timedelta(seconds=58),
                                                                    keys=("SHOP-2168",))
    assert reviewing.cards_shown([(at("10:00"), at("10:01"))], [jira_tab]) == ["SHOP-2168  58s"]
    window = mine("10:00", None)._replace(text="Release notes draft - shop_odoo - Visual Studio Code",
                                          keys=("Release notes draft",), app="com.microsoft.VSCode")
    [(what, rule)] = rules.scopes(window)
    assert what == 'the VS Code window "Release notes draft"'
    assert rule["regex"] == r"^Release\ notes\ draft\ \-\ shop_odoo\ \-\ Visual\ Studio\ Code$"
