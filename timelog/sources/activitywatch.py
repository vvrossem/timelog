"""ActivityWatch, read through its local REST API (aw-server, localhost only): the lanes and the
evidence of DESIGN.md. timelog needs aw-server; each watcher the user chose to install adds what it
records, and a missing one is left out.
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from .. import config
from .. import model
from .. import rules
from .. import style
from . import browser

BUCKET_TYPES = {"agent.turn": "aw-watcher-agents", "afkstatus": "aw-watcher-afk",
                "currentwindow": "aw-watcher-window", "web.tab.current": "aw-watcher-web"}


def fetch(path, params=None):
    url = f"{config.ACTIVITYWATCH_URL}/api/0{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    with style.working("reading ActivityWatch"), urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read())


def bucket_events(buckets, kind, start, end):
    """events of the buckets of that type, oldest first"""
    out = []
    for bucket_id, bucket in buckets.items():
        if bucket.get("type") == kind:
            params = {"start": start.isoformat(), "end": end.isoformat(), "limit": -1}
            out += fetch(f"/buckets/{urllib.parse.quote(bucket_id)}/events", params)
    for e in out:
        e["start"] = dt.datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone()
        e["end"] = e["start"] + dt.timedelta(seconds=e.get("duration", 0))
    return sorted(out, key=lambda e: e["start"])


def host_buckets():
    """this machine's buckets; stops when aw-server doesn't answer, says which watchers give nothing"""
    try:
        host = fetch("/info")["hostname"]
        buckets = {k: v for k, v in fetch("/buckets/").items() if v.get("hostname") == host}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        raise SystemExit(f"# ActivityWatch doesn't answer at {config.ACTIVITYWATCH_URL} ({e}): start it first")
    missing = sorted(name for kind, name in BUCKET_TYPES.items()
                     if not any(b.get("type") == kind for b in buckets.values()))
    if missing:
        print(f"# ActivityWatch has no data from {', '.join(missing)}: left out", file=sys.stderr)
    return buckets


def combine(start, end, cards, me, events=()):
    """the day's events from ActivityWatch: your prompts, the agent and call lanes, your lane cut by
    its evidence, and the stretches away; and per session its card, title and starting command"""
    buckets = host_buckets()
    # a session resumed on a later day keeps the card it started with: its earlier turns tell it
    history = bucket_events(buckets, "agent.turn", start - SESSION_HISTORY, end)
    info = sessions(history, cards, me)
    turns = [t for t in history if t["start"] >= start and not info[session_of_turn(t)]["loop"]
             and not config.ignored(t.get("data", {}).get("cwd"))]
    afk = bucket_events(buckets, "afkstatus", start, end)
    tabs = bucket_events(buckets, "web.tab.current", start, end)
    windows = bucket_events(buckets, "currentwindow", start, end)
    edits = bucket_events(buckets, "app.editor.activity", start, end)  # an editor watcher, if any
    events = list(events) + prompts(turns, info) + agent_turns(turns, info) + calls(tabs)
    events += [model.Event(e["start"], None, False, "afk", "away", "activitywatch afk", "", until=e["end"])
               for e in afk if e.get("data", {}).get("status") == "afk"]
    here = [(e["start"], e["end"]) for e in afk if e.get("data", {}).get("status") == "not-afk"]
    segments = [(a, b, w.get("data", {}).get("app", ""), w.get("data", {}).get("title", ""))
                for w in windows for a, b in (clip(w["start"], w["end"], here) if afk else [(w["start"], w["end"])])]
    if not windows:
        segments = [(a, b, "", "") for a, b in here]
    working = {session_of_turn(t) for t in history if not info[session_of_turn(t)]["loop"]
               and not config.ignored(t.get("data", {}).get("cwd"))}  # the sessions whose turns count
    return events + my_lane(segments, tabs, edits, cards, me, info, working), info


BROWSERS = re.compile(r"firefox|chrom|brave|edge|vivaldi|opera|safari", re.I)


def clip(start, end, intervals):
    """the parts of start-end that fall in the intervals"""
    out = []
    for i0, i1 in intervals:
        a, b = max(start, i0), min(end, i1)
        if b > a:
            out.append((a, b))
    return out


# the browser watcher only sees the tab in front: a glance at another tab of the same window stops
# the call's tab for a while, the call goes on
CALL_GAP = dt.timedelta(minutes=5)


def calls(tabs):
    """a lane per call: a tab playing sound that matches an `always_active` rule, focused or not"""
    out = []
    for t in tabs:
        data = t.get("data", {})
        string = f"{data.get('title', '')}|{data.get('url', '')}"
        rule, name, _card = rules.match(string)
        if not (data.get("audible") and rule and rule.get("always_active")):
            continue
        if out and out[-1].keys == (name,) and t["start"] - out[-1].until <= CALL_GAP:
            out[-1] = out[-1]._replace(until=max(out[-1].until, t["end"]))
        else:
            out.append(model.Event(t["start"], None, False, "call", "call", "activitywatch web", string,
                                   until=t["end"], keys=(name,)))
    return out


def covering(items, when):
    return next((i for i in items if i["start"] <= when < i["end"]), None)


def my_lane(segments, tabs, edits, cards, me, info, working):
    """your lane: each focused window while at the keyboard, cut by the tabs in a browser window and
    by an editor's activity elsewhere. Each piece has its match string and the card of its evidence,
    if any; the rules, the answers and the folding do the rest (DESIGN.md)"""
    titles = {i["title"]: s for s, i in info.items() if i["title"]}
    out = []
    for s0, s1, app, title in segments:
        browsing = bool(BROWSERS.search(app))
        session = session_of(title, titles) if "code" in app.lower() else None
        title = whole_title(title, info[session]["title"]) if session else title
        # its session's card, even when its turns were on an earlier day
        told = info[session]["card"] if session in working else None
        mine_tabs = [t for t in tabs if t["start"] < s1 and t["end"] > s0] if browsing else []
        # an editor's heartbeats run on a little after you leave it: not over the browser
        mine_edits = [] if browsing else [e for e in edits if e["start"] < s1 and e["end"] > s0
                                          and editor_place(e.get("data", {}))[0]]
        cuts = sorted({s0, s1} | {x for i in mine_tabs + mine_edits for x in (i["start"], i["end"]) if s0 < x < s1})
        for a, b in zip(cuts, cuts[1:]):
            kind, card, review, source, text = evidence(
                title, told, covering(mine_tabs, a), covering(mine_edits, a), cards, me)
            last = out[-1] if out else None
            same = last and (last.kind, last.card, last.review, last.text, last.app) == (kind, card, review, text, app)
            if same and last.until == a:
                out[-1] = last._replace(until=b)
            else:
                out.append(model.Event(a, card, review, f"me {kind}", kind, source, text, until=b, app=app))
    return out


def evidence(title, told, tab, edit, cards, me):
    """(kind, card, review, source, match string) of a piece: its tab in a browser, else a Claude
    session's window, else an editor's activity, else the window; the card from a card key, a PR or
    the session"""
    if tab:
        data = tab.get("data", {})
        page = browser.classify(data.get("url", ""), data.get("title", ""), cards, me)
        string = f"{data.get('title', '')}|{data.get('url', '')}"[:300]
        card, review = page if page else (model.card_of_text(string, cards), False)
        return ("browser", card, review, "activitywatch web", string)
    if told:  # an agent session's window: (card, review) of its session
        return ("focus", *told, "activitywatch window", title[:300])
    if edit:
        data = edit.get("data", {})
        repository, branch = editor_place(data)
        folder = config.folder_card(data.get("file") or data.get("project")) or config.folder_card(repository)
        card, review = model.split_review(folder) if folder else (model.card_of_text(branch.upper(), cards), False)
        return ("editor", card, review, "activitywatch editor", f"{repository}|{branch}")
    return ("focus", model.card_of_text(title, cards), False, "activitywatch window", title[:300])


def editor_place(data):
    """(repository, branch) of an editor's event: aw-watcher-vscode-odoo sends the repository,
    aw-watcher-vscode only the project's folder and no branch; "unknown" counts as nothing"""
    known = [v if v and v != "unknown" else "" for v in (data.get("repository"), data.get("project"),
                                                         data.get("branch"))]
    folder = os.path.basename(known[1].rstrip("/\\")) if known[1] else ""
    return known[0] or folder, known[2]


def session_of(window_title, titles):
    """the session whose title VS Code shows first in its window title, maybe cut with an ellipsis.
    A title not cut must be followed by " - ": a folder named like the start of one isn't its window"""
    whole = next((s for t, s in titles.items() if len(t) >= 8 and window_title.startswith(f"{t} - ")), None)
    head, cut, _rest = window_title.partition("…")
    if whole or not cut or len(head.strip()) < 8:
        return whole
    return next((s for t, s in titles.items() if t.startswith(head.strip())), None)


def whole_title(window_title, title):
    """a VS Code window title with its session's `title`, which it cut with an ellipsis, made whole"""
    head, cut, rest = window_title.partition("…")
    return f"{title}{rest}" if cut and title.startswith(head.strip()) else window_title


SESSION_HISTORY = dt.timedelta(days=14)
COMMAND = re.compile(r"<command-name>(/[\w:.-]+)</command-name>")


def session_of_turn(t):
    return (t.get("data", {}).get("session_id") or "")[:8]


def agent_stream(t):
    provider = t.get("data", {}).get("agent")
    name = {"github-copilot": "copilot"}.get(provider, "claude")
    return f"{name} {session_of_turn(t)}"


def sessions(turns, cards, me):
    """per session (the first 8 characters of its id): its card, its folder's in folder_cards, else
    the first one its prompts give, kept for the whole session (DESIGN.md); its title; the slash
    command it started with; and whether it's a /loop, whose turns aren't work you did"""
    out = {}
    for t in sorted(turns, key=lambda t: t["start"]):
        data = t.get("data", {})
        empty = {"card": None, "title": "", "command": "", "loop": False, "prompted": False}
        info = out.setdefault(session_of_turn(t), empty)
        info["title"] = data.get("title") or info["title"]
        if data.get("trigger") not in ("human", "command"):
            continue
        text = data.get("prompt") or ""
        if not info["prompted"]:
            # the watcher's `command` field; turns recorded before it had the command in the prompt
            m = COMMAND.search(text) if data["trigger"] == "command" else None
            command = data.get("command") or (m.group(1) if m else "")
            info.update(prompted=True, command=command, loop=command == "/loop")
        folder = config.folder_card(data.get("cwd"))
        if folder:
            info["card"] = model.split_review(folder)
        elif info["card"] is None:
            info["card"] = prompt_card(text, data.get("cwd") or "", cards, me)
    return out


# a multi-line paste; prompts recorded before the watcher kept them whole lose the closing tag
PASTED = re.compile(r"<pasted_content[^>]*>.*?(?:</pasted_content[^>]*>|\Z)", re.S)


def prompt_card(text, cwd, cards, me):
    """(card, review) a prompt gives: a card key, else a PR's card (a review when it's someone
    else's PR), else the tooling card for tooling work; None without any. Keys and PRs in a
    multi-line paste (a log, a terminal) don't count; its words still tell tooling work."""
    typed = PASTED.sub(" ", text)
    card, review = model.card_of_text(typed, cards), False
    for repo, number in config.PR_URL.findall(typed):
        info = model.pr_info(repo, number)
        card = card or model.card_of_pr(info, cards)
        review = review or ("review" in typed.lower() and model.is_review(info, me))
    tooling = config.TOOLING.search(text) or cwd.startswith(config.CLAUDE_DIR)
    if not card and config.TOOLING_CARD and tooling:
        card = config.TOOLING_CARD
    return (card, review) if card else None


def prompts(turns, info):
    """your prompts (and slash commands), each at the start of the turn it began, with its
    session's card"""
    out = []
    for t in turns:
        data = t.get("data", {})
        # a turn continued after a blocked Stop is the same prompt's work, not a new prompt
        if data.get("trigger") not in ("human", "command") or data.get("continued"):
            continue
        session = session_of_turn(t)
        card, review = info[session]["card"] or (None, False)
        stream = agent_stream(t)
        out.append(model.Event(t["start"], card, review, stream, "claude",
                               f"{stream} {data.get('project', '')}", (data.get("prompt") or "")[:200],
                               until=t["start"], ref=data.get("turn_id")))
    return out


# what aw-watcher-agents writes of a turn's usage, from its version with it
TOKENS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def worked(t):
    """a turn's stretches of work: its time without its waits for your approval (`waits`, written by
    aw-watcher-agents from its version with them)"""
    pieces = [(t["start"], t["end"])]
    for since, until in t.get("data", {}).get("waits") or []:
        w0, w1 = (dt.datetime.fromisoformat(x).astimezone() for x in (since, until))
        pieces = [p for a, b in pieces for p in ((a, min(b, w0)), (max(a, w1), b)) if p[1] > p[0]]
    return pieces


def agent_turns(turns, info):
    """the turns as events with a duration, on their session's card, without their waits: while it
    waits for your approval, the agent doesn't work, you do"""
    out = []
    for t in turns:
        data = t.get("data", {})
        session = agent_stream(t)
        card, review = info[session_of_turn(t)]["card"] or (None, False)
        if data.get("subagent_id"):
            stream, text = f"{session}/{data['subagent_id'][:8]}", data.get("description", "")
        else:
            stream, text = session, data.get("prompt", "")
        tokens = tuple(data.get(k) or 0 for k in TOKENS)
        out += [model.Event(a, card, review, stream, "aw-agent", "activitywatch agents", text, until=b,
                            ref=data.get("turn_id"), tokens=tokens if i == 0 and any(tokens) else ())
                for i, (a, b) in enumerate(worked(t))]
    return out
