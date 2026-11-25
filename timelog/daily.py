"""The day: built from the events and the corrections its file keeps, what each entry is made of, the
default comments, and the corrections themselves"""
import datetime as dt
import glob
import json
import os
import re
from collections import defaultdict

from . import config
from . import jira
from . import model
from . import rules
from . import sources
from . import store
from . import style
from . import timeline


def minutes(e):
    return int((e.end - e.start).total_seconds()) // 60


def fmt(d):
    return f"{int(d.total_seconds()) // 3600}h{int(d.total_seconds()) % 3600 // 60:02d}"


def wall_clock(entries):
    return sum((s1 - s0 for s0, s1, _ in timeline.merge([(e.start, e.end, set()) for e in entries])), dt.timedelta())


def overlaps(entries):
    """what the time logged has over the wall clock: (a task's lanes at the same time, you and an
    agent or two agents; different tasks at the same time)"""
    tasks = defaultdict(list)
    for e in entries:
        tasks[e.task].append(e)
    per_task = sum((wall_clock(es) for es in tasks.values()), dt.timedelta())
    return logged({"entries": entries}) - per_task, per_task - wall_clock(entries)


def logged_rows(entries, worklogs=None):
    """(what, time, about) for what Jira gets, the time logged and what makes it more than the wall
    clock; `worklogs`: the day's worklogs' time"""
    same, parallel = overlaps(entries)
    rows = [("worklogs", fmt(worklogs), "what Jira gets: a card's parallel lanes count once")] if worklogs is not None else []
    rows.append(("logged", fmt(logged({"entries": entries})), f"over {fmt(wall_clock(entries))} of wall clock"))
    if same:
        rows.append(("same card", fmt(same), "in parallel lanes"))
    if parallel:
        rows.append(("other cards", fmt(parallel), "at the same time"))
    return rows


def print_logged(entries, worklogs=None):
    for what, time, about in logged_rows(entries, worklogs):
        print(f"{style.paint(f'{what:<12}', 'bold')}{time:>5}  {style.paint(about, 'dim')}")


def build_day(date, events, rebuild=False):
    """the day from its events, keeping what the day file holds: manual entries, comments, and the
    corrections made with set, replayed; the tasks without a comment get a default one. The gaps
    you already said were breaks aren't returned. A day handed over keeps its entries as saved,
    since a later timelog could count them differently and move worklogs you sent: only its
    late_work is added, unless `rebuild`."""
    day = store.load_day(date) or {"entries": [], "comments": {}, "renames": {}, "overrides": [], "pushed": []}
    manual = [e for e in day["entries"] if e.source == "manual"]
    entries, gaps, notes = timeline.build(events, manual, day_end(date))
    breaks = [tuple(dt.datetime.fromisoformat(t) for t in b) for b in day.get("breaks", [])]
    gaps = [(g0, g1) for g0, g1 in gaps
            if not any(b0 <= to_minute(g0) and to_minute(g1) <= b1 for b0, b1 in breaks)]
    entries = timeline.normalize(timeline.replay(entries, day.get("renames", {}), day.get("overrides", [])) + manual)
    if handed_over(day) and not rebuild:
        late = late_work(entries, day["entries"])
        entries = timeline.normalize(day["entries"] + [timeline.clip(e, a, b) for e in entries if e.source == "auto"
                                                       for a, b in late if e.start < b and a < e.end])
        gaps = []
    day.update(entries=entries)
    fill_comments(day, events, date)
    return day, gaps, notes


LATE_WORK = dt.timedelta(minutes=15)


def handed_over(day):
    """pushed, or entered in Tempo"""
    return bool(day.get("pushed") or day.get("entered"))


def late_work(built, saved):
    """the stretches of LATE_WORK or more that `built` has and `saved` doesn't: work after the push"""
    spans = timeline.merge([(e.start, e.end, set()) for e in built])
    late = timeline.subtract(spans, [(e.start, e.end) for e in saved])
    return [(a, b) for a, b, _streams in late if b - a >= LATE_WORK]



def to_minute(when):
    """a time as the review shows it, and keeps the breaks you answered: cut to the minute"""
    return when.replace(second=0, microsecond=0)


def card_refs(events, task, cards):
    """the PRs (`repo#N`: title) and prompts of a task's card, from the day's events"""
    card = model.card_of_text(task, cards)
    review = task.endswith(": PR review")
    prs, prompts = {}, []
    for e in events:
        if not card or e.card != card or e.kind == "aw-agent" or e.review != review:
            continue
        for repo, number in config.PR_URL.findall(e.text):
            prs[f"{repo}#{number}"] = model.pr_info(repo, number).get("title", "")
        if e.kind == "claude":
            prompts.append(e.text)
    return prs, prompts


def reviewed(date, entries, cards):
    """{card: ["repo#N"]} of the PRs you reviewed that day on GitHub: the card the PR names, else the
    one an agent worked on when you posted the review (it posts them for you)"""
    out = defaultdict(list)
    for when, ref in sources.reviews():
        if f"{when:%Y-%m-%d}" != date:
            continue
        card = model.card_of_pr(model.pr_info(*ref.split("#")), cards)
        if not card:
            working = [e for e in entries if e.start <= when < e.end + dt.timedelta(minutes=1)
                       and not model.is_unattributed(e.task) and e.task != config.DROP]
            working.sort(key=lambda e: model.lane_of(e.streams) == "you")  # an agent's first
            card = working and jira.card_of_task(working[0].task, cards)
        short = ref.split("/", 1)[1]
        if card and short not in out[card]:
            out[card].append(short)
    return out


REVIEW_ASKED = re.compile(r"\s*review\b", re.I)


def asked_to_review(events, card):
    """a session on the card started with "review ...": you asked an agent to review the card, which
    may have no PR to review on GitHub"""
    first = {}
    for e in sorted(events, key=lambda e: e.when):
        if e.kind == "claude":
            first.setdefault(e.stream, e)
    return any(p.card == card and REVIEW_ASKED.match(p.text) for p in first.values())




def what_was_done(task, entries, events):
    """for a card that only says which kind of work it is (tooling, admin): the titles of its agent
    sessions, then the comments of the rules its pieces matched however short. Not your windows'
    titles: they say where you were, not what you did"""
    sessions, yours = defaultdict(list), defaultdict(list)
    for entry in (e for e in entries if e.task == task):
        for (stream, label), spans in entry_spans(entry, entries, events)[0].items():
            (sessions if stream.startswith("agent ") else yours)[label] += spans
    sessions, yours = ({label: spent(s) for label, s in held.items()} for held in (sessions, yours))
    noted = {r["name"]: r["comment"] for r in rules.DEFAULT_RULES + config.RULES if r.get("name") and r.get("comment")}
    items = sorted(sessions, key=sessions.get, reverse=True)
    items += [noted[n] for n in sorted(yours, key=yours.get, reverse=True) if n in noted]
    out = []
    for item in items:
        if any(item.removesuffix("…").strip().lower() in o.lower() for o in out):  # a title clipped by its window
            continue
        out.append(item)
    return ", ".join(out) or None


def default_comment(task, events, date, cards, reviews=None, entries=(), earlier=None):
    """what a worklog says when nobody wrote its comment: the card's Jira summary and its PRs, or
    the PRs reviewed: those you reviewed on GitHub that day, whatever the session's prompts named;
    a card an agent was asked to review without a PR reviewed on GitHub: "Review: " and its summary.
    `earlier`: {card: the comment you wrote last time}, used over the summary"""
    if model.is_unattributed(task):
        return None
    prs = [ref.split("/", 1)[1] for ref in card_refs(events, task, cards)[0]]
    mine = (reviews or {}).get(jira.card_of_task(task, cards), [])
    if task.endswith(": PR review") or mine:
        prs = mine + [p for p in prs if p not in mine] if task.endswith(": PR review") else mine
        return f"PR review of {', '.join(prs)}" if prs else "PR review"
    known = (jira.load(date) or {"known": {}})["known"].get(jira.card_of_task(task, cards), {})
    summary = known.get("summary", "")
    if asked_to_review(events, jira.card_of_task(task, cards)):
        return f"Review: {summary}" if summary else "Review"
    last = (earlier or {}).get(jira.card_of_task(task, cards))
    if jira.card_of_task(task, cards) in {config.TRACKER.get("tooling_card"), config.TRACKER.get("admin_card")}:
        return what_was_done(task, entries, events) or last or None  # its summary never changes
    head = last or summary
    named = set(re.findall(r"[\w.-]+#\d+", head))
    return ", ".join(p for p in (head, *(p for p in prs if p not in named)) if p) or None


EARLIER_DAYS = 30


def earlier_comments(date, cards):
    """{card: the last comment you wrote for it}, from the day files of the EARLIER_DAYS before"""
    out = {}
    for path in sorted(glob.glob(f"{config.DAY_DIR}/????-??-??.json"), reverse=True):
        day = os.path.basename(path)[:10]
        if day >= date:
            continue
        if day < (dt.date.fromisoformat(date) - dt.timedelta(days=EARLIER_DAYS)).isoformat():
            break
        try:
            with open(path) as f:
                comments = json.load(f).get("comments", {})
        except (OSError, ValueError):
            continue
        for task, comment in comments.items():
            if comment:
                out.setdefault(jira.card_of_task(task, cards), comment)
    return out


def fill_comments(day, events, date):
    """the default comments of the tasks you didn't write one for: kept apart, so that the next
    build can update them"""
    cards = model.card_regex()
    tasks = {e.task for e in day["entries"]} - set(day["comments"])
    reviews = reviewed(date, day["entries"], cards)
    earlier = earlier_comments(date, cards)
    defaults = {task: default_comment(task, events, date, cards, reviews, day["entries"], earlier) for task in tasks}
    day["defaults"] = {task: comment for task, comment in defaults.items() if comment}


def comment_of(day, task):
    return day["comments"].get(task) or day.get("defaults", {}).get(task)


def day_end(date):
    """the end of the day, or now for today: an agent still working counts up to now, not ahead"""
    return min(model.at(date, "23:59"), dt.datetime.now().astimezone())


def require_day(date):
    day = store.load_day(date)
    if day is None:
        raise SystemExit(f"# no {store.day_path(date)}: run `timelog day` first")
    return day


_FOLDED = {}


def folded(events):
    """timeline.fold of the day's events, once per list of events"""
    if _FOLDED.get("events") is not events:
        _FOLDED.update(events=events, folded=timeline.fold(events))
    return _FOLDED["folded"]


def entry_pieces(entry, entries, events):
    """what an entry is made of: the time of each page, window, call and agent session in it, the
    largest first, and the prompts written during it"""
    spans, prompts = entry_spans(entry, entries, events)
    held = {key: sum((b - a for a, b in spans), dt.timedelta()) for key, spans in spans.items()}
    return sorted(held.items(), key=lambda kv: -kv[1]), prompts


def spent(spans):
    """the time [(start, end)] cover, once where they overlap: a Meet page is both a call and a tab"""
    return sum((b - a for a, b, _ in timeline.merge([(a, b, set()) for a, b in spans])), dt.timedelta())


def entry_spans(entry, entries, events):
    """{(stream, name): [(start, end)]} of each page, window, call and agent session in an entry,
    and the prompts written during it"""
    cards = model.card_regex()
    streams = {s.removesuffix(" (folded)") for s in entry.streams}
    own = jira.card_of_task(entry.task, cards)
    others = by_card(entry, entries, cards)
    held, prompts = defaultdict(list), []
    names = whole_names(events)
    for e in folded(events):  # your pieces as the entry counts them: not your time in an agent's turn
        if e.stream.removesuffix(" (folded)") not in streams or counted_elsewhere(e, own, others):
            continue
        if e.kind == "claude":
            if entry.start <= e.when < entry.end:
                prompts.append(e)
            continue
        if e.until and min(e.until, entry.end) > max(e.when, entry.start):
            stream = e.stream.removesuffix(" (folded)").replace("claude ", "agent ")
            label = full_name(e.keys[0] if e.keys and e.keys[0] else e.text, names)
            held[(stream, label)].append((max(e.when, entry.start), min(e.until, entry.end)))
    return held, prompts


def whole_names(events):
    """the names of the day's pieces, but those cut short"""
    return {e.keys[0] for e in events if e.keys and e.keys[0] and not e.keys[0].endswith("…")}


def full_name(label, names):
    """a name cut short (…), as an editor's window title shows its agent session's, made whole when
    one name of the day starts with it"""
    if not label.endswith("…") or len(label) < 13:
        return label
    whole = [n for n in names if n.startswith(label[:-1])]
    return whole[0] if len(whole) == 1 else label


def by_card(entry, entries, cards):
    """{card: [(start, end, streams)]} of the entries but `entry`, to find quickly the one counting an event"""
    out = defaultdict(list)
    for o in entries:
        if o is not entry:
            out[jira.card_of_task(o.task, cards)].append((o.start, o.end, {s.removesuffix(" (folded)") for s in o.streams}))
    return out


def counted_elsewhere(event, own, others):
    """an event of another card than `own` that an overlapping entry of that card counts (`others`,
    by_card's): your lane runs through entries of several cards when an agent works in parallel"""
    if not event.card or event.card == own:
        return False
    until = event.until or event.when
    return any(event.stream in streams and event.when < end and until > start
               for start, end, streams in others.get(event.card, ()))


def correct(day, renames, drops, tasks=None, comments=None, added=(), breaks=()):
    """applies corrections to the day: entry N (from 1) renamed or dropped, a task renamed, comments
    set ("" removes one), manual entries added, gaps (start, end) said to be breaks"""
    tasks, new = tasks or {}, []
    overrides = list(day.get("overrides", []))
    for i, e in enumerate(day["entries"], 1):
        task = config.DROP if i in drops else renames.get(i, tasks.get(e.task, e.task))
        if e.source == "auto" and (i in drops or i in renames):
            overrides.append({"task": e.task, "start": e.start.isoformat(), "end": e.end.isoformat(), "to": task,
                              "lane": model.lane_of(e.streams)})
        if task != config.DROP:
            new.append(e._replace(task=task))
    day_renames = {old: tasks.get(new_, new_) for old, new_ in day.get("renames", {}).items()}
    day_renames.update({old: new_ for old, new_ in tasks.items() if old not in day_renames.values()})
    day_comments = {tasks.get(t, t): c for t, c in day["comments"].items()}
    for task, text in (comments or {}).items():
        if text:
            day_comments[task] = text
        else:
            day_comments.pop(task, None)
    day.update(entries=timeline.normalize(new + list(added)), comments=day_comments, renames=day_renames,
               overrides=overrides, breaks=day.get("breaks", []) + [[s.isoformat(), e.isoformat()] for s, e in breaks])


def dominant(line, events):
    """the piece (page, window, call or agent session) that holds more than half of the line's
    entries, or None. Pages showing a card of their own don't count: a rule never applies over them"""
    held, longest = defaultdict(dt.timedelta), {}
    for entry in line:
        streams = {s for s in entry.streams if not s.endswith(" (folded)")}  # what was folded followed it
        for e in events:
            if (not e.card or e.guessed) and e.keys and e.stream in streams and e.until:
                overlap = min(e.until, entry.end) - max(e.when, entry.start)
                if overlap > dt.timedelta(0):
                    held[e.keys[0]] += overlap
                    if e.keys[0] not in longest or e.until - e.when > longest[e.keys[0]].until - longest[e.keys[0]].when:
                        longest[e.keys[0]] = e
    name = max(held, key=held.get, default=None)
    if name and held[name] * 2 > sum((entry.end - entry.start for entry in line), dt.timedelta()):
        return longest[name]
    return None


def add_rule(rule):
    config.add_rule(rule)
    print(f"# rule added: {rule['regex']} -> {rule['card']}")


def remember(date, kept):
    """a rule for exactly the piece holding most of each line (`!` in the review file, or `set
    --entry N=TASK!`), with the line's task"""
    if not kept:
        return
    events = sources.day_events(date, model.card_regex())
    for line, task in kept:
        piece = dominant(line, events)
        if piece:
            add_rule({**rules.scopes(piece)[0][1], "card": task})
        else:
            style.note(f"no rule added: no page or window without a card of its own holds more than half of "
                       f"{line[0].start:%H:%M}-{line[-1].end:%H:%M}", "yellow")


def logged(day):
    return sum((e.end - e.start for e in day["entries"]), dt.timedelta())


def short_date(date):
    """Mon 09.11, in your date_order"""
    d = dt.date.fromisoformat(date)
    parts = {"d": f"{d:%d}", "m": f"{d:%m}"}
    sep = {"dmy": ".", "mdy": "/", "ymd": "-"}[config.DATE_ORDER]
    return f"{WEEKDAYS[d.weekday()][:3].title()} {sep.join(parts[c] for c in config.DATE_ORDER if c != 'y')}"


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
