import re

from .. import config
from .. import model


def classify(url, title, cards, me):
    """(card, review) of a PR, a commit or a card page; None for any other page"""
    pr = config.PR_URL.search(url)
    if pr:
        info = model.pr_info(*pr.groups())
        card = model.card_of_title(title, cards) or model.card_of_pr(info, cards)
        review = model.is_review(info, me)
    elif re.search(r"github\.com/[\w.-]+/[\w.-]+/commit/", url) or config.JIRA_KEY.search(url):
        jira = config.JIRA_KEY.search(url)
        card = model.card_of_title(title, cards) or model.card_of_text(url, cards) or (jira and jira.group(1))
        review = False
    else:
        return None  # lists, home pages, notifications: no card to read
    return card, review
