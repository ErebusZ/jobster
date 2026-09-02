"""Registry of available job sources.

To add a new board: write a JobSource subclass in this package, then add it
to build_sources() below. Nothing else in the codebase needs to change.
"""

from .base import JobSource
from .indeed import IndeedSource
from .linkedin import LinkedInSource

__all__ = ["IndeedSource", "JobSource", "LinkedInSource", "build_sources"]


def build_sources(proxy_url: str | None = None) -> list[JobSource]:
    return [
        LinkedInSource(proxy_url=proxy_url),
        IndeedSource(proxy_url=proxy_url),
    ]
