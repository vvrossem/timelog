import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
from collections import defaultdict

from . import config
from . import daily
from . import detect
from . import jira
from . import model
from . import review
from . import reviewing
from . import sources
from . import store
from . import style
from . import sync
from . import timeline


def print_day(day, breaks=(), notes=(), date=None):
    entries = day["entries"]
    width = min(max((len(e.task) for e in entries), default=0), 44)
    room = style.columns()
    for i, e in enumerate(entries, 1):
        task = style.clip(e.task, width)
        head = f"{i:3}  {e.start:%H:%M}-{e.end:%H:%M} {daily.minutes(e):4}m  "
        where = style.clip(sources_of(e), room and room - len(head) - width - 2)
        print(f"{style.paint(head[:3], 'dim')}{head[3:]}{style.pad(task, style.task(task), width)}  "
              f"{style.paint(where, 'dim')}")
    comments = {t: (daily.comment_of(day, t), t not in day["comments"]) for t in sorted({e.task for e in entries})}
    comments = {t: c for t, c in comments.items() if c[0]}
    if comments:
        print(style.paint("\ncomments", "bold"))
        for task, (comment, default) in comments.items():
            label = f"{comment} (default)" if default else comment
            print(f"     {style.pad(style.clip(task, width), style.task(style.clip(task, width)), width)}  "
                  f"{style.paint(label, 'dim') if default else label}")
    print()
    for note in notes:
        style.note(note, "yellow")
    for g0, g1, hint in breaks:
        style.note(f"nothing active {g0:%H:%M}-{g1:%H:%M}: {hint}", "yellow")
    if date:
        print_jira(day, date)
    print(style.paint("logged ", "bold") + daily.logged_over(entries))


def sources_of(entry):
    """where an entry's time comes from, short: your lane's kinds, calls, then the agent sessions
    (subagents in their session). What was folded in from your lane shows in `show N`"""
    streams = [s for s in entry.streams if not s.endswith(" (folded)")] or [
        s.removesuffix(" (folded)") for s in entry.streams]
    mine = dict.fromkeys(s.removeprefix("me ") for s in streams if not s.startswith("claude "))
    agents = dict.fromkeys(s.removeprefix("claude ").split("/")[0] for s in streams if s.startswith("claude "))
    return ", ".join([*mine, *([f"agent {' '.join(agents)}"] if agents else [])])


def print_jira(day, date):
    tasks = {e.task for e in day["entries"]}
    missing = jira.unknown(tasks, date, model.card_regex())
    if missing is None:
        style.note("Jira not asked: no card key checked")
        return
    if missing:
        style.note(f"not in Jira: {', '.join(missing)}", "red")
    candidates = jira.load(date)["candidates"]
    if any(model.is_unattributed(t) for t in tasks) and candidates:
        projects = {t.split("-")[0] for t in tasks}
        ranked = sorted(candidates.items(), key=lambda kv: (kv[0].split("-")[0] not in projects, kv[0]))
        style.note("cards waiting for review, the day's projects first, for the unattributed entries:")
        for key, summary in ranked[:10]:
            print(f"#   {style.paint(key, 'cyan')} {summary[:80]}")
        if len(ranked) > 10:
            style.note(f"  and {len(ranked) - 10} more in jira-{date}.json")


def cmd_cards(a):
    """the card keys of the day to check in Jira"""
    print(" ".join(sync.day_cards(a.date)))


def print_events(events):
    for e in events:
        if e.kind != "presence":
            until = f"-{e.until:%H:%M:%S}" if e.until and e.until != e.when else ""
            print(f"{e.when:%H:%M:%S}{until} {e.card or '?':12} {'R' if e.review else ' '} {e.source:32} {e.text!r}")
    print()


def cmd_config(a):
    """the effective settings: the config's values over the defaults, and what's missing; --write
    writes them all to the config, the values already set kept"""
    print(json.dumps(config.SETTINGS, indent=1, ensure_ascii=False))
    for problem in [config.ERROR] * bool(config.ERROR) + config.PROBLEMS + detect.warnings(config.SETTINGS):
        print(f"# {problem}", file=sys.stderr)
    if not a.write:
        return
    if config.ERROR:  # not written over with the defaults
        raise SystemExit(f"# {config.ERROR}")
    if os.path.exists(config.CONFIG):
        shutil.copy2(config.CONFIG, f"{config.CONFIG}.bak")
    os.makedirs(os.path.dirname(config.CONFIG), exist_ok=True)
    with open(config.CONFIG, "w") as f:
        json.dump(config.SETTINGS, f, indent=1, ensure_ascii=False)
    print(f"# written to {config.CONFIG}", file=sys.stderr)


def cmd_day(a):
    """the day rebuilt from ActivityWatch, keeping your corrections, and saved"""
    for problem in config.PROBLEMS:
        style.note(f"config: {problem}", "yellow")
    events = sources.day_events(a.date, model.card_regex())
    if a.events:
        print_events(events)
    day, gaps, notes = daily.build_day(a.date, events, rebuild=a.rebuild)
    breaks = [(g0, g1, "a break, or a meeting to add?") for g0, g1 in gaps]
    print_day(day, breaks, notes, a.date)
    if a.rebuild and daily.handed_over(day):
        print_rebuilt(day)
    store.save_day(a.date, day)


def print_rebuilt(day):
    """a pushed day built again: each card's minutes, pushed and now"""
    before = defaultdict(int)
    for p in day["pushed"]:
        before[p["card"]] += int(p["minutes"])
    now = {w.card: w.minutes for w in sync.day_worklogs(day, day["entries"], model.card_regex())}
    changed = [f"{card} {before.get(card, 0)}m -> {now.get(card, 0)}m" for card in sorted({*before, *now})
               if before.get(card, 0) != now.get(card, 0)]
    style.note(f"rebuilt, against what was pushed: {', '.join(changed) or 'the same'}; `timelog push -n` shows "
               "what Jira would get, the previous file is in backups", "yellow")


def cmd_show(a):
    day = daily.require_day(a.date)
    if a.entry is None:
        print_day(day, date=a.date)
        return
    if not 1 <= a.entry <= len(day["entries"]):
        raise SystemExit(f"# no entry {a.entry} on {a.date}: see `timelog show`")
    events = sources.day_events(a.date, model.card_regex())
    print_entry(day["entries"][a.entry - 1], day["entries"], events)


def print_entry(entry, entries, events):
    print(f"{entry.start:%H:%M}-{entry.end:%H:%M} {daily.minutes(entry)}m  " + style.task(entry.task))
    held, prompts = daily.entry_pieces(entry, entries, events)
    glances = [spent for _key, spent in held if spent < timeline.GLANCE]
    room = style.columns()
    width = max((len(stream) for (stream, _label), _spent in held), default=0)
    for (stream, label), spent in held:
        if spent >= timeline.GLANCE:
            print(f"  {round(spent.total_seconds() / 60):4}m  {style.paint(f'{stream:{width}}', 'dim')}  "
                  + style.clip(label, room and room - width - 10))
    if glances:
        seconds = round(sum(glances, dt.timedelta()).total_seconds())
        print(style.paint(f"        and {len(glances)} glances of under 30 s, {seconds} s in all", "dim"))
    for p in prompts:
        print(f"  {p.when:%H:%M}  {style.paint('prompt', 'magenta')} {style.clip(repr(p.text[:100]), room and room - 16)}")


def cmd_add(a):
    day = store.load_day(a.date) or {"entries": [], "comments": {}}
    start, end = model.round_minute(model.at(a.date, a.start)), model.round_minute(model.at(a.date, a.end))
    if end <= start:
        raise SystemExit("# --to must be after --from")
    day["entries"] = timeline.normalize(day["entries"] + [model.Entry(a.task, start, end, "manual", ("manual",))])
    if a.comment:
        day["comments"][a.task] = a.comment
    print_day(day)
    store.save_day(a.date, day)


def cmd_set(a):
    day = daily.require_day(a.date)
    entries = list(day["entries"])
    tasks = pairs(a.task, "--task OLD=NEW")
    unknown = set(tasks) - {e.task for e in entries}
    renames = pairs(a.entry, "--entry N=TASK")
    unknown |= {n for n in renames if not n.isdigit()}
    renames = {int(n): new for n, new in renames.items() if n.isdigit()}
    marked = {n: review.remembering(t) for n, t in renames.items()}
    renames = {n: task for n, (task, _wanted) in marked.items()}
    keep = {n: task for n, (task, wanted) in marked.items() if wanted}
    unknown |= {str(n) for n in (set(renames) | set(a.drop)) - set(range(1, len(entries) + 1))}
    if unknown:
        raise SystemExit(f"# no such task or entry on {a.date}: {', '.join(sorted(unknown))}")
    kept = [((entries[n - 1],), task) for n, task in keep.items()]
    daily.correct(day, renames, set(a.drop), tasks, pairs(a.comment, "--comment TASK=TEXT"))
    print_day(day)
    store.save_day(a.date, day)
    daily.remember(a.date, kept)


def pairs(values, usage):
    wrong = [v for v in values if "=" not in v]
    if wrong:
        raise SystemExit(f"# {usage}, not {', '.join(wrong)}")
    return dict(v.split("=", 1) for v in values)


def cmd_notes(a):
    cards = model.card_regex()
    events = sources.day_events(a.date, cards)
    day = daily.require_day(a.date)
    spent = defaultdict(dt.timedelta)
    for e in day["entries"]:
        spent[e.task] += e.end - e.start
    for task, total in sorted(spent.items(), key=lambda kv: -kv[1]):
        card = model.card_of_text(task, cards)
        prs, prompts = daily.card_refs(events, task, cards)
        print(f"## {task}  ({daily.fmt(total)})")
        known = (jira.load(a.date) or {"known": {}})["known"].get(jira.card_of_task(task, cards))
        if known:
            print(f"jira: {known.get('summary', '')} [{known.get('status', '')}]")
        if task in day["comments"]:
            print(f"comment: {day['comments'][task]}")
        elif task in day.get("defaults", {}):
            print(f"default comment: {day['defaults'][task]}")
        if not card:
            print("no card yet: `timelog show N` tells what its entries are made of")
        elif not prs and not prompts:
            print("no prompt or PR for this card")
        for ref, title in prs.items():
            print(f"pr: {ref} {title}")
        for text in prompts:
            print(f"prompt: {text!r}")
        print()


def cmd_summary(a):
    """the day's totals, rebuilt from ActivityWatch first: up to now for today"""
    events = sources.day_events(a.date, model.card_regex())
    day, _gaps, _notes = daily.build_day(a.date, events)
    spent = defaultdict(dt.timedelta)
    for e in day["entries"]:
        spent[e.task] += e.end - e.start
    # days written before the agent minutes were kept per entry have them per task
    agent = timeline.agent_minutes(day["entries"]) if any(e.agent for e in day["entries"]) else day.get("agent_minutes", {})
    cards = model.card_regex()
    worklogs = {task: dt.timedelta(minutes=sum(w.minutes for w in sync.day_worklogs(
        day, [e for e in day["entries"] if e.task == task], cards))) for task in spent}
    tokens = task_tokens(day["entries"], events)
    print(style.paint(f"{'time':>6}  {'agent':>6}  {'you':>6}  {'rounded':>7}  {'in':>5}  {'out':>5}  {'':10}  task",
                      "dim"))
    longest = max(spent.values(), default=dt.timedelta())
    for task, total in sorted(spent.items(), key=lambda kv: -kv[1]):
        mins = round(total.total_seconds() / 60)
        a_min = min(agent.get(task, 0), mins)
        fresh, out = tokens.get(task, (0, 0))
        print(f"{daily.fmt(total):>6}  {daily.fmt(dt.timedelta(minutes=a_min)):>6}  {daily.fmt(dt.timedelta(minutes=mins - a_min)):>6}  "
              f"{daily.fmt(worklogs[task]):>7}  {count(fresh):>5}  {count(out):>5}  {bar(a_min, mins, longest)}  "
              f"{style.task(task)}")
    total = sum(spent.values(), dt.timedelta())
    fresh, out = (sum(t[i] for t in tokens.values()) for i in (0, 1))
    sent = sum(w.minutes for w in sync.day_worklogs(day, day["entries"], cards))  # one worklog per card, as pushed
    rounded = (f", {daily.fmt(dt.timedelta(minutes=sent))} once each worklog is rounded ({config.ROUND_MODE})"
               f" to {config.ROUND} min" if config.ROUND > 1 else "")
    print(style.paint(f"{daily.fmt(total):>6}  total logged", "bold") + rounded)
    same, parallel = daily.overlaps(day["entries"])
    if same:
        print(f"{daily.fmt(same):>6}  of it the same card in parallel lanes: you and an agent, or two agents")
    if parallel:
        print(f"{daily.fmt(parallel):>6}  of it different cards in parallel")
    print(f"{daily.fmt(daily.wall_clock(day['entries'])):>6}  wall clock with at least one task active")
    if fresh or out:
        print(f"{count(fresh + out):>6}  agent tokens: {count(fresh)} in (cache writes included), {count(out)} out; "
              "cache reads left out")
    print(style.paint(f"{'':6}  {'':6}  {'':6}  {'':7}  {'':5}  {'':5}  ", "dim") + bar_legend())


def task_tokens(entries, events):
    """per task, the tokens of the agent turns its entries hold: (input with cache writes, output). A
    turn's tokens go with the entry of its stream it starts in, also once you moved it to another card"""
    out = defaultdict(lambda: (0, 0))
    for e in events:
        if e.kind != "aw-agent" or not e.tokens:
            continue
        entry = next((x for x in entries if e.stream in x.streams and x.start <= model.round_minute(e.when) < x.end),
                     None)
        if entry:
            fresh, done = out[entry.task]
            out[entry.task] = (fresh + e.tokens[0] + e.tokens[2], done + e.tokens[1])
    return dict(out)


def count(n):
    """tokens, short: 850, 12k, 1.4M; - for none"""
    if not n:
        return "-"
    return str(n) if n < 1000 else f"{n / 1000:.0f}k" if n < 999_500 else f"{n / 1e6:.1f}M"


BAR = 10


def bar(agent, total, longest):
    """a task's time against the day's longest: the agent's part, then yours"""
    cells = max(round(BAR * total / (longest.total_seconds() / 60)), 1) if longest and total else 0
    mine = round(cells * (total - agent) / total) if total else 0
    return (style.paint("▒" * (cells - mine), "magenta") + style.paint("█" * mine, "blue")
            + " " * (BAR - cells))


def bar_legend():
    return style.paint("▒", "magenta") + style.paint(" agent  ", "dim") + style.paint("█", "blue") + style.paint(" you", "dim")


def date_forms(order=None):
    """the dates timelog reads, as examples in your date_order"""
    order = order or config.DATE_ORDER
    example = {"d": "14", "m": "11", "y": "2026"}
    full = ".".join(example[c] for c in order)
    short_year = "/".join(example[c][-2:] if c == "y" else example[c] for c in order)
    no_year = "-".join(example[c] for c in order if c != "y")
    return f"{full}, {short_year}, {no_year}, 2026-11-14, 20261114, today, yesterday, friday, -2 (days ago)"


def parse_date(value, today=None, order=None):
    """a day as YYYY-MM-DD, from the forms of date_forms; ValueError otherwise"""
    today, order = today or dt.date.today(), order or config.DATE_ORDER
    v = value.strip().lower()
    if v == "today":
        return today.isoformat()
    if v == "yesterday":
        return (today - dt.timedelta(days=1)).isoformat()
    if v.startswith("-") and v[1:].isdigit():
        return (today - dt.timedelta(days=int(v[1:]))).isoformat()
    for i, name in enumerate(daily.WEEKDAYS):
        if len(v) >= 3 and name.startswith(v):  # the last one, today included
            return (today - dt.timedelta(days=(today.weekday() - i) % 7)).isoformat()
    if len(v) == 8 and v.isdigit():
        return dt.date(int(v[:4]), int(v[4:6]), int(v[6:])).isoformat()
    parts = re.split(r"\s*[-/.]\s*", v.rstrip("."))  # de_CH writes 14.11.26, fr_CH 14. 11. 26
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError(value)
    if len(parts) == 3 and len(parts[0]) == 4:  # 2026-11-14, whatever the order
        fields = dict(zip("ymd", parts))
    else:
        fields = dict(zip(order if len(parts) == 3 else order.replace("y", ""), parts))
    month, day = int(fields["m"]), int(fields["d"])
    if "y" not in fields:  # the last one, so 30.12 in January is last year's
        date = dt.date(today.year, month, day)
        return (date if date <= today else date.replace(year=today.year - 1)).isoformat()
    if len(fields["y"]) not in (2, 4):
        raise ValueError(value)
    year = int(fields["y"]) + (2000 if len(fields["y"]) == 2 else 0)
    return dt.date(year, month, day).isoformat()


USAGE = """\
timelog [DATE] [--edit]      review a day: questions in the terminal, or your editor; it asks before sending to Jira
       timelog COMMAND [DATE] ...   for scripts and /timelog (timelog COMMAND --help)"""


def main():
    names = ("review", "summary", "day", "show", "add", "set", "notes", "cards", "fetch", "push")
    helps = {
        "review": "what `timelog` runs: the days not pushed yet, then the day, as questions in the terminal "
                  "(--edit: in your editor); sent to Jira only if you answer y",
        "summary": "per task: time, agent time and yours, agent tokens; total and wall clock, up to now",
        "day": "rebuild the day from ActivityWatch, keeping your corrections, and save it",
        "show": "the saved day, entries numbered; with N, what entry N is made of",
        "add": "a manual entry: a meeting, work without traces",
        "set": "rename or drop entries, set comments (entry numbers from `show`)",
        "notes": "per task: its time, comment, PRs and prompts, to write the comments",
        "cards": "the day's card keys",
        "fetch": "ask Jira for the day's cards, candidates and worklogs (API token)",
        "push": "send the day's worklogs to Jira (-n, --dry-run: only show the plan)",
        "config": "the effective settings and what's missing (--write: all of them, to the config)",
    }
    listing = "\n".join(f"  {name:9} {text}" for name, text in helps.items())
    p = argparse.ArgumentParser(usage=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=f"DATE: {date_forms()}. Today when left out.\n\ncommands:\n{listing}")
    sub = p.add_subparsers(dest="cmd", required=True, help=argparse.SUPPRESS, prog="timelog")
    parsers = {name: sub.add_parser(name, description=helps[name]) for name in (*names, "config")}
    for sp in (parsers[name] for name in names):
        sp.add_argument("date", nargs="?", metavar="DATE", help=f"{date_forms()}; today when left out")
        sp.add_argument("--date", dest="date_option", metavar="DATE", help="the same, as an option")
    parsers["review"].add_argument("--edit", action="store_true", help="the day in your editor, not as questions")
    parsers["config"].add_argument("--write", action="store_true", help="write every setting to the config file")
    parsers["show"].add_argument("entry", nargs="?", type=int, metavar="N",
                                 help="an entry's number, to see what it's made of")
    parsers["day"].add_argument("--events", action="store_true", help="also print the raw events")
    parsers["day"].add_argument("--rebuild", action="store_true", help="a day already pushed too: built again from "
                                "ActivityWatch, your corrections replayed; the previous file stays in backups")
    parsers["add"].add_argument("--from", dest="start", required=True, metavar="HH:MM")
    parsers["add"].add_argument("--to", dest="end", required=True, metavar="HH:MM")
    parsers["add"].add_argument("--task", required=True, metavar="CARD", help="e.g. ABC-12, or ABC-12: PR review")
    parsers["add"].add_argument("--comment", help="the worklog comment")
    parsers["set"].add_argument("--task", action="append", default=[], help="OLD=NEW, every entry of that task")
    parsers["set"].add_argument("--entry", action="append", default=[], help="N=TASK, entry N as `show` numbers it; "
                               "N=TASK! also gives its page or window that task from now on")
    parsers["set"].add_argument("--drop", action="append", default=[], type=int, help="N, entry N as `show` numbers it")
    parsers["set"].add_argument("--comment", action="append", default=[], help="TASK=TEXT, the worklog comment")
    parsers["push"].add_argument("-n", "--dry-run", action="store_true", help="show the plan, send nothing")
    parsers["push"].add_argument("--json", action="store_true", help="the plan as JSON, for the session; sends nothing")
    parsers["push"].add_argument("--record", help="a JSON list of the worklogs added or updated: id, card, "
                                 "started, minutes, comment")
    argv = sys.argv[1:]
    implicit = not argv or argv[0] not in (*sub.choices, "-h", "--help")
    if implicit:
        argv = ["review", *argv]
    a = p.parse_args(argv)
    if a.cmd == "show" and a.entry is None and a.date and a.date.isdigit() and len(a.date) < 8:
        a.date, a.entry = None, int(a.date)  # `show 3`: an entry of today
    if a.cmd != "config":
        a.date_given = bool(a.date or a.date_option)
        if a.date and a.date_option:
            parsers[a.cmd].error(f"two dates: {a.date} and --date {a.date_option}")
        value = a.date or a.date_option or "today"
        try:
            a.date = parse_date(value)
        except ValueError:
            if implicit and value == a.date:  # a bare word: maybe a mistyped command
                p.error(f"{value!r} is neither a command nor a date. Commands: {', '.join(sub.choices)}. "
                        f"Dates: {date_forms()}")
            parsers[a.cmd].error(f"{value!r} isn't a date. Dates: {date_forms()}")
    if config.ERROR and a.cmd != "config":  # not a day built from the defaults
        raise SystemExit(f"# {config.ERROR}")
    home = {"review": reviewing, "fetch": sync, "push": sync}.get(a.cmd)
    (getattr(home, f"cmd_{a.cmd}") if home else globals()[f"cmd_{a.cmd}"])(a)
