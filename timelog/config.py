import datetime as dt
import json
import locale
import os
import re

HOME = os.path.expanduser("~")
CONFIG = os.environ.get("TIMELOG_CONFIG", f"{HOME}/.config/timelog/config.json")
DAY_DIR = os.environ.get("TIMELOG_DIR", f"{HOME}/.local/share/timelog")
BACKUPS = f"{DAY_DIR}/backups"
CACHE_DIR = f"{HOME}/.cache/timelog"  # PR lookups, and the Jira answers the session writes
PR_CACHE = f"{CACHE_DIR}/prs.json"
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or f"{HOME}/.claude"  # an agent working here does tooling work


# every setting of config.json, with its default; `timelog config` prints the effective ones
DEFAULTS = {
    "activitywatch_url": "http://localhost:5600",
    "card_prefixes": [],
    "tooling_keywords": [],  # a prompt matching one of them goes to tracker.tooling_card
    "ignored_folders": [],  # agent sessions run in these folders (or below) aren't counted: personal projects
    # {folder: card}: agent sessions and editor time in that folder (or below) go to that card, whatever
    # card the prompts or the branch name. A bare name is any folder of that name: a repository's clones
    "folder_cards": {},
    "rules": [],  # tried before the defaults of rules.py: see DESIGN.md
    "start_date": None,  # YYYY-MM-DD: the catch-up offers no day before it, e.g. the day you started
    "date_order": None,  # dmy, mdy or ymd: how 14/11/26 reads; your locale's (LC_TIME) when not set
    "summary_width": None,  # the cards' Jira summaries offered in the review's questions: whole, or cut to this
    # your windows and tabs while an agent turn runs, on top of the agents' time: "other_cards"
    # count but on the cards of the agents running, "calls" don't. Your calls and manual entries
    # count either way
    "your_time_during_agent_turns": "other_cards",
    "timing": {  # in minutes
        "settle": 1,  # a piece of your lane this long makes its card your current one: a glance doesn't
        "break_gap": 20,  # nothing active this long: asked about, and ends your current card
        "merge_gap": 1,  # a card's stretches this close are joined
        "join_gap": 30,  # your entries of a card this close make one row of the review
        "round": 5,  # each worklog is rounded to a multiple of this, at least one: 1 is to the minute
        "round_mode": "nearest",  # nearest: 12 -> 10, 13 -> 15; up: 11 -> 15
    },
    "tracker": {
        "kind": "jira",
        "site": None,  # e.g. example.atlassian.net: Jira's host, also the Atlassian connector's cloudId
        "email": None,  # your Atlassian account's email: with an API token, timelog reaches Jira itself
        "token_command": None,  # prints the API token, e.g. `lpass show XXXX -p`, when JIRA_API_TOKEN isn't set
        "tooling_card": None,  # training and tooling work
        "admin_card": None,  # timesheeting, catch-up
        "retired_cards": [],  # never proposed, refused by `push` and the review
        "review_jql": None,  # cards offered for unattributed entries, e.g. the review queue
    },
}

DURING_AGENTS_CHOICES = ("other_cards", "calls")
ROUND_MODES = ("nearest", "up")
UNATTRIBUTED = "?"
DROP = "-"  # as a task: the time goes to no card

PR_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
BROWSE_KEY = re.compile(r"/browse/([A-Z][A-Z0-9]+-\d+)")
TITLE_KEY = re.compile(r"\[?([A-Z][A-Z0-9]+-\d+)[]:]")


def load_config(path=None):
    """(the user's config, what makes the file unreadable): missing or empty, it's the defaults"""
    path = path or CONFIG
    try:
        with open(path) as f:
            text = f.read()
        user = json.loads(text) if text.strip() else {}
    except OSError:
        return {}, None
    except ValueError as e:
        return {}, f"{path} isn't valid JSON ({e}): fix it first"
    if not isinstance(user, dict):
        return {}, f"{path} must hold a JSON object: fix it first"
    return user, None


def merged(user):
    """the defaults overridden by the user's values, section by section"""
    out = {**DEFAULTS, **user}
    for key, value in DEFAULTS.items():
        if isinstance(value, dict):
            out[key] = {**value, **user.get(key, {})}
    return out


def problems(user):
    """what's wrong in the user's config: unknown keys, values out of their choices"""
    found = [f"unknown key: {k}" for k in sorted(set(user) - set(DEFAULTS))]
    for section in ("timing", "tracker"):
        found += [f"unknown key: {section}.{k}" for k in sorted(set(user.get(section, {})) - set(DEFAULTS[section]))]
    settings = merged(user)
    if settings["tracker"]["kind"] != "jira":
        found.append(f"tracker.kind: {settings['tracker']['kind']} is not supported, only jira")
    if settings["start_date"] is not None and not iso_date(settings["start_date"]):
        found.append(f"start_date: {settings['start_date']} is not a date like 2026-11-16")
    if settings["your_time_during_agent_turns"] not in DURING_AGENTS_CHOICES:
        found.append(f"your_time_during_agent_turns: {settings['your_time_during_agent_turns']} is not one of "
                     f"{', '.join(DURING_AGENTS_CHOICES)}")
    if settings["timing"]["round_mode"] not in ROUND_MODES:
        found.append(f"timing.round_mode: {settings['timing']['round_mode']} is not one of {', '.join(ROUND_MODES)}")
    if not valid_width(settings["summary_width"]):
        found.append(f"summary_width: {settings['summary_width']} is not a number of characters above 0, or null")
    if settings["date_order"] not in (None, "dmy", "mdy", "ymd"):
        found.append(f"date_order: {settings['date_order']} is not one of dmy, mdy, ymd")
    return found


def valid_width(value):
    return value is None or isinstance(value, int) and not isinstance(value, bool) and value > 0


def iso_date(value):
    try:
        return dt.date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def order_of(d_fmt):
    """dmy, mdy or ymd, from a strftime date format such as %d/%m/%y"""
    places = {c: d_fmt.lower().find(f"%{c}") for c in "dmy"}
    return "".join(sorted(places, key=places.get)) if -1 not in places.values() else "ymd"


def locale_order():
    """the date order of your LC_TIME locale"""
    current = locale.setlocale(locale.LC_TIME)
    try:
        locale.setlocale(locale.LC_TIME, "")
        return order_of(locale.nl_langinfo(locale.D_FMT))
    except locale.Error:
        return "ymd"
    finally:
        locale.setlocale(locale.LC_TIME, current)


def expand(path):
    return path and os.path.expanduser(path)


_user, ERROR = load_config()
SETTINGS = merged(_user)
PROBLEMS = problems(_user)

ACTIVITYWATCH_URL = SETTINGS["activitywatch_url"]
CARD_PREFIXES = SETTINGS["card_prefixes"]
TOOLING = re.compile("|".join(map(re.escape, SETTINGS["tooling_keywords"])) or r"(?!)", re.I)
RULES = SETTINGS["rules"]
SUMMARY_WIDTH = SETTINGS["summary_width"] if valid_width(SETTINGS["summary_width"]) else None
START_DATE = iso_date(SETTINGS["start_date"])
DATE_ORDER = SETTINGS["date_order"] if SETTINGS["date_order"] in ("dmy", "mdy", "ymd") else locale_order()
DURING_AGENTS = (SETTINGS["your_time_during_agent_turns"]
                 if SETTINGS["your_time_during_agent_turns"] in DURING_AGENTS_CHOICES else "other_cards")
IGNORED_FOLDERS = [os.path.normpath(expand(p)) for p in SETTINGS["ignored_folders"]]
# the longest first: a subfolder's card wins over its parent's
FOLDER_CARDS = sorted(((k if os.sep not in k and not k.startswith("~") else os.path.normpath(expand(k)), v)
                       for k, v in SETTINGS["folder_cards"].items()), key=lambda kv: -len(kv[0]))


def add_rule(rule):
    """puts `rule` first in the config file's rules, the rest of the file kept: the latest answer
    is the most precise one"""
    user, error = load_config()
    if error:
        raise SystemExit(f"# no rule added: {error}")
    user["rules"] = [rule] + [r for r in user.get("rules", []) if r.get("regex") != rule["regex"]]
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(user, f, indent=1, ensure_ascii=False)
    RULES[:] = user["rules"]


def folder_card(path):
    """the card of folder_cards for a path: its folder or one above, or a folder of that name in it"""
    path = os.path.normpath(path) if path else ""
    for folder, card in FOLDER_CARDS if path else ():
        if (folder in path.split(os.sep) if os.sep not in folder
                else path == folder or path.startswith(folder + os.sep)):
            return card
    return None


def ignored(cwd):
    """a folder in ignored_folders, or below one"""
    cwd = os.path.normpath(cwd) if cwd else ""
    return any(cwd == p or cwd.startswith(p + os.sep) for p in IGNORED_FOLDERS)


_timing = {k: dt.timedelta(minutes=v) for k, v in SETTINGS["timing"].items() if k != "round_mode"}
SETTLE = _timing["settle"]
BREAK_GAP = _timing["break_gap"]
MERGE_GAP = _timing["merge_gap"]
JOIN_GAP = _timing["join_gap"]
ROUND = SETTINGS["timing"]["round"]
ROUND_MODE = SETTINGS["timing"]["round_mode"] if SETTINGS["timing"]["round_mode"] in ROUND_MODES else "nearest"

TRACKER = SETTINGS["tracker"]
TOOLING_CARD = TRACKER["tooling_card"]
RETIRED_CARDS = set(TRACKER["retired_cards"])
# the host of the card pages: the configured site, or any Atlassian cloud site
TRACKER_HOST = re.escape(TRACKER["site"]) if TRACKER["site"] else r"[\w-]+\.atlassian\.net"
JIRA_KEY = re.compile(rf"{TRACKER_HOST}/(?:browse/|.*[?&]selectedIssue=)([A-Z][A-Z0-9]*-\d+)")
