"""SQLite-backed repository for jobster.

Database wraps a single sqlite3 connection and is the only place in the
codebase that knows SQL exists. Every public method takes and returns typed
models from models.py - callers (bot.py) never see a raw sqlite3.Row or
write a query themselves.

Three tables:
  jobs          - every job the scraper has posted (shared across all users)
  users         - a user is created lazily the first time they interact with
                  the bot; their Discord ID is the only identity we need
  applications  - one row per (user, job): the user's tracking status for it
"""

import sqlite3
from datetime import UTC, datetime

from models import Job, ScrapedJob, Status, TrackedJob, UserStats

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    url TEXT NOT NULL,
    message_id TEXT,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    discord_id TEXT PRIMARY KEY,
    username TEXT,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(discord_id),
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_applications_user ON applications(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_message ON jobs(message_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        ref=row["ref"],
        job_id=row["job_id"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        url=row["url"],
        message_id=row["message_id"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
    )


def _row_to_tracked_job(row: sqlite3.Row) -> TrackedJob:
    return TrackedJob(
        ref=row["ref"],
        job_id=row["job_id"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        url=row["url"],
        status=Status(row["status"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- jobs -----------------------------------------------------------

    def is_job_known(self, job_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return row is not None

    def upsert_job(self, job: ScrapedJob, message_id: str | None = None) -> Job:
        """Inserts a new job (or, if it already exists, just updates its
        message_id). Returns the persisted Job, including its short `ref`."""
        self._conn.execute(
            """
            INSERT INTO jobs (job_id, title, company, location, url, message_id, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET message_id = excluded.message_id
            """,
            (job.job_id, job.title, job.company, job.location, job.url, message_id, _now()),
        )
        self._conn.commit()
        return self.get_job_by_id(job.job_id)

    def attach_message(self, job_id: str, message_id: str) -> None:
        self._conn.execute("UPDATE jobs SET message_id = ? WHERE job_id = ?", (message_id, job_id))
        self._conn.commit()

    def get_job_by_message_id(self, message_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT rowid AS ref, * FROM jobs WHERE message_id = ?", (str(message_id),)
        ).fetchone()
        return _row_to_job(row) if row else None

    def get_job_by_id(self, job_id: str) -> Job | None:
        row = self._conn.execute("SELECT rowid AS ref, * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def get_job_by_ref(self, ref: int) -> Job | None:
        row = self._conn.execute("SELECT rowid AS ref, * FROM jobs WHERE rowid = ?", (ref,)).fetchone()
        return _row_to_job(row) if row else None

    def autocomplete_jobs(self, user_id: int | str, query: str, limit: int = 25) -> list[TrackedJob | Job]:
        """Jobs to suggest for /status's autocomplete.

        Empty query: the user's own tracked jobs, most recently updated first -
        this is what makes referencing "that job from last week" fast, no
        scrolling the backlog. Non-empty query: also matches a typed #ref number.
        """
        query = query.strip().lstrip("#")

        if not query:
            rows = self._conn.execute(
                """
                SELECT j.rowid AS ref, j.*, a.status, a.updated_at
                FROM applications a JOIN jobs j ON j.job_id = a.job_id
                WHERE a.user_id = ?
                ORDER BY a.updated_at DESC
                LIMIT ?
                """,
                (str(user_id), limit),
            ).fetchall()
            if rows:
                return [_row_to_tracked_job(r) for r in rows]
            # user isn't tracking anything yet - fall back to the most recent postings
            rows = self._conn.execute(
                "SELECT rowid AS ref, * FROM jobs ORDER BY first_seen_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [_row_to_job(r) for r in rows]

        like = f"%{query}%"
        rows = self._conn.execute(
            """
            SELECT rowid AS ref, * FROM jobs
            WHERE CAST(rowid AS TEXT) = ? OR title LIKE ? OR company LIKE ?
            ORDER BY first_seen_at DESC
            LIMIT ?
            """,
            (query, like, like, limit),
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    # --- users ------------------------------------------------------------

    def upsert_user(self, discord_id: int | str, username: str) -> None:
        self._conn.execute(
            """
            INSERT INTO users (discord_id, username, first_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET username = excluded.username
            """,
            (str(discord_id), username, _now()),
        )
        self._conn.commit()

    # --- applications -----------------------------------------------------

    def set_application_status(self, user_id: int | str, job_id: str, status: Status) -> Status | None:
        """Upserts the (user, job) status. Returns the previous status, or None if this is new."""
        row = self._conn.execute(
            "SELECT status FROM applications WHERE user_id = ? AND job_id = ?",
            (str(user_id), job_id),
        ).fetchone()
        previous = Status(row["status"]) if row else None

        now = _now()
        self._conn.execute(
            """
            INSERT INTO applications (user_id, job_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, job_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
            """,
            (str(user_id), job_id, status.value, now, now),
        )
        self._conn.commit()
        return previous

    def get_user_applications(self, user_id: int | str, status: Status | None = None) -> list[TrackedJob]:
        query = """
            SELECT a.status, a.updated_at, j.rowid AS ref, j.job_id, j.title, j.company, j.location, j.url
            FROM applications a JOIN jobs j ON j.job_id = a.job_id
            WHERE a.user_id = ?
        """
        params: list = [str(user_id)]
        if status:
            query += " AND a.status = ?"
            params.append(status.value)
        query += " ORDER BY a.updated_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_tracked_job(r) for r in rows]

    def get_user_stats(self, user_id: int | str) -> UserStats:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) as n FROM applications WHERE user_id = ? GROUP BY status",
            (str(user_id),),
        ).fetchall()
        counts = dict.fromkeys(Status, 0)
        for row in rows:
            counts[Status(row["status"])] = row["n"]

        total = sum(counts.values())
        applied_or_further = (
            counts[Status.APPLIED] + counts[Status.INTERVIEW] + counts[Status.OFFER] + counts[Status.REJECTED]
        )
        responses = counts[Status.INTERVIEW] + counts[Status.OFFER] + counts[Status.REJECTED]
        response_rate = round(responses / applied_or_further * 100, 1) if applied_or_further else 0.0

        return UserStats(counts=counts, total=total, response_rate=response_rate)
