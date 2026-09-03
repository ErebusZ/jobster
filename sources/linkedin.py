import aiohttp
from bs4 import BeautifulSoup

from models import ScrapedJob
from base import JobSource


class LinkedInSource(JobSource):
    name = "linkedin"

    def __init__(self, proxy_url: str | None = None):
        self._proxy_url = proxy_url

    async def fetch(self, keyword: str, location: str) -> list[ScrapedJob]:
        url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        params = {
            "keywords": keyword,
            "location": location,
            "f_TPR": "r86400",  # posted in the last 24 hours
            "sortBy": "DD",      # newest first
            "start": 0,
        }
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        jobs: list[ScrapedJob] = []
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    url, params=params, headers=headers, proxy=self._proxy_url, timeout=10
                ) as resp:
                    if resp.status != 200:
                        print(f"[linkedin] HTTP {resp.status} for '{keyword}' in '{location}'")
                        return []

                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")

                    for card in soup.find_all("li"):
                        link_elem = card.find("a", class_="base-card__full-link")
                        title_elem = card.find("h3", class_="base-search-card__title")
                        company_elem = card.find("h4", class_="base-search-card__subtitle")
                        loc_elem = card.find("span", class_="job-search-card__location")

                        if not link_elem or not title_elem:
                            continue

                        job_url = link_elem["href"].split("?")[0]
                        job_id = job_url.split("-")[-1]

                        jobs.append(
                            ScrapedJob(
                                source=self.name,
                                job_id=job_id,
                                title=title_elem.text.strip(),
                                company=company_elem.text.strip() if company_elem else "Unknown",
                                location=loc_elem.text.strip() if loc_elem else location,
                                url=job_url,
                            )
                        )
            except Exception as e:  # noqa: BLE001 - one bad response shouldn't kill the search cycle
                print(f"[linkedin] Scraping error for '{keyword}' in '{location}': {e}")

        return jobs
