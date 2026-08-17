# Day planner bot

A Telegram bot that plans your day: timed blocks and loose tasks in one list, a
morning agenda, a nudge before each block, and a view of the free time you have
left.

## Planning

Send your plans as plain messages, one per line — no command needed:

```
9am-11am Gym
12.30pm lunch with Ada
tomorrow 1400-1600 project review
buy milk
```

A line with a time range becomes a block, a line with one time becomes a
one-hour block, and a line without a time becomes a task for that day. Dates can
be written as `today`, `tonight`, `tomorrow`, `friday`, `next tue`, `14/8` or
`14 Aug`; times as `9am`, `9.30pm`, `21:30` or `0930`.

Each plan gets a number of its own (`#3`) counting from 1 for every user, which
is what `/done`, `/move` and `/delete` take. Overlapping blocks are logged but
flagged as a clash.

## Your day

| Command | Purpose |
| --- | --- |
| `/today` | Today's plan, what's left, and your free gaps |
| `/tomorrow` | Tomorrow's plan |
| `/day <date>` | Any day, e.g. `/day friday` |
| `/week` | The next seven days |
| `/todo` | Everything still open, overdue items marked |
| `/free [date]` | The stretches of the day nothing is booked into |

## Changing plans

| Command | Purpose |
| --- | --- |
| `/plan <line>` | Add a plan (alias `/add`; plain messages work too) |
| `/done 3` | Tick a plan off (several numbers at once are fine) |
| `/undone 3` | Put it back on the list |
| `/move 3 tomorrow 4pm-5pm` | Reschedule it |
| `/delete 3` | Remove it |
| `/clear [day\|all]` | Wipe a day, after a confirmation |

## Reminders

`/reminders on` sends the day's agenda each morning at 08:00 (GMT+8 by default)
and a heads-up 30 minutes before every timed block. `/reminders 07:30 +8`
changes the time or timezone, `/reminders off` stops them. The bot process has
to be running for these to arrive.

## Running it

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=<token from @BotFather>
python -m planner.bot
```

Plans live in `planner.sqlite3` (`PLANNER_DB` to move it).

## Hosting

The `Dockerfile` runs `planner.web:app`, which polls Telegram in the background
and serves `/healthz`, so any platform that health-checks an HTTP port can host
it. `fly.toml` deploys it to Fly.io with a volume mounted at `/data`:

```bash
fly launch --no-deploy
fly volumes create planner_data --size 1
fly secrets set TELEGRAM_BOT_TOKEN=<token>
fly deploy
```

## Tests

```bash
python -m pytest -q
```
