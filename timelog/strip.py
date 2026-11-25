"""The day as a strip in the terminal: a line per lane, a character per few minutes, to see at a
glance what's attributed, what's left to answer and where you were away"""
import datetime as dt

from . import config
from . import model
from . import style
from . import timeline

LABEL = 11  # the lane's name, before its cells
INDENT = 2  # the review prints the strip indented
STEPS = [dt.timedelta(minutes=m) for m in (10, 15, 20, 30)]  # the shortest that fits the terminal
CELLS = {"?": ("?", "yellow", "bold"), "work": ("▓", "cyan"), "call": ("█", "magenta"), "away": ("░", "dim"),
         "agent": ("▒", "blue"), "": ("·", "dim")}
LEGEND = "▓ attributed   ? to answer   ░ away   █ call   ▒ agent"


def bounds(entries, gaps):
    """the whole hours the day's entries and gaps fall in"""
    times = [t for e in entries for t in (e.start, e.end)] + [t for g in gaps for t in g]
    if not times:
        return None
    first = min(times).replace(minute=0, second=0, microsecond=0)
    last = max(times)
    end = last.replace(minute=0, second=0, microsecond=0)
    return first, end + dt.timedelta(hours=1) if end < last else end


def step_for(first, end):
    room = style.columns() or 100
    return next((s for s in STEPS if INDENT + LABEL + (end - first) / s + 2 <= room), STEPS[-1])  # + the last hour


def overlap(a0, a1, b0, b1):
    return max(min(a1, b1) - max(a0, b0), dt.timedelta(0))


def lanes(entries, events, first, step, n):
    """(your cells, the agents' cells): `?` where something waits for a card, else what holds the
    cell longest; away only when no card of yours holds that time, as a stretch you gave one"""
    mine = [(e.start, e.end, set()) for e in entries
            if model.lane_of(e.streams) == "you" and not model.is_unattributed(e.task) and e.task != config.DROP]
    away = [(a, b) for a, b, _ in timeline.subtract(
        [(e.when, e.until, set()) for e in events if e.kind == "away" and e.until], [(a, b) for a, b, _ in timeline.merge(mine)])]
    you, agents = [], []
    for i in range(n):
        c0, c1 = first + i * step, first + (i + 1) * step
        held, asked, agent, agent_asked = {}, False, False, False
        for e in entries:
            if overlap(e.start, e.end, c0, c1) <= dt.timedelta(0) or e.task == config.DROP:
                continue
            if model.lane_of(e.streams) != "you":
                agent, agent_asked = True, agent_asked or model.is_unattributed(e.task)
            elif model.is_unattributed(e.task):
                asked = True
            else:
                kind = "call" if "call" in e.streams else "work"
                held[kind] = held.get(kind, dt.timedelta(0)) + overlap(e.start, e.end, c0, c1)
        held["away"] = sum((overlap(a0, a1, c0, c1) for a0, a1 in away), dt.timedelta(0))
        best = max((k for k in held if held[k] > dt.timedelta(0)), key=held.get, default="")
        you.append("?" if asked else best)
        agents.append("?" if agent_asked else "agent" if agent else "")
    return you, agents


def cells(kinds):
    return "".join(style.paint(*CELLS[k]) for k in kinds)


def lines(date, entries, events, gaps, mark=None):
    """the strip: hours, your lane, the agents' lane, and with `mark` (start, end) a ^ under it;
    nothing for a day without entries"""
    span = bounds(entries, gaps)
    if not span:
        return []
    first, end = span
    step = step_for(first, end)
    n = int((end - first) / step)
    per_hour = int(dt.timedelta(hours=1) / step)
    every = 1 if per_hour >= 3 else 2  # hours apart when their labels would touch
    hours = "".join(f"{(first + dt.timedelta(hours=h)).hour:02d}".ljust(per_hour * every)
                    for h in range(0, n // per_hour, every))
    hours += f"{end.hour:02d}" if (n // per_hour) % every == 0 else ""
    you, agents = lanes(entries, events, first, step, n)
    out = [style.paint(f"{date:<{LABEL}}", "bold") + style.paint(hours, "dim"),
           f"{'you':<{LABEL}}" + cells(you)]
    if any(agents):
        out.append(f"{'agents':<{LABEL}}" + cells(agents))
    if mark:
        under = "".join("^" if overlap(*mark, first + i * step, first + (i + 1) * step) > dt.timedelta(0) else " "
                        for i in range(n))
        if "^" not in under:  # shorter than a minute of a cell: under the cell it starts in
            i = min(max(int((mark[0] - first) / step), 0), n - 1)
            under = " " * i + "^" + " " * (n - i - 1)
        out.append(" " * LABEL + style.paint(under.rstrip(), "yellow", "bold"))
    return out
