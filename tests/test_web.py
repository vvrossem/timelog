"""`timelog serve`: the day as the page gets it, its answers, and the server around it"""
import datetime as dt
import http.client
import json
import os
import threading

import pytest

from timelog import model, sources, store, web
from helpers import DATE, at, ev, mine, written_rules


def window(hhmm, minutes, title):
    return model.Event(at(hhmm), None, False, "me focus", "focus", "activitywatch window", title,
                       until=at(hhmm) + dt.timedelta(minutes=minutes), keys=(title,), app="libreoffice")


def day_events():
    """an agent on ABC-1, a spreadsheet twice without a card, DEF-2, away, and two stretches with
    nothing active"""
    return sorted([ev("09:00", "ABC-1", minutes=30), window("10:00", 20, "Q4 planning.ods"),
                   mine("10:30", "DEF-2", minutes=20), window("11:30", 15, "Q4 planning.ods"),
                   model.Event(at("10:50"), None, False, "me away", "away", "afk", "", until=at("11:10"))],
                  key=lambda e: e.when)


@pytest.fixture
def built(days, monkeypatch):
    """the server builds the day from day_events, with nothing kept from another test"""
    monkeypatch.setattr(sources, "day_events", lambda date, cards: day_events())
    monkeypatch.setattr(web.Handler, "built", {})


def test_the_day_has_a_lane_per_agent_session_and_flags_what_to_answer(built, days):
    view = web.day_view(DATE, web.build(DATE))
    assert view["lanes"] == ["you", "agent s1"]
    assert [(e["n"], e["task"], e["lane"], e["kind"]) for e in view["entries"]] == [
        (1, "ABC-1", "agent s1", "agent"), (2, "?", "you", "todo"), (3, "DEF-2", "you", "work"), (4, "?", "you", "todo")]
    assert view["away"] == [{"start": at("10:50").isoformat(), "end": at("11:10").isoformat()}]
    assert [g["question"] for g in view["gaps"]] == [0, 2]  # the stretches with nothing active, to answer
    # the worklogs total is the list's: the `?` entries have none
    assert [(w["card"], w["minutes"]) for w in view["worklogs"]] == [("ABC-1", 30), ("DEF-2", 20)]
    assert [(t["what"], t["time"]) for t in view["totals"]] == [("worklogs", "0h50"), ("logged", "1h25")]
    assert not os.path.exists(days)  # reading saves nothing


def test_the_questions_are_the_reviews(built):
    asked = web.day_view(DATE, web.build(DATE))["questions"]
    assert [(q["kind"], q["span"], q["entries"]) for q in asked] == [
        ("nothing active", "09:30-10:00 · 0h30", []), ("your windows", "10:00-10:20 · 0h20", [2]),
        ("nothing active", "10:50-11:30 · 0h40", []), ("your windows", "11:30-11:45 · 0h15", [4])]
    spreadsheet = asked[1]
    assert [(o["task"], o["why"]) for o in spreadsheet["options"]] == [("ABC-1", "just before"), ("DEF-2", "just after")]
    assert spreadsheet["scopes"] == ['the libreoffice window "Q4 planning.ods"']
    assert {"label": "what was open", "lines": [" 20m  libreoffice  Q4 planning.ods"]} in spreadsheet["sections"]


def test_an_entry_shows_what_it_is_made_of(built):
    b = web.build(DATE)
    entry = web.entry_view(b["day"], b["events"], 3)
    assert (entry["task"], entry["minutes"]) == ("DEF-2", 20)
    assert entry["pieces"] == [{"stream": "me focus", "label": "", "seconds": 1200}] and entry["glances"] == []


def answer(hhmm, task, scope=None):
    """answers the question starting at hhmm, as the page does: on the day it just read"""
    b = web.build(DATE)
    q = next(q for q in web.day_view(DATE, b)["questions"] if q["start"] == at(hhmm).isoformat())
    return web.answer(DATE, b, {"question": q["id"], "start": q["start"], "task": task, "scope": scope})


def test_a_card_goes_to_the_window_everywhere_that_day_and_is_saved(built):
    assert answer("10:00", "def-2") == 2  # the same spreadsheet at 11:30 takes it too
    view = web.day_view(DATE, web.build(DATE))
    assert [(e["task"], e["start"][11:16]) for e in view["entries"] if e["lane"] == "you"] == [
        ("DEF-2", "10:00"), ("DEF-2", "10:30"), ("DEF-2", "11:30")]
    assert [q["kind"] for q in view["questions"]] == ["nothing active", "nothing active"]
    assert written_rules() == []  # for that day only


def test_a_break_and_a_rule(built):
    answer("09:30", "-")  # a break
    answer("10:00", "TOOL-1", scope=0)
    saved = store.load_day(DATE)
    assert saved["breaks"] == [[at("09:30").isoformat(), at("10:00").isoformat()]]
    assert [q["span"] for q in web.day_view(DATE, web.build(DATE))["questions"]] == ["10:50-11:30 · 0h40"]
    assert [(r["regex"], r["card"]) for r in written_rules()] == [(r"^Q4\ planning\.ods$", "TOOL-1")]


def test_a_dropped_window_leaves_the_day_everywhere_that_day(built):
    assert answer("11:30", "-") == 2
    view = web.day_view(DATE, web.build(DATE))
    assert [e["task"] for e in view["entries"]] == ["ABC-1", "DEF-2"]


def test_an_answer_to_a_day_the_page_read_before_it_changed_is_refused(built):
    b = web.build(DATE)
    with pytest.raises(web.Stale):
        web.answer(DATE, b, {"question": 1, "start": at("08:00").isoformat(), "task": "ABC-1"})
    with pytest.raises(web.Invalid, match="a card like ABC-1"):
        web.answer(DATE, b, {"question": 1, "start": at("10:00").isoformat(), "task": "planning"})
    with pytest.raises(web.Invalid, match="no such scope"):
        web.answer(DATE, b, {"question": 0, "start": at("09:30").isoformat(), "task": "-", "scope": 0})


def entries_now():
    return [(e["n"], e["task"], e["start"][11:16]) for e in web.day_view(DATE, web.build(DATE))["entries"]]


def edit(task, *numbers, scope=None):
    """sets entries as the page does: on the day it just read"""
    b = web.build(DATE)
    wanted = [{"n": n, "start": b["day"]["entries"][n - 1].start.isoformat()} for n in numbers]
    return web.set_entries(DATE, b, {"entries": wanted, "task": task, "scope": scope})


def test_an_entry_with_a_card_takes_another_one_or_is_dropped(built):
    assert web.entry_view(web.build(DATE)["day"], day_events(), 2)["scopes"] == ['the libreoffice window "Q4 planning.ods"']
    edit("XYZ-9", 3)  # DEF-2 at 10:30
    assert (3, "XYZ-9", "10:30") in entries_now()
    edit("-", 1)  # the agent's ABC-1
    assert [task for _n, task, _start in entries_now()] == ["?", "XYZ-9", "?"]
    with pytest.raises(web.Stale):
        web.set_entries(DATE, web.build(DATE), {"entries": [{"n": 1, "start": at("08:00").isoformat()}], "task": "A-1"})


def test_a_card_given_to_an_entry_can_become_a_rule(built):
    edit("TOOL-1", 2, scope=0)
    assert [(r["regex"], r["card"]) for r in written_rules()] == [(r"^Q4\ planning\.ods$", "TOOL-1")]
    assert [task for _n, task, _start in entries_now()] == ["ABC-1", "TOOL-1", "DEF-2", "?"]  # the rule applies
    # when ActivityWatch is read, which day_events stands for here


def test_what_was_answered_is_listed_and_can_be_taken_back(built):
    answer("09:30", "-")
    answer("10:50", "XYZ-9")
    edit("-", 1)
    b = web.build(DATE)
    answers = web.day_view(DATE, b)["answers"]
    assert [(a["kind"], a["start"][11:16], a.get("task")) for a in answers] == [
        ("dropped", "09:00", "ABC-1"), ("break", "09:30", None), ("added", "10:50", "XYZ-9")]
    for a in answers[:2]:
        web.undo(DATE, web.build(DATE), a)
    view = web.day_view(DATE, web.build(DATE))
    assert [a["kind"] for a in view["answers"]] == ["added"]
    assert view["entries"][0]["task"] == "ABC-1"  # back as it was
    assert [q["span"] for q in view["questions"]][:1] == ["09:30-10:00 · 0h30"]  # asked again


def test_a_handed_over_day_keeps_what_was_sent(built):
    answer("09:30", "-")
    day = store.load_day(DATE)
    day["pushed"] = [{"id": "1", "card": "ABC-1", "started": "", "minutes": 30, "comment": ""}]
    store.save_day(DATE, day, quiet=True)
    b = web.build(DATE)
    with pytest.raises(web.Invalid, match="handed over"):
        web.undo(DATE, b, web.day_view(DATE, b)["answers"][0])


@pytest.fixture
def server(built):
    httpd = web.server(0)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, path, method="GET", host=None, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    sent = {"Host": host or f"127.0.0.1:{port}"}
    if method == "POST":
        sent |= {"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=sent | (headers or {}))
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, res.getheader("Content-Type"), data


def test_the_server_gives_the_page_and_the_day_and_takes_answers(server):
    status, kind, body = call(server, "/")
    assert status == 200 and kind.startswith("text/html") and b"<title>timelog</title>" in body
    status, kind, body = call(server, f"/api/day?date={DATE}")
    assert status == 200 and kind.startswith("application/json")
    question = json.loads(body)["questions"][1]
    status, _kind, body = call(server, f"/api/entry?date={DATE}&n=3")
    assert status == 200 and json.loads(body)["task"] == "DEF-2"
    status, _kind, body = call(server, "/api/answer", "POST", body={
        "date": DATE, "question": 1, "start": question["start"], "task": "ABC-1", "scope": None})
    assert (status, json.loads(body)) == (200, {"changed": 2})
    assert [e.task for e in store.load_day(DATE)["entries"]].count("ABC-1") == 3


@pytest.mark.parametrize("path, method, host, headers, status", [
    ("/api/day?date=2026-02-30", "GET", None, None, 400),
    ("/api/day?date=2999-01-01", "GET", None, None, 400),
    (f"/api/entry?date={DATE}&n=9", "GET", None, None, 400),
    ("/nope", "GET", None, None, 404),
    ("/api/day", "GET", "evil.example:5610", None, 403),
    ("/api/answer", "POST", None, {"Origin": "https://evil.example"}, 403),
    ("/api/answer", "POST", None, {"Origin": ""}, 403),
    ("/api/answer", "POST", None, {"Content-Type": "text/plain"}, 415),
    ("/api/answer", "POST", None, None, 409),  # the page never read that day
    ("/api/day", "DELETE", None, None, 405),
])
def test_the_server_refuses_what_it_shouldnt_answer(server, path, method, host, headers, status):
    body = {"date": DATE, "question": 0, "start": "", "task": "ABC-1"} if method == "POST" else None
    assert call(server, path, method, host, body, headers)[0] == status
    assert not os.path.exists(store.day_path(DATE))


def test_activitywatch_down_is_said_on_the_page(days, monkeypatch):
    def down(date):
        raise SystemExit("# ActivityWatch doesn't answer at http://localhost:5600: start it first")
    monkeypatch.setattr(web, "build", down)
    monkeypatch.setattr(web.Handler, "built", {})
    httpd = web.server(0)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    try:
        status, _kind, body = call(httpd.server_address[1], f"/api/day?date={DATE}")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 503 and json.loads(body)["error"].startswith("ActivityWatch doesn't answer")
