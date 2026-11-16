"""Events, entries and arguments as the tests write them"""
import datetime as dt
import json
import re
import types


from timelog import cli, config, jira_rest, model, push, reviewing, sources, store, sync, timeline


DATE = "2026-10-20"


def at(hhmm):
    return model.at(DATE, hhmm)


def ev(hhmm, card, stream="claude s1", kind="aw-agent", review=False, text="", minutes=5):
    """an event; an agent's turn (aw-agent) lasts `minutes`"""
    until = at(hhmm) + dt.timedelta(minutes=minutes) if kind == "aw-agent" else None
    return model.Event(at(hhmm), card, review, stream, kind, stream, text, until=until)


def mine(hhmm, card, kind="focus", text="", minutes=5):
    """a piece of your lane"""
    return model.Event(at(hhmm), card, False, f"me {kind}", kind, "activitywatch window", text,
                       until=at(hhmm) + dt.timedelta(minutes=minutes))


def prompt(hhmm, card, stream="claude s1", text="", ref=None):
    return model.Event(at(hhmm), card, False, stream, "claude", stream, text, until=at(hhmm), ref=ref)


def written_rules():
    """`rules` in the config file"""
    try:
        return json.loads(open(config.CONFIG).read()).get("rules", [])
    except OSError:
        return []


def turn(hhmm, minutes, session, prompt, trigger="human", **data):
    return aw_event(hhmm, minutes, session_id=session, turn_id=f"{session}-{hhmm}", prompt=prompt,
                    trigger=trigger, **data)


def entries_of(events, kept=(), end="18:00"):
    entries, breaks, notes = timeline.build(events, list(kept), at(end))
    return [(e.task, f"{e.start:%H:%M}", f"{e.end:%H:%M}") for e in entries], breaks, notes


def yours_of(events, end="18:00"):
    """your lane's pieces, merged per card they count for, before the agents' time is cut out of them"""
    per_task = {}
    pieces = [e for e in timeline.fold(events) if model.lane_of(e.stream) == "you"]
    for s0, s1, task, _stream in timeline.spans_of(timeline.timed(pieces), at(end)):
        per_task.setdefault(task, []).append((s0, s1, set()))
    return sorted((task, f"{a:%H:%M}", f"{b:%H:%M}")
                  for task, spans in per_task.items() for a, b, _ in timeline.merge(spans, config.MERGE_GAP))


def glance(hhmm, card, seconds, review=False):
    """a few seconds on a page that shows a card: a tab you passed through"""
    return mine(hhmm, card, "browser")._replace(until=at(hhmm) + dt.timedelta(seconds=seconds), review=review)


def ns(**kw):
    defaults = dict(date=DATE, date_given=True, write=False, task=[], entry=[], drop=[], comment=[], events=False,
                    rebuild=False)
    return types.SimpleNamespace(**{**defaults, **kw})


def worklogs():
    """the lines the review prints to enter in Tempo"""
    sync.print_worklogs(sync.worklog_lines(ns(), store.load_day(DATE)))


def aw_event(hhmm, minutes, **data):
    return {"timestamp": at(hhmm).isoformat(), "duration": minutes * 60, "data": data}


def wline(card, hhmm, minutes, comment=""):
    return push.Line(card, at(hhmm), minutes, comment)


def pushed(wid, card, hhmm, minutes, comment=""):
    return {"id": wid, "card": card, "started": f"{at(hhmm):%Y-%m-%dT%H:%M:%S.000%z}", "minutes": minutes,
            "comment": comment}


class FakeJira(jira_rest.Client):
    """Jira's REST API as the tests need it: issues, worklogs, and the calls received"""

    def __init__(self, issues, worklogs):
        self.issues, self.logs, self.calls, self.next_id = issues, worklogs, [], 900

    def call(self, method, path, body=None, params=None):
        self.calls.append((method, path, body, params))
        if path == "/myself":
            return {"accountId": "me"}
        if path == "/search/jql":
            jql = body["jql"]
            if jql.startswith("key in"):
                keys = re.findall(r"[A-Z]+-\d+", jql)
                return {"issues": [{"key": k, "fields": self.issues[k]} for k in keys if k in self.issues]}
            if jql.startswith("worklogAuthor"):
                return {"issues": [{"key": k} for k in sorted({w["card"] for w in self.logs})]}
            return {"issues": [{"key": "GHI-1", "fields": {"summary": "to review"}}]}
        if method == "GET":  # a card's worklogs, two per page
            card = path.split("/")[2]
            mine = [w for w in self.logs if w["card"] == card]
            page = mine[params["startAt"]:params["startAt"] + 2]
            return {"worklogs": page, "total": len(mine)}
        if method == "DELETE":
            return None
        if method == "PUT":
            return {"id": path.rsplit("/", 1)[-1]}
        self.next_id += 1
        return {"id": str(self.next_id)}


def jira_worklog(wid, card, hhmm, minutes, author="me", comment="x"):
    return {"id": wid, "card": card, "author": {"accountId": author}, "started": f"{at(hhmm):%Y-%m-%dT%H:%M:%S.000%z}",
            "timeSpentSeconds": minutes * 60,
            "comment": jira_rest.document(comment)}


def review_day(days, monkeypatch, edits, answer=""):
    """runs `timelog` (the review) on a day, with `edits` as successive editor results: functions of
    the file's text"""
    events = [ev("09:00", "ABC-1", minutes=30), mine("10:00", None, text="Q4 planning.ods", minutes=30)._replace(
        keys=("Q4 planning.ods",)), mine("11:00", None, text="news", minutes=10)._replace(keys=("news",))]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    monkeypatch.setattr(jira_rest, "available", lambda: False)
    monkeypatch.setattr("builtins.input", lambda question: answer)  # entered in Tempo?
    seen = []

    def edit(text):
        seen.append(text)
        return edits[len(seen) - 1](text)
    monkeypatch.setattr(reviewing, "edit", edit)
    reviewing.cmd_review(ns())
    return seen


def on(date, hhmm, card, minutes=30):
    """an agent's turn on another day than DATE"""
    start = model.at(date, hhmm)
    return model.Event(start, card, False, "claude s1", "aw-agent", "claude s1", "",
                       until=start + dt.timedelta(minutes=minutes))


def asked_day(days, monkeypatch, answers, edits=(), **kw):
    """runs `timelog` with its questions on review_day's day, `answers` typed in turn, `edits` as the
    editor's results; returns the prompts and what the editor was given"""
    events = [ev("09:00", "ABC-1", minutes=30), mine("10:00", None, text="Q4 planning.ods", minutes=30)._replace(
        keys=("Q4 planning.ods",)), mine("11:00", None, text="news", minutes=10)._replace(keys=("news",))]
    monkeypatch.setattr(sources, "day_events", lambda date, cards: events)
    monkeypatch.setattr(jira_rest, "available", lambda: False)
    monkeypatch.setattr(reviewing, "interactive", lambda a: True)
    monkeypatch.setattr(config, "TRACKER", {**config.TRACKER, "admin_card": "ADM-1"})
    answers, prompts, seen = list(answers), [], []

    def typed(prompt):
        prompts.append(prompt)
        return answers.pop(0)
    monkeypatch.setattr("builtins.input", typed)
    monkeypatch.setattr(reviewing, "edit", lambda text: seen.append(text) or edits[len(seen) - 1](text))
    done = reviewing.review_day(ns(**kw))
    return done, prompts, seen


def chat(hhmm, minutes):
    return mine(hhmm, None, text="Slack - dev", minutes=minutes)._replace(keys=("Slack - dev",), side=True)


MONDAY = dt.date(2026, 11, 16)


PARSE_DATE = cli.parse_date
