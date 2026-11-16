"""What to send to Jira so that its worklogs match the day, whatever was pushed before.

The day file keeps the worklogs timelog pushed (`pushed`: id, card, started, minutes, comment).
The session also reads the day's worklogs from Jira (`worklogs` in jira-<date>.json): a pushed
worklog deleted in Jira is forgotten, and a worklog timelog didn't push (made by hand in Tempo) is
only reported, never changed.

Each worklog line of the day becomes:
- keep: a pushed worklog has the same card, start, duration and comment;
- update: a pushed worklog has the same card and start, or is a leftover of the same card;
- add: nothing pushed matches it.
A pushed worklog left without a line is to delete: `push` does it, while through the Atlassian
connector it's done by hand (the connector can add and update worklogs, not delete them).
"""
import datetime as dt
from collections import defaultdict
from typing import List, NamedTuple, Optional


class Line(NamedTuple):
    card: str
    started: dt.datetime
    minutes: int
    comment: str


class Action(NamedTuple):
    action: str  # add, update, keep, delete, foreign
    card: str
    started: dt.datetime
    minutes: int
    comment: str
    worklog_id: Optional[str] = None


def round_lines(lines, step, mode="nearest"):
    """each worklog rounded to a multiple of `step` minutes, never under `step`: `nearest` as cents
    are (12 -> 10, 13 -> 15, 1 -> 5), `up` to the next one (11 -> 15)"""
    extra = step - 1 if mode == "up" else step // 2
    return [line._replace(minutes=max(step, (line.minutes + extra) // step * step)) for line in lines]


def parse_started(value):
    """Jira's 2026-11-12T09:36:00.000+0100, or an ISO date-time"""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return dt.datetime.strptime(value.replace("Z", "+0000"), fmt)
        except ValueError:
            pass
    return dt.datetime.fromisoformat(value)


def record_line(r):
    return Line(r["card"], parse_started(r["started"]), int(r["minutes"]), r.get("comment", ""))


def plan(lines: List[Line], pushed: List[dict], jira_worklogs: Optional[List[dict]], truncated=()):
    """the actions that make Jira match the lines. `truncated`: the cards whose worklogs Jira's search
    returned only in part (it stops at 20 per card): a pushed worklog missing there isn't deleted"""
    mine = {p["id"] for p in pushed}
    foreign = []
    if jira_worklogs is not None:
        existing = {w["id"] for w in jira_worklogs}
        # deleted in Jira since: push it again
        pushed = [p for p in pushed if p["id"] in existing or p["card"] in truncated]
        foreign = [w for w in jira_worklogs if w["id"] not in mine]
    known = {(p["card"], parse_started(p["started"])): p for p in pushed}
    used, pending, actions = set(), [], []
    for line in lines:
        p = known.get((line.card, line.started))
        if p and p["id"] not in used:
            used.add(p["id"])
            same = int(p["minutes"]) == line.minutes and p.get("comment", "") == line.comment
            actions.append(Action("keep" if same else "update", *line, worklog_id=p["id"]))
        else:
            pending.append(line)
    leftovers = defaultdict(list)
    for p in pushed:
        if p["id"] not in used:
            leftovers[p["card"]].append(p)
    for line in pending:
        if leftovers[line.card]:
            p = leftovers[line.card].pop(0)
            actions.append(Action("update", *line, worklog_id=p["id"]))
        else:
            actions.append(Action("add", *line))
    for rest in leftovers.values():
        actions += [Action("delete", *record_line(p), worklog_id=p["id"]) for p in rest]
    actions += [Action("foreign", *record_line(w), worklog_id=w["id"]) for w in foreign]
    return sorted(actions, key=lambda a: (a.started, a.card))


def record(pushed: List[dict], results: List[dict]):
    """the pushed worklogs once the session reports what it added or updated"""
    by_id = {p["id"]: p for p in pushed}
    for r in results:
        by_id[str(r["id"])] = {"id": str(r["id"]), "card": r["card"], "started": r["started"],
                               "minutes": int(r["minutes"]), "comment": r.get("comment", "")}
    return sorted(by_id.values(), key=lambda p: p["started"])
