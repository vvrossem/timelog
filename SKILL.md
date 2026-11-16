---
name: timelog
description: Suggest the user's daily worklogs from what ActivityWatch recorded (agent turns, focused windows, browser tabs and calls, keyboard presence), with cards worked on in parallel each getting their full time, and push them to Jira once reviewed. Use when asked to fill, log, reconstruct, sum up or check time spent on cards, or on /timelog.
---

# Daily worklogs from ActivityWatch

`timelog` is installed with pipx from this directory. The README has the commands; DESIGN.md how
time is counted (read it before changing that). Change the day file only through `timelog`. After
changing the code, run `python3 -m pytest -q -p no:cacheprovider`.

Never run `timelog` without a command, nor `timelog review`: they ask the user questions in a
terminal, or open an editor, and wait. Use the commands below; they work on today, or on the day given after the command, e.g.
`timelog day 2026-11-13`.

Start with `timelog config`. From its `tracker`:
- no `site`: ask the user for it and leave Jira alone;
- a `site` but no token (`timelog config` says so): use the Atlassian connector (cloudId = `site`)
  where the steps below say "connector";
- never propose `retired_cards`; tooling work (e.g. on Claude skills) goes to `tooling_card`,
  admin time to `admin_card`.

## `/timelog`

**1. Build.** `timelog fetch`, then `timelog day`, and show
the entries. If `timelog config` reports something missing, carry on and ask about it first in
step 2. Look for sessions whose prompts moved to other cards: they're all on their first card.

Connector: `timelog cards`, then `searchJiraIssuesUsingJql` with `key in (<keys>)` (fields
`summary`, `status`), and `tracker.review_jql` if set (field `summary`). Write both to
`~/.cache/timelog/jira-<date>.json` as
`{"known": {"KEY": {"summary": "...", "status": "..."}}, "candidates": {"KEY": "summary"}}`.

**2. Ask, in one message:** the card of each `?` entry (show what `timelog show N` gives, never
guess from the branch or repo), how to split each session that moved cards, the card of each call
or `-` to drop it, and for each `nothing active` stretch whether it was a break, a meeting or work.
Never spread that time over cards. For example:

```text
1. Entry 1, 08:58-09:20, call "Meet - Daily sync": which card, or drop it?
2. Entry 4, 11:15-11:45, "Q4 planning.ods": which card?
3. Nothing active 10:40-11:15: a break, a meeting or other work?
```

Apply the answers with `set` and `add`. If an answer is a habit (a recurring call, a repository),
ask whether to make it a rule: `set --entry N=CARD!` gives exactly that window, tab or session the
card from now on (a wider scope, such as a site or a repository: edit `rules` in the config).

**3. Comments.** From `timelog notes`, show a `task | time | comment` table to approve. A task
without a comment has a default one (your last comment for the card, else its Jira summary, and
its PRs; or the PRs you reviewed on GitHub
that day, `default comment:` in `notes`):
keep it when it fits, else write one. A comment
says what was done for a project manager, with PRs as `repo#N`:
`Discount on invoice lines, options for the BA, erp_odoo#627`. Leave out how (tests, DB,
screenshots), reasons in parentheses, second clauses after a semicolon, prompt quotes, Claude, and
anything of another card. Reviews: `PR review of repo#N ...`. No activity: "(nothing in the
logs)". Then `set --comment "TASK=comment"`.

**4. Totals.** `timelog summary`: give the total and the wall clock.

**5. Push, when asked.** Run `timelog fetch` again if steps 2-3 added cards. Show `timelog push -n`,
ask, then `timelog push`. Report `card | start-end | duration` per worklog and check the
total against `summary`.

Connector:
1. `searchJiraIssuesUsingJql` with `worklogAuthor = currentUser() AND worklogDate = "<date>"`
   (field `worklog`). Add the user's worklogs started that day to `jira-<date>.json` as
   `"worklogs": [{"id": "...", "card": "KEY", "started": "<as Jira gives it>", "minutes": N, "comment": "..."}]`, and
   `"truncated": [...]` for cards whose `worklog.total` exceeds what came back (20 per card,
   everyone's). Read a large answer from the saved file with a script.
2. After approval, send each `add` and `update` of `timelog push --json` with
   `addWorklogToJiraIssue` (`started`, `timeSpent` as `Nm`, `commentBody`, `worklogId` for an
   update).
3. Write what was sent, with Jira's ids, to a file as
   `[{"id": "...", "card": "...", "started": "...", "minutes": N, "comment": "..."}]` and run
   `timelog push --record <file>`.
4. The connector can't delete: list the `delete` lines for the user to remove in Tempo.
