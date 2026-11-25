"""The day in a browser: `timelog serve` shows the lanes, entries and totals of a day, read-only, on
a page served from this machine only"""
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
from . import sources
from . import strip
from . import sync
from . import timeline

def build(date):
    """(day, events, gaps, notes): the day rebuilt from ActivityWatch with your corrections, as
    `summary` builds it, and not saved"""
    events = sources.day_events(date, model.card_regex())
    day, gaps, notes = daily.build_day(date, events)
    return day, events, gaps, notes


def kind_of(entry):
    if model.is_unattributed(entry.task):
        return "todo"
    if model.lane_of(entry.streams) != "you":
        return "agent"
    return "call" if "call" in entry.streams else "work"


def span(start, end):
    return {"start": start.isoformat(), "end": end.isoformat()}


def day_view(date, day, events, gaps, notes):
    """the day as the page draws it: entries numbered as `show` numbers them, per lane"""
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
        "gaps": [span(a, b) for a, b in gaps],
        "away": [span(a, b) for a, b in strip.away_spans(entries, events)],
        "totals": [{"what": what, "time": time, "about": about}
                   for what, time, about in daily.logged_rows([e for _n, e in shown], sent)],
        "worklogs": worklogs,
        "notes": list(notes),
        "pushed": daily.handed_over(day),
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
    }


class Invalid(ValueError):
    pass


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
    built = {}  # the last day built, by date: entry N is the one the page shows

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

    def do_GET(self):
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):  # DNS rebinding
            return self.send(403, {"error": "this page answers on 127.0.0.1 only"})
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                return self.send(200, importlib.resources.files("timelog").joinpath("page/index.html").read_bytes(),
                                 "text/html")
            if url.path == "/api/day":
                date = date_of(query)
                self.built.clear()
                self.built[date] = build(date)
                return self.send(200, day_view(date, *self.built[date]))
            if url.path == "/api/entry":
                date = date_of(query)
                if date not in self.built:
                    self.built.clear()
                    self.built[date] = build(date)
                day, events, _gaps, _notes = self.built[date]
                n = query.get("n", [""])[0]
                if not n.isdigit() or not 1 <= int(n) <= len(day["entries"]):
                    raise Invalid(f"no entry {n} on {date}")
                return self.send(200, entry_view(day, events, int(n)))
            return self.send(404, {"error": f"no {url.path}"})
        except Invalid as e:
            return self.send(400, {"error": str(e)})
        except SystemExit as e:  # ActivityWatch doesn't answer, the config can't be read
            return self.send(503, {"error": str(e.code).removeprefix("# ")})

    def refuse(self):
        self.send(405, {"error": "this page only reads"})

    do_POST = do_PUT = do_PATCH = do_DELETE = refuse


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
