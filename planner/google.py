"""Talk to the owner's Google account: Calendar, Gmail and Drive.

The bot never holds a password. Each person taps a consent link once, Google
hands back a refresh token, and that token is traded for a short-lived key
whenever a request needs one.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlencode

import httpx

from .storage import Storage

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
DRIVE_URL = "https://www.googleapis.com/drive/v3/files"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)
TIMEOUT = 30.0
EARLY = timedelta(minutes=2)  # refresh before the key actually lapses
BIGGEST_ATTACHMENT = 20 * 1024 * 1024  # Gmail's plain upload limit is 25MB encoded


class GoogleError(RuntimeError):
    """Something Google refused, phrased for the chat."""


@dataclass(frozen=True)
class Event:
    id: str
    summary: str
    start: str
    end: str
    link: str
    meet: str
    attendees: tuple[str, ...]


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    link: str


def configured() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def redirect_uri() -> str:
    return os.environ.get(
        "GOOGLE_REDIRECT_URI", "https://day-planner-bot.fly.dev/oauth/callback"
    )


def consent_url(state: str) -> str:
    """Where the person taps to hand the bot access."""
    query = urlencode(
        {
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


async def _post_token(form: dict[str, str]) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(TOKEN_URL, data=form)
    if response.status_code >= 400:
        raise GoogleError(f"Google turned the sign-in down: {response.text[:200]}")
    return response.json()


async def finish_consent(storage: Storage, user_id: int, code: str) -> str:
    """Swap the one-time code for tokens, remember them, and say whose account it is."""
    answer = await _post_token(
        {
            "code": code,
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        }
    )
    refresh = str(answer.get("refresh_token", ""))
    access = str(answer.get("access_token", ""))
    if not refresh or not access:
        raise GoogleError("Google didn't hand back a lasting token — try connecting again.")
    expires = _expiry(answer)
    email = await _email_of(access)
    storage.save_google(user_id, email, refresh, access, expires)
    return email


def _expiry(answer: dict[str, object]) -> datetime:
    seconds = answer.get("expires_in")
    lasts = int(seconds) if isinstance(seconds, (int, float, str)) and str(seconds).isdigit() else 0
    return datetime.now(timezone.utc) + timedelta(seconds=lasts or 3600)


async def _email_of(access_token: str) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
    if response.status_code >= 400:
        return ""
    return str(response.json().get("email", ""))


async def access_token(storage: Storage, user_id: int) -> str:
    """The current key for this person, refreshed when it has gone stale."""
    account = storage.get_google(user_id)
    if account is None:
        raise GoogleError("Connect your Google account first with /connect.")
    if account.expires_at - EARLY > datetime.now(timezone.utc):
        return account.access_token
    answer = await _post_token(
        {
            "refresh_token": account.refresh_token,
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "grant_type": "refresh_token",
        }
    )
    fresh = str(answer.get("access_token", ""))
    if not fresh:
        raise GoogleError("Google wouldn't renew the access — reconnect with /connect.")
    storage.save_google_access(user_id, fresh, _expiry(answer))
    return fresh


async def disconnect(storage: Storage, user_id: int) -> bool:
    """Hand the grant back to Google and forget it here."""
    account = storage.get_google(user_id)
    if account is None:
        return False
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await client.post(REVOKE_URL, data={"token": account.refresh_token})
    return storage.forget_google(user_id)


async def _call(
    token: str,
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=body,
        )
    if response.status_code >= 400:
        raise GoogleError(_complaint(response))
    if not response.content:
        return {}
    return response.json()


def _complaint(response: httpx.Response) -> str:
    """Google's error message, without the JSON scaffolding."""
    try:
        problem = response.json().get("error", {})
    except ValueError:
        return f"Google said {response.status_code}."
    if isinstance(problem, dict) and problem.get("message"):
        return f"Google said: {problem['message']}"
    return f"Google said {response.status_code}."


def _when(edge: object) -> str:
    """An event edge is either a timestamp or, for all-day events, a date."""
    if not isinstance(edge, dict):
        return ""
    return str(edge.get("dateTime") or edge.get("date") or "")


def _event(row: dict[str, object]) -> Event:
    people = row.get("attendees", [])
    conference = row.get("conferenceData", {})
    meet = str(row.get("hangoutLink", ""))
    if not meet and isinstance(conference, dict):
        for point in conference.get("entryPoints", []):
            if isinstance(point, dict) and point.get("entryPointType") == "video":
                meet = str(point.get("uri", ""))
    return Event(
        id=str(row.get("id", "")),
        summary=str(row.get("summary", "(no title)")),
        start=_when(row.get("start")),
        end=_when(row.get("end")),
        link=str(row.get("htmlLink", "")),
        meet=meet,
        attendees=tuple(
            str(person.get("email", ""))
            for person in people
            if isinstance(person, dict) and person.get("email")
        )
        if isinstance(people, list)
        else (),
    )


async def list_events(token: str, start: datetime, end: datetime, limit: int = 20) -> list[Event]:
    answer = await _call(
        token,
        "GET",
        CALENDAR_URL,
        params={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(limit),
        },
    )
    rows = answer.get("items", [])
    return [_event(row) for row in rows if isinstance(row, dict)]


async def find_event(token: str, query: str, start: datetime, end: datetime) -> Event | None:
    """The next event matching a few words, e.g. "standup with Ada"."""
    answer = await _call(
        token,
        "GET",
        CALENDAR_URL,
        params={
            "q": query,
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "5",
        },
    )
    rows = [row for row in answer.get("items", []) if isinstance(row, dict)]
    return _event(rows[0]) if rows else None


def _times(start: datetime, end: datetime) -> dict[str, object]:
    return {
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }


async def create_event(
    token: str,
    summary: str,
    start: datetime,
    end: datetime,
    *,
    attendees: list[str] | None = None,
    description: str = "",
    location: str = "",
    meet: bool = False,
) -> Event:
    body: dict[str, object] = {"summary": summary, **_times(start, end)}
    if attendees:
        body["attendees"] = [{"email": address} for address in attendees]
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if meet:
        body["conferenceData"] = {
            "createRequest": {
                "requestId": f"meet-{int(start.timestamp())}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    answer = await _call(
        token,
        "POST",
        CALENDAR_URL,
        params={
            "sendUpdates": "all",
            "conferenceDataVersion": "1" if meet else "0",
        },
        body=body,
    )
    return _event(answer)


async def update_event(
    token: str,
    event_id: str,
    *,
    summary: str = "",
    start: datetime | None = None,
    end: datetime | None = None,
    attendees: list[str] | None = None,
    description: str = "",
    location: str = "",
) -> Event:
    body: dict[str, object] = {}
    if summary:
        body["summary"] = summary
    if start is not None and end is not None:
        body.update(_times(start, end))
    if attendees:
        body["attendees"] = [{"email": address} for address in attendees]
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    answer = await _call(
        token,
        "PATCH",
        f"{CALENDAR_URL}/{event_id}",
        params={"sendUpdates": "all"},
        body=body,
    )
    return _event(answer)


async def delete_event(token: str, event_id: str) -> None:
    await _call(token, "DELETE", f"{CALENDAR_URL}/{event_id}", params={"sendUpdates": "all"})


async def find_files(token: str, name: str, limit: int = 5) -> list[DriveFile]:
    """Drive files whose name contains the words asked for."""
    escaped = name.replace("'", "\\'")
    answer = await _call(
        token,
        "GET",
        DRIVE_URL,
        params={
            "q": f"name contains '{escaped}' and trashed = false",
            "fields": "files(id,name,mimeType,webViewLink)",
            "pageSize": str(limit),
            "orderBy": "modifiedTime desc",
        },
    )
    rows = answer.get("files", [])
    return [
        DriveFile(
            id=str(row.get("id", "")),
            name=str(row.get("name", "")),
            mime_type=str(row.get("mimeType", "")),
            link=str(row.get("webViewLink", "")),
        )
        for row in rows
        if isinstance(row, dict)
    ]


EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/pdf",
        ".pdf",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/pdf",
        ".pdf",
    ),
    "application/vnd.google-apps.presentation": (
        "application/pdf",
        ".pdf",
    ),
}


async def download(token: str, file: DriveFile) -> tuple[str, bytes]:
    """The file's bytes, Google-native documents coming back as PDF."""
    export = EXPORTS.get(file.mime_type)
    if export is None:
        url, params = f"{DRIVE_URL}/{file.id}", {"alt": "media"}
        name = file.name
    else:
        url, params = f"{DRIVE_URL}/{file.id}/export", {"mimeType": export[0]}
        name = file.name if file.name.endswith(export[1]) else file.name + export[1]
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            url, headers={"Authorization": f"Bearer {token}"}, params=params
        )
    if response.status_code >= 400:
        raise GoogleError(_complaint(response))
    if len(response.content) > BIGGEST_ATTACHMENT:
        size = len(response.content) // 1_000_000
        raise GoogleError(f"{file.name} is too big to email ({size}MB).")
    return name, response.content


async def send_email(
    token: str,
    to: list[str],
    subject: str,
    body: str,
    *,
    cc: list[str] | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
) -> str:
    """Send mail as the connected account, handing back the thread it landed in."""
    if not to:
        raise GoogleError("I need at least one address to send to.")
    message = EmailMessage()
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message.set_content(body)
    for name, content in attachments or []:
        message.add_attachment(
            content, maintype="application", subtype="octet-stream", filename=name
        )
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    answer = await _call(token, "POST", f"{GMAIL_URL}/messages/send", body={"raw": raw})
    return str(answer.get("threadId", ""))


@dataclass(frozen=True)
class Mail:
    id: str
    sender: str
    subject: str
    snippet: str


async def search_mail(token: str, query: str, limit: int = 5) -> list[Mail]:
    """Recent mail matching a Gmail search, e.g. "from:ada contract"."""
    found = await _call(
        token,
        "GET",
        f"{GMAIL_URL}/messages",
        params={"q": query, "maxResults": str(limit)},
    )
    rows = [row for row in found.get("messages", []) if isinstance(row, dict)]
    mails = []
    for row in rows:
        message = await _call(
            token,
            "GET",
            f"{GMAIL_URL}/messages/{row.get('id')}",
            params={"format": "metadata"},
        )
        headers = message.get("payload", {})
        fields = headers.get("headers", []) if isinstance(headers, dict) else []
        named = {
            str(field.get("name", "")).lower(): str(field.get("value", ""))
            for field in fields
            if isinstance(field, dict)
        }
        mails.append(
            Mail(
                id=str(message.get("id", "")),
                sender=named.get("from", ""),
                subject=named.get("subject", "(no subject)"),
                snippet=str(message.get("snippet", "")),
            )
        )
    return mails
