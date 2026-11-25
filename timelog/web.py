"""The day in a browser: `timelog serve` shows the lanes, entries and totals of a day, and asks
about what has no card as the terminal review does, on a page served from this machine only"""
import argparse
import datetime as dt
import http.server
import importlib.resources
import json
import urllib.parse
import webbrowser

from . import cli
from . import config
from . import daily
from . import model
from . import questions
from . import reviewing
from . import sources
from . import store
from . import strip
from . import sync
from . import timeline

def build(date):
    """the day rebuilt from ActivityWatch with your corrections, as the review builds it, and not
    saved: {day, events, gaps, notes, asked}, `asked` the review's (row, question) pairs"""
    events = sources.day_events(date, model.card_regex())
    day, gaps, notes = daily.build_day(date, events)
    rows, labels = reviewing.review_rows(day, gaps, events)
    asked = reviewing.questions_of(date, day, rows, labels, events)
    return {"day": day, "events": events, "gaps": gaps, "notes": notes, "asked": asked}


def kind_of(entry):
    if model.is_unattributed(entry.task):
        return "todo"
    if model.lane_of(entry.streams) != "you":
        return "agent"
    return "call" if "call" in entry.streams else "work"


def span(start, end):
    return {"start": start.isoformat(), "end": end.isoformat()}


def question_view(i, row, q):
    """a question of the review, numbered as `asked` orders them"""
    return {**span(*q.mark), "id": i, "kind": q.kind, "span": q.span, "gap": q.gap, "name": q.name,
            "entries": [] if q.gap else list(row.indices or (row.index,)),
            "sections": [{"label": label, "lines": lines} for label, lines in q.sections if lines],
            "options": [{"task": task, "summary": summary, "why": why} for task, summary, why in q.options],
            "scopes": [what for what, _rule in q.scopes]}


def answers_of(day):
    """what you answered that day, from the day file: the breaks, the entries without a card you
    gave one or dropped, and the entries you added"""
    out = [{"kind": "break", "start": b0, "end": b1} for b0, b1 in day.get("breaks", [])]
    for o in day.get("overrides", []):
        if o["to"] == config.DROP:
            out.append({"kind": "dropped", "start": o["start"], "end": o["end"], "lane": o.get("lane"), "task": o["task"]})
        elif model.is_unattributed(o["task"]):
            out.append({"kind": "card", "start": o["start"], "end": o["end"], "lane": o.get("lane"), "task": o["to"]})
    out += [{"kind": "added", **span(e.start, e.end), "lane": "you", "task": e.task}
            for e in day["entries"] if e.source == "manual"]
    return sorted(out, key=lambda a: a["start"])


def day_view(date, built):
    """the day as the page draws it: entries numbered as `show` numbers them, per lane, and the
    questions"""
    day, events, gaps, notes = built["day"], built["events"], built["gaps"], built["notes"]
    entries = day["entries"]
    shown = [(n, e) for n, e in enumerate(entries, 1) if e.task != config.DROP]
    first_start = {}
    for _n, e in shown:
        lane = model.lane_of(e.streams)
        first_start[lane] = min(first_start.get(lane, e.start), e.start)
    agents = sorted((lane for lane in first_start if lane != "you"), key=lambda lane: (first_start[lane], lane))
    bounds = strip.bounds([e for _n, e in shown], gaps)
    lines = sync.day_worklogs(day, [e for _n, e in shown if not model.is_unattributed(e.task)], model.card_regex())
    worklogs = [{"card": w.card, "start": w.started.isoformat(), "minutes": w.minutes, "comment": w.comment}
                for w in lines]
    sent = dt.timedelta(minutes=sum(w.minutes for w in lines))  # the list's total: no `?` worklog
    return {
        "date": date,
        "label": daily.short_date(date),
        "today": dt.date.today().isoformat(),
        "bounds": span(*bounds) if bounds else None,
        "lanes": ["you", *agents],
        "entries": [{**span(e.start, e.end), "n": n, "task": e.task, "minutes": daily.minutes(e),
                     "lane": model.lane_of(e.streams), "kind": kind_of(e), "sources": cli.sources_of(e),
                     "comment": daily.comment_of(day, e.task) or "", "default": e.task not in day["comments"]}
                    for n, e in shown],
        "gaps": [{**span(a, b), "question": next((i for i, (_row, q) in enumerate(built["asked"])
                                                  if q.gap and f"{q.mark[0]:%H:%M}" == f"{a:%H:%M}"), None)}
                 for a, b in gaps],  # the review asks from the minute
        "away": [span(a, b) for a, b in strip.away_spans(entries, events)],
        "totals": [{"what": what, "time": time, "about": about}
                   for what, time, about in daily.logged_rows([e for _n, e in shown], sent)],
        "worklogs": worklogs,
        "notes": list(notes),
        "pushed": daily.handed_over(day),
        "questions": [question_view(i, row, q) for i, (row, q) in enumerate(built["asked"])],
        "answers": answers_of(day),
        "cards": sorted(set(reviewing.cards_to_offer(date, day))),
    }


def entry_view(day, events, n):
    """what entry N is made of, as `show N` prints it"""
    entry = day["entries"][n - 1]
    held, prompts = daily.entry_pieces(entry, day["entries"], events)
    glances = [(key, spent) for key, spent in held if spent < timeline.GLANCE]
    return {
        **span(entry.start, entry.end), "n": n, "task": entry.task, "minutes": daily.minutes(entry),
        "pieces": [{"stream": stream, "label": label, "seconds": round(spent.total_seconds())}
                   for (stream, label), spent in held if spent >= timeline.GLANCE],
        "glances": [{"stream": stream, "label": label, "seconds": round(spent.total_seconds())}
                    for (stream, label), spent in glances],
        "prompts": [{"when": p.when.isoformat(), "text": p.text} for p in prompts],
        "scopes": [what for what, _rule in reviewing.piece_scopes([entry], events)[1]],
    }


class Invalid(ValueError):
    pass


class Stale(ValueError):
    """the page shows another build of the day than the server holds"""


def answer(date, built, body):
    """applies an answer to question `question`: a card, or `-` (a break for a stretch with nothing
    active, else dropped), with `scope`, the rule it becomes from now on, or none. The questions about
    the same page or window take it too, as in the terminal. Returns how many questions it answered"""
    asked = built["asked"]
    i = body.get("question")
    if not isinstance(i, int) or not 0 <= i < len(asked) or asked[i][1].mark[0].isoformat() != body.get("start"):
        raise Stale("the day changed since the page read it: refresh")
    row, q = asked[i]
    task = card_of(body)
    scope = body.get("scope")
    if scope is not None and (not isinstance(scope, int) or not 0 <= scope < len(q.scopes)
                              or (q.gap and task == config.DROP)):
        raise Invalid("no such scope for this question")
    answered = [(row, task)] + [(r, task) for j, (r, other) in enumerate(asked)
                                if j != i and not other.gap and questions.same_piece(q.name, other.name)]
    reviewing.apply_answers(argparse.Namespace(date=date), built["day"], built["events"], answered)
    if scope is not None:
        daily.add_rule({**q.scopes[scope][1], "card": task})
    return len(answered)


def card_of(body):
    task = questions.task_of(str(body.get("task", "")).strip())
    if task is None:
        raise Invalid("a card like ABC-1, or -")
    return task


def set_entries(date, built, body):
    """gives entries a card, or drops them (`-`), as `set --entry N=CARD` does; with `scope`, the
    rule it becomes from now on, for the piece holding most of them. Returns how many it changed"""
    day, events = built["day"], built["events"]
    wanted = body.get("entries")
    if not isinstance(wanted, list) or not wanted:
        raise Invalid("no entry")
    numbers = []
    for item in wanted:
        n = item.get("n") if isinstance(item, dict) else None
        if not isinstance(n, int) or not 1 <= n <= len(day["entries"]) \
                or day["entries"][n - 1].start.isoformat() != item.get("start"):
            raise Stale("the day changed since the page read it: refresh")
        numbers.append(n)
    task = card_of(body)
    line = [day["entries"][n - 1] for n in numbers]
    scope, scopes = body.get("scope"), reviewing.piece_scopes(line, events)[1]
    if scope is not None and (not isinstance(scope, int) or not 0 <= scope < len(scopes) or task == config.DROP):
        raise Invalid("no such scope for these entries")
    if task == config.DROP:
        daily.correct(day, {}, set(numbers))
    else:
        daily.correct(day, {n: task for n in numbers}, set())
    reviewing.saved_then_fetched(argparse.Namespace(date=date), day, events)
    if scope is not None:
        daily.add_rule({**scopes[scope][1], "card": task})
    return len(numbers)


def undo(date, built, body):
    """takes back a break, so that its stretch is asked about again, or a drop, so that its entry
    comes back as it was"""
    day = built["day"]
    if daily.handed_over(day):
        raise Invalid("this day was handed over: its entries stay as sent, `timelog day --rebuild` builds it again")
    kind, start, end = body.get("kind"), body.get("start"), body.get("end")
    if kind == "break" and [start, end] in day.get("breaks", []):
        day["breaks"].remove([start, end])
    elif kind == "dropped":
        found = next((o for o in day.get("overrides", []) if o["to"] == config.DROP and o["start"] == start
                      and o["end"] == end and o.get("lane") == body.get("lane")), None)
        if found is None:
            raise Stale("the day changed since the page read it: refresh")
        day["overrides"].remove(found)
    else:
        raise Stale("the day changed since the page read it: refresh")
    store.save_day(date, day, quiet=True)
    return 1


def date_of(query):
    """the day asked for, as YYYY-MM-DD: today when left out, never after today"""
    value = query.get("date", [""])[0] or dt.date.today().isoformat()
    try:
        date = dt.date.fromisoformat(value)
    except ValueError:
        raise Invalid(f"{value!r} isn't a date (YYYY-MM-DD)")
    if date > dt.date.today():
        raise Invalid(f"{value} is still to come")
    return date.isoformat()


class Handler(http.server.BaseHTTPRequestHandler):
    built = {}  # the last day built, by date: entry N and question I are the ones the page shows

    def log_message(self, *args):
        pass

    def send(self, status, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; img-src data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def ours(self):
        """the Host is this server's, against DNS rebinding"""
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def day(self, date):
        if date not in self.built:
            self.built.clear()
            self.built[date] = build(date)
        return self.built[date]

    def do_GET(self):
        if not self.ours():
            return self.send(403, {"error": "this page answers on 127.0.0.1 only"})
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                return self.send(200, importlib.resources.files("timelog").joinpath("page/index.html").read_bytes(),
                                 "text/html")
            if url.path == "/api/day":
                date = date_of(query)
                self.built.clear()  # read ActivityWatch again
                return self.send(200, day_view(date, self.day(date)))
            if url.path == "/api/entry":
                date = date_of(query)
                day, events = self.day(date)["day"], self.day(date)["events"]
                n = query.get("n", [""])[0]
                if not n.isdigit() or not 1 <= int(n) <= len(day["entries"]):
                    raise Invalid(f"no entry {n} on {date}")
                return self.send(200, entry_view(day, events, int(n)))
            return self.send(404, {"error": f"no {url.path}"})
        except Invalid as e:
            return self.send(400, {"error": str(e)})
        except SystemExit as e:  # ActivityWatch doesn't answer, the config can't be read
            return self.send(503, {"error": str(e.code).removeprefix("# ")})

    def do_POST(self):
        """an answer, from the page itself: another site can make your browser post here, but not
        with its Origin, nor as JSON without asking first"""
        port = self.server.server_address[1]
        if not self.ours() or self.headers.get("Origin") not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return self.send(403, {"error": "answers come from the page only"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(415, {"error": "JSON only"})
        path = urllib.parse.urlsplit(self.path).path
        if path not in ACTIONS:
            return self.send(404, {"error": f"no {path}"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            if length > 65536:
                raise Invalid("too long")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise Invalid("a JSON object")
            date = date_of({"date": [str(body.get("date", ""))]})
            if date not in self.built:
                raise Stale("the day changed since the page read it: refresh")
            count = ACTIONS[path](date, self.built[date], body)
            self.built.clear()  # rebuilt with the answer on the next read
            return self.send(200, {"changed": count})
        except Stale as e:
            return self.send(409, {"error": str(e)})
        except (Invalid, json.JSONDecodeError) as e:
            return self.send(400, {"error": str(e)})
        except SystemExit as e:
            return self.send(503, {"error": str(e.code).removeprefix("# ")})

    def refuse(self):
        self.send(405, {"error": "not here"})

    do_PUT = do_PATCH = do_DELETE = refuse


ACTIONS = {"/api/answer": answer, "/api/set": set_entries, "/api/undo": undo}


def server(port):
    return http.server.HTTPServer(("127.0.0.1", port), Handler)


def cmd_serve(a):
    try:
        httpd = server(a.port)
    except OSError as e:
        raise SystemExit(f"# port {a.port} is taken ({e.strerror}): try --port")
    url = f"http://127.0.0.1:{httpd.server_address[1]}/" + (f"?date={a.date}" if a.date_given else "")
    print(f"# timelog on {url}, Ctrl+C to stop")
    if not a.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
