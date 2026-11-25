"""Colors and widths for a terminal. Piped output, which the /timelog skill reads, stays plain text,
as with NO_COLOR (https://no-color.org) or TERM=dumb."""
import contextlib
import os
import shutil
import sys

CODES = {"bold": 1, "dim": 2, "red": 31, "green": 32, "yellow": 33, "blue": 34, "magenta": 35, "cyan": 36}


def on():
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def paint(text, *names):
    if not names or not text or not on():
        return text
    return f"\033[{';'.join(str(CODES[n]) for n in names)}m{text}\033[0m"


def columns():
    """the terminal's width, or None when piped: nothing is cut"""
    return shutil.get_terminal_size().columns if on() else None


def clip(text, width):
    if width is None or len(text) <= width:
        return text
    return text[:max(width - 1, 0)] + "…"


def pad(text, styled, width):
    """`styled` (`text` with colors) padded to `width` visible characters"""
    return styled + " " * max(width - len(text), 0)


def task(text):
    """a task: its card key in color, a ? to answer in yellow, a dropped one dimmed"""
    if text.startswith("?"):
        return paint(text, "yellow", "bold")
    if text == "-":
        return paint(text, "dim")
    key, sep, rest = text.partition(": ")
    return paint(key, "cyan") + sep + rest


def note(text, *names):
    """a line about the output, after a #: dimmed unless it says something to act on"""
    print(paint(f"# {text}", *(names or ("dim",))))


_working = []


def doing():
    """the slow step running now, the innermost: what the page shows while it waits; None when idle"""
    return _working[-1] if _working else None


@contextlib.contextmanager
def working(text):
    """`# text…` on the terminal while a slow step runs, so that it doesn't look stuck; wiped when
    it's done. Inside another one, the outer text stays: it says more than a network call's. Nothing
    when stderr is piped"""
    shown = not _working and sys.stderr.isatty() and os.environ.get("TERM") != "dumb"
    if shown:
        sys.stderr.write(paint(f"# {text}…", "dim") if on() else f"# {text}…")
        sys.stderr.flush()
    _working.append(text)
    try:
        yield
    finally:
        _working.pop()
        if shown:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
