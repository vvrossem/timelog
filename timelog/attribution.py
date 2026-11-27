"""Which task an event without a card goes to, after the sources have looked for a card key
(DESIGN.md): the rules on its match string, yours (`rules` in the config, which the review adds to)
before the built-in ones.

The match string is a piece's (its tab's "title|url", an editor's "repository|branch", a window's
title), a call's, or for an agent's prompt or turn its session's "title|/command". The name is the
matching rule's, or the title.

A dropped event (the task `-`) keeps you present (no false gap) but gives no card any time.
"""
from . import config
from . import model
from . import rules as match_rules

DROP = config.DROP
NO_CARD_KINDS = ("presence", "away")


def string_of(e, sessions):
    if model.is_agent_stream(e.stream):
        info = sessions.get(model.agent_session(e.stream), {})
        return f"{info.get('title', '')}|{info.get('command', '')}"
    return e.text


def apply(events, sessions, rules=None):
    """the events with their name (`keys`) and side-activity flag, and a card for those without one
    when a rule gives it"""
    out = []
    for e in events:
        if e.kind in NO_CARD_KINDS or not (model.is_agent_stream(e.stream) or e.stream.startswith("me ")
                                            or e.kind == "call"):
            out.append(e)
            continue
        rule, name, card = match_rules.match(string_of(e, sessions), rules, e.kind)
        e = e._replace(keys=(name,) if name else (), side=bool(rule and rule.get("side_activity")))
        if not e.card and card == DROP:
            e = e._replace(kind="presence")
        elif not e.card and card:
            card, review = model.split_review(card)
            e = e._replace(card=card, review=review, guessed=True)
        out.append(e)
    return out
