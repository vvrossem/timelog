"""The day as a text file to review in an editor: the agents, one line per session and task, then
yours, one line per entry or stretch with nothing active, then one comment per task. An agent's line
is known by its session: change its task to move all of it, delete it to drop it. One of yours is
known by its times: change its task, delete it to drop it, change its times or add a line for a
manual entry. A `!` after the task of a line timelog found gives its page or window that task from
now on. What follows a `#` is ignored."""
import re
from typing import NamedTuple

from . import config
from . import model

HEADER = """\
# timelog {date}: {logged} logged over {wall} of wall clock.
# Give each ? a card, or - to drop it (a break, for nothing active); delete a line to drop it. A card holds for this day: add a
# ! (ABC-1!, -!) for its page or window to get it from now on. Save and quit when you're done;
# delete everything to cancel.
"""
AGENTS = "# Agents: one line per session and card, its turns summed. Change the card to move them all.\n"
YOU = "# You: change a line's times to fix them, add a line for what left no trace.\n"
LINE = re.compile(r"^(\d{1,2}:\d\d)\s*-\s*(\d{1,2}:\d\d)\s+(.+)$")
AGENT_LINE = re.compile(r"^agent\s+(\S+)\s+(.+)$")
COMMENT = re.compile(r"^(.+?)\s*=\s*(.*)$")
CARD_FIRST = re.compile(r"^[A-Z][A-Z0-9]*-\d+\b")
GAP = "nothing active"


class Invalid(ValueError):
    pass


class Row(NamedTuple):
    start: str  # HH:MM
    end: str
    task: str
    kind: str  # entry, gap, or agent: a session's entries of one task
    index: int  # the entry's number, from 1; 0 for a gap or an agent
    indices: tuple = ()  # the entries of an agent's row, or of one of yours grouping several
    session: str = ""


def render(date, rows, labels, comments, candidates, logged, wall):
    """rows: Row per agent session and task, then per entry of yours and gap, in order; labels: what
    each row is made of; comments: {task: text}"""
    out = HEADER.format(date=date, logged=logged, wall=wall)
    if candidates:
        out += "# Cards waiting for review: " + ", ".join(f"{k} {v}" for k, v in candidates) + "\n"
    width = max([len(r.task) for r in rows] + [8])
    agents = [(r, label) for r, label in zip(rows, labels) if r.kind == "agent"]
    if agents:
        session = max(len(r.session) for r, _label in agents)
        out += "\n" + AGENTS + "".join(f"agent {r.session:{session}}  {r.task:{width}}  # {label}".rstrip(" #") + "\n"
                                     for r, label in agents)
    out += "\n" + YOU + "".join(f"{r.start}-{r.end}  {r.task:{width}}  # {label}".rstrip(" #") + "\n"
                                for r, label in zip(rows, labels) if r.kind != "agent")
    out += "\n# Comments, one per card; add a line for a new card.\n"
    out += "".join(f"{t} = {c}".rstrip() + "\n" for t, c in sorted(comments.items()))
    return out


def hhmm(value):
    """HH:MM, or None for a time that doesn't exist"""
    h, m = value.split(":")
    return f"{int(h):02d}:{m}" if int(h) < 24 and int(m) < 60 else None


def checked(task, n, line):
    if task.endswith("!"):
        raise Invalid(f"line {n}: a ! goes after the task of a line timelog found: {line}")
    if task in (config.UNATTRIBUTED, config.DROP) or task.startswith(f"{config.UNATTRIBUTED} ") or CARD_FIRST.match(task):
        return task
    raise Invalid(f"line {n}: a card like ABC-1, ? or -, not {task!r}: {line}")


def remembering(task):
    """(the task, whether a ! asks for a rule)"""
    return (task[:-1].rstrip(), True) if task.endswith("!") else (task, False)


def parse(text, rows):
    """what was changed: ({entry number: task}, {dropped entry numbers}, [(start, end, task) added,
    task "-" for a gap that was a break],
    {task: comment}, [(entry numbers of a line, task of its rule)]), or None when the file was emptied. Raises
    Invalid on a line it can't read."""
    lines = [(n, line.strip()) for n, line in enumerate(text.splitlines(), 1)]
    # what follows " #" on a time line describes it; a comment can hold a "#"
    lines = [(n, line.split(" #", 1)[0].strip() if LINE.match(line) or AGENT_LINE.match(line) else line)
             for n, line in lines if line and not line.startswith("#")]
    if not lines:
        return None
    unused = list(rows)
    renames, added, comments, remember = {}, [], {}, []
    for n, line in lines:
        a = AGENT_LINE.match(line)
        if a:
            task, keep = remembering(a.group(2).strip())
            session, task = a.group(1), checked(task, n, line)
            if keep and model.is_unattributed(task):
                raise Invalid(f"line {n}: a ! needs a card or -: {line}")
            mine = [r for r in unused if r.kind == "agent" and r.session == session]
            row = next((r for r in mine if r.task == task), mine[0] if mine else None)
            if not row:
                raise Invalid(f"line {n}: no session {session} left in the day: {line}")
            unused.remove(row)
            if task != row.task:
                renames.update({i: task for i in row.indices})
            if keep:
                remember.append((row.indices, task))
            continue
        m = LINE.match(line)
        if not m:
            c = COMMENT.match(line)
            if not c:
                raise Invalid(f"line {n}: a time like 09:00-09:30, or CARD = comment: {line}")
            task = checked(c.group(1), n, line)
            if task in (config.UNATTRIBUTED, config.DROP):
                raise Invalid(f"line {n}: a comment needs a card: {line}")
            comments[task] = c.group(2).strip()
            continue
        task, keep = remembering(m.group(3).strip())
        start, end, task = hhmm(m.group(1)), hhmm(m.group(2)), checked(task, n, line)
        if not (start and end):
            raise Invalid(f"line {n}: no such time: {line}")
        if end <= start:
            raise Invalid(f"line {n}: it ends before it starts: {line}")
        # entries of two cards can share their times: the line is the one of its task, if any
        same = [r for r in unused if (r.start, r.end) == (start, end)]
        row = next((r for r in same if r.task == task), same[0] if same else None)
        # a call's title can hold a " #", cut above: still to attribute, it wasn't renamed
        known = row and (task == row.task or model.is_unattributed(task) and model.is_unattributed(row.task))
        if keep and (not row or row.kind != "entry" or model.is_unattributed(task)):
            raise Invalid(f"line {n}: a ! needs a card or - on a line timelog found: {line}")
        if keep:
            remember.append((row.indices or (row.index,), task))
        if row:
            unused.remove(row)
            if row.kind == "entry" and not known:
                renames.update({i: task for i in row.indices or (row.index,)})
            elif row.kind == "gap" and task != config.UNATTRIBUTED:  # "-": a break
                added.append((start, end, task))
        elif task not in (config.UNATTRIBUTED, config.DROP) and not task.startswith(f"{config.UNATTRIBUTED} "):
            added.append((start, end, task))
    drops = {i for r in unused if r.kind in ("entry", "agent") for i in r.indices or (r.index,)}
    changed = drops | set(renames)
    tasks = ({r.task for r in rows if r.kind in ("entry", "agent") and not {r.index, *r.indices} & changed}
             | set(renames.values()) | {task for _s, _e, task in added})
    # the comment lines of the tasks you renamed or dropped stay in the file: left out, not refused
    unknown = sorted(set(comments) - tasks - {r.task for r in rows})
    if unknown:
        raise Invalid(f"a comment for a task that isn't in the day: {', '.join(unknown)}")
    return renames, drops, added, {t: c for t, c in comments.items() if t in tasks}, remember
