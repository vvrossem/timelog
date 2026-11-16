"""Jira Cloud's REST API, for `fetch` and `push`: the script reaches Jira itself, without a
Claude session. It signs in with `tracker.email` and an API token (Jira: Account settings, Security,
API tokens), taken from JIRA_API_TOKEN or printed by `tracker.token_command` (e.g. `lpass show
XXXX -p`): never stored in the config."""
import base64
import datetime as dt
import functools
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import config
from . import style


def token():
    if os.environ.get("JIRA_API_TOKEN"):
        return os.environ["JIRA_API_TOKEN"]
    command = config.TRACKER.get("token_command")
    return command_output(command) if command else None


FAILED = {}  # a token command that failed: why


@functools.lru_cache(maxsize=None)  # once per run: a password manager may ask to unlock each time
def command_output(command):
    """its output; its prompts and errors go to your terminal, where a password manager can ask you
    to unlock it (lpass there rather than in a pinentry window, which a session without one keeps)"""
    env = {**os.environ, "LPASS_DISABLE_PINENTRY": "1"} if sys.stdin.isatty() else None
    try:
        res = subprocess.run(shlex.split(command), stdout=subprocess.PIPE, text=True, env=env)
    except OSError as e:
        FAILED[command] = str(e)
        return None
    if res.returncode != 0:
        FAILED[command] = f"exit {res.returncode}"
        return None
    return res.stdout.strip()


def unavailable():
    """why timelog can't reach Jira, or None when it can"""
    if not (config.TRACKER["site"] and config.TRACKER.get("email")) or not (
            os.environ.get("JIRA_API_TOKEN") or config.TRACKER.get("token_command")):
        return "Jira isn't set up (see `timelog config`)"
    if token():
        return None
    command = config.TRACKER["token_command"]
    return f"the Jira token didn't come: `{command}` failed: {FAILED.get(command, 'no output')}"


def available():
    return unavailable() is None


class Client:
    def __init__(self):
        site, email, secret = config.TRACKER["site"], config.TRACKER.get("email"), token()
        if not (site and email and secret):
            raise SystemExit("# Jira can't be reached: set tracker.site and tracker.email in the config, and"
                             " JIRA_API_TOKEN or tracker.token_command")
        self.base = f"https://{site}/rest/api/3"
        self.auth = "Basic " + base64.b64encode(f"{email}:{secret}".encode()).decode()

    def call(self, method, path, body=None, params=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, method=method, data=data,
                                         headers={"Authorization": self.auth, "Accept": "application/json",
                                                  "Content-Type": "application/json"})
        try:
            with style.working("asking Jira"), urllib.request.urlopen(request, timeout=30) as response:
                text = response.read()
        except urllib.error.HTTPError as e:
            raise SystemExit(f"# Jira answered {e.code} to {method} {path}: {e.read().decode(errors='replace')[:300]}")
        except urllib.error.URLError as e:
            raise SystemExit(f"# Jira can't be reached ({e.reason}) for {method} {path}")
        return json.loads(text) if text else None

    def search(self, jql, fields):
        """every issue the JQL finds, as {key: fields}"""
        out, page = {}, None
        while True:
            body = {"jql": jql, "fields": fields, "maxResults": 100, **({"nextPageToken": page} if page else {})}
            answer = self.call("POST", "/search/jql", body)
            out.update({i["key"]: i.get("fields", {}) for i in answer.get("issues", [])})
            page = answer.get("nextPageToken")
            if not page:
                return out

    def worklogs(self, key, start, end):
        out, at = [], 0
        while True:
            params = {"startAt": at, "maxResults": 1000, "startedAfter": int(start.timestamp() * 1000) - 1,
                      "startedBefore": int(end.timestamp() * 1000)}
            answer = self.call("GET", f"/issue/{key}/worklog", params=params)
            out += answer.get("worklogs", [])
            at += len(answer.get("worklogs", []))
            if not answer.get("worklogs") or at >= answer.get("total", 0):
                return out


def text_of(adf):
    """the plain text of an Atlassian document, as a worklog comment is"""
    if isinstance(adf, str):
        return adf
    if not isinstance(adf, dict):
        return ""
    return adf.get("text", "") + "".join(text_of(c) for c in adf.get("content", []))


def document(text):
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}] if text else []}


def fetch(client, date, keys, review_jql):
    """what `jira-<date>.json` holds (see jira.py): the cards Jira knows, the candidates, and your
    worklogs of the day, read card by card so none is cut off"""
    known = {}
    if keys:
        found = client.search(f"key in ({', '.join(sorted(keys))})", ["summary", "status"])
        known = {k: {"summary": f.get("summary", ""), "status": (f.get("status") or {}).get("name", "")}
                 for k, f in found.items()}
    candidates = {}
    if review_jql:
        candidates = {k: f.get("summary", "") for k, f in client.search(review_jql, ["summary"]).items()}
    return {"known": known, "candidates": candidates, "worklogs": my_worklogs(client, date), "truncated": []}


def my_worklogs(client, date):
    """your worklogs started that day, as push.plan takes them"""
    me = client.call("GET", "/myself")["accountId"]
    start = dt.datetime.fromisoformat(f"{date}T00:00").astimezone()
    end = start + dt.timedelta(days=1)
    out = []
    for key in client.search(f'worklogAuthor = currentUser() AND worklogDate = "{date}"', ["summary"]):
        for w in client.worklogs(key, start, end):
            if (w.get("author") or {}).get("accountId") == me:
                out.append({"id": str(w["id"]), "card": key, "started": w["started"],
                            "minutes": int(w.get("timeSpentSeconds", 0)) // 60, "comment": text_of(w.get("comment"))})
    return out


def send(client, action, started):
    """an add, update or delete of the plan; the worklog's id, or None once deleted"""
    # Jira's default adjusts the card's remaining estimate by the time logged, as Tempo does
    path = f"/issue/{action.card}/worklog"
    if action.action == "delete":
        client.call("DELETE", f"{path}/{action.worklog_id}")
        return None
    body = {"started": started, "timeSpentSeconds": action.minutes * 60, "comment": document(action.comment)}
    if action.action == "update":
        return str(client.call("PUT", f"{path}/{action.worklog_id}", body)["id"])
    return str(client.call("POST", path, body)["id"])
