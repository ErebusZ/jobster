import asyncio

import tls_client
from bs4 import BeautifulSoup
from models import ScrapedJob

from .base import JobSource

_HEADERS = {
    # Truncated UAs (missing the "Chrome/... Safari/..." suffix) are a
    # cheap fingerprinting tell - use a complete, real one.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


class IndeedSource(JobSource):
    """Scrapes Indeed's public job search.

    Indeed sits behind Cloudflare bot management, which turned out to be
    *probabilistic*, not a flat wall - the exact same request sometimes
    gets a 200 with real results and sometimes a 403, even back to back.
    Plain aiohttp got blocked consistently; a TLS-fingerprint-spoofing
    client (tls_client, impersonating real Chrome at the handshake level -
    the same technique the JobSpy library uses) gets through some of the
    time. Two things make that usable instead of just "better odds":

    1. One persistent tls_client.Session reused across calls, so Cloudflare
       sees a returning session (with its cookies) rather than a fresh
       anonymous client on every request - closer to real browsing behavior.
    2. A few retries on 403 within a single fetch, since the block isn't
       sticky - the next attempt seconds later often succeeds.
    """

    name = "indeed"

    def __init__(self, proxy_url: str | None = None, max_retries: int = 3):
        self._proxy_url = proxy_url
        self._max_retries = max_retries
        self._session = tls_client.Session(
            client_identifier="chrome_120", random_tls_extension_order=True
        )

    def _get(self, keyword: str, location: str):
        return self._session.get(
            "https://www.indeed.com/jobs",
            params={"q": keyword, "l": location, "fromage": "1", "sort": "date"},
            headers=_HEADERS,
            proxy=self._proxy_url,
        )

    async def fetch(self, keyword: str, location: str) -> list[ScrapedJob]:
        resp = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await asyncio.to_thread(self._get, keyword, location)
            except Exception as e:  # noqa: BLE001 - one bad attempt shouldn't kill the search cycle
                print(f"[indeed] request error (attempt {attempt}) for '{keyword}' in '{location}': {e}")
                resp = None
                continue

            if resp.status_code == 200:
                break

            print(f"[indeed] HTTP {resp.status_code} (attempt {attempt}/{self._max_retries}) for '{keyword}' in '{location}'")
            if attempt < self._max_retries:
                await asyncio.sleep(2)

        if resp is None or resp.status_code != 200:
            return []

        jobs: list[ScrapedJob] = []
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.job_seen_beacon") or soup.select("td.resultContent")

        for card in cards:
            job_id = card.get("data-jk")
            title_elem = card.select_one("h2.jobTitle span") or card.select_one("h2.jobTitle")
            company_elem = card.select_one('[data-testid="company-name"]')
            loc_elem = card.select_one('[data-testid="text-location"]')
            link_elem = card.select_one("a")

            if not job_id and link_elem:
                href = link_elem.get("href", "")
                job_id = href.split("jk=")[-1].split("&")[0] if "jk=" in href else None

            if not job_id or not title_elem:
                continue

            jobs.append(
                ScrapedJob(
                    source=self.name,
                    job_id=job_id,
                    title=title_elem.text.strip(),
                    company=company_elem.text.strip() if company_elem else "Unknown",
                    location=loc_elem.text.strip() if loc_elem else location,
                    url=f"https://www.indeed.com/viewjob?jk={job_id}",
                )
            )

        return jobs
