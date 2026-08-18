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

## Class timetables

Send a photo of your timetable and the bot reads the grid, then writes every
class of the term into your plans:

```
📚 Timetable added — 5 classes, 62 sessions
Mon  09:30–11:20  IE4727 LEC @ S2-B3A_06     wk 1–11
Mon  14:30–17:20  ES5003 LEC @ LT19          wk 1–13
Fri  10:30–12:20  HW0288 TUT @ LHN-TR+18     wk 2–13
```

Tell it when the term starts and which weeks are off, as a caption on the photo
or with the command: `/timetable 10 Aug recess 28 Sep`. A recess week holds no
classes and doesn't count as a teaching week, so week 8 lands the week after it.
Both are remembered for the next import; without them week 1 is this week.
Sending a timetable again replaces the last import rather than doubling it, and
`/timetable clear` removes the classes from today onwards.

Send the screenshot itself rather than a photo of your screen, cropped to the
grid if you can — the picture is read at several sizes and the best reading
wins, but a small or blurred grid still defeats it. `/timetable 10 Aug recess
28 Sep` on its own re-dates the timetable it last read, without a new picture.

If the picture reads badly, type the rows instead — one class a line, naming
its day:

```
/timetable
MON 0930-1120 IE4727 LEC S2-B3A_06 Wk1-11
FRI 1030-1220 HW0288 TUT LHN-TR+18 Wk2-13
```

Reading pictures needs Tesseract (`apt-get install tesseract-ocr`); the
`Dockerfile` installs it.

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
| `/timetable 10 Aug recess 28 Sep` | Import a class timetable (`/timetable clear` removes it) |

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
