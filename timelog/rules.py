"""Rules (DESIGN.md): `{"regex", "card", "name", "always_active", "side_activity", "comment"}`,
tried in order on a piece's match string, the config's `rules` first, then the defaults below. The regex's groups fill
`$1`, `$2`... in `card` and `name`."""
import re
import urllib.parse

from . import config

DEFAULT_RULES = [
    {"regex": r"^Meet [-–] (.+?)\|https://meet\.google\.com/(?:meet/)?[a-z]{3}-[a-z]{4}-[a-z]{3}",
     "name": "Meet - $1", "always_active": True},
    {"regex": r"^(.+?)\|https://(?:app\.)?zoom\.us/", "name": "Zoom - $1", "always_active": True},
    {"regex": r"^Meet \| (.+?) \| Microsoft Teams", "name": "Teams - $1", "always_active": True},
    {"regex": r"^Chat \| (.+?) \| Microsoft Teams", "name": "Teams - $1", "side_activity": True},
    {"regex": r"^(.+?) - [^-|]+(?: - \d+ new items?)? - Slack", "name": "Slack - $1", "side_activity": True},
    {"regex": r"^Slack - (.+?)(?: - \d+ new items?)?$", "name": "Slack - $1", "side_activity": True},  # the app
    {"regex": r"^(?:\(\d+\) )?(.+?) - Discord", "name": "Discord - $1", "side_activity": True},
    # mail and documentation follow what you're on, as chat does
    {"regex": r"\|https://mail\.google\.com/", "name": "Gmail", "side_activity": True},
    {"regex": r"^(.+) - Google (Docs|Sheets|Slides)\|", "name": "Google $2 - $1"},
    {"regex": r"^(?:Edit - )?(.+?) - [^-|]+ - Confluence", "name": "Confluence - $1", "side_activity": True},
    # timesheeting is admin time: tracker.admin_card, when set
    {"regex": r"^Tempo(?: - Jira)?(?: — .+)?$|\|https://[\w.-]+/plugins/servlet/ac/io\.tempo\.jira",
     "name": "Tempo", "admin": True, "comment": "timesheeting"},
    {"regex": r"\.timelog\b", "name": "timelog review", "admin": True},  # the file `timelog` opens
]


def scopes(piece):
    """(what it covers, rule without its card) for the rules that would cover `piece`, narrowest
    first: its title, window, or agent session; the site of a tab; an editor's repository on any
    branch. A rule for exactly the piece keeps the name and flags of the rule it matched, so that a
    call stays a call"""
    if piece.kind in ("claude", "aw-agent"):  # its text is a prompt; its name, its session's title
        name = piece.keys[0]
        return [(f'this session title: "{name}"', {"regex": rf"^{re.escape(name)}(?: - [^|]*)?\|", "name": name})]
    rule, name, _card = match(piece.text, kind=piece.kind)
    name = piece.keys[0] if piece.keys else name  # the name it was given
    kept = {"name": name} | {k: True for k in ("always_active", "side_activity") if rule and rule.get(k)}
    if piece.kind == "editor":
        repository, branch = piece.text.split("|", 1)
        out = [(f"{repository} on {branch}", {"regex": rf"^{re.escape(repository)}\|{re.escape(branch)}$", **kept})]
        return out + [(f"{repository}, any branch", {"regex": rf"^{re.escape(repository)}\|"})] * bool(branch)
    if piece.kind in ("browser", "call"):
        title, url = piece.text.split("|", 1)
        out = [(f'this title: "{title}"', {"regex": rf"^{re.escape(title)}\|", **kept})]
        host = urllib.parse.urlsplit(url).netloc
        if piece.kind == "browser" and host and not kept.get("always_active"):
            out.append((f"anything on {host}", {"regex": rf"\|https?://{re.escape(host)}(?:[/?#]|$)"}))
        return out
    return [(f'this window: "{piece.text}"', {"regex": rf"^{re.escape(piece.text)}$", **kept})]


def fill(template, m):
    return re.sub(r"\$(\d)", lambda g: m.group(int(g.group(1))) or "", template)


def default_name(string, kind=None):
    """without a rule: an editor's "repository branch", else the title, cut at its first " - " """
    if kind == "editor":
        return string.replace("|", " ").strip()
    return string.split("|")[0].split(" - ")[0].strip()


def match(string, rules=None, kind=None):
    """(rule, name, card) of the first rule matching the string; (None, default name, None) without"""
    for rule in (config.RULES if rules is None else rules) + DEFAULT_RULES:
        m = re.search(rule["regex"], string)
        if m:
            name = fill(rule["name"], m) if rule.get("name") else default_name(string, kind)
            card = fill(rule["card"], m) if rule.get("card") else None
            if rule.get("admin"):
                card = config.TRACKER.get("admin_card")
            return rule, name, card
    return None, default_name(string, kind), None
