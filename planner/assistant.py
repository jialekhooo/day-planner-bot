"""Turn a plain-English message into calendar, mail and file work.

The model only decides *what* to do — it hands back a small list of steps in a
fixed shape, and this module carries them out against the person's own Google
account. Nothing is invented here: a step the model gets wrong is reported
rather than guessed at.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import google, model
from .google import DriveFile, GoogleError
from .storage import Storage

logger = logging.getLogger(__name__)

DEFAULT_MEETING = timedelta(hours=1)
LOOK_BACK = timedelta(days=7)
LOOK_AHEAD = timedelta(days=180)

PROMPT = """You are the assistant behind a Telegram bot. The person texts you \
in plain English and you decide which steps to run against their Google account.

Answer with JSON only:
{"reply": "one short line saying what you are doing", "steps": [ ...steps... ]}

A step is one of:
{"do": "create_event", "title": "...", "start": "2026-08-12T15:00", \
"end": "2026-08-12T16:00", "attendees": ["a@b.com"], "location": "", \
"description": "", "meet": true}
{"do": "update_event", "find": "words from the event title", "title": "", \
"start": "", "end": "", "attendees": [], "location": ""}
{"do": "cancel_event", "find": "words from the event title"}
{"do": "list_events", "start": "2026-08-12T00:00", "end": "2026-08-13T00:00"}
{"do": "send_email", "to": ["a@b.com"], "cc": [], "subject": "...", \
"body": "...", "attach": ["contract"], "include_meet_link": true}
{"do": "find_files", "name": "words in the file name"}
{"do": "search_mail", "query": "a gmail search, e.g. from:ada contract"}

Rules:
- Times are the person's local wall clock, written as YYYY-MM-DDTHH:MM with no zone.
- A meeting with no stated length runs an hour.
- "meet": true whenever a video link is wanted or people are invited remotely.
- "attach" names files to look up in their Drive and attach to the mail.
- "include_meet_link": true adds the link of the event created in an earlier \
step to the mail body, so "send the contract with the meeting link" is one \
create_event step followed by one send_email step.
- Only use an email address the person gave you, or one you found via \
search_mail; never invent an address.
- If the message is not something you can do with these steps, return no steps \
and say so in reply."""


@dataclass
class Result:
    """What the run did, as lines for the chat."""

    lines: list[str] = field(default_factory=list)
    failed: bool = False


def _local(text: str, offset_minutes: int) -> datetime:
    """A wall-clock string from the model, pinned to the person's zone."""
    stamp = datetime.fromisoformat(text)
    zone = timezone(timedelta(minutes=offset_minutes))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=zone)


def _strings(step: dict[str, object], key: str) -> list[str]:
    values = step.get(key, [])
    if isinstance(values, str):
        return [values] if values else []
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if str(value)]


def _clock(stamp: str) -> str:
    """An event time from Google, shortened for the chat."""
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    return when.strftime("%a %d %b %H:%M")


def plan_steps(message: str, now: datetime) -> tuple[str, list[dict[str, object]]]:
    """Ask the model what to do; an unreadable answer means no steps."""
    question = (
        f"Right now it is {now.strftime('%A %d %B %Y %H:%M')} in the person's own timezone.\n"
        f"Their message: {message}"
    )
    try:
        answer = json.loads(model.ask_json(PROMPT, question))
    except (ValueError, KeyError) as exc:
        logger.warning("assistant could not read the model's answer: %s", exc)
        return "", []
    if not isinstance(answer, dict):
        return "", []
    steps = answer.get("steps", [])
    return (
        str(answer.get("reply", "")),
        [step for step in steps if isinstance(step, dict) and step.get("do")],
    )


async def _attachments(token: str, names: list[str], result: Result) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for name in names:
        found = await google.find_files(token, name, limit=1)
        if not found:
            result.lines.append(f"📎 No file in Drive named like “{name}” — sent without it.")
            continue
        files.append(await google.download(token, found[0]))
    return files


def _file_line(file: DriveFile) -> str:
    return f"• {file.name} — {file.link}"


async def _run_step(
    step: dict[str, object],
    token: str,
    offset_minutes: int,
    now: datetime,
    result: Result,
    last_meet: list[str],
) -> None:
    do = str(step.get("do", ""))
    if do == "create_event":
        start = _local(str(step["start"]), offset_minutes)
        end = (
            _local(str(step["end"]), offset_minutes)
            if step.get("end")
            else start + DEFAULT_MEETING
        )
        event = await google.create_event(
            token,
            str(step.get("title", "Meeting")),
            start,
            end,
            attendees=_strings(step, "attendees"),
            description=str(step.get("description", "")),
            location=str(step.get("location", "")),
            meet=bool(step.get("meet")),
        )
        if event.meet:
            last_meet.append(event.meet)
        guests = f" · invited {', '.join(event.attendees)}" if event.attendees else ""
        result.lines.append(
            f"📅 {event.summary} — {_clock(event.start)}–{_clock(event.end)[-5:]}{guests}"
        )
        if event.meet:
            result.lines.append(f"🔗 {event.meet}")
        return

    if do in {"update_event", "cancel_event"}:
        event = await google.find_event(
            token, str(step.get("find", "")), now - LOOK_BACK, now + LOOK_AHEAD
        )
        if event is None:
            result.lines.append(f"🔎 No event matching “{step.get('find', '')}”.")
            result.failed = True
            return
        if do == "cancel_event":
            await google.delete_event(token, event.id)
            result.lines.append(
                f"🗑 Cancelled {event.summary} ({_clock(event.start)}) — guests told."
            )
            return
        start = _local(str(step["start"]), offset_minutes) if step.get("start") else None
        end = _local(str(step["end"]), offset_minutes) if step.get("end") else None
        if start is not None and end is None:
            end = start + DEFAULT_MEETING
        changed = await google.update_event(
            token,
            event.id,
            summary=str(step.get("title", "")),
            start=start,
            end=end,
            attendees=_strings(step, "attendees"),
            location=str(step.get("location", "")),
        )
        result.lines.append(
            f"✏️ {changed.summary} — now {_clock(changed.start)}–{_clock(changed.end)[-5:]}"
        )
        return

    if do == "list_events":
        start = (
            _local(str(step["start"]), offset_minutes)
            if step.get("start")
            else now
        )
        end = _local(str(step["end"]), offset_minutes) if step.get("end") else start + LOOK_AHEAD
        events = await google.list_events(token, start, end)
        if not events:
            result.lines.append("📅 Nothing in the calendar for that stretch.")
            return
        result.lines.extend(
            f"📅 {_clock(event.start)} {event.summary}" for event in events
        )
        return

    if do == "send_email":
        body = str(step.get("body", ""))
        if step.get("include_meet_link") and last_meet:
            body = f"{body}\n\nMeeting link: {last_meet[-1]}"
        files = await _attachments(token, _strings(step, "attach"), result)
        await google.send_email(
            token,
            _strings(step, "to"),
            str(step.get("subject", "")),
            body,
            cc=_strings(step, "cc"),
            attachments=files,
        )
        attached = f" with {', '.join(name for name, _ in files)}" if files else ""
        result.lines.append(
            f"📧 Sent “{step.get('subject', '')}” to {', '.join(_strings(step, 'to'))}{attached}"
        )
        return

    if do == "find_files":
        files = await google.find_files(token, str(step.get("name", "")))
        if not files:
            result.lines.append(f"🔎 Nothing in Drive named like “{step.get('name', '')}”.")
            return
        result.lines.append("📁 In your Drive:")
        result.lines.extend(_file_line(file) for file in files)
        return

    if do == "search_mail":
        mails = await google.search_mail(token, str(step.get("query", "")))
        if not mails:
            result.lines.append(f"🔎 No mail matching “{step.get('query', '')}”.")
            return
        result.lines.append("📬 Found:")
        result.lines.extend(f"• {mail.sender} — {mail.subject}" for mail in mails)
        return

    result.lines.append(f"🤷 I don't know how to {do}.")
    result.failed = True


async def handle(
    storage: Storage,
    user_id: int,
    message: str,
    now: datetime,
    offset_minutes: int,
) -> Result:
    """Work out what the message asks for and do it, reporting each step."""
    if not model.available():
        return Result(["The assistant needs GEMINI_API_KEY set."], failed=True)
    if not google.configured():
        return Result(["Google isn't set up on this bot yet."], failed=True)
    reply, steps = await asyncio.to_thread(plan_steps, message, now)
    if not steps:
        return Result([reply or "I couldn't turn that into anything I can do."], failed=True)

    result = Result([reply] if reply else [])
    last_meet: list[str] = []
    try:
        token = await google.access_token(storage, user_id)
        for step in steps:
            await _run_step(step, token, offset_minutes, now, result, last_meet)
    except GoogleError as exc:
        result.lines.append(f"⚠️ {exc}")
        result.failed = True
    except (KeyError, ValueError) as exc:
        logger.warning("assistant step was malformed: %s", exc)
        result.lines.append("⚠️ I understood the request but the details came back wrong.")
        result.failed = True
    return result
