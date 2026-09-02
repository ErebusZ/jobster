import aiohttp
from bs4 import BeautifulSoup

from models import ScrapedJob

from .base import JobSource


class IndeedSource(JobSource):
    """Scrapes Indeed's public job search.

    Heads up: Indeed is far more aggressively anti-bot-protected than
    LinkedIn's guest endpoint (Cloudflare, frequent markup changes, quick to
    block datacenter/proxy IPs). The selectors below matched Indeed's
    markup as of writing, but this is the source most likely to need
    re-tuning after a live test - if fetch() starts returning [] on every
    call, check for a CAPTCHA/challenge page in the raw response before
    assuming the parser is wrong.
    """

    name = "indeed"

    def __init__(self, proxy_url: str | None = None):
        self._proxy_url = proxy_url

    async def fetch(self, keyword: str, location: str) -> list[ScrapedJob]:
        url = "https://www.indeed.com/jobs"
        params = {
            "q": keyword,
            "l": location,
            "fromage": "1",  # posted in the last 1 day
            "sort": "date",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }

        jobs: list[ScrapedJob] = []
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    url, params=params, headers=headers, proxy=self._proxy_url, timeout=10
                ) as resp:
                    if resp.status != 200:
                        print(f"[indeed] HTTP {resp.status} for '{keyword}' in '{location}'")
                        return []

                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
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
            except Exception as e:  # noqa: BLE001 - one bad response shouldn't kill the search cycle
                print(f"[indeed] Scraping error for '{keyword}' in '{location}': {e}")

        return jobs
