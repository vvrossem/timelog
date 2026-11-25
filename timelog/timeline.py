import datetime as dt
from collections import defaultdict

from . import config
from . import model


def timed(events):
    """the events that give time: those with a duration (lanes and pieces of yours). A prompt's time is
    its turn's; a piece dropped by a rule
    (presence) or a stretch away gives none."""
    return [e for e in events if e.until and e.until > e.when and e.kind not in ("presence", "away")]


def event_spans(events, end):
    """(start, end, event) of the events that have a duration, cut at the end"""
    for e in events:
        if e.until:
            yield e.when, max(e.when, min(e.until, end)), e


def spans_of(events, end):
    return [(s0, s1, model.task_of(e.card, e.review), e.stream) for s0, s1, e in event_spans(events, end)]


GLANCE = dt.timedelta(seconds=30)


def fold(events):
    """your lane's pieces, with the card each one counts for (DESIGN.md). In a window you prompted
    an agent from that day, a piece counts only between turns, when you read an answer or write a
    prompt, for the session that answered last: during a turn, the agent's lane counts. Elsewhere,
    a piece without a card, or a glance at one (under GLANCE), folds into the call in progress, else
    the card of the running agent you interacted with last (prompted, or read in its window), else
    your current card (until a break): the card of the agent you last prompted or read, none if its
    session has none, or of your last piece that showed one for `settle` minutes, whichever came
    last. Otherwise a piece stays to answer, and a glance counts no time"""
    # a call no rule or answer attributed stays apart, under its title
    events = [e._replace(card=f"{config.UNATTRIBUTED} {e.keys[0] if e.keys else e.text}")
              if e.kind == "call" and not e.card else e
              for e in events]
    calls = [e for e in events if e.kind == "call"]
    turns = [e for e in events if e.kind == "aw-agent"]
    away = [(e.when, e.until) for e in events if e.kind == "away"]
    mine = sorted((e for e in events if e.stream.startswith("me ") and e.kind != "presence"), key=lambda e: e.when)
    others = [e for e in events if not (e.stream.startswith("me ") and e.kind != "presence")]
    prompts = [p for p in events if p.kind == "claude"]

    def window(e):
        return e.keys[0] if e.keys else e.text

    # the windows you prompted an agent from, and those titled like a session (its own window), with
    # their sessions
    sessions = defaultdict(set)
    for e in mine:
        sessions[window(e)] |= {p.stream for p in prompts if e.when <= p.when < e.until}
    for t in turns:
        sessions[window(t)].add(t.stream.split("/")[0])
    agent_windows = {w for w, found in sessions.items() if found} - {""}
    # your prompts count as activity for the breaks, and as an interaction with their session
    yours = sorted([(p.when, 0, p) for p in prompts] + [(e.when, 1, e) for e in mine], key=lambda x: (x[0], x[1]))
    out, current, last_end, interacted = [], None, None, {}  # interacted: session -> when you last did
    for when, is_piece, e in yours:
        went_away = any(a0 < when and a1 > (last_end or when) for a0, a1 in away)
        if last_end and (when - last_end >= config.BREAK_GAP or went_away):
            current = None
        last_end = max(last_end or when, e.until if is_piece else when)
        if not is_piece:
            interacted[e.stream] = when
            # what you do between its turns goes with it too; a session without a card leaves you without one
            current = (e.card, e.review) if e.card else None
            continue
        if window(e) in agent_windows:
            mine_there = [t for t in turns if t.stream.split("/")[0] in sessions[window(e)]]
            out += between_turns(e, mine_there)
            answered = max((t for t in mine_there if t.until <= e.when), key=lambda t: t.until, default=None)
            if answered:  # you read its answer, which gives its card, or none
                interacted[answered.stream.split("/")[0]] = e.when
                current = (answered.card, answered.review) if answered.card else None
            continue
        if e.card and e.until - e.when >= GLANCE:
            if e.until - e.when >= config.SETTLE and not e.side:  # a side activity doesn't change it
                current = (e.card, e.review)
            out.append(e)
            continue
        # the calls and turns starting or ending within the piece cut it: each part folds on its own
        inside = {x for lane in calls + turns for x in (lane.when, lane.until) if e.when < x < e.until}
        cuts = sorted({e.when, e.until} | inside)
        for a, b in zip(cuts, cuts[1:]):
            call = next((c for c in calls if c.when <= a < c.until), None)
            running = {t.stream.split("/")[0]: t for t in turns if t.when <= a < t.until and t.card}
            steered = max((s for s in running if s in interacted and interacted[s] <= a), key=interacted.get,
                          default=None)
            if call:
                folded = (call.card, call.review)
            elif steered:  # what you do while an agent you steer works goes with its card
                folded = (running[steered].card, running[steered].review)
            elif current:
                folded = current
            else:  # stays to answer; a glance at a card gives no time
                if not e.card:
                    out.append(e._replace(when=a, until=b))
                continue
            out.append(e._replace(when=a, until=b, card=folded[0], review=folded[1], stream=f"{e.stream} (folded)"))
    return others + [part for e in out for part in into_calls(e, calls)]


def into_calls(e, calls):
    """a piece of your lane cut by the calls: while one runs you're in it, so the piece counts for its
    card, even if it shows another one or is an agent's window; your lane has one card at a time"""
    inside = {x for c in calls for x in (c.when, c.until) if e.when < x < e.until}
    cuts = sorted({e.when, e.until} | inside)
    out = []
    for a, b in zip(cuts, cuts[1:]):
        call = next((c for c in calls if c.when <= a < c.until), None)
        if call and (e.card, e.review) != (call.card, call.review):
            out.append(e._replace(when=a, until=b, card=call.card, review=call.review,
                                  stream=f"{e.stream.removesuffix(' (folded)')} (folded)"))
        else:
            out.append(e._replace(when=a, until=b))
    return out


def between_turns(e, turns):
    """the parts of a piece in an agent's window when none of its sessions' `turns` runs, for the card
    the window shows, else the one of the session that answered last, or before any answer the one
    you write a prompt to; none if that session has none"""
    inside = {x for t in turns for x in (t.when, t.until) if e.when < x < e.until}
    cuts = sorted({e.when, e.until} | inside)
    out = []
    for a, b in zip(cuts, cuts[1:]):
        if any(t.when <= a < t.until for t in turns):
            continue  # the agent's lane counts it
        if e.card and e.until - e.when >= GLANCE:
            out.append(e._replace(when=a, until=b))
            continue
        last = max((t for t in turns if t.until <= a), key=lambda t: t.until, default=None)
        last = last or min((t for t in turns if t.when >= b), key=lambda t: t.when, default=None)
        if last and last.card:
            out.append(e._replace(when=a, until=b, card=last.card, review=last.review, stream=f"{e.stream} (folded)"))
        elif not e.card:
            out.append(e._replace(when=a, until=b))
    return out


def gaps_of(intervals, min_gap=dt.timedelta(0)):
    """stretches between the first and last interval where none is active"""
    gaps, cursor = [], None
    for s0, s1 in sorted(intervals):
        if cursor is not None and s0 - cursor > min_gap:
            gaps.append((cursor, s0))
        cursor = s1 if cursor is None else max(cursor, s1)
    return gaps


def merge(intervals, gap=dt.timedelta(0)):
    """[(start, end, streams)] merged when they overlap or are at most `gap` apart"""
    out = []
    for s0, s1, streams in sorted(intervals, key=lambda i: (i[0], i[1])):
        if out and s0 - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], s1), out[-1][2] | streams)
        else:
            out.append((s0, s1, set(streams)))
    return out


def subtract(intervals, holes):
    out = []
    for s0, s1, streams in intervals:
        pieces = [(s0, s1)]
        for h0, h1 in holes:
            pieces = [p for a, b in pieces for p in ((a, min(b, h0)), (max(a, h1), b)) if p[1] > p[0]]
        out += [(a, b, streams) for a, b in pieces]
    return out


def build(events, kept, end):
    """auto entries: your lane folded, then per task and lane the union of its spans, to the minute.
    Time already in a manual entry of the same task isn't counted twice. Returns the entries, the
    stretches of config.BREAK_GAP or more where nothing was active (to ask about) and the notes on
    what rounding dropped."""
    events = fold(events)
    spans = spans_of(timed(events), end)
    presence = [(s0, s1) for s0, s1, _t, _s in spans_of([e for e in events if e.kind != "away"], end)]
    presence += [(k.start, k.end) for k in kept]
    per_lane = defaultdict(list)  # a card's lanes count apart: each in full (DESIGN.md)
    for s0, s1, task, stream in spans:
        per_lane[(task, model.lane_of(stream))].append((s0, s1, {stream}))
    held = defaultdict(list)
    for k in kept:
        held[k.task].append((k.start, k.end))
    agents = [(s0, s1, e.stream) for s0, s1, e in event_spans(timed(events), end) if e.kind == "aw-agent"]
    entries, notes = [], []
    busy = []  # (card, start, end) of the agents' entries, as built: your lane may not count in them
    for (task, lane), intervals in sorted(per_lane.items(), key=lambda kv: kv[0][1] == "you"):  # agents first
        joined = merge(subtract(merge(intervals, config.MERGE_GAP), held[task]), config.MERGE_GAP)
        if lane == "you":
            holes = [(a, b) for card, a, b in busy
                     if config.DURING_AGENTS == "calls" or (card and card == card_of(task))]
            calls = [i for i in intervals if "call" in i[2]]  # they count whole
            joined = merge(subtract(joined, holes) + subtract(calls, held[task]))
        for s0, s1, streams in joined:
            r0, r1 = model.round_minute(s0), model.round_minute(s1)
            if r1 <= r0:
                notes.append((task, s1 - s0))
                continue
            # the agents' part: their turns within the entry, which moves with it when it's renamed
            worked = merge([(max(a, s0), min(b, s1), set()) for a, b, stream in agents
                            if stream in streams and b > s0 and a < s1])
            minutes = round(sum((b - a for a, b, _ in worked), dt.timedelta()).total_seconds() / 60)
            entries.append(model.Entry(task, r0, r1, "auto", tuple(sorted(streams)), min(minutes, minutes_of(r0, r1))))
            if lane != "you":
                busy.append((card_of(task), r0, r1))
    entries = normalize(entries)
    breaks = [(g0, g1) for g0, g1 in gaps_of(presence) if g1 - g0 >= config.BREAK_GAP]
    if notes:
        lost = sum((d for _t, d in notes), dt.timedelta())
        notes = [f"dropped once rounded to the minute: {len(notes)} bits, {lost.total_seconds() / 60:.0f} min in all"
                 f" ({', '.join(sorted({t for t, _d in notes}))})"]
    return entries, breaks, notes


def card_of(task):
    """a task's card, its PR review included; None for an unattributed one"""
    return None if model.is_unattributed(task) else task.split(": ", 1)[0]


def minutes_of(start, end):
    return round((end - start).total_seconds() / 60)


def normalize(entries):
    """entries of one task, source and lane that touch or overlap become one; where a task's auto
    and manual entries overlap, the manual one wins, so no minute counts twice. An entry's agent
    minutes are those of the entries it's made of, within its length"""
    groups, sources = defaultdict(list), defaultdict(list)
    for e in entries:
        key = (e.task, e.source, model.lane_of(e.streams))
        groups[key].append((e.start, e.end, set(e.streams)))
        sources[key].append(e)
    manual = defaultdict(list)
    for (task, source, _lane), intervals in groups.items():
        if source == "manual":
            manual[task] += [(a, b) for a, b, _ in intervals]
    out = []
    for (task, source, lane), intervals in groups.items():
        intervals = merge(intervals)
        if source == "auto":
            intervals = subtract(intervals, [(a, b) for a, b, _ in merge([(a, b, set()) for a, b in manual[task]])])
        for s0, s1, streams in intervals:
            agent = sum(e.agent for e in sources[(task, source, lane)] if e.start < s1 and e.end > s0)
            out.append(model.Entry(task, s0, s1, source, tuple(sorted(streams)), min(agent, minutes_of(s0, s1))))
    return sorted(out, key=lambda e: (e.start, e.task))


REPLAY_SLACK = dt.timedelta(minutes=2)  # a rebuilt entry can start or end a minute apart


def replay(entries, renames, overrides):
    """the user's decisions of an earlier build, applied to a rebuild's auto entries: the renames
    and drops of single entries, in the order they were made, each on an entry it covers at least
    half of and that has the task it had then, and only for the time it was made for (give or take
    REPLAY_SLACK): the rest of a longer entry keeps its task; then the task renames"""
    out = []
    for e in entries:
        if e.source != "auto":
            out.append(e)
            continue
        parts = [e]
        for o in overrides:
            if o.get("lane", model.lane_of(e.streams)) != model.lane_of(e.streams):
                continue  # another lane's entry of the same card at the same time
            o0, o1 = dt.datetime.fromisoformat(o["start"]), dt.datetime.fromisoformat(o["end"])
            split = []
            for p in parts:
                overlap = min(p.end, o1) - max(p.start, o0)
                if o["task"] not in (p.task, renames.get(p.task, p.task)) or overlap * 2 < p.end - p.start:
                    split.append(p)
                    continue
                a = o0 if o0 - p.start > REPLAY_SLACK else p.start
                b = o1 if p.end - o1 > REPLAY_SLACK else p.end
                split += [clip(p, p.start, a)] * (a > p.start) + [clip(p, a, b)._replace(task=o["to"])] \
                    + [clip(p, b, p.end)] * (p.end > b)
            parts = split
        out += [p._replace(task=renames.get(p.task, p.task)) for p in parts
                if renames.get(p.task, p.task) != config.DROP]
    return out


def clip(entry, start, end):
    """the part of an entry from start to end, with the agent's minutes in it at most"""
    start, end = max(entry.start, start), min(entry.end, end)
    return entry._replace(start=start, end=end, agent=min(entry.agent, int((end - start).total_seconds()) // 60))


def agent_minutes(entries):
    """per task, the minutes agents worked on it"""
    out = defaultdict(int)
    for e in entries:
        out[e.task] += e.agent
    return dict(out)
