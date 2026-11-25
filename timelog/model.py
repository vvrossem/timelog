import datetime as dt
import glob
import json
import os
import re
import subprocess
from typing import NamedTuple, Optional

from . import config
from . import style


class Event(NamedTuple):
    when: dt.datetime
    card: Optional[str]
    review: bool
    stream: str  # a Claude session or subagent, a call, your lane ("me <kind>")
    kind: str  # claude (a prompt), aw-agent (a turn), call, focus (a window),
    #            browser (a PR or card tab), meet (a Meet tab), presence (dropped by a rule), away
    source: str
    text: str
    until: Optional[dt.datetime] = None  # end of an event that has a duration
    ref: Optional[str] = None  # the turn a prompt starts or a turn is
    keys: tuple = ()  # its name, as the review shows it: see attribution
    side: bool = False  # a side activity: doesn't change your current card
    guessed: bool = False  # its card came from a rule or an answer, not from what it shows
    tokens: tuple = ()  # an agent turn's (input, output, cache write, cache read), on its first stretch
    app: str = ""  # the application of a piece of your lane, as ActivityWatch names it


class Entry(NamedTuple):
    task: str
    start: dt.datetime
    end: dt.datetime
    source: str  # auto or manual
    streams: tuple = ()
    agent: int = 0  # the minutes of it an agent worked: the rest is yours


def lane_of(streams):
    """the lane of a stream, or of an entry's streams: `agent <session>` (its subagents in it), else
    `you` (your windows and calls, manual entries)"""
    names = [streams] if isinstance(streams, str) else list(streams)
    if names and all(s.startswith("claude ") for s in names):
        return f"agent {min(s.removeprefix('claude ').split('/')[0] for s in names)}"
    return "you"


def is_unattributed(task):
    """`?`, or `? <what>` for one still to attribute that should stay apart (a call)"""
    return task == config.UNATTRIBUTED or task.startswith(f"{config.UNATTRIBUTED} ")


def task_of(card, review):
    if not card:
        return config.UNATTRIBUTED
    return f"{card}: PR review" if review else card


def card_regex():
    """the card prefixes of the config and of the worklogs already pushed: Jira accepted those cards,
    while an entry's task can be a look-alike (UTF-8)"""
    prefixes = set(config.CARD_PREFIXES)
    texts = []
    for path in glob.glob(f"{config.DAY_DIR}/*.json"):
        try:
            with open(path) as f:
                texts += [p["card"] for p in json.load(f).get("pushed", [])]
        except (OSError, ValueError, KeyError):
            continue
    for text in texts:
        m = re.match(r"\s*([A-Z][A-Z0-9]*)-\d+", text)
        if m:
            prefixes.add(m.group(1))
    if config.TOOLING_CARD:
        prefixes.add(config.TOOLING_CARD.split("-")[0])
    if not prefixes:
        return re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
    return re.compile(r"\b(?:%s)-\d+\b" % "|".join(sorted(prefixes, key=len, reverse=True)))


def load_pr_cache():
    try:
        with open(config.PR_CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_pr_cache = load_pr_cache()
_pr_failed = set()  # lookups that failed in this run: not retried at every visit of the PR


def gh(*args):
    """gh's output, or None when gh is missing or fails (not logged in, no access)"""
    try:
        with style.working("asking GitHub"):
            res = subprocess.run(["gh", *args], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    return res.stdout if res.returncode == 0 else None


def is_review(info, me):
    """someone else's PR; without your login (gh missing or logged out), none is"""
    return bool(me) and (info.get("author") or {}).get("login") not in (None, me)


def pr_info(repo, number):
    key = f"{repo}#{number}"
    if key in _pr_failed:
        return {}
    if "body" not in _pr_cache.get(key, {}):  # entries cached before the description was read
        out = gh("pr", "view", number, "-R", repo, "--json", "title,headRefName,author,body")
        if out is None:
            _pr_failed.add(key)
            return {}
        _pr_cache[key] = json.loads(out)
        os.makedirs(os.path.dirname(config.PR_CACHE), exist_ok=True)
        with open(config.PR_CACHE, "w") as f:
            json.dump(_pr_cache, f)
    return _pr_cache[key]


def split_review(task):
    """(card, review) of a task like ABC-1 or ABC-1: PR review"""
    return (task[: -len(": PR review")], True) if task.endswith(": PR review") else (task, False)


def card_of_text(text, cards):
    m = cards.search(text or "")
    return m.group(0) if m else None


def card_of_pr(info, cards):
    """a PR's card: from its branch (any case: ghi-310-...), its title, or its description, where a
    Jira link names it even for a project never logged yet"""
    branch = (info.get("headRefName") or "").upper()
    body = info.get("body") or ""
    browse = config.BROWSE_KEY.search(body)
    return (card_of_text(branch, cards) or card_of_title(info.get("title"), cards) or card_of_text(body, cards)
            or (browse and browse.group(1)))


def card_of_title(title, cards):
    """a PR or Jira page title names its card first ("MNO-312: ...", "[MNO-312] ..."), even for
    a project never logged yet"""
    m = config.TITLE_KEY.match(title or "")
    return card_of_text(title, cards) or (m and m.group(1))


def at(date, hhmm):
    return dt.datetime.fromisoformat(f"{date}T{hhmm}").astimezone()


def round_minute(when):
    return (when + dt.timedelta(seconds=30)).replace(second=0, microsecond=0)


# the apps' short names, as ActivityWatch's window watcher gives them (lower case); others as they come
APPS = {"firefox": "Firefox", "firefox-default": "Firefox", "firefox-esr": "Firefox", "google-chrome": "Chrome",
        "chromium": "Chromium", "brave-browser": "Brave", "com.microsoft.vscode": "VS Code", "code": "VS Code",
        "code-oss": "VS Code", "slack": "Slack", "org.gnome.nautilus": "Files", "gnome-terminal-server": "Terminal",
        "org.gnome.terminal": "Terminal"}


def app_name(app):
    """an app's short name; none for one ActivityWatch couldn't tell"""
    return "" if app.lower() in ("", "unknown") else APPS.get(app.lower(), app)


def without_app(name, app):
    """a name without the app it starts with: a rule names Slack's windows "Slack - <channel>" """
    return name[len(app) + 3:] if app and name.lower().startswith(f"{app.lower()} - ") else name
