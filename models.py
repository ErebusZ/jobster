"""Typed domain models shared between the data layer (db.py), the job
sources (sources/), and the bot."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ScrapedJob:
    """A job as it comes out of a JobSource, before it has a DB identity."""

    source: str
    job_id: str
    title: str
    company: str
    location: str
    url: str


@dataclass(frozen=True, slots=True)
class Job:
    """A job as persisted in the database. (source, job_id) is its identity -
    the same posting on two different sources is two different Jobs."""

    source: str
    job_id: str
    title: str
    company: str
    location: str
    url: str
    first_seen_at: datetime


@dataclass(frozen=True, slots=True)
class TrackedKeyword:
    """One user's subscription to a (keyword, location) search."""

    id: int
    user_id: str
    keyword: str
    location: str
    created_at: datetime
