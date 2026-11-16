"""What `config` checks on this machine: the watchers timelog reads, `gh`, the Jira site."""
import json
import shutil
import urllib.request

from . import config
from . import jira_rest
from .sources import activitywatch


def missing_watchers():
    """the watchers timelog needs that ActivityWatch has no bucket of, or None when it doesn't answer"""
    try:
        with urllib.request.urlopen(f"{config.ACTIVITYWATCH_URL}/api/0/buckets/", timeout=2) as response:
            buckets = json.loads(response.read())
    except (OSError, ValueError):
        return None
    types = {b.get("type") for b in buckets.values()}
    return [name for kind, name in activitywatch.BUCKET_TYPES.items() if kind not in types]


def warnings(settings):
    out = []
    missing = missing_watchers()
    if missing is None:
        out.append(f"ActivityWatch doesn't answer at {config.ACTIVITYWATCH_URL}: timelog needs it, see the README")
    elif missing:
        out.append(f"ActivityWatch has no data from {', '.join(missing)}: timelog leaves them out")
    if not shutil.which("gh"):
        out.append("gh not found: PRs won't give their card, and reviews won't be told from your own PRs")
    tracker = settings["tracker"]
    jira = [name for name, ok in (("tracker.site", tracker["site"]), ("tracker.email", tracker.get("email")),
                                  ("an API token (JIRA_API_TOKEN or tracker.token_command)", jira_rest.token()))
            if not ok]
    if jira:
        out.append(f"Jira isn't set up, missing {', '.join(jira)}: the day is still built, cards aren't checked"
                   " and worklogs aren't pushed")
    cards = [name for name in ("tooling_card", "admin_card") if not tracker.get(name)]
    if cards:
        out.append(f"no tracker.{' nor tracker.'.join(cards)}: that time is asked about every day")
    if not settings["card_prefixes"]:
        out.append("card_prefixes is empty: anything shaped like a card key, e.g. UTF-8, counts as a card until a"
                   " worklog is pushed; list your projects' prefixes")
    return out
