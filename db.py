"""SQLite-backed repository for jobster.

Database wraps a single sqlite3 connection and is the only place in the
codebase that knows SQL exists. Every public method takes and returns typed
models from models.py - callers (bot.py) never see a raw sqlite3.Row or
write a query themselves.

Three tables:
  jobs               - every job any source has returned (shared, deduped
                        per (source, job_id) so the same posting on two
                        sources is tracked separately)
  users              - a user is created lazily the first time they
                        interact with the bot; their Discord ID is the only
                        identity we need
  tracked_keywords   - one row per (user, keyword, location) subscription
"""

import sqlite3
from datetime import UTC, datetime

from models import Job, ScrapedJob, TrackedKeyword

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    source TEXT NOT NULL,
    job_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (source, job_id)
);

CREATE TABLE IF NOT EXISTS users (
    discord_id TEXT PRIMARY KEY,
    username TEXT,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracked_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(discord_id),
    keyword TEXT NOT NULL,
    location TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, keyword, location)
);

CREATE INDEX IF NOT EXISTS idx_tracked_keywords_user ON tracked_keywords(user_id);
CREATE INDEX IF NOT EXISTS idx_tracked_keywords_kw_loc ON tracked_keywords(keyword, location);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        source=row["source"],
        job_id=row["job_id"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        url=row["url"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
    )


def _row_to_keyword(row: sqlite3.Row) -> TrackedKeyword:
    return TrackedKeyword(
        id=row["id"],
        user_id=row["user_id"],
        keyword=row["keyword"],
        location=row["location"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- jobs -----------------------------------------------------------

    def is_job_known(self, source: str, job_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE source = ? AND job_id = ?", (source, job_id)
        ).fetchone()
        return row is not None

    def upsert_job(self, job: ScrapedJob) -> Job:
        self._conn.execute(
            """
            INSERT INTO jobs (source, job_id, title, company, location, url, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, job_id) DO NOTHING
            """,
            (job.source, job.job_id, job.title, job.company, job.location, job.url, _now()),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE source = ? AND job_id = ?", (job.source, job.job_id)
        ).fetchone()
        return _row_to_job(row)

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

    # --- tracked keywords ---------------------------------------------------

    def add_tracked_keyword(self, user_id: int | str, keyword: str, location: str) -> bool:
        """Returns True if newly added, False if this user already tracked it."""
        keyword, location = keyword.strip().lower(), location.strip().lower()
        try:
            self._conn.execute(
                """
                INSERT INTO tracked_keywords (user_id, keyword, location, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(user_id), keyword, location, _now()),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # UNIQUE(user_id, keyword, location) already exists

    def remove_tracked_keyword(self, user_id: int | str, keyword_id: int) -> bool:
        """Returns True if a row was deleted. Scoped to user_id so you can
        only ever remove your own subscriptions."""
        cur = self._conn.execute(
            "DELETE FROM tracked_keywords WHERE id = ? AND user_id = ?", (keyword_id, str(user_id))
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_user_keywords(self, user_id: int | str) -> list[TrackedKeyword]:
        rows = self._conn.execute(
            "SELECT * FROM tracked_keywords WHERE user_id = ? ORDER BY created_at", (str(user_id),)
        ).fetchall()
        return [_row_to_keyword(r) for r in rows]

    def get_distinct_search_targets(self) -> list[tuple[str, str]]:
        """Every unique (keyword, location) pair across all users - what
        actually gets searched. Two users tracking the same thing means one
        search, not two."""
        rows = self._conn.execute("SELECT DISTINCT keyword, location FROM tracked_keywords").fetchall()
        return [(r["keyword"], r["location"]) for r in rows]

    def get_users_tracking(self, keyword: str, location: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT user_id FROM tracked_keywords WHERE keyword = ? AND location = ?",
            (keyword.strip().lower(), location.strip().lower()),
        ).fetchall()
        return [r["user_id"] for r in rows]
