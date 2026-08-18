"""Telegram bot that plans the day: timed blocks, tasks, agenda and reminders."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from typing import Awaitable, Callable

from telegram import BotCommand, PhotoSize, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import assistant, google
from .agenda import (
    DAY_END,
    DAY_START,
    clashes_with,
    clashing,
    free_gaps,
    local_now,
    local_today,
)
from .parsing import Entry, ParseError, parse_date, parse_entries, parse_entry, parse_time
from .storage import DEFAULT_AGENDA_AT, DEFAULT_UTC_OFFSET_MINUTES, Plan, Storage, Term
from .timetable import (
    Class,
    TimetableError,
    classes_as_text,
    classes_from_image,
    classes_from_text,
    week_dates,
)

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("PLANNER_DB", "planner.sqlite3")
NUDGE_AHEAD = timedelta(minutes=30)
TIMETABLE = "timetable"
SEMESTER_WEEKS = 14
CLASHES_SHOWN = 5  # a term's import can clash in many places; the rest are counted

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    names: tuple[str, ...]
    usage: str
    summary: str
    handler: Handler
    section: str


SAMPLE = (
    "Send your plans, one per line:\n\n"
    "<code>9am-11am Gym\n"
    "12.30pm lunch with Ada\n"
    "tomorrow 1400-1600 project review\n"
    "buy milk</code>\n\n"
    "A line with times becomes a block in your day, one without becomes a task."
)


def _storage(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.application.bot_data["storage"]


def _offset(storage: Storage, user_id: int) -> int:
    reminder = storage.get_reminder(user_id)
    return DEFAULT_UTC_OFFSET_MINUTES if reminder is None else reminder.utc_offset_minutes


def _today(storage: Storage, user_id: int) -> date:
    return local_today(_offset(storage, user_id))


def _aligned(rows: list[tuple[str, ...]]) -> list[str]:
    """Pad the columns so a monospace block lines up."""
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return [
        "  ".join(cell.ljust(widths[column]) for column, cell in enumerate(row)).rstrip()
        for row in rows
    ]


def _block(lines: list[str]) -> str:
    return "<pre>" + "\n".join(escape(line) for line in lines) + "</pre>"


async def _send_html(update: Update, lines: list[str]) -> None:
    await update.message.reply_text("\n".join(lines).strip(), parse_mode=ParseMode.HTML)


def _clock(plan: Plan) -> str:
    if plan.start is None:
        return "—"
    if plan.end is None:
        return plan.start.strftime("%H:%M")
    return f"{plan.start.strftime('%H:%M')}–{plan.end.strftime('%H:%M')}"


def _plan_rows(plans: list[Plan], clashes: set[int]) -> list[tuple[str, ...]]:
    return [
        (
            "✔" if plan.done else "·",
            _clock(plan),
            plan.title,
            "clash" if plan.id in clashes and not plan.done else "",
        )
        for plan in plans
    ]


def _day_label(day: date, today: date) -> str:
    if day == today:
        return f"Today · {day.strftime('%a %d %b')}"
    if day == today + timedelta(days=1):
        return f"Tomorrow · {day.strftime('%a %d %b')}"
    return day.strftime("%a %d %b %Y")


def _gaps_line(plans: list[Plan], day: date, today: date, now: datetime) -> str | None:
    after = now.time() if day == today else None
    if after and after >= DAY_END:
        return None
    gaps = free_gaps([p for p in plans if not p.done], day, after=after)
    if not gaps:
        return (
            f"No free time left between {DAY_START.strftime('%H:%M')} "
            f"and {DAY_END.strftime('%H:%M')}."
        )
    shown = ", ".join(f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}" for start, end in gaps)
    return f"Free  {shown}"


async def _show_day(update: Update, context: ContextTypes.DEFAULT_TYPE, day: date) -> None:
    storage = _storage(context)
    user_id = update.effective_user.id
    today = _today(storage, user_id)
    now = local_now(_offset(storage, user_id))
    plans = storage.plans_on(user_id, day)
    heading = f"🗓 <b>{escape(_day_label(day, today))}</b>"
    if not plans:
        await _send_html(update, [heading, "Nothing planned yet — send me a line to add one."])
        return
    lines = [heading, _block(_aligned(_plan_rows(plans, clashing(plans))))]
    left = sum(1 for plan in plans if not plan.done)
    lines.append(f"{len(plans)} planned · <b>{left} left</b>")
    gaps = _gaps_line(plans, day, today, now)
    if gaps:
        lines.append(gaps)
    await _send_html(update, lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_html(
        update,
        [
            "👋 <b>Your day planner</b>",
            SAMPLE,
            "Then <code>/today</code> for the plan, <code>/done gym</code> to tick something "
            "off, "
            "<code>/commands</code> for everything else.",
            "Studying? Send a photo of your class timetable and I'll put the whole term in.",
            "Connect Google with <code>/connect</code> and I'll also book meetings, send "
            "invites and email files — just say what you need.",
        ],
    )


async def commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["📋 <b>Commands</b>"]
    for section in ("Planning", "Your day", "Assistant", "Reminders", "Help"):
        rows = [
            (command.usage, command.summary)
            for command in COMMANDS
            if command.section == section
        ]
        if rows:
            lines.append(f"\n<b>{section}</b>")
            lines.append(_block(_aligned(rows)))
    await _send_html(update, lines)


def _added_lines(entries: list[tuple[int, Entry]], today: date) -> list[str]:
    rows = [
        (
            _day_label(entry.day, today).split(" · ")[0],
            _clock(Plan(ref, entry.day, entry.title, entry.start, entry.end, False)),
            entry.title,
        )
        for ref, entry in entries
    ]
    return [_block(_aligned(rows))]


def _clash_lines(storage: Storage, user_id: int, refs: list[int], today: date) -> list[str]:
    """A warning naming what each new plan runs into, so a double booking is obvious."""
    lines: list[str] = []
    for ref in refs:
        plan = storage.get_plan(user_id, ref)
        if plan is None:
            continue
        against = clashes_with(plan, storage.plans_on(user_id, plan.day))
        if not against:
            continue
        lines.append(
            f"⚠️ <b>Clash</b> on {escape(_day_label(plan.day, today).split(' · ')[0])}: "
            f"{escape(_clock(plan))} {escape(plan.title)} runs into "
            + ", ".join(
                f"{escape(_clock(other))} {escape(other.title)}" for other in against
            )
        )
    return lines


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store every line of the message as a block or a task."""
    storage = _storage(context)
    user_id = update.effective_user.id
    today = _today(storage, user_id)
    text = " ".join(context.args) if context.args else (update.message.text or "")
    results = parse_entries(text, today=today)
    if not results:
        await _send_html(update, ["I didn't catch a plan there.", SAMPLE])
        return

    stored: list[tuple[int, Entry]] = []
    problems: list[str] = []
    for line, outcome in results:
        if isinstance(outcome, ParseError):
            problems.append(f"{line} — {outcome}")
            continue
        ref = storage.add_plan(user_id, outcome.day, outcome.title, outcome.start, outcome.end)
        stored.append((ref, outcome))

    lines: list[str] = []
    if stored:
        lines.append(f"✅ <b>Added {len(stored)}</b>")
        lines.extend(_added_lines(stored, today))
        lines.extend(_clash_lines(storage, user_id, [ref for ref, _ in stored], today))
    if problems:
        lines.append("⚠️ <b>Couldn't read</b>")
        lines.append(_block(problems))
    await _send_html(update, lines)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage = _storage(context)
    await _show_day(update, context, _today(storage, update.effective_user.id))


async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage = _storage(context)
    await _show_day(
        update, context, _today(storage, update.effective_user.id) + timedelta(days=1)
    )


async def day_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The plan for any date: /day friday, /day 14/8."""
    storage = _storage(context)
    user_id = update.effective_user.id
    if not context.args:
        await _show_day(update, context, _today(storage, user_id))
        return
    try:
        day = parse_date(" ".join(context.args), _today(storage, user_id))
    except ParseError as exc:
        await update.message.reply_text(str(exc))
        return
    await _show_day(update, context, day)


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The next seven days, day by day."""
    storage = _storage(context)
    user_id = update.effective_user.id
    start_day = _today(storage, user_id)
    plans = storage.plans_between(user_id, start_day, start_day + timedelta(days=6))
    if not plans:
        await update.message.reply_text("Nothing planned in the next 7 days.")
        return
    clashes = clashing(plans)
    lines = ["🗓 <b>Next 7 days</b>"]
    for offset in range(7):
        day = start_day + timedelta(days=offset)
        of_the_day = [plan for plan in plans if plan.day == day]
        if not of_the_day:
            continue
        lines.append(f"\n<b>{escape(_day_label(day, start_day))}</b>")
        lines.append(_block(_aligned(_plan_rows(of_the_day, clashes))))
    left = sum(1 for plan in plans if not plan.done)
    lines.append(f"{len(plans)} planned · <b>{left} left</b>")
    await _send_html(update, lines)


async def todo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Everything still open, whenever it was planned for."""
    storage = _storage(context)
    user_id = update.effective_user.id
    plans = storage.open_plans(user_id)
    if not plans:
        await update.message.reply_text("Nothing outstanding — all done. 🎉")
        return
    today_local = _today(storage, user_id)
    rows = [
        (
            _day_label(plan.day, today_local).split(" · ")[0],
            _clock(plan),
            plan.title,
            "overdue" if plan.day < today_local else "",
        )
        for plan in plans
    ]
    await _send_html(
        update,
        ["📝 <b>Still to do</b>", _block(_aligned(rows)), f"<b>{len(plans)} open</b>"],
    )


def _numbers(args: list[str]) -> list[int]:
    return [int(arg.lstrip("#")) for arg in args if arg.lstrip("#").isdigit()]


def _picked(storage: Storage, user_id: int, args: list[str], today: date) -> list[int]:
    """Which plans the words point at — a few words of the title, nearest day first."""
    refs = _numbers(args)
    if refs:
        return refs
    text = " ".join(args).strip()
    if not text:
        return []
    found = sorted(
        storage.find_plans(user_id, text),
        key=lambda plan: (plan.done, plan.day < today, abs((plan.day - today).days)),
    )
    return [found[0].id] if found else []


async def _mark(update: Update, context: ContextTypes.DEFAULT_TYPE, done: bool) -> None:
    storage = _storage(context)
    user_id = update.effective_user.id
    args = context.args or []
    refs = _picked(storage, user_id, args, _today(storage, user_id))
    if not refs:
        word = "done" if done else "undone"
        await update.message.reply_text(
            f"Nothing matched that — name the plan, e.g. /{word} gym."
            if args
            else f"Name the plan, e.g. /{word} gym."
        )
        return
    changed = [ref for ref in refs if storage.set_done(user_id, ref, done)]
    missing = sorted(set(refs) - set(changed))
    word = "Done" if done else "Reopened"
    lines = []
    if changed:
        titles = [storage.get_plan(user_id, ref) for ref in changed]
        rows = [(_clock(plan), plan.title) for plan in titles if plan is not None]
        lines += [f"{'✅' if done else '↩️'} <b>{word}</b>", _block(_aligned(rows))]
    if missing:
        lines.append("Couldn't find some of those.")
    left = len(storage.open_plans(user_id))
    lines.append(f"<b>{left} open</b> in total")
    await _send_html(update, lines)


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _mark(update, context, True)


async def undone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _mark(update, context, False)


def _split_move(args: list[str]) -> tuple[list[str], list[str]]:
    """`gym to tomorrow 4pm` splits into which plan and when; a number needs no `to`."""
    if args and args[0].lstrip("#").isdigit():
        return args[:1], args[1:]
    for position in range(len(args) - 1, -1, -1):
        if args[position].lower() == "to":
            return args[:position], args[position + 1 :]
    return args[:1], args[1:]


async def move(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reschedule a plan: /move gym to tomorrow 4pm-5pm."""
    storage = _storage(context)
    user_id = update.effective_user.id
    args = context.args or []
    today_local = _today(storage, user_id)
    which, when = _split_move(args)
    refs = _picked(storage, user_id, which, today_local)
    if not refs or not when:
        await update.message.reply_text("Use: /move gym to tomorrow 4pm-5pm")
        return
    ref = refs[0]
    plan = storage.get_plan(user_id, ref)
    if plan is None:
        await update.message.reply_text("I couldn't find that plan.")
        return
    try:
        entry = parse_entry(f"{' '.join(when)} {plan.title}", today=today_local)
    except ParseError as exc:
        await update.message.reply_text(str(exc))
        return
    storage.move_plan(user_id, ref, entry.day, entry.start, entry.end)
    moved = storage.get_plan(user_id, ref)
    if moved is None:
        await update.message.reply_text("I couldn't find that plan.")
        return
    await _send_html(
        update,
        [
            "🔁 <b>Moved</b>",
            _block(
                _aligned(
                    [
                        (
                            _day_label(moved.day, today_local).split(" · ")[0],
                            _clock(moved),
                            moved.title,
                        )
                    ]
                )
            ),
            *_clash_lines(storage, user_id, [moved.id], today_local),
        ],
    )


async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage = _storage(context)
    user_id = update.effective_user.id
    args = context.args or []
    refs = _picked(storage, user_id, args, _today(storage, user_id))
    if not refs:
        await update.message.reply_text("Name the plan, e.g. /delete gym.")
        return
    removed = [
        plan
        for plan in (storage.get_plan(user_id, ref) for ref in refs)
        if plan is not None and storage.delete_plan(user_id, plan.id)
    ]
    if not removed:
        await update.message.reply_text("Nothing matched that.")
        return
    rows = [(_clock(plan), plan.title) for plan in removed]
    await _send_html(update, ["🗑 <b>Deleted</b>", _block(_aligned(rows))])


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe a day (or everything) once you confirm it."""
    storage = _storage(context)
    user_id = update.effective_user.id
    args = [arg for arg in (context.args or [])]
    confirmed = bool(args) and args[-1].lower() == "confirm"
    if confirmed:
        args = args[:-1]
    today_local = _today(storage, user_id)
    day: date | None = None
    if args and args[0].lower() != "all":
        try:
            day = parse_date(" ".join(args), today_local)
        except ParseError as exc:
            await update.message.reply_text(str(exc))
            return
    scope = _day_label(day, today_local) if day else "everything"
    if not confirmed:
        await update.message.reply_text(
            f"This deletes {scope}. Send the same command with 'confirm' to go ahead."
        )
        return
    removed = storage.delete_plans(user_id, day)
    await _send_html(update, [f"🗑 Cleared <b>{escape(scope)}</b> — {removed} removed."])


async def free(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Where the gaps are in a day."""
    storage = _storage(context)
    user_id = update.effective_user.id
    today_local = _today(storage, user_id)
    day = today_local
    if context.args:
        try:
            day = parse_date(" ".join(context.args), today_local)
        except ParseError as exc:
            await update.message.reply_text(str(exc))
            return
    plans = storage.plans_on(user_id, day)
    now = local_now(_offset(storage, user_id))
    gaps = _gaps_line(plans, day, today_local, now)
    await _send_html(
        update,
        [
            f"🕳 <b>{escape(_day_label(day, today_local))}</b>",
            gaps or "The day is over — nothing free left.",
        ],
    )


BREAK_WORDS = ("recess", "break", "holiday", "skip")


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _term(args: list[str], today: date, saved: Term | None) -> Term:
    """`10 Aug recess 28 Sep` — when week one starts and which weeks are off.

    Anything left out keeps what the last import used, or this week for a first one.
    """
    parts: list[list[str]] = [[]]
    for word in args:
        if word.lower().strip(",") in BREAK_WORDS:
            parts.append([])
            continue
        parts[-1].append(word)
    week_one = _read_date(" ".join(parts[0]), today)
    breaks = tuple(
        day
        for part in parts[1:]
        for piece in " ".join(part).split(",")
        if (day := _read_date(piece, today)) is not None
    )
    if week_one is None and saved is not None:
        return Term(saved.week_one, breaks or saved.breaks)
    return Term(_monday(week_one or today), breaks)


def _read_date(text: str, today: date) -> date | None:
    if not text.strip():
        return None
    try:
        return parse_date(text.strip(), today)
    except ParseError:
        return None


def _weeks_label(weeks: tuple[int, ...]) -> str:
    """`1,2,3,5` reads as `1–3,5`."""
    parts: list[str] = []
    for week in weeks:
        if parts and week == int(parts[-1].split("–")[-1]) + 1:
            parts[-1] = f"{parts[-1].split('–')[0]}–{week}"
            continue
        parts.append(str(week))
    return ",".join(parts)


def _class_rows(classes: list[Class], weeks: int) -> list[tuple[str, ...]]:
    return [
        (
            "MonTueWedThuFriSatSun"[lesson.weekday * 3 :][:3],
            f"{lesson.start.strftime('%H:%M')}–{lesson.end.strftime('%H:%M')}",
            lesson.title,
            f"wk {_weeks_label(lesson.weeks)}" if lesson.weeks else f"wk 1–{weeks}",
        )
        for lesson in sorted(classes, key=lambda item: (item.weekday, item.start))
    ]


async def _import_classes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    classes: list[Class],
    args: list[str],
) -> None:
    """Put every class of the term into the diary, replacing an earlier import."""
    storage = _storage(context)
    user_id = update.effective_user.id
    today = _today(storage, user_id)
    if not classes:
        await _send_html(
            update,
            [
                "I couldn't find any classes in that.",
                "Send the timetable screenshot itself, or type the rows out like "
                "<code>MON 0930-1120 IE4727 LEC S2-B3A_06</code>.",
            ],
        )
        return

    term = _term(args, today, storage.get_term(user_id))
    storage.save_term(user_id, term)
    storage.save_classes(user_id, classes_as_text(classes))
    weeks = max((max(lesson.weeks) for lesson in classes if lesson.weeks), default=SEMESTER_WEEKS)
    storage.delete_from_source(user_id, TIMETABLE, since=term.week_one)
    dated = week_dates(classes, term.week_one, weeks, term.breaks)
    refs = [
        storage.add_plan(user_id, day, lesson.title, lesson.start, lesson.end, source=TIMETABLE)
        for day, lesson in dated
    ]
    clashes = _clash_lines(storage, user_id, refs, today)

    await _send_html(
        update,
        [
            f"📚 <b>Timetable added</b> — {len(classes)} classes, {len(dated)} sessions",
            _block(_aligned(_class_rows(classes, weeks))),
            f"Week 1 starts {escape(term.week_one.strftime('%a %d %b %Y'))}"
            + (
                ", off the week of "
                + ", ".join(escape(_monday(day).strftime("%d %b")) for day in term.breaks)
                + "."
                if term.breaks
                else "."
            ),
            *clashes[:CLASHES_SHOWN],
            *(
                [f"…and {len(clashes) - CLASHES_SHOWN} more clashes."]
                if len(clashes) > CLASHES_SHOWN
                else []
            ),
            "Redo it with <code>/timetable 10 Aug recess 28 Sep</code>, or "
            "<code>/timetable clear</code> to remove the classes.",
        ],
    )


async def _import_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    photo: PhotoSize,
    args: list[str],
) -> None:
    picture = await (await photo.get_file()).download_as_bytearray()
    await update.message.reply_text("📖 Reading your timetable…")
    try:  # reading a picture takes seconds, so keep the bot answering meanwhile
        classes = await asyncio.to_thread(classes_from_image, bytes(picture))
    except TimetableError as exc:
        await update.message.reply_text(str(exc))
        return
    await _import_classes(update, context, classes, args)


async def timetable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/timetable — import a class timetable, sent as a screenshot or typed out."""
    storage = _storage(context)
    user_id = update.effective_user.id
    args = list(context.args or [])
    if args and args[0].lower() == "clear":
        removed = storage.delete_from_source(user_id, TIMETABLE, since=_today(storage, user_id))
        await _send_html(update, [f"🗑 Removed {removed} timetable sessions from today on."])
        return

    replied = update.message.reply_to_message
    photo = update.message.photo or (replied.photo if replied else None)
    if photo:
        await _import_photo(update, context, photo[-1], args)
        return

    body = "\n".join((update.message.text or "").splitlines()[1:])
    typed = classes_from_text(body)
    # `/timetable 10 Aug recess 28 Sep` on its own re-dates the timetable already read
    remembered = classes_from_text(storage.get_classes(user_id)) if not typed else []
    await _import_classes(update, context, typed or remembered, args)


async def timetable_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo on its own is read as a timetable."""
    words = (update.message.caption or "").split()
    args = [word for word in words if not word.startswith("/")]
    await _import_photo(update, context, update.message.photo[-1], args)


async def reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reminders on|off|08:00 [+8] — the morning agenda and 30-minute heads-ups."""
    storage = _storage(context)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    current = storage.get_reminder(user_id)
    agenda_at = current.agenda_at if current else DEFAULT_AGENDA_AT
    offset = current.utc_offset_minutes if current else DEFAULT_UTC_OFFSET_MINUTES
    enabled = current.enabled if current else False
    args = [arg.lower() for arg in (context.args or [])]

    if not args:
        state = "on" if enabled else "off"
        await _send_html(
            update,
            [
                f"⏰ Reminders are <b>{state}</b> — agenda at "
                f"{agenda_at.strftime('%H:%M')} (UTC{offset // 60:+d}), "
                "plus a nudge 30 minutes before each timed plan.",
                "Change with <code>/reminders on</code>, <code>/reminders 07:30 +8</code> "
                "or <code>/reminders off</code>.",
            ],
        )
        return

    for arg in args:
        if arg == "on":
            enabled = True
        elif arg == "off":
            enabled = False
        elif arg.startswith(("+", "-")) and arg[1:].replace(":", "").isdigit():
            hours, _, minutes = arg[1:].partition(":")
            total = int(hours) * 60 + int(minutes or 0)
            offset = total if arg[0] == "+" else -total
        else:
            try:
                agenda_at = parse_time(arg)
                enabled = True
            except ParseError:
                await update.message.reply_text(f"I didn't understand {arg!r}.")
                return

    storage.save_reminder(user_id, chat_id, agenda_at, offset, enabled)
    state = "on" if enabled else "off"
    await _send_html(
        update,
        [
            f"⏰ Reminders <b>{state}</b> — agenda at {agenda_at.strftime('%H:%M')} "
            f"(UTC{offset // 60:+d})."
        ],
    )


def _agenda_lines(plans: list[Plan], day: date) -> list[str]:
    rows = [(_clock(plan), plan.title) for plan in plans]
    return [
        f"☀️ <b>{escape(day.strftime('%A %d %b'))}</b>",
        _block(_aligned(rows)),
        f"<b>{len(plans)} planned</b>",
    ]


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Once a minute: send the morning agenda and any 30-minute heads-up that is due."""
    storage: Storage = context.application.bot_data["storage"]
    for reminder in storage.enabled_reminders():
        now = local_now(reminder.utc_offset_minutes)
        day = now.date()
        plans = [plan for plan in storage.plans_on(reminder.user_id, day) if not plan.done]
        if plans and now.time() >= reminder.agenda_at and reminder.last_sent_on != day:
            await _send(context, reminder.chat_id, _agenda_lines(plans, day))
            storage.mark_agenda_sent(reminder.user_id, day)
        for plan in storage.pending_nudges(reminder.user_id, day):
            due = datetime.combine(day, plan.start or time())
            if now >= due - NUDGE_AHEAD:
                storage.mark_nudged(reminder.user_id, plan.id)
                if now <= due:
                    await _send(
                        context,
                        reminder.chat_id,
                        [
                            f"⏰ <b>{escape(plan.title)}</b> at "
                            f"{(plan.start or time()).strftime('%H:%M')} "
                            f"— in {int((due - now).total_seconds() // 60)} min."
                        ],
                    )


async def _send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, lines: list[str]) -> None:
    try:
        await context.bot.send_message(
            chat_id, "\n".join(lines).strip(), parse_mode=ParseMode.HTML
        )
    except TelegramError:
        logger.exception("Could not send a reminder to chat %s", chat_id)


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("I don't know that one — /commands lists them all.")


ASSISTANT_HINT = re.compile(
    r"\b(email|e-mail|mail|gmail|inbox|invite|invitation|attach|attachment|contract|"
    r"drive|file|document|doc|send|forward|reply|meeting|meet|zoom|call|schedule|"
    r"reschedule|postpone|cancel|book|calendar)\b",
    re.IGNORECASE,
)


async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hand over a one-tap consent link for the person's Google account."""
    storage = _storage(context)
    if not google.configured():
        await update.message.reply_text(
            "Google isn't set up on this bot yet — it needs GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET."
        )
        return
    state = secrets.token_urlsafe(24)
    storage.start_google_link(state, update.effective_user.id, update.effective_chat.id)
    await _send_html(
        update,
        [
            "🔗 <b>Connect Google</b>",
            f'<a href="{escape(google.consent_url(state))}">Tap here to allow calendar, '
            "mail and files</a>, then just tell me what you need.",
            "<code>/disconnect</code> revokes it again.",
        ],
    )


async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage = _storage(context)
    dropped = await google.disconnect(storage, update.effective_user.id)
    await update.message.reply_text(
        "Google disconnected." if dropped else "No Google account was connected."
    )


async def _assist(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    storage = _storage(context)
    user_id = update.effective_user.id
    if storage.get_google(user_id) is None:
        await update.message.reply_text("Connect your Google account first with /connect.")
        return
    offset = _offset(storage, user_id)
    await update.message.chat.send_action(ChatAction.TYPING)
    result = await assistant.handle(storage, user_id, text, local_now(offset), offset)
    await _send_html(update, [escape(line) for line in result.lines])


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Do whatever the sentence asks of the calendar, mail and files."""
    text = " ".join(context.args or [])
    if not text:
        await update.message.reply_text(
            "Tell me what you need, e.g. /ask meet Ada tomorrow 3pm with a Meet link "
            "and email her the contract."
        )
        return
    await _assist(update, context, text)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A plan line goes in the diary; anything about mail or meetings goes to the assistant."""
    storage = _storage(context)
    text = update.message.text or ""
    connected = storage.get_google(update.effective_user.id) is not None
    if connected and ASSISTANT_HINT.search(text):
        await _assist(update, context, text)
        return
    await add(update, context)


COMMANDS: tuple[Command, ...] = (
    Command(("plan", "add"), "/plan <line>", "Add a block or a task", add, "Planning"),
    Command(("move",), "/move gym to tomorrow 4pm", "Reschedule a plan", move, "Planning"),
    Command(("done",), "/done gym", "Tick a plan off", done, "Planning"),
    Command(("undone", "reopen"), "/undone gym", "Put it back on the list", undone, "Planning"),
    Command(("delete", "del"), "/delete gym", "Remove a plan", delete, "Planning"),
    Command(("clear",), "/clear [day|all]", "Wipe a day (asks first)", clear, "Planning"),
    Command(("today",), "/today", "Today's plan", today, "Your day"),
    Command(("tomorrow",), "/tomorrow", "Tomorrow's plan", tomorrow, "Your day"),
    Command(("day", "on"), "/day <date>", "Any day's plan", day_plan, "Your day"),
    Command(("week",), "/week", "The next 7 days", week, "Your day"),
    Command(("todo", "open"), "/todo", "Everything still open", todo, "Your day"),
    Command(("free", "gaps"), "/free [date]", "Where your free time is", free, "Your day"),
    Command(
        ("timetable", "classes"),
        "/timetable 10 Aug recess 28 Sep",
        "Import a class timetable",
        timetable,
        "Planning",
    ),
    Command(
        ("reminders", "remind"),
        "/reminders on|off|08:00",
        "Morning agenda + 30-min nudges",
        reminders,
        "Reminders",
    ),
    Command(
        ("ask", "assistant"),
        "/ask <what you need>",
        "Calendar, mail and files",
        ask,
        "Assistant",
    ),
    Command(("connect",), "/connect", "Link your Google account", connect, "Assistant"),
    Command(("disconnect",), "/disconnect", "Unlink Google", disconnect, "Assistant"),
    Command(("commands", "cmds"), "/commands", "This list", commands, "Help"),
    Command(("help", "start"), "/help", "How to plan your day", start, "Help"),
)


async def _publish_commands(application: Application) -> None:
    await application.bot.set_my_commands(
        [BotCommand(command.names[0], command.summary) for command in COMMANDS]
    )


def build_application(token: str, db_path: str = DB_PATH) -> Application:
    application = ApplicationBuilder().token(token).post_init(_publish_commands).build()
    application.bot_data["storage"] = Storage(db_path)
    for command in COMMANDS:
        application.add_handler(CommandHandler(list(command.names), command.handler))
    application.add_handler(MessageHandler(filters.PHOTO, timetable_photo))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    if application.job_queue is not None:
        application.job_queue.run_repeating(tick, interval=60, first=10)
    return application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN to your @BotFather token.")
    build_application(token).run_polling()


if __name__ == "__main__":
    main()
