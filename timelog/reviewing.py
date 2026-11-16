"""The review `timelog` runs: the days not pushed yet, then the day, as questions in the terminal or
in your editor, then the push"""
import argparse
import datetime as dt
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import defaultdict

from . import config
from . import daily
from . import jira
from . import jira_rest
from . import model
from . import questions
from . import review
from . import rules
from . import sources
from . import store
from . import style
from . import sync
from . import timeline


CATCH_UP_DAYS = 14
ONE_OFF = re.compile(r"pull request \d+|/pull/\d+|#\d+\b", re.I)  # a PR: a rule for it would never match again


def cmd_review(a):
    """the day as questions in the terminal (or in your editor), then sent to Jira if you answer y;
    without a date, the days of the last two weeks not pushed yet are offered first, the oldest first, since an answer only helps the days
    after it"""
    for problem in config.PROBLEMS:
        style.note(f"config: {problem}", "yellow")
    jira_rest.available()  # unlocks the token: a password manager asks now, not in the middle of a day
    if not a.date_given:
        with style.working("checking the last two weeks for days not pushed"):
            days = pending_days(dt.date.fromisoformat(a.date))
        if days:
            fresh = [f"{daily.short_date(date)} ({daily.fmt(daily.logged(day))})" for date, day, late in days if not late]
            since = [f"{daily.short_date(date)} ({daily.fmt(sum((b - e for e, b in late), dt.timedelta()))} since)"
                     for date, _day, late in days if late]
            if fresh:
                style.note(f"not pushed yet: {', '.join(fresh)}", "yellow", "bold")
            if since:
                style.note(f"pushed, with work after the push: {', '.join(since)}", "yellow", "bold")
            if ask(f"Review {'it' if len(days) == 1 else 'them'} first? [Y/n] ", default=True):
                for date, _day, late in days:
                    after = ", only the work after the push" if late else ""
                    print(style.paint(f"\n── {daily.short_date(date)}{after} ──", "bold"))
                    if not review_day(argparse.Namespace(**{**vars(a), "date": date, "late": late})):
                        return
                print(style.paint(f"\n── {daily.short_date(a.date)}, today ──", "bold"))
    review_day(a)




def pending_days(today):
    """(date, day, late) of the days before today, up to CATCH_UP_DAYS and from start_date, that weren't
    handed over: never pushed nor entered in Tempo, or with daily.LATE_WORK at a stretch that the day
    handed over doesn't have. A day handed over isn't reviewed again for the rest: rebuilt by a
    later timelog, it could move worklogs you sent. `late`: for a day handed over, the stretches
    worked that it doesn't have, the only ones its review asks about; None otherwise"""
    cards = model.card_regex()
    out = []
    for back in range(CATCH_UP_DAYS, 0, -1):
        if config.START_DATE and today - dt.timedelta(days=back) < config.START_DATE:
            continue
        date = (today - dt.timedelta(days=back)).isoformat()
        saved = store.load_day(date)
        handed = saved and (saved["pushed"] or saved.get("entered"))
        if handed and saved_at(date, saved) >= midnight_after(date):
            continue
        day, _gaps, _notes = daily.build_day(date, sources.day_events(date, cards), rebuild=True)
        if not day["entries"]:
            continue
        if handed:
            late = daily.late_work(day["entries"], saved["entries"])
            if late:
                out.append((date, daily.build_day(date, sources.day_events(date, cards))[0], late))
            continue
        out.append((date, day, None))
    return out


def saved_at(date, day):
    """when the day was last saved; the file's time for the days saved before it was kept"""
    if day.get("saved"):
        return dt.datetime.fromisoformat(day["saved"])
    return dt.datetime.fromtimestamp(os.path.getmtime(store.day_path(date))).astimezone()


def midnight_after(date):
    return model.at((dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat(), "00:00")


def ask(question, default=False):
    try:
        answer = input(question).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes") or (default and not answer)


def interactive(a):
    """questions in the terminal, unless --edit or no terminal to ask in"""
    return not getattr(a, "edit", False) and sys.stdin.isatty()


def review_day(a):
    """one day as questions in the terminal, or in your editor, then sent to Jira if you answer y;
    False when you cancelled or stopped"""
    with style.working(f"reading ActivityWatch for {daily.short_date(a.date)}"):
        events = sources.day_events(a.date, model.card_regex())
        day, gaps, _notes = daily.build_day(a.date, events)
    store.save_day(a.date, day, quiet=True)
    if jira_rest.available():  # the cards of the day just built, for their comments and the candidates
        sync.cmd_fetch(a)
        with style.working("looking up the day's PRs on GitHub"):
            daily.fill_comments(day, events, a.date)
    stop = None
    if interactive(a):
        rows, labels = review_rows(day, gaps, events)
        late = getattr(a, "late", None)
        asked = questions_of(a.date, day, rows, labels, events, late)
        completing_cards(a.date, day)
        try:
            answers, new_rules = questions.ask([q for _row, q in asked])
        except questions.Stop as s:
            answers, new_rules, stop = s.answers, s.rules, s.reason
        apply_answers(a, day, events, [(row, answers[n]) for n, (row, _q) in enumerate(asked) if n in answers])
        for rule in new_rules:
            daily.add_rule(rule)
        if stop == "quit":
            print("# stopped: your answers are kept, nothing sent")
            return False
        if stop is None and not settled(a, day):
            return True
        if stop is None:
            tasks = {e.task for e in day["entries"] if not late or overlaps(e.start, e.end, late)}
            lines = [(t, worklog_time(day, t), daily.comment_of(day, t) or "", where_of(day, t, events))
                     for t in sorted(tasks) if not model.is_unattributed(t)]
            if lines:
                daily.correct(day, {}, set(), comments=questions.ask_comments(lines))
            with style.working("looking up the day's PRs on GitHub"):
                daily.fill_comments(day, events, a.date)
            store.save_day(a.date, day, quiet=True)
        else:  # the editor, on the day rebuilt with your answers
            day, gaps, _notes = daily.build_day(a.date, events)
    if stop == "edit" or not interactive(a):
        if not edit_day(a, day, gaps, events):
            return False
    todo = [e for e in day["entries"] if model.is_unattributed(e.task)]
    if todo:
        for e in todo:
            print(f"{e.start:%H:%M}-{e.end:%H:%M} {daily.minutes(e):4}m  {style.task(e.task)}")
        style.note(f"{len(todo)} entries still without a card: run `timelog {daily.short_date(a.date).split()[1]}` "
                   "again once you know", "yellow")
        return True
    entries = day["entries"]
    print(style.paint("logged ", "bold") + daily.logged_over(entries))
    finish_review(a, day)
    return True


def settled(a, day):
    """False when lines are left without a card and you keep them for later: nothing else is asked
    then, since the day can't be pushed; or they're dropped, as when it was something personal"""
    todo = [i for i, e in enumerate(day["entries"], 1) if model.is_unattributed(e.task)]
    if not todo:
        return True
    entries = [day["entries"][i - 1] for i in todo]
    for e in entries:
        print(f"{e.start:%H:%M}-{e.end:%H:%M} {daily.minutes(e):4}m  {style.task(e.task)}")
    spent = daily.fmt(sum((e.end - e.start for e in entries), dt.timedelta()))
    if ask(f"Still without a card, so the day can't be pushed. Drop {'it' if len(todo) == 1 else 'them'} "
           f"({spent}) and go on? [y/N] "):
        daily.correct(day, {}, set(todo))
        store.save_day(a.date, day, quiet=True)
        return True
    style.note(f"run `timelog {daily.short_date(a.date).split()[1]}` again once you know", "yellow")
    return False


def overlaps(start, end, spans):
    return any(start < b and end > a for a, b in spans)


def worklog_time(day, task):
    """what Jira gets for a task: its parallel lanes once, rounded"""
    lines = sync.day_worklogs(day, [e for e in day["entries"] if e.task == task], model.card_regex())
    return daily.fmt(dt.timedelta(minutes=sum(w.minutes for w in lines)))


def apply_answers(a, day, events, answered):
    """the cards you gave to the review's rows: an entry or agent line takes it, a gap becomes a
    manual entry"""
    renames, added, breaks = {}, [], []
    for row, task in answered:
        if row.kind == "gap":
            span = (model.at(a.date, row.start), model.at(a.date, row.end))
            if task == config.DROP:
                breaks.append(span)
            else:
                added.append(model.Entry(task, *span, "manual", ("manual",)))
        else:
            renames.update({i: task for i in row.indices or (row.index,)})
    daily.correct(day, renames, set(), added=added, breaks=breaks)
    saved_then_fetched(a, day, events)  # the cards you gave


def saved_then_fetched(a, day, events):
    """the day saved, then Jira asked about its cards it doesn't know yet: the fetch reads the file"""
    daily.fill_comments(day, events, a.date)
    store.save_day(a.date, day, quiet=True)
    if jira_rest.available() and jira.unknown({e.task for e in day["entries"]}, a.date, model.card_regex()):
        sync.cmd_fetch(a)
        daily.fill_comments(day, events, a.date)
        store.save_day(a.date, day, quiet=True)


def edit_day(a, day, gaps, events):
    """the day in your editor, its changes applied; False when you cancelled"""
    rows, text = review_file(a.date, day, gaps, events)
    while True:
        edited = edit(text)
        try:
            changes = review.parse(edited, rows)
            break
        except review.Invalid as e:  # back to the editor if you want, with what went wrong on top
            style.note(f"ERROR {e}", "red", "bold")
            if not ask("Back to the editor to fix it? [Y/n] ", default=True):
                changes = None
                break
            text = f"# ERROR {e}\n" + "".join(l + "\n" for l in edited.splitlines() if not l.startswith("# ERROR"))
    if changes is None:
        print("# cancelled: nothing changed")
        return False
    renames, drops, added, comments, keep = changes
    kept = [(tuple(day["entries"][i - 1] for i in indices), task) for indices, task in keep]
    breaks = [(model.at(a.date, s), model.at(a.date, e)) for s, e, task in added if task == config.DROP]
    added = [model.Entry(task, model.at(a.date, s), model.at(a.date, e), "manual", ("manual",))
             for s, e, task in added if task != config.DROP]
    comments = {t: c for t, c in comments.items() if c != daily.comment_of(day, t)}
    daily.correct(day, renames, drops, comments=comments, added=added, breaks=breaks)
    saved_then_fetched(a, day, events)  # cards you gave in the editor
    daily.remember(a.date, kept)
    return True


def questions_of(date, day, rows, labels, events, late=None):
    """(row, question) for each row to answer, in order of time: your lines and agent sessions
    without a card, and the stretches with nothing active; with `late`, only those within it"""
    entries, out = day["entries"], []
    known = (jira.load(date) or {"known": {}})["known"]
    names = daily.whole_names(events)
    for row, label in zip(rows, labels):
        if row.kind != "gap" and not model.is_unattributed(row.task):
            continue
        line = [entries[i - 1] for i in row.indices or (row.index,)] if row.kind != "gap" else []
        if row.kind == "gap":
            start, end = model.at(date, row.start), model.at(date, row.end)
            title = f"{row.start}-{row.end}  {daily.fmt(end - start)}  nothing active: a break, or what you did?"
        else:
            start, end = min(e.start for e in line), max(e.end for e in line)
            spent = daily.fmt(sum((e.end - e.start for e in line), dt.timedelta()))
            title = (f"agent {row.session}  {spent}  {style.task(row.task)}" if row.kind == "agent"
                     else f"{row.start}-{row.end}  {spent}  {style.task(row.task)}")
        if late and not overlaps(start, end, late):
            continue
        details = what_it_holds(line, entries, events)
        piece = daily.dominant(line, events) if line else None
        name = daily.full_name(piece.keys[0], names) if piece and piece.keys else ""
        scopes = rules.scopes(piece) if piece and not ONE_OFF.search(f"{piece.text} {name}") else []
        out.append((start, row, questions.Question(title, details, likely_cards(entries, start, end, known),
                                                   scopes, row.kind == "gap", name)))
    return [(row, q) for _start, row, q in sorted(out, key=lambda x: x[0])]


def largest_pieces(line, entries, events):
    """(label, time) of the three largest pages, windows and sessions of a line's entries, glances
    left out, and its prompts"""
    held, prompts = defaultdict(dt.timedelta), []
    for entry in line:
        pieces, said = daily.entry_pieces(entry, entries, events)
        for (_stream, label), spent in pieces:
            held[label] += spent
        prompts += said
    return [(label, spent) for label, spent in sorted(held.items(), key=lambda kv: -kv[1])[:3]
            if spent >= timeline.GLANCE], prompts


def what_it_holds(line, entries, events):
    """the largest pieces of a line's entries, and its prompts"""
    largest, prompts = largest_pieces(line, entries, events)
    return [f"{round(spent.total_seconds() / 60):3}m  {label}" for label, spent in largest] + [
        f"prompt {style.clip(repr(p.text.strip().splitlines()[0] if p.text.strip() else ''), 160)}"
        for p in prompts[:2]]


def where_of(day, task, events):
    """when a task's time was, and its largest pages, windows and sessions: what its comment is about"""
    line = [e for e in day["entries"] if e.task == task]
    spans = timeline.merge([(e.start, e.end, set()) for e in line], config.JOIN_GAP)
    when = ", ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b, _streams in spans[:3]) + (
        f" and {len(spans) - 3} more" if len(spans) > 3 else "")
    largest, _prompts = largest_pieces(line, day["entries"], events)
    return " · ".join(part for part in (when, ", ".join(style.clip(label, 40) for label, _spent in largest)) if part)


def likely_cards(entries, start, end, known):
    """(task, why) of the cards you were probably on: the agents working then, the cards just before
    and after, then admin and tooling"""
    cards = model.card_regex()
    found = []
    attributed = [e for e in entries if not model.is_unattributed(e.task) and e.task != config.DROP]
    for e in attributed:
        lane = model.lane_of(e.streams)
        if lane != "you" and e.start < end and e.end > start:
            found.append((e.task, f"{lane} working then"))
    before = max((e for e in attributed if e.end <= start + dt.timedelta(minutes=1)), key=lambda e: e.end, default=None)
    after = min((e for e in attributed if e.start >= end - dt.timedelta(minutes=1)), key=lambda e: e.start,
                default=None)
    found += [(e.task, why) for e, why in ((before, "just before"), (after, "just after")) if e]
    found += [(config.TRACKER.get(k), k.split("_")[0]) for k in ("admin_card", "tooling_card") if config.TRACKER.get(k)]
    out, seen = [], set()
    for task, why in found:
        if task not in seen:
            seen.add(task)
            summary = known.get(jira.card_of_task(task, cards), {}).get("summary", "")
            out.append((task, f"{style.clip(summary, config.SUMMARY_WIDTH)} · {why}" if summary else why))
    return out[:6]


def completing_cards(date, day):
    """Tab completes the day's cards, the ones Jira knows and the candidates, admin and tooling"""
    answer = jira.load(date) or {"known": {}, "candidates": {}}
    questions.completing([e.task for e in day["entries"] if not model.is_unattributed(e.task)]
                         + list(answer["known"]) + list(answer["candidates"])
                         + [config.TRACKER.get(k) for k in ("admin_card", "tooling_card") if config.TRACKER.get(k)])


def review_file(date, day, gaps, events):
    """the rows of the review file, and its text"""
    entries = day["entries"]
    rows, labels = review_rows(day, gaps, events)
    comments = {t: daily.comment_of(day, t) or "" for t in {e.task for e in entries} if not model.is_unattributed(t)}
    candidates = []
    if any(model.is_unattributed(e.task) for e in entries):
        candidates = sorted((jira.load(date) or {"candidates": {}})["candidates"].items())[:10]
    logged = sum((e.end - e.start for e in entries), dt.timedelta())
    return rows, review.render(date, rows, labels, comments, candidates, daily.fmt(logged), daily.fmt(daily.wall_clock(entries)))


def review_rows(day, gaps, events):
    """the review's rows and what each is made of: a line per agent session and task, then your
    entries grouped per task while less than JOIN_GAP apart (per window for those to answer), and
    the gaps"""
    entries = day["entries"]

    def name(e):  # what an entry is mostly made of
        held, _prompts = daily.entry_pieces(e, entries, events)
        return "" if e.task.startswith(f"{config.UNATTRIBUTED} ") else held[0][0][1] if held else ""

    sessions = defaultdict(list)  # the agents' entries, per session and task, in order of their first
    groups, open_ = [], {}  # yours: [task, source, [entry numbers], name]
    for i, e in sorted(enumerate(entries, 1), key=lambda ie: ie[1].start):
        lane = model.lane_of(e.streams)
        if lane != "you":
            sessions[(lane.removeprefix("agent "), e.task)].append(i)
            continue
        label = name(e)
        key = (e.task, e.source, label if model.is_unattributed(e.task) else "")
        group = open_.get(key)
        if group and e.source == "auto" and e.start - entries[group[2][-1] - 1].end <= config.JOIN_GAP:
            group[2].append(i)
            if daily.minutes(e) > max(daily.minutes(entries[j - 1]) for j in group[2][:-1]):
                group[3] = label  # named after its largest entry
            continue
        open_[key] = [e.task, e.source, [i], label]
        groups.append(open_[key])
    rows, labels = [], []
    for (session, task), indices in sessions.items():
        rows.append(review.Row("", "", task, "agent", 0, tuple(indices), session))
        total = sum(daily.minutes(entries[i - 1]) for i in indices)
        labels.append(f"{daily.fmt(dt.timedelta(minutes=total))} {name(entries[indices[0] - 1])}".rstrip())
    yours = []
    for task, _source, indices, label in groups:
        first, last = entries[indices[0] - 1], entries[indices[-1] - 1]
        if len(indices) > 1:  # its span holds other lines: its own time is its minutes
            label = f"{daily.fmt(dt.timedelta(minutes=sum(daily.minutes(entries[i - 1]) for i in indices)))} {label}".rstrip()
        yours.append((review.Row(f"{first.start:%H:%M}", f"{last.end:%H:%M}", task, "entry", indices[0],
                                 tuple(indices)), label))
    yours += [(review.Row(f"{g0:%H:%M}", f"{g1:%H:%M}", config.UNATTRIBUTED, "gap", 0), review.GAP) for g0, g1 in gaps]
    for row, label in sorted(yours, key=lambda rl: (rl[0].start, rl[0].index)):
        rows.append(row)
        labels.append(label)
    return rows, labels


def edit(text):
    """the text once you've edited it in $VISUAL or $EDITOR"""
    with tempfile.NamedTemporaryFile("w", suffix=".timelog", delete=False) as f:
        f.write(text)
    try:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        if subprocess.run([*shlex.split(editor), f.name]).returncode != 0:
            raise SystemExit("# the editor failed: nothing changed")
        with open(f.name) as f2:
            return f2.read()
    finally:
        os.remove(f.name)


def finish_review(a, day):
    """the push plan, sent to Jira once you agree; without Jira, the lines to enter by hand"""
    if jira_rest.unavailable() and jira_rest.FAILED:  # set up, but the token didn't come: try again later
        style.note(f"{jira_rest.unavailable()}. Nothing sent: run `timelog` again once it works", "yellow")
        return
    if not jira_rest.available():
        print("# Jira isn't set up: enter these worklogs in Tempo")
        lines = sync.worklog_lines(a, day)
        sync.print_worklogs(lines)
        if ask("Entered them in Tempo? [y/N] "):  # the day is done: not offered again
            day["entered"] = sync.snapshot(lines)
            store.save_day(a.date, day, quiet=True)
        return
    client = jira_rest.Client()  # one client and one read of Jira: the plan shown is the one sent
    actions = sync.push_plan(a, day, client)
    if not any(x.action in ("add", "update", "delete") for x in actions):
        print("# Jira already has these worklogs")
        return
    sync.print_plan(actions)
    if ask("Send them to Jira? [y/N] "):
        sync.send(a.date, day, client, actions)
