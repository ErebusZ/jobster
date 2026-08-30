import asyncio
import contextlib
import os
import random

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from db import Database
from models import EMOJI_TO_STATUS, REPLY_ALIASES, STATUS_EMOJI, Job, ScrapedJob, Status

load_dotenv()

# --- CONFIGURATION ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
GUILD_ID = os.getenv("GUILD_ID")  # optional - if set, slash commands sync instantly to this server
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
database = Database(DB_PATH)

# --- BOT SETUP ---
intents = discord.Intents.default()
intents.message_content = True  # needed to parse "applied" etc. in replies to job posts
bot = commands.Bot(command_prefix="!", intents=intents)


async def fetch_new_jobs() -> list[Job]:
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

    new_listings: list[Job] = []

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

                    if database.is_job_known(job_id):
                        continue

                    scraped = ScrapedJob(
                        job_id=job_id,
                        title=title_elem.text.strip(),
                        company=company_elem.text.strip() if company_elem else "Unknown",
                        location=loc_elem.text.strip() if loc_elem else LOCATION,
                        url=job_url,
                    )
                    # Reserve it immediately (message_id=None) so an overlapping
                    # manual /check / scheduled run can't post it twice.
                    new_listings.append(database.upsert_job(scraped))
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
            title=f"#{job.ref} · {job.title}",
            url=job.url,
            description=f"**Company:** {job.company}\n**Location:** {job.location}",
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"React to track it, or /status job:{job.ref} later")
        message = await channel.send(embed=embed)

        database.attach_message(job.job_id, str(message.id))
        for status in Status:
            try:
                await message.add_reaction(STATUS_EMOJI[status])
            except discord.HTTPException as e:
                print(f"Could not add reaction for {status}: {e}")


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def scheduled_job_check():
    # Small random jitter so requests aren't perfectly periodic
    await asyncio.sleep(random.uniform(0, 30))
    await run_job_check()


# --- APPLICATION TRACKING ---

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return  # ignore the bot's own reactions (the ones it pre-attaches)

    status = EMOJI_TO_STATUS.get(str(payload.emoji))
    if not status:
        return  # not one of our tracked-status emoji

    job = database.get_job_by_message_id(payload.message_id)
    if not job:
        return  # reaction on some unrelated message

    member = payload.member
    username = member.display_name if member else str(payload.user_id)
    database.upsert_user(payload.user_id, username)
    database.set_application_status(payload.user_id, job.job_id, status)

    # Tidy up: remove this user's other tracked-status reactions on the same
    # message so only their current status is shown. Needs "Manage Messages".
    try:
        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        for other_status, emoji in STATUS_EMOJI.items():
            if other_status != status:
                await message.remove_reaction(emoji, member or discord.Object(id=payload.user_id))
    except discord.HTTPException:
        pass  # missing permission or reaction already gone - not critical


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.reference and message.reference.message_id:
        job = database.get_job_by_message_id(message.reference.message_id)
        if job:
            content = message.content.lower()
            matched_status = next(
                (status for status, words in REPLY_ALIASES.items() if any(w in content for w in words)),
                None,
            )
            if matched_status:
                database.upsert_user(message.author.id, message.author.display_name)
                database.set_application_status(message.author.id, job.job_id, matched_status)
                with contextlib.suppress(discord.HTTPException):
                    await message.add_reaction(STATUS_EMOJI[matched_status])

    await bot.process_commands(message)


@bot.tree.command(name="check", description="Manually trigger a LinkedIn job check")
async def check_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    await run_job_check()
    await interaction.followup.send("✅ Check completed!")


@bot.tree.command(name="status", description="Set your status for a tracked job")
@app_commands.describe(job="Pick from the list, or type its #ref number directly", stage="New status")
@app_commands.choices(
    stage=[app_commands.Choice(name=s.value.capitalize(), value=s.value) for s in Status]
)
async def status_cmd(interaction: discord.Interaction, job: str, stage: app_commands.Choice[str]):
    job = job.strip().lstrip("#")
    job_row = database.get_job_by_ref(int(job)) if job.isdigit() else database.get_job_by_id(job)
    if not job_row:
        await interaction.response.send_message(
            "Couldn't find that job — pick one from the autocomplete list, or use its #ref number "
            "from the original post.",
            ephemeral=True,
        )
        return

    new_status = Status(stage.value)
    database.upsert_user(interaction.user.id, interaction.user.display_name)
    database.set_application_status(interaction.user.id, job_row.job_id, new_status)
    await interaction.response.send_message(
        f"{STATUS_EMOJI[new_status]} **#{job_row.ref} {job_row.title}** "
        f"@ {job_row.company} → {stage.name}",
        ephemeral=True,
    )


@status_cmd.autocomplete("job")
async def job_autocomplete(interaction: discord.Interaction, current: str):
    rows = database.autocomplete_jobs(interaction.user.id, current)
    return [
        app_commands.Choice(name=f"#{r.ref} {r.title} @ {r.company}"[:100], value=str(r.ref))
        for r in rows
    ]


@bot.tree.command(name="myjobs", description="List the jobs you're tracking (only visible to you)")
@app_commands.describe(stage="Filter by a specific status (optional)")
@app_commands.choices(
    stage=[app_commands.Choice(name=s.value.capitalize(), value=s.value) for s in Status]
)
async def myjobs_cmd(interaction: discord.Interaction, stage: app_commands.Choice[str] = None):
    database.upsert_user(interaction.user.id, interaction.user.display_name)
    rows = database.get_user_applications(interaction.user.id, Status(stage.value) if stage else None)

    if not rows:
        await interaction.response.send_message("You're not tracking any jobs yet.", ephemeral=True)
        return

    embed = discord.Embed(title=f"{interaction.user.display_name}'s tracked jobs", color=discord.Color.blue())
    for r in rows[:25]:
        embed.add_field(
            name=f"{STATUS_EMOJI[r.status]} #{r.ref} {r.title}",
            value=f"{r.company} — [link]({r.url})",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="See your job search stats")
@app_commands.describe(public="Post this in the channel for everyone to see (default: only you)")
async def stats_cmd(interaction: discord.Interaction, public: bool = False):
    database.upsert_user(interaction.user.id, interaction.user.display_name)
    stats = database.get_user_stats(interaction.user.id)

    embed = discord.Embed(
        title=f"\U0001F4CA {interaction.user.display_name}'s Job Search Stats",
        color=discord.Color.green(),
    )
    for s in Status:
        embed.add_field(name=f"{STATUS_EMOJI[s]} {s.value.capitalize()}", value=str(stats.counts[s]), inline=True)
    embed.add_field(name="Total tracked", value=str(stats.total), inline=True)
    embed.add_field(name="Response rate", value=f"{stats.response_rate}%", inline=True)
    embed.set_footer(text="Response rate = (interview + offer + rejected) / applied")

    await interaction.response.send_message(embed=embed, ephemeral=not public)


@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user} (ID: {bot.user.id})")

    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Slash commands synced instantly to guild {GUILD_ID}")
    else:
        await bot.tree.sync()
        print("Slash commands synced globally (can take up to ~1h to propagate)")

    if not scheduled_job_check.is_running():
        scheduled_job_check.start()


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
