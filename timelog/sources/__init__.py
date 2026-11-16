import datetime as dt
import functools
import json

from .. import attribution
from .. import model
from . import activitywatch


@functools.lru_cache(maxsize=None)
def github_login():
    """your GitHub login, asked once per run: the catch-up builds a dozen days"""
    return (model.gh("api", "user", "-q", ".login") or "").strip()


@functools.lru_cache(maxsize=None)
def reviews():
    """(when, "owner/repo#N") of the reviews and review comments you posted on GitHub, from your
    events: GitHub keeps the last 300, of up to 90 days. Empty without gh or a login"""
    me = github_login()
    out = me and model.gh("api", "--paginate", f"users/{me}/events?per_page=100", "--jq",
                          '.[] | select(.type == "PullRequestReviewEvent" or .type == "PullRequestReviewCommentEvent")'
                          ' | [.created_at, .repo.name, .payload.pull_request.number]')
    found = [json.loads(line) for line in (out or "").splitlines() if line.strip()]
    return sorted({(dt.datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(), f"{repo}#{number}")
                   for when, repo, number in found})


def day_events(date, cards):
    start, end = model.at(date, "00:00"), model.at(date, "23:59")
    events, sessions = activitywatch.combine(start, end, cards, github_login())
    events = attribution.apply(events, sessions)
    return sorted(events, key=lambda e: e.when)
