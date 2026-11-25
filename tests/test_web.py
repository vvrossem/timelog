"""`timelog serve`: the day as the page gets it, and the server around it"""
import http.client
import json
import os
import threading

import pytest

from timelog import daily, model, web
from helpers import DATE, at, ev, mine


def day_events():
    return sorted([ev("09:00", "ABC-1", minutes=30), mine("10:00", None, minutes=20),
                   mine("10:30", "DEF-2", minutes=20),
                   model.Event(at("10:50"), None, False, "me away", "away", "afk", "", until=at("11:10"))],
                  key=lambda e: e.when)


@pytest.fixture
def built(days, monkeypatch):
    """the server builds the day from day_events, with nothing kept from another test"""
    def build(date):
        events = day_events()
        day, gaps, notes = daily.build_day(date, events)
        return day, events, gaps, notes
    monkeypatch.setattr(web, "build", build)
    monkeypatch.setattr(web.Handler, "built", {})


def test_the_day_has_a_lane_per_agent_session_and_flags_what_to_answer(days):
    events = day_events()
    day, gaps, notes = daily.build_day(DATE, events)
    view = web.day_view(DATE, day, events, gaps, notes)
    assert view["lanes"] == ["you", "agent s1"]
    assert [(e["n"], e["task"], e["lane"], e["kind"]) for e in view["entries"]] == [
        (1, "ABC-1", "agent s1", "agent"), (2, "?", "you", "todo"), (3, "DEF-2", "you", "work")]
    assert view["gaps"] == [{"start": at("09:30").isoformat(), "end": at("10:00").isoformat()}]
    assert view["away"] == [{"start": at("10:50").isoformat(), "end": at("11:10").isoformat()}]
    # the worklogs total is the list's: the `?` entry has none
    assert [(w["card"], w["minutes"]) for w in view["worklogs"]] == [("ABC-1", 30), ("DEF-2", 20)]
    assert [(t["what"], t["time"]) for t in view["totals"]] == [("worklogs", "0h50"), ("logged", "1h10")]
    assert not os.path.exists(days)  # read-only: no day file written


def test_an_entry_shows_what_it_is_made_of(days):
    events = day_events()
    day, _gaps, _notes = daily.build_day(DATE, events)
    entry = web.entry_view(day, events, 2)
    assert (entry["task"], entry["minutes"]) == ("?", 20)
    assert entry["pieces"] == [{"stream": "me focus", "label": "", "seconds": 1200}] and entry["glances"] == []


@pytest.fixture
def server(built):
    httpd = web.server(0)
    thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def get(port, path, method="GET", host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, headers={"Host": host or f"127.0.0.1:{port}"})
    res = conn.getresponse()
    body = res.read()
    conn.close()
    return res.status, res.getheader("Content-Type"), body


def test_the_server_gives_the_page_and_the_day(server):
    status, kind, body = get(server, "/")
    assert status == 200 and kind.startswith("text/html") and b"<title>timelog</title>" in body
    status, kind, body = get(server, f"/api/day?date={DATE}")
    assert status == 200 and kind.startswith("application/json")
    assert json.loads(body)["lanes"] == ["you", "agent s1"]
    status, _kind, body = get(server, f"/api/entry?date={DATE}&n=3")
    assert status == 200 and json.loads(body)["task"] == "DEF-2"


@pytest.mark.parametrize("path, method, host, status", [
    ("/api/day?date=2026-02-30", "GET", None, 400),
    ("/api/day?date=2999-01-01", "GET", None, 400),
    (f"/api/entry?date={DATE}&n=9", "GET", None, 400),
    ("/nope", "GET", None, 404),
    ("/api/day", "POST", None, 405),
    ("/api/day", "GET", "evil.example:5610", 403),
])
def test_the_server_refuses_what_it_shouldnt_answer(server, path, method, host, status):
    assert get(server, path, method, host)[0] == status


def test_activitywatch_down_is_said_on_the_page(days, monkeypatch):
    def down(date):
        raise SystemExit("# ActivityWatch doesn't answer at http://localhost:5600: start it first")
    monkeypatch.setattr(web, "build", down)
    httpd = web.server(0)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    try:
        status, _kind, body = get(httpd.server_address[1], f"/api/day?date={DATE}")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 503 and json.loads(body)["error"].startswith("ActivityWatch doesn't answer")
