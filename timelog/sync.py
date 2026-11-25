"""Jira: what it knows of the day's cards (`fetch`), and the worklogs sent to it (`push`)"""
import datetime as dt
import json
import sys
from collections import defaultdict

from . import config
from . import daily
from . import jira
from . import jira_rest
from . import model
from . import push
from . import sources
from . import store
from . import style
from . import timeline


def day_cards(date):
    """the card keys of the day: the day file's tasks, or a fresh build's"""
    cards = model.card_regex()
    day = store.load_day(date)
    tasks = {e.task for e in day["entries"]} if day else set()
    if not day:
        events = sources.day_events(date, cards)
        tasks = {e.task for e in timeline.build(events, [], daily.day_end(date))[0]}
    return sorted({jira.card_of_task(t, cards) for t in tasks if not model.is_unattributed(t)})


def cmd_fetch(a):
    """asks Jira for the day's cards, the candidates and your worklogs, into jira-<date>.json"""
    if not jira_rest.available():
        style.note(f"{jira_rest.unavailable()}: carry on, cards just aren't checked", "yellow")
        return
    with style.working("asking Jira for the day's cards and worklogs"):
        answer = jira_rest.fetch(jira_rest.Client(), a.date, day_cards(a.date), config.TRACKER["review_jql"])
    jira.save(a.date, answer)
    print(f"# Jira: {len(answer['known'])} cards known, {len(answer['candidates'])} candidates,"
          f" {len(answer['worklogs'])} worklogs of yours that day")


def worklog_lines(a, day):
    """the day's worklogs, one per entry; refuses unattributed entries and cards Jira doesn't know"""
    cards = model.card_regex()
    todo = [e for e in day["entries"] if model.is_unattributed(e.task)]
    if todo:
        raise SystemExit(f"# {len(todo)} unattributed entries: set their card first")
    missing = jira.unknown({e.task for e in day["entries"]}, a.date, cards)
    if missing:
        raise SystemExit(f"# not in Jira: {', '.join(missing)}: fix these tasks first")
    if missing is None:
        print("# Jira not asked: card keys unchecked", file=sys.stderr)
    retired = sorted({jira.card_of_task(e.task, cards) for e in day["entries"]} & config.RETIRED_CARDS)
    if retired:
        raise SystemExit(f"# retired cards: {', '.join(retired)}: move their entries first")
    return day_worklogs(day, day["entries"], cards)


def day_worklogs(day, entries, cards):
    """one worklog per card: from its first entry, the time anything worked on it (its parallel
    lanes count once), its tasks' comments, rounded"""
    per_card = defaultdict(list)
    for e in sorted(entries, key=lambda e: e.start):
        per_card[jira.card_of_task(e.task, cards)].append(e)
    lines = []
    for card, es in per_card.items():
        comments = (daily.comment_of(day, e.task) or (e.task.split(": ", 1)[1] if ": " in e.task else "")
                    for e in es)
        lines.append(push.Line(card, es[0].start, int(daily.wall_clock(es).total_seconds()) // 60,
                               join_comments(comments)))
    return push.round_lines(lines, config.ROUND, config.ROUND_MODE)


def join_comments(comments):
    """the comments of a card's tasks, each without the comma-separated parts an earlier one said"""
    seen, out = set(), []
    for comment in comments:
        parts = [p for p in (comment or "").split(", ") if p and p.lower() not in seen]
        seen |= {p.lower() for p in parts}
        if parts:
            out.append(", ".join(parts))
    return "; ".join(out)


def jira_started(when):
    return f"{when:%Y-%m-%dT%H:%M:%S.000%z}"


def print_worklogs(lines):
    """one tab-separated line per worklog, to enter them in Tempo"""
    for line in lines:
        end = line.started + dt.timedelta(minutes=line.minutes)
        print(f"{line.card}\t{line.started:%H:%M}-{end:%H:%M}\t{line.minutes}m\t{jira_started(line.started)}\t{line.comment}")


def cmd_push(a):
    """makes Jira's worklogs match the day, as `git push` does; -n (--dry-run) and --json only show
    the plan, --record stores what the session pushed through the connector"""
    day = daily.require_day(a.date)
    if a.record:
        with open(a.record) as f:
            sent = json.load(f)
        day["pushed"] = push.record(day["pushed"], sent)
        store.save_day(a.date, day)
        jira.record_sent(a.date, sent)
        return
    dry = a.dry_run or a.json
    if not dry and not jira_rest.available():
        raise SystemExit(f"# {jira_rest.unavailable()}: nothing sent; `timelog push -n` shows the plan")
    client = jira_rest.Client() if jira_rest.available() else None  # Jira as it is now, also for a dry run
    actions = push_plan(a, day, client)
    if not dry:
        if not any(x.action in ("add", "update", "delete") for x in actions):
            style.note("Jira already has these worklogs")
            return
        send(a.date, day, client, actions)
        return
    if a.json:
        print(json.dumps([{**x._asdict(), "started": jira_started(x.started)} for x in actions], indent=1))
        return
    print_plan(actions)


def print_plan(actions, date=None):
    """a title with the counts and what Jira will hold, then a line per worklog in order of time"""
    if not actions:
        style.note("nothing to push: the day has no worklog", "bold")
        return
    counts = defaultdict(int)
    for x in actions:
        counts[x.action] += 1
    held = sum(x.minutes for x in actions if x.action in ("add", "update", "keep"))
    title = f"Jira, {daily.short_date(date)}" if date else "Jira"
    summary = " · ".join(f"{k} {counts[k]}" for k in ACTIONS if counts[k])
    print(f"{style.paint(title, 'bold')}   {summary}   {style.paint('→', 'dim')} {daily.fmt(dt.timedelta(minutes=held))}")
    card = max(len(x.card) for x in actions)
    room = style.columns()
    for x in sorted(actions, key=lambda x: (x.started, x.card)):
        end = x.started + dt.timedelta(minutes=x.minutes)
        head = f"  {ACTIONS[x.action][0]} {x.action:<8} {x.started:%H:%M}-{end:%H:%M}  {daily.fmt(dt.timedelta(minutes=x.minutes))}  "
        comment = style.clip(x.comment, room and room - len(head) - card - 2)
        print(f"{style.paint(head, *ACTIONS[x.action][1:])}{style.pad(x.card, style.paint(x.card, 'cyan'), card)}  {comment}")


# each action's sign, readable without colors, and its colors
ACTIONS = {"add": ("+", "green"), "update": ("~", "yellow"), "delete": ("−", "red"), "keep": ("=", "dim"),
           "foreign": ("!", "magenta")}


def push_plan(a, day, client=None):
    """the push's actions; with a client, from Jira's worklogs as they are now"""
    answer = jira.load(a.date)
    if client:  # Jira as it is now, every worklog
        with style.working("reading the day's worklogs in Jira"):
            worklogs = jira_rest.my_worklogs(client, a.date)
        answer = {**(answer or {"known": {}, "candidates": {}}), "worklogs": worklogs, "truncated": []}
        jira.save(a.date, answer)
    worklogs = answer["worklogs"] if answer else None
    truncated = answer["truncated"] if answer else []
    if worklogs is None:
        print("# the day's worklogs weren't read from Jira: worklogs added by hand can't be seen", file=sys.stderr)
    return push.plan(worklog_lines(a, day), day["pushed"], worklogs, truncated)


def snapshot(lines):
    return [[w.card, jira_started(w.started), w.minutes, w.comment] for w in lines]


def send(date, day, client, actions):
    """sends the plan's adds, updates and deletes, and keeps what Jira holds now in the day file"""
    sent, deleted = [], set()
    todo = [x for x in actions if x.action in ("add", "update", "delete")]
    try:
        for i, x in enumerate(todo, 1):
            with style.working(f"sending {i} of {len(todo)} to Jira: {x.action} {x.card}"):
                worklog_id = jira_rest.send(client, x, jira_started(x.started))
            if worklog_id is None:
                deleted.add(x.worklog_id)
            else:
                sent.append({"id": worklog_id, "card": x.card, "started": jira_started(x.started),
                             "minutes": x.minutes, "comment": x.comment})
    finally:  # what Jira accepted before a failure is kept, so the next push doesn't send it again
        day["pushed"] = push.record([p for p in day["pushed"] if p["id"] not in deleted], sent)
        store.save_day(date, day)
        jira.record_sent(date, sent, deleted)
        done = f"sent: {len(sent)} added or updated, {len(deleted)} deleted"
        if len(sent) + len(deleted) < len(todo):  # stopped by an error, shown after this
            style.note(f"{done}, then stopped at {len(sent) + len(deleted) + 1} of {len(todo)}", "yellow")
        else:
            print(style.paint(f"✓ {done}", "green") if style.on() else f"# {done}")
