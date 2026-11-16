"""What Jira says about the day's cards: `fetch` asks Jira (jira_rest.py), or else the session asks it
through the Atlassian connector, and writes the answer to CACHE_DIR/jira-<date>.json as

    {"known": {"ABC-641": {"summary": "...", "status": "Done"}},
     "candidates": {"GHI-357": "summary of a card waiting for review"},
     "worklogs": [{"id": "123", "card": "ABC-641", "started": "...", "minutes": 35, "comment": ""}],
     "truncated": ["ABC-5"]}

`known` holds the cards the `cards` command listed that Jira returned: a listed card missing from it
doesn't exist. `candidates` holds cards the user is likely to have worked on that day (the review
queue), offered for the unattributed entries. `worklogs` holds the user's worklogs of the day as Jira
has them, read before a push (see push.py); `truncated` the cards whose worklogs Jira's search cut off.
"""
import json
import os

from . import config
from . import model


def path(date):
    return f"{config.CACHE_DIR}/jira-{date}.json"


def save(date, answer):
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    with open(path(date), "w") as f:
        json.dump(answer, f, indent=1, ensure_ascii=False)


def load(date):
    """the day's Jira answer, or None when the session hasn't asked Jira"""
    try:
        with open(path(date)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return {"known": data.get("known", {}), "candidates": data.get("candidates", {}),
            "worklogs": data.get("worklogs"),  # None: the day's worklogs weren't read from Jira
            "truncated": data.get("truncated", [])}


def record_sent(date, sent, deleted=()):
    """the day's worklogs as Jira holds them once a push added, updated or deleted some, so that
    the next `push` plans from Jira's state, not from the one read before"""
    answer = load(date)
    if answer is None or answer["worklogs"] is None:
        return
    ids = {str(w["id"]) for w in sent} | set(deleted)
    answer["worklogs"] = [w for w in answer["worklogs"] if w["id"] not in ids] + [
        {"id": str(w["id"]), "card": w["card"], "started": w["started"], "minutes": int(w["minutes"]),
         "comment": w.get("comment", "")} for w in sent]
    save(date, answer)


def card_of_task(task, cards):
    return model.card_of_text(task, cards) or task.split(":", 1)[0]


def unknown(tasks, date, cards):
    """the cards of these tasks that Jira doesn't know, or None when Jira wasn't asked"""
    answer = load(date)
    if answer is None:
        return None
    return sorted({card_of_task(t, cards) for t in tasks if not model.is_unattributed(t)}
                  - set(answer["known"]))
