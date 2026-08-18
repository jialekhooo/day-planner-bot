"""SQLite persistence for plans and per-user reminder settings."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

DEFAULT_UTC_OFFSET_MINUTES = 480  # GMT+8
DEFAULT_AGENDA_AT = time(8, 0)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'you',
    done INTEGER NOT NULL DEFAULT 0,
    nudged INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_plans_user_day ON plans (user_id, day);

CREATE TABLE IF NOT EXISTS terms (
    user_id INTEGER PRIMARY KEY,
    week_one TEXT NOT NULL,
    breaks TEXT NOT NULL DEFAULT '',
    classes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS google_accounts (
    user_id INTEGER PRIMARY KEY,
    email TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL,
    access_token TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00'
);

CREATE TABLE IF NOT EXISTS google_links (
    state TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    made_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reminders (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    agenda_at TEXT NOT NULL DEFAULT '08:00',
    utc_offset_minutes INTEGER NOT NULL DEFAULT 480,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_sent_on TEXT
);
"""


@dataclass(frozen=True)
class Plan:
    id: int
    day: date
    title: str
    start: time | None
    end: time | None
    done: bool

    @property
    def timed(self) -> bool:
        return self.start is not None


@dataclass(frozen=True)
class Term:
    """The Monday teaching week one starts on, and the Mondays of holiday weeks."""

    week_one: date
    breaks: tuple[date, ...] = ()


@dataclass(frozen=True)
class GoogleAccount:
    """One person's Google grant: the lasting refresh token and the current key."""

    user_id: int
    email: str
    refresh_token: str
    access_token: str
    expires_at: datetime


@dataclass(frozen=True)
class Reminder:
    user_id: int
    chat_id: int
    agenda_at: time
    utc_offset_minutes: int
    enabled: bool
    last_sent_on: date | None


class Storage:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._add_column("plans", "source", "TEXT NOT NULL DEFAULT 'you'")
        self._add_column("terms", "classes", "TEXT NOT NULL DEFAULT ''")
        self._conn.commit()

    def _add_column(self, table: str, column: str, kind: str) -> None:
        """Bring a database made by an older version up to the schema above."""
        columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def close(self) -> None:
        self._conn.close()

    def add_plan(
        self,
        user_id: int,
        day: date,
        title: str,
        start: time | None,
        end: time | None,
        source: str = "you",
    ) -> int:
        """Store a plan and hand back its number, which counts from 1 per user."""
        ref = int(
            self._conn.execute(
                "SELECT COALESCE(MAX(ref), 0) + 1 FROM plans WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        )
        self._conn.execute(
            """
            INSERT INTO plans (ref, user_id, day, start_time, end_time, title, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref,
                user_id,
                day.isoformat(),
                None if start is None else start.isoformat(timespec="minutes"),
                None if end is None else end.isoformat(timespec="minutes"),
                title,
                source,
            ),
        )
        self._conn.commit()
        return ref

    def plans_on(self, user_id: int, day: date) -> list[Plan]:
        return self.plans_between(user_id, day, day)

    def plans_between(self, user_id: int, first: date, last: date) -> list[Plan]:
        """Plans in [first, last], timed ones first and in clock order."""
        rows = self._conn.execute(
            """
            SELECT * FROM plans
            WHERE user_id = ? AND day BETWEEN ? AND ?
            ORDER BY day ASC, start_time IS NULL, start_time ASC, ref ASC
            """,
            (user_id, first.isoformat(), last.isoformat()),
        )
        return [_to_plan(row) for row in rows]

    def open_plans(self, user_id: int) -> list[Plan]:
        """Everything not ticked off yet, oldest first."""
        rows = self._conn.execute(
            """
            SELECT * FROM plans
            WHERE user_id = ? AND done = 0
            ORDER BY day ASC, start_time IS NULL, start_time ASC, ref ASC
            """,
            (user_id,),
        )
        return [_to_plan(row) for row in rows]

    def get_plan(self, user_id: int, ref: int) -> Plan | None:
        row = self._conn.execute(
            "SELECT * FROM plans WHERE user_id = ? AND ref = ?", (user_id, ref)
        ).fetchone()
        return None if row is None else _to_plan(row)

    def find_plans(self, user_id: int, text: str) -> list[Plan]:
        """Plans whose title contains these words, so a name works instead of a number."""
        rows = self._conn.execute(
            """
            SELECT * FROM plans
            WHERE user_id = ? AND lower(title) LIKE ?
            ORDER BY day ASC, start_time IS NULL, start_time ASC, ref ASC
            """,
            (user_id, f"%{text.strip().lower()}%"),
        )
        return [_to_plan(row) for row in rows]

    def set_done(self, user_id: int, ref: int, done: bool) -> bool:
        cursor = self._conn.execute(
            "UPDATE plans SET done = ? WHERE user_id = ? AND ref = ?",
            (int(done), user_id, ref),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def move_plan(
        self,
        user_id: int,
        ref: int,
        day: date,
        start: time | None,
        end: time | None,
    ) -> bool:
        cursor = self._conn.execute(
            """
            UPDATE plans SET day = ?, start_time = ?, end_time = ?, nudged = 0
            WHERE user_id = ? AND ref = ?
            """,
            (
                day.isoformat(),
                None if start is None else start.isoformat(timespec="minutes"),
                None if end is None else end.isoformat(timespec="minutes"),
                user_id,
                ref,
            ),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_plan(self, user_id: int, ref: int) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM plans WHERE user_id = ? AND ref = ?", (user_id, ref)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_plans(self, user_id: int, day: date | None = None) -> int:
        query = "DELETE FROM plans WHERE user_id = ?"
        params: list[object] = [user_id]
        if day is not None:
            query += " AND day = ?"
            params.append(day.isoformat())
        cursor = self._conn.execute(query, params)
        self._conn.commit()
        return cursor.rowcount

    def delete_from_source(self, user_id: int, source: str, since: date | None = None) -> int:
        """Drop plans that came from one place, e.g. a timetable that has changed."""
        query = "DELETE FROM plans WHERE user_id = ? AND source = ?"
        params: list[object] = [user_id, source]
        if since is not None:
            query += " AND day >= ?"
            params.append(since.isoformat())
        cursor = self._conn.execute(query, params)
        self._conn.commit()
        return cursor.rowcount

    def get_classes(self, user_id: int) -> str:
        """The timetable last imported, as it was written down."""
        row = self._conn.execute(
            "SELECT classes FROM terms WHERE user_id = ?", (user_id,)
        ).fetchone()
        return "" if row is None else row["classes"]

    def save_classes(self, user_id: int, classes: str) -> None:
        self._conn.execute(
            "UPDATE terms SET classes = ? WHERE user_id = ?", (classes, user_id)
        )
        self._conn.commit()

    def get_term(self, user_id: int) -> Term | None:
        """When teaching week one starts, and which weeks are holidays."""
        row = self._conn.execute(
            "SELECT * FROM terms WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        breaks = tuple(date.fromisoformat(day) for day in row["breaks"].split(",") if day)
        return Term(date.fromisoformat(row["week_one"]), breaks)

    def save_term(self, user_id: int, term: Term) -> None:
        self._conn.execute(
            """
            INSERT INTO terms (user_id, week_one, breaks) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                week_one = excluded.week_one, breaks = excluded.breaks
            """,
            (
                user_id,
                term.week_one.isoformat(),
                ",".join(day.isoformat() for day in term.breaks),
            ),
        )
        self._conn.commit()

    def save_google(
        self,
        user_id: int,
        email: str,
        refresh_token: str,
        access_token: str,
        expires_at: datetime,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO google_accounts (user_id, email, refresh_token, access_token, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                email = excluded.email,
                refresh_token = excluded.refresh_token,
                access_token = excluded.access_token,
                expires_at = excluded.expires_at
            """,
            (user_id, email, refresh_token, access_token, expires_at.isoformat()),
        )
        self._conn.commit()

    def save_google_access(self, user_id: int, access_token: str, expires_at: datetime) -> None:
        """Keep the freshly refreshed key so the next request doesn't refresh again."""
        self._conn.execute(
            "UPDATE google_accounts SET access_token = ?, expires_at = ? WHERE user_id = ?",
            (access_token, expires_at.isoformat(), user_id),
        )
        self._conn.commit()

    def get_google(self, user_id: int) -> GoogleAccount | None:
        row = self._conn.execute(
            "SELECT * FROM google_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return GoogleAccount(
            user_id=row["user_id"],
            email=row["email"],
            refresh_token=row["refresh_token"],
            access_token=row["access_token"],
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )

    def forget_google(self, user_id: int) -> bool:
        cursor = self._conn.execute("DELETE FROM google_accounts WHERE user_id = ?", (user_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def start_google_link(self, state: str, user_id: int, chat_id: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO google_links (state, user_id, chat_id) VALUES (?, ?, ?)",
            (state, user_id, chat_id),
        )
        self._conn.commit()

    def take_google_link(self, state: str) -> tuple[int, int] | None:
        """Who started this sign-in, spent so the link can't be replayed."""
        row = self._conn.execute(
            "SELECT user_id, chat_id FROM google_links WHERE state = ?", (state,)
        ).fetchone()
        if row is None:
            return None
        self._conn.execute("DELETE FROM google_links WHERE state = ?", (state,))
        self._conn.commit()
        return row["user_id"], row["chat_id"]

    def get_reminder(self, user_id: int) -> Reminder | None:
        row = self._conn.execute(
            "SELECT * FROM reminders WHERE user_id = ?", (user_id,)
        ).fetchone()
        return None if row is None else _to_reminder(row)

    def save_reminder(
        self,
        user_id: int,
        chat_id: int,
        agenda_at: time,
        utc_offset_minutes: int,
        enabled: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO reminders (user_id, chat_id, agenda_at, utc_offset_minutes, enabled)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                agenda_at = excluded.agenda_at,
                utc_offset_minutes = excluded.utc_offset_minutes,
                enabled = excluded.enabled
            """,
            (
                user_id,
                chat_id,
                agenda_at.isoformat(timespec="minutes"),
                utc_offset_minutes,
                int(enabled),
            ),
        )
        self._conn.commit()

    def enabled_reminders(self) -> list[Reminder]:
        rows = self._conn.execute("SELECT * FROM reminders WHERE enabled = 1")
        return [_to_reminder(row) for row in rows]

    def mark_agenda_sent(self, user_id: int, day: date) -> None:
        self._conn.execute(
            "UPDATE reminders SET last_sent_on = ? WHERE user_id = ?",
            (day.isoformat(), user_id),
        )
        self._conn.commit()

    def mark_nudged(self, user_id: int, ref: int) -> None:
        self._conn.execute(
            "UPDATE plans SET nudged = 1 WHERE user_id = ? AND ref = ?", (user_id, ref)
        )
        self._conn.commit()

    def pending_nudges(self, user_id: int, day: date) -> list[Plan]:
        """Timed plans for the day that haven't had their heads-up yet."""
        rows = self._conn.execute(
            """
            SELECT * FROM plans
            WHERE user_id = ? AND day = ? AND start_time IS NOT NULL
                  AND done = 0 AND nudged = 0
            ORDER BY start_time ASC
            """,
            (user_id, day.isoformat()),
        )
        return [_to_plan(row) for row in rows]


def _to_plan(row: sqlite3.Row) -> Plan:
    return Plan(
        id=row["ref"],
        day=date.fromisoformat(row["day"]),
        title=row["title"],
        start=time.fromisoformat(row["start_time"]) if row["start_time"] else None,
        end=time.fromisoformat(row["end_time"]) if row["end_time"] else None,
        done=bool(row["done"]),
    )


def _to_reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(
        user_id=row["user_id"],
        chat_id=row["chat_id"],
        agenda_at=time.fromisoformat(row["agenda_at"]),
        utc_offset_minutes=row["utc_offset_minutes"],
        enabled=bool(row["enabled"]),
        last_sent_on=date.fromisoformat(row["last_sent_on"]) if row["last_sent_on"] else None,
    )
