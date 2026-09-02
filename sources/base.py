"""The interface every job source implements. Adding a new job board means
writing one class here, not touching the bot or the scheduling logic."""

from abc import ABC, abstractmethod

from models import ScrapedJob


class JobSource(ABC):
    #: short, stable identifier stored in the DB (e.g. "linkedin") - never
    #: rename this on an existing source, it'd desync dedup for old jobs.
    name: str

    @abstractmethod
    async def fetch(self, keyword: str, location: str) -> list[ScrapedJob]:
        """Return jobs posted in roughly the last 24h matching keyword/location.

        Must not raise on network/parsing failures - catch internally, log,
        and return [] so one broken source doesn't take down the others.
        """
