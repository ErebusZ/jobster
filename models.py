"""Typed domain models shared between the data layer (db.py) and the bot."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Status(StrEnum):
    QUEUED = "queued"
    APPLIED = "applied"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"


STATUS_EMOJI: dict[Status, str] = {
    Status.QUEUED: "👀",
    Status.APPLIED: "✅",
    Status.INTERVIEW: "📞",
    Status.OFFER: "🎉",
    Status.REJECTED: "❌",
}
EMOJI_TO_STATUS: dict[str, Status] = {emoji: status for status, emoji in STATUS_EMOJI.items()}

# Words a user might use when replying in-thread to a job post - first match wins.
REPLY_ALIASES: dict[Status, list[str]] = {
    Status.QUEUED: ["queue", "queued", "watching", "save", "saved"],
    Status.APPLIED: ["applied", "apply", "applying"],
    Status.INTERVIEW: ["interview", "interviewing", "call", "calls", "screen"],
    Status.OFFER: ["offer", "offered"],
    Status.REJECTED: ["reject", "rejected", "declined", "ghosted"],
}


@dataclass(frozen=True, slots=True)
class ScrapedJob:
    """A job as it comes out of the LinkedIn scraper, before it has a DB identity."""

    job_id: str
    title: str
    company: str
    location: str
    url: str


@dataclass(frozen=True, slots=True)
class Job:
    """A job as persisted in the database - has a short `ref` for easy lookup."""

    ref: int
    job_id: str
    title: str
    company: str
    location: str
    url: str
    message_id: str | None
    first_seen_at: datetime


@dataclass(frozen=True, slots=True)
class TrackedJob:
    """A job joined with one user's application status - what /myjobs and
    /status's autocomplete work with."""

    ref: int
    job_id: str
    title: str
    company: str
    location: str
    url: str
    status: Status
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UserStats:
    counts: dict[Status, int]
    total: int
    response_rate: float
