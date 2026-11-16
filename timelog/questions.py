"""The review as questions in the terminal: a card for each line timelog couldn't attribute, picked
from a short list or typed (Tab completes the day's cards), the scope of a rule when the answer
should hold from now on, then the comments. `e` opens the editor, `q` stops: the answers given so
far are kept either way."""
import re
from typing import NamedTuple

from . import config
from . import style

try:
    import readline
except ImportError:  # not on every platform: no completion nor prefilled answers then
    readline = None

KEY = re.compile(r"^([A-Za-z][A-Za-z0-9]*-\d+)\b")


class Question(NamedTuple):
    title: str  # what the line is: its times, length and task, or its session
    details: list  # what it's made of, a line each
    options: list  # (task, why it's offered)
    scopes: list  # (what it covers, rule without its card): the rules an answer can become
    gap: bool = False  # nothing active: a card makes a manual entry, Enter (or -) makes it a break
    name: str = ""  # the page, window or session it's mostly made of: an answer carries over to its namesakes


def same_piece(a, b):
    """two names of one piece: equal, or one cut short (…) where the other goes on, as a session's
    title in its window's"""
    if not (a and b):
        return False
    if a.endswith("…") or b.endswith("…"):
        short, long_ = sorted((a.removesuffix("…"), b.removesuffix("…")), key=len)
        return len(short) >= 12 and long_.startswith(short)
    return a == b


class Stop(Exception):
    """`e` or `q` at a prompt: `reason` is "edit" or "quit", with the answers given before"""

    def __init__(self, reason, answers, rules):
        super().__init__(reason)
        self.reason, self.answers, self.rules = reason, answers, rules


def completing(words):
    """Tab completes these words at the prompts"""
    if not readline:
        return
    words = sorted(set(words))
    readline.set_completer(lambda text, i: ([w for w in words if w.lower().startswith(text.lower())] + [None])[i])
    readline.set_completer_delims(" ")
    readline.parse_and_bind("tab: complete")


def prefilled(prompt, text):
    """what you type at `prompt`, starting from `text` you can edit"""
    if readline:
        readline.set_startup_hook(lambda: readline.insert_text(text))
    try:
        return input(prompt)
    finally:
        if readline:
            readline.set_startup_hook()


def task_of(answer):
    """the task an answer gives, its card key in capitals; None when it isn't one"""
    m = KEY.match(answer)
    if answer == config.DROP:
        return answer
    return m.group(1).upper() + answer[m.end():] if m else None


def keys(*pairs):
    """a prompt's keys, in brackets (and bold), each followed by what it does; a key can be a tuple
    of keys doing the same"""
    def shown(key):
        return " or ".join(style.paint(f"[{k}]", "bold") for k in (key if isinstance(key, tuple) else (key,)))
    return "".join(f"   {shown(key)} {style.paint(what, 'dim')}" for key, what in pairs)


def read(prompt, answers, rules):
    try:
        answer = input(prompt).strip()
    except EOFError:
        answer = "q"
    if answer in ("e", "q"):
        raise Stop({"e": "edit", "q": "quit"}[answer], answers, rules)
    return answer


def ask(questions):
    """({question number: task}, [rules with their card]) from your answers. A card given to a page,
    window or session is given to its later namesakes too, and offered to the questions after it"""
    answers, rules, typed, carried = {}, [], [], {}
    for n, q in enumerate(questions):
        print(style.paint(f"\n[{n + 1}/{len(questions)}] ", "dim") + q.title)
        if n in carried:
            task, k = carried[n]
            answers[n] = task
            print(f"  {style.task(task)}  {style.paint(f'as [{k + 1}], the same {q.name!r}', 'dim')}")
            continue
        for line in q.details:
            print(style.paint(f"      {line}", "dim"))
        options = q.options + [(t, "given above") for t in typed if t not in {o for o, _why in q.options}]
        for i, (task, why) in enumerate(options, 1):
            print(f"  {i}) {style.task(task)}  {style.paint(why, 'dim')}")
        numbers = (f"1-{len(options)}",) if len(options) > 1 else ("1",) if options else ()
        print(keys(("Enter", "a break" if q.gap else "later"), ((*numbers, "ABC-1"), "that card"),
                   *([] if q.gap else [("-", "drop")]), ("e", "editor"), ("q", "stop"))[1:])
        while True:
            answer = read("> ", answers, rules)
            if not answer:
                if q.gap:
                    answers[n] = config.DROP
                break
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                task = options[int(answer) - 1][0]
            else:
                task = task_of(answer)
            if task is None:
                print(style.paint(f"  a number from 1 to {len(options)}, a card like ABC-1, or -", "yellow"))
                continue
            answers[n] = task
            if task != config.DROP and task not in typed:
                typed.append(task)
            carried |= {m: (task, n) for m in range(n + 1, len(questions))
                        if m not in carried and same_piece(q.name, questions[m].name)}
            if q.scopes and not (q.gap and task == config.DROP):
                rule = ask_scope(q.scopes, answers, rules)
                if rule:
                    rules.append({**rule, "card": task})
            break
    return answers, rules


def ask_scope(scopes, answers, rules):
    """the rule an answer should become, or None for that day only"""
    if len(scopes) == 1:
        print(f"  From now on too, for {scopes[0][0]}?" + keys(("y", "yes"), ("Enter", "today only")))
    else:
        print("  From now on too?" + keys((f"1-{len(scopes)}", "for"), ("Enter", "today only")))
        for i, (what, _rule) in enumerate(scopes, 1):
            print(f"    {i}) {what}")
    while True:
        answer = read("  > ", answers, rules).lower()
        if answer in ("", "n", "no"):
            return None
        if len(scopes) == 1 and answer in ("y", "yes"):
            answer = "1"
        if answer.isdigit() and 1 <= int(answer) <= len(scopes):
            return scopes[int(answer) - 1][1]
        print(style.paint("  y or Enter" if len(scopes) == 1 else f"  a number from 1 to {len(scopes)}, or Enter", "yellow"))


def ask_comments(lines):
    """{task: comment} you changed; lines: (task, time, comment, where: when and what its time was)"""
    changed = {}
    width = max((len(task) for task, *_rest in lines), default=0)
    number = len(str(len(lines)))
    while True:
        print(style.paint("\nComments:", "bold"))
        for i, (task, time, comment, where) in enumerate(lines, 1):
            text = changed.get(task, comment) or style.paint("(none)", "yellow")
            print(f"  {i:{number}}) {style.pad(task, style.task(task), width)}  {time}  {text}")
            if where:
                print(" " * (number + 4) + style.paint(where, "dim"))
        try:
            answer = input("Enter to keep them, or a number to change one: ").strip()
        except EOFError:
            return changed
        if answer in ("", "q"):
            return changed
        if not (answer.isdigit() and 1 <= int(answer) <= len(lines)):
            continue
        task, _time, comment, _where = lines[int(answer) - 1]
        changed[task] = prefilled(f"{task} = ", changed.get(task, comment) or "").strip()
