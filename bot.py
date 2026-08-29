import asyncio
import os
import random
import sqlite3

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURATION ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
KEYWORDS = os.getenv("KEYWORDS", "Python")
LOCATION = os.getenv("LOCATION", "Morocco")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))

# Optional proxy setup (format: "http://user:pass@host:port")
PROXY_URL = os.getenv("PROXY_URL") or None

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is not set. Copy .env.example to .env and fill it in.")

# --- DATABASE SETUP ---
DB_PATH = os.getenv("DB_PATH", "job_alerts.db")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS seen_jobs (job_id TEXT PRIMARY KEY)")
conn.commit()

# --- BOT SETUP ---
intents = discord.Intents.default()
intents.message_content = True  # Required to read !check command
bot = commands.Bot(command_prefix="!", intents=intents)

async def fetch_new_jobs():
    url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    params = {
        "keywords": KEYWORDS,
        "location": LOCATION,
        "f_TPR": "r86400",  # Posted in last 24 hours
        "sortBy": "DD",      # Newest first
        "start": 0
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    new_listings = []

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                url, params=params, headers=headers, proxy=PROXY_URL, timeout=10
            ) as resp:
                if resp.status != 200:
                    print(f"HTTP Error: {resp.status}")
                    return []

                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                job_cards = soup.find_all("li")

                for card in job_cards:
                    link_elem = card.find("a", class_="base-card__full-link")
                    title_elem = card.find("h3", class_="base-search-card__title")
                    company_elem = card.find("h4", class_="base-search-card__subtitle")
                    loc_elem = card.find("span", class_="job-search-card__location")

                    if not link_elem or not title_elem:
                        continue

                    job_url = link_elem["href"].split("?")[0]
                    job_id = job_url.split("-")[-1]

                    cursor.execute("SELECT job_id FROM seen_jobs WHERE job_id = ?", (job_id,))
                    if not cursor.fetchone():
                        cursor.execute("INSERT INTO seen_jobs VALUES (?)", (job_id,))
                        conn.commit()

                        new_listings.append({
                            "id": job_id,
                            "title": title_elem.text.strip(),
                            "company": company_elem.text.strip() if company_elem else "Unknown",
                            "location": loc_elem.text.strip() if loc_elem else LOCATION,
                            "url": job_url
                        })
        except Exception as e:  # noqa: BLE001 - keep the check loop alive on any
            # unexpected network/parsing failure (LinkedIn markup or connectivity
            # issues shouldn't crash the scheduled task), just log and retry next cycle
            print(f"Scraping error: {e}")

    return new_listings

async def run_job_check():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"Error: Channel ID {CHANNEL_ID} not found.")
        return

    print("Fetching jobs from LinkedIn...")
    new_jobs = await fetch_new_jobs()
    print(f"Found {len(new_jobs)} new unposted job(s).")

    for job in reversed(new_jobs):  # Post oldest first
        embed = discord.Embed(
            title=job["title"],
            url=job["url"],
            description=f"**Company:** {job['company']}\n**Location:** {job['location']}",
            color=discord.Color.blue()
        )
        embed.set_footer(text="LinkedIn Job Monitor")
        await channel.send(embed=embed)

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def scheduled_job_check():
    # Small random jitter so requests aren't perfectly periodic
    await asyncio.sleep(random.uniform(0, 30))
    await run_job_check()

@bot.command(name="check")
async def manual_check(ctx):
    """Trigger check on demand by typing !check in Discord"""
    await ctx.send("🔎 Checking LinkedIn for new job listings...")
    await run_job_check()
    await ctx.send("✅ Check completed!")

@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user} (ID: {bot.user.id})")
    if not scheduled_job_check.is_running():
        scheduled_job_check.start()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
