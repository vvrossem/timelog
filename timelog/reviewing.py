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
from . import strip
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
        late = getattr(a, "late", None)
        with style.working("preparing the questions"):
            rows, labels = review_rows(day, gaps, events)
            asked = questions_of(a.date, day, rows, labels, events, late)
        completing_cards(a.date, day)
        when = daily.short_date(a.date)

        def day_strip(mark=None, answers=None):
            return strip.lines(when, answered(day["entries"], asked, answers or {}), events, gaps, mark)
        if asked:
            print()
            for line in day_strip():
                print(f"  {line}")
            print(style.paint(f"  {' ' * strip.LABEL}{strip.LEGEND}", "dim"))
        try:
            known = (jira.load(a.date) or {"known": {}})["known"]
            answers, new_rules = questions.ask([q for _row, q in asked], day_strip, lambda t: summary_of(t, known))
        except questions.Stop as s:
            answers, new_rules, stop = s.answers, s.rules, s.reason
        if asked and stop is None:  # the day as you answered it
            print("\n" + style.paint("─── the day as answered " + "─" * (questions.width() - 24), "dim"))
            for line in day_strip(None, answers):
                print(f"  {line}")
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
            first = {t: min(e.start for e in day["entries"] if e.task == t) for t in tasks}
            lines = [(t, f"{first[t]:%H:%M}", worklog_time(day, t), daily.comment_of(day, t) or "",
                      where_of(day, t, events))
                     for t in sorted(tasks, key=lambda t: (first[t], t)) if not model.is_unattributed(t)]
            if lines:
                daily.correct(day, {}, set(), comments=questions.ask_comments(
                    lines, sync.worklogs_time(day, {t for t, *_rest in lines})))
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
    print()
    daily.print_logged(day["entries"], sync.worklogs_time(day))
    finish_review(a, day)
    return True


def answered(entries, asked, answers):
    """the entries as the answers so far make them, for the strip: an entry takes its card or is
    dropped, a stretch with nothing active given a card is one"""
    tasks = {}
    added = []
    for n, task in answers.items():
        row, q = asked[n]
        if row.kind == "gap":
            if task != config.DROP:
                added.append(model.Entry(task, *q.mark, "manual", ("manual",)))
            continue
        tasks |= {i: task for i in row.indices or (row.index,)}
    return [e._replace(task=tasks.get(i, e.task)) for i, e in enumerate(entries, 1)
            if tasks.get(i) != config.DROP] + added


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
    apps = apps_of(events)
    for row, label in zip(rows, labels):
        if row.kind != "gap" and not model.is_unattributed(row.task):
            continue
        line = [entries[i - 1] for i in row.indices or (row.index,)] if row.kind != "gap" else []
        if row.kind == "gap":
            start, end = model.at(date, row.start), model.at(date, row.end)
            spent = end - start
        else:
            start, end = min(e.start for e in line), max(e.end for e in line)
            spent = sum((e.end - e.start for e in line), dt.timedelta())
        if late and not overlaps(start, end, late):
            continue
        if row.kind == "gap":
            kind, sections = "nothing active", what_the_gap_holds(start, end, events, apps)
        else:
            kind = (f"agent {row.session}" if row.kind == "agent"
                    else "call" if any("call" in e.streams for e in line) else "your windows")
            sections = what_it_holds(line, entries, events, apps)
            if row.kind != "agent":
                sections[1:1] = [("cards shown", cards_shown([(e.start, e.end) for e in line], events)),
                                 ("around it", around(start, end, events, apps))]
        piece = daily.dominant(line, events) if line else None
        name = daily.full_name(piece.keys[0], names) if piece and piece.keys else ""
        scopes = rules.scopes(piece) if piece and not ONE_OFF.search(f"{piece.text} {name}") else []
        out.append((start, row, questions.Question(
            kind, f"{start:%H:%M}-{end:%H:%M} · {daily.fmt(spent)}", sections,
            likely_cards(entries, start, end, known), scopes, row.kind == "gap", name, (start, end))))
    return [(row, q) for _start, row, q in sorted(out, key=lambda x: x[0])]


def largest_pieces(line, entries, events):
    """(label, time) of the three largest pages, windows and sessions of a line's entries, glances
    left out unless that's all there is, and its prompts"""
    spans, prompts = defaultdict(list), []
    for entry in line:
        pieces, said = daily.entry_spans(entry, entries, events)
        for (_stream, label), held in pieces.items():
            spans[label] += held
        prompts += said
    held = {label: daily.spent(s) for label, s in spans.items()}
    ranked = sorted(((k, v) for k, v in held.items() if v >= dt.timedelta(seconds=1)), key=lambda kv: -kv[1])
    return ([kv for kv in ranked if kv[1] >= timeline.GLANCE] or ranked)[:3], prompts


def spent_of(spent):
    """4 characters: minutes, or seconds under a minute"""
    seconds = round(spent.total_seconds())
    return f"{round(seconds / 60):3}m" if seconds >= 60 else f"{seconds:3}s"


# what an IDE or an agent adds to a prompt (a subagent's report comes as one): not what you wrote
WRAPPED = re.compile(r"<(ide_[a-z_]+|system-reminder|agent-message)\b[^>]*>.*?(?:</\1>|\Z)", re.S)
OPENED = re.compile(r"<ide_opened_file>The user opened the file (\S+)")


def gist(text):
    """a prompt's first line with words in it, without what the IDE or an agent added; else the file
    the IDE says was open"""
    said = re.sub(r"<[^>]+>", " ", WRAPPED.sub(" ", text))
    line = next((line.strip() for line in said.splitlines() if re.search(r"\w", line)), "")
    opened = OPENED.search(text)
    return line or (f"(in {opened.group(1).replace(os.path.expanduser('~'), '~', 1)})" if opened else "")


def prompt_lines(prompts):
    said = [g for g in (gist(p.text) for p in prompts) if g]
    return [style.clip(repr(g), 160) for g in said[:2]]


def apps_of(events):
    """{piece's name: the app it was in longest}, cut-short names made whole too"""
    held = defaultdict(lambda: defaultdict(dt.timedelta))
    for e in events:
        if e.app and e.keys and e.until:
            held[e.keys[0]][model.app_name(e.app)] += e.until - e.when
    names = daily.whole_names(events)
    out = {name: max(apps, key=apps.get) for name, apps in held.items()}
    return out | {daily.full_name(name, names): app for name, app in out.items()}


def names_card(name, card):
    """the name says the card, as a word: ABC-1 isn't in "ABC-12 follow-up" """
    return bool(re.search(rf"\b{re.escape(card)}\b", name))


def app_columns(rows):
    """rows of (head, app, name) as lines, the apps in a column of their own when any has one"""
    width = max((len(app) for _head, app, _name in rows), default=0)
    return [f"{head}  {app:<{width}}  {model.without_app(name, app)}" if width else f"{head}  {name}"
            for head, app, name in rows]


def what_it_holds(line, entries, events, apps=None):
    """[(what was open, its largest pieces, each with its app), (prompts, the first two)]"""
    apps = apps_of(events) if apps is None else apps
    largest, prompts = largest_pieces(line, entries, events)
    return [("what was open", app_columns([(spent_of(spent), apps.get(label, ""), label) for label, spent in largest])),
            ("prompts", prompt_lines(prompts))]


def cards_shown(spans, events, most=4):
    """the cards your windows and tabs showed within the spans, glances too, which give no time: a
    line per page or window that showed them, the longest first, as a mailbox shows many in passing"""
    shown = defaultdict(lambda: defaultdict(dt.timedelta))  # where -> card -> time
    for e in events:
        if not (e.stream.startswith("me ") and e.card and e.until):
            continue
        for a, b in spans:
            overlap = min(e.until, b) - max(e.when, a)
            if overlap > dt.timedelta(0):
                shown[e.keys[0] if e.keys else ""][e.card] += overlap
    shown = {w: {c: t for c, t in cards.items() if t >= dt.timedelta(seconds=1)} for w, cards in shown.items()}
    shown = {w: cards for w, cards in shown.items() if cards}  # not a flash under a second
    places = sorted(shown, key=lambda w: -sum(shown[w].values(), dt.timedelta()))
    width = min(max((len(w) for w in places), default=0), 40)
    out = []
    for where in places:
        cards = sorted(shown[where], key=lambda c: -shown[where][c])
        text = " · ".join((f"{c} " if not names_card(where, c) else "") + spent_of(shown[where][c]).strip()
                          for c in cards[:most])
        more = f" · {len(cards) - most} more" if len(cards) > most else ""
        out.append(f"{style.clip(where, width):<{width}}  {text}{more}")
    return out


def around(start, end, events, apps=None):
    """what you had open just before and just after: once when it's the same, without its card
    when its name says it"""
    apps = apps_of(events) if apps is None else apps
    yours = [e for e in events if e.until and e.until > e.when and (e.stream.startswith("me ") or e.kind == "call")
             and e.kind not in ("presence", "away")]
    slack = dt.timedelta(seconds=30)  # the span's ends are rounded to the minute
    before = max((e for e in yours if e.when < start and e.until <= start + slack), key=lambda e: e.until, default=None)
    after = min((e for e in yours if e.until > end and e.when >= end - slack), key=lambda e: e.when, default=None)
    names = daily.whole_names(events)

    def named(e):
        name = daily.full_name(e.keys[0], names) if e.keys else e.text.split("|")[0]
        app = "call" if e.kind == "call" else model.app_name(e.app) or apps.get(name, "")
        return app, name + (f" ({e.card})" if e.card and not names_card(name, e.card) else "")
    if before and after and named(before)[1] == named(after)[1]:  # the same page, as a tab then a call too
        return app_columns([(f"until {before.until:%H:%M} and from {after.when:%H:%M}", *named(before))])
    return app_columns([(f"{what} {e.until if e is before else e.when:%H:%M}", *named(e))
                    for e, what in ((before, "until"), (after, "from ")) if e])


def what_the_gap_holds(start, end, events, apps=None):
    """[(while it lasted, when you were away from the keyboard), (around it, what you had open just
    before and after), (prompts, those you wrote then)]"""
    away = timeline.merge([(max(e.when, start), min(e.until, end), set()) for e in events
                           if e.kind == "away" and e.until and e.when < end and e.until > start])
    held = sum((b - a for a, b, _ in away), dt.timedelta())
    if held >= end - start - dt.timedelta(minutes=1):
        lasted = ["away from the keyboard the whole time"]
    else:
        lasted = [f"away from the keyboard {a:%H:%M}-{b:%H:%M}" for a, b, _ in away]
        lasted.append(f"{daily.fmt(end - start - held)} with nothing recorded: the computer off, asleep or locked?"
                      if away else "nothing recorded: the computer off, asleep or locked?")
    return [("while it lasted", lasted), ("around it", around(start, end, events, apps)),
            ("prompts", prompt_lines([e for e in events if e.kind == "claude" and start <= e.when < end]))]


def where_of(day, task, events):
    """when a task's time was, and its largest pages, windows and sessions: what its comment is about"""
    line = [e for e in day["entries"] if e.task == task]
    spans = timeline.merge([(e.start, e.end, set()) for e in line], config.JOIN_GAP)
    when = ", ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b, _streams in spans[:3]) + (
        f" and {len(spans) - 3} more" if len(spans) > 3 else "")
    largest, _prompts = largest_pieces(line, day["entries"], events)
    return " · ".join(part for part in (when, ", ".join(style.clip(label, 40) for label, _spent in largest)) if part)


def likely_cards(entries, start, end, known):
    """(task, its Jira summary, why) of the cards you were probably on: the agents working then, the cards just before
    and after, then admin and tooling"""
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
            out.append((task, summary_of(task, known), why))
    return out[:6]


def summary_of(task, known):
    """a task's card's Jira summary, as wide as `summary_width` allows; "" when Jira wasn't asked"""
    summary = known.get(jira.card_of_task(task, model.card_regex()), {}).get("summary", "")
    return style.clip(summary, config.SUMMARY_WIDTH)


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
    print()
    sync.print_plan(actions, a.date)
    if ask("Send them to Jira? [y/N] "):
        sync.send(a.date, day, client, actions)
