# timelog

timelog turns what [ActivityWatch](https://activitywatch.net) recorded into your Jira worklogs,
one per card and day. It asks only about what it couldn't attribute, and sends nothing until you
say yes.

Agents working in parallel each count in full: two agents working 30 minutes on two cards log 30
minutes on each. [DESIGN.md](DESIGN.md) explains how the time is counted.

## A day

```console
$ timelog
# Jira: 2 cards known, 2 candidates, 0 worklogs of yours that day

  Tue 20.10  08    09    10    11    12    13
  you        ·····???▓▓▓▓▓▓▓▓···????▓▓▓▓···
  agents     ······▒▒▒▒▒▒▒·················
             ▓ attributed   ? to answer   ░ away   █ call   ▒ agent

─── 1 of 3 ── call ── 08:58-09:20 · 0h22 ───────────────────────────────────────────────────────────
  Tue 20.10  08    09    10    11    12    13
  you        ·····???▓▓▓▓▓▓▓▓···????▓▓▓▓···
  agents     ······▒▒▒▒▒▒▒·················
                  ^^^
  what was open
       22m  call  Meet - Daily sync
  around it
      from  09:20  VS Code  Discount on invoice lines (ABC-641)

   1  ABC-641  Discount on invoice lines  agent a1b2c3d4 working then
   2  TOOL-69                             admin

  [1-2] or [ABC-1] card · [Enter] later · [-] drop · [e] editor · [q] stop
> 2
  From now on too, for this title: "Meet – Daily sync"?   [y] yes · [n] no
  > y

─── 2 of 3 ── nothing active ── 10:40-11:15 · 0h35 ─────────────────────────────────────────────────
  Tue 20.10  08    09    10    11    12    13
  you        ·····▓▓▓▓▓▓▓▓▓▓▓···????▓▓▓▓···
  agents     ······▒▒▒▒▒▒▒·················
                             ^^^^
  while it lasted
      away from the keyboard the whole time
  around it
      until 10:40  Firefox  Review PR 412 (DEF-77)
      from  11:15           Q4 planning.ods

   1  DEF-77: PR review  Refund rounding  just before
   2  TOOL-69                             admin

  [1-2] or [ABC-1] card · [Enter] a break · [e] editor · [q] stop
> 2

─── 3 of 3 ── your windows ── 11:15-11:45 · 0h30 ───────────────────────────────────────────────────
  Tue 20.10  08    09    10    11    12    13
  you        ·····▓▓▓▓▓▓▓▓▓▓▓▓▓▓????▓▓▓▓···
  agents     ······▒▒▒▒▒▒▒·················
                                ^^^^
  what was open
       30m  Q4 planning.ods
  around it
      until 11:15  Firefox  Review PR 412 (DEF-77)
      from  11:45  VS Code  shop_odoo (ABC-641)

   1  TOOL-69  admin

  [1] or [ABC-1] card · [Enter] later · [-] drop · [e] editor · [q] stop
> 1
  From now on too, for this window: "Q4 planning.ods"?   [y] yes · [n] no
  > n
# rule added: ^Meet\ –\ Daily\ sync\| -> TOOL-69

Comments   3h40 in worklogs
  1  08:58  TOOL-69            1h27  (none)
                                     08:58-09:20, 10:40-11:45 · Meet - Daily sync, Q4 planning.ods
  2  09:20  ABC-641            1h15  Discount on invoice lines, rounding test
                                     09:20-09:50, 11:45-12:30 · Discount on invoice lines, shop_odoo
  3  09:40  DEF-77: PR review  1h00  PR review of shop_odoo#412
                                     09:40-10:40 · Review PR 412, Refund rounding by bob
Enter to keep them, or a number to change one: 1
TOOL-69 = Daily, team meeting, Q4 planning
  1  08:58  TOOL-69            1h27  Daily, team meeting, Q4 planning  ✓ changed
Enter to keep them, or a number to change one:

worklogs     3h40  what Jira gets: a card's parallel lanes count once
logged       3h42  over 3h32 of wall clock
other cards  0h10  at the same time

Jira, Tue 20.10   add 3   → 3h40
  + add      08:58-10:23  1h25  TOOL-69  Daily, team meeting, Q4 planning
  + add      09:20-10:35  1h15  ABC-641  Discount on invoice lines, rounding test
  + add      09:40-10:40  1h00  DEF-77   PR review of shop_odoo#412
Send them to Jira? [y/N] y
```

Pick a number or type a card (Tab completes the day's cards). An answer holds for that day;
"From now on too?" makes it a rule, so tomorrow's daily goes to TOOL-69 on its own.

ABC-641 gets its agent's 1h05 plus the 10 minutes you spent reading its last answer. The time you
spent on its Jira page while the agent worked isn't added on top. The day is made up: the
[DESIGN.md](DESIGN.md) timeline shows it lane by lane.

## Install

You need ActivityWatch with its AFK and window watchers, plus
[aw-watcher-web](https://github.com/ActivityWatch/aw-watcher-web) for browser tabs and calls, and
[aw-watcher-agents](https://github.com/vvrossem/aw-watcher-agents) if you use Claude Code.
ActivityWatch records from the day you install it, so set it up before the first day you want to
log. [`gh`](https://cli.github.com), logged in, gives PRs their card.

```sh
pipx install git+ssh://git@github.com/vvrossem/timelog.git
```

To update it:

```sh
pipx reinstall timelog
```

`pipx upgrade` and `pipx install --force` keep the code you installed.

<details>
<summary>With <code>/timelog</code> in Claude Code</summary>

Install it from a checkout in your skills, so `git pull` updates both:

```sh
git clone git@github.com:vvrossem/timelog.git ~/.claude/skills/timelog
pipx install ~/.claude/skills/timelog
pipx runpip timelog install --no-deps -e ~/.claude/skills/timelog
```

</details>

## Setup

`timelog config --write` writes the settings to `~/.config/timelog/config.json` and lists what's
missing. Fill it in:

```json
{
 "card_prefixes": ["ABC", "DEF"],
 "tracker": {
  "site": "acme.atlassian.net",
  "email": "you@example.com",
  "token_command": "pass show jira-token",
  "tooling_card": "TOOL-68",
  "admin_card": "TOOL-69"
 }
}
```

`card_prefixes` are the keys of the Jira projects you work on. The token is an
[Atlassian API token](https://id.atlassian.com/manage-profile/security/api-tokens), stored for
example with `secret-tool store --label jira service jira` and read with
`"token_command": "secret-tool lookup service jira"`.

The setup is complete when `timelog config` prints no warning. "ActivityWatch has no data from"
names a watcher that isn't running. Without a Jira token, timelog still builds the day and prints
the lines to type into Tempo.

<details>
<summary>All settings</summary>

| Setting | Default | |
|---|---|---|
| `tracker.review_jql` | | a JQL search for cards to offer when timelog doesn't know the card |
| `tracker.retired_cards` | | cards never to log on |
| `tooling_keywords` | | words that send an agent session to the tooling card, e.g. `["timelog"]` |
| `ignored_folders` | | folders whose agent sessions aren't counted, e.g. `["~/projects"]` |
| `folder_cards` | | the card of a folder's agent sessions and editor time, e.g. `{"timelog": "TOOL-68"}`; a bare name is any folder of that name |
| `your_time_during_agent_turns` | `other_cards` | `other_cards`: your windows don't count on a card while one of its agents works; `calls`: on no card |
| `rules` | | see [Rules](#rules) |
| `timing.round` | 5 | worklogs are rounded to a multiple of this, never under it; 1: as recorded |
| `timing.round_mode` | `nearest` | `nearest`: 12 → 10, 13 → 15, 3 → 5; `up`: 11 → 15 |
| `timing.join_gap` | 30 | your entries of a card this close (min) make one row of the review |
| `timing.break_gap` | 20 | this long (min) with nothing active is a break |
| `start_date` | | `YYYY-MM-DD`: no day before it is offered |
| `date_order` | your locale's | `dmy`, `mdy` or `ymd`: how `14/11/26` reads |
| `summary_width` | whole | the length of the Jira summaries shown in the questions |
| `activitywatch_url` | `http://localhost:5600` | |

`token_command` asks for your password in the terminal you run `timelog` from.

</details>

## Every day

| | |
|---|---|
| `timelog` | the questions above, then the comments, then the push |
| `/timelog` in Claude Code | the same, as a conversation |
| `timelog summary` | how long you've worked so far, per card, without questions |
| `timelog serve` | the day in your browser: its lanes, entries and totals, read-only for now |
| `timelog friday`, `timelog 14.11.26` | another day (`timelog --help` lists the date forms) |

Run it as often as you like: it keeps what you've already answered. Missed days come first, the
oldest first, since an answer helps the days after it:

```console
$ timelog
# not pushed yet: Thu 12.11 (5h10), Fri 13.11 (6h22)
Review them first? [Y/n]
```

A pushed day isn't offered again, unless you worked a quarter of an hour more after the push: only
that work is asked about. To fix a mistake, `timelog 16.11 --edit` opens the day as you pushed
it, and the push updates only the worklogs you changed.

<details>
<summary><code>timelog summary</code></summary>

```console
$ timelog summary
  time   agent     you  rounded     in    out              task
  1h27    0h00    1h27     1h25      -      -  ██████████  TOOL-69
  1h15    1h05    0h10     1h15    62k    23k  ▒▒▒▒▒▒▒▒█   ABC-641
  1h00    0h50    0h10     1h00    44k     9k  ▒▒▒▒▒▒█     DEF-77: PR review
  3h42  total logged, 3h40 once each worklog is rounded (nearest) to 5 min
  0h10  of it different cards in parallel
  3h32  wall clock with at least one task active
  138k  agent tokens: 106k in (cache writes included), 32k out; cache reads left out
                                               ▒ agent  █ you
```

`in` and `out` are the tokens the agents used per card. Cache reads are left out: they're most of
the volume and the cheapest.

</details>

<details>
<summary>The day in your editor</summary>

`e` at any question, or `timelog --edit`, opens the day in `$VISUAL` or `$EDITOR`. There you can
also move an entry's times, or add a line for what left no trace:

```text
# Agents: one line per session and card, its turns summed. Change the card to move them all.
agent a1b2c3d4  ABC-641              # 1h05 Discount on invoice lines
agent e5f6a7b8  DEF-77: PR review    # 0h50 Review PR 412

# You: change a line's times to fix them, add a line for what left no trace.
08:58-09:20  ? Meet - Daily sync
10:30-10:40  DEF-77: PR review    # Refund rounding by bob · Pull Request #412 · acme/shop_odoo
10:40-11:15  ?                    # nothing active
11:15-11:45  ?                    # Q4 planning.ods
12:20-12:30  ABC-641              # shop_odoo - Visual Studio Code

# Comments, one per card; add a line for a new card.
ABC-641 = Discount on invoice lines
DEF-77: PR review = PR review of shop_odoo#412
```

Give each `?` a card, or `-` to drop it. `ABC-1!` also adds a rule for that window, tab or session.

</details>

<details>
<summary>Commands, for scripts and <code>/timelog</code></summary>

| Command | |
|---|---|
| `fetch` | asks Jira about the day's cards |
| `cards` | the day's card keys |
| `day [--rebuild]` | builds the day again, keeping your answers, and saves it; a pushed day only with `--rebuild` |
| `show [N]` | the day, numbered; with N, what entry N is made of |
| `set --entry N=CARD[!]`, `--drop N`, `--comment CARD=TEXT` | fixes the day; `!` adds a rule |
| `add --from HH:MM --to HH:MM --task CARD` | adds an entry |
| `notes` | each card's PRs and prompts, to write the comments |
| `push [-n]` | sends the day to Jira, like `git push`; `-n` only shows the plan |

Each takes a date after its name, e.g. `timelog show friday 3`. `NO_COLOR=1` turns colors off.

</details>

## Where the card comes from

timelog finds the card by itself when it is in front of you: a card key in a window or tab title
or URL (a Jira card, a PR, a branch), a PR whose branch or description names it, or an agent
session's prompts. Anything else, such as an Odoo screen, a call or a shared document, is a `?`
to answer, until a rule answers it for you. Without agents, expect most questions the first days.

## Rules

A rule gives a card to a window, tab or call you see every day. "From now on too?" adds one; you
can edit, widen or delete them under `rules` in the config:

```json
"rules": [
 {"regex": "^dev-chat - Acme - Slack", "card": "DEF-12", "side_activity": true},
 {"regex": "^Meet - Daily sync\\|", "card": "TOOL-69", "always_active": true}
]
```

The regex is matched against the window title, or `title|url` for a tab. `"card": "-"` drops the
time. [DESIGN.md](DESIGN.md#rules) has the other fields.

## Limitations

- One Claude session per card: a session that moves to another card keeps logging on its first
  one, so `/clear` between cards.
- A session's card is its folder's in `folder_cards`, else the first card its prompts name or the
  card of a PR they link. Keys in a multi-line paste don't count.
- Sessions started with `/loop`, sessions in `ignored_folders` and cloud sessions aren't counted.

## Privacy

timelog reads ActivityWatch on your machine and writes to Jira only the worklogs you approve. What
ActivityWatch keeps (window titles, URLs, prompts) stays in its local database, unencrypted:
[aw-watcher-agents](https://github.com/vvrossem/aw-watcher-agents#privacy) can store less of the
prompts.
`timelog serve` answers on `127.0.0.1` only, to pages served from that address.

## Development

```sh
python3 -m pytest -q -p no:cacheprovider
```
