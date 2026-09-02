import asyncio
import os
import random
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from db import Database
from models import Job
from sources import build_sources

load_dotenv()

# --- CONFIGURATION ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
GUILD_ID = os.getenv("GUILD_ID")  # optional - if set, slash commands sync instantly to this server
DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "Morocco")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))
MAX_KEYWORDS_PER_USER = int(os.getenv("MAX_KEYWORDS_PER_USER", "5"))
CHECK_COOLDOWN_SECONDS = int(os.getenv("CHECK_COOLDOWN_SECONDS", "120"))

# Optional proxy setup (format: "http://user:pass@host:port")
PROXY_URL = os.getenv("PROXY_URL") or None

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is not set. Copy .env.example to .env and fill it in.")

# --- DATABASE + SOURCES SETUP ---
DB_PATH = os.getenv("DB_PATH", "job_alerts.db")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
database = Database(DB_PATH)
sources = build_sources(proxy_url=PROXY_URL)

# --- BOT SETUP ---
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


async def run_search_cycle() -> list[tuple[Job, set[str]]]:
    """Runs every distinct (keyword, location) search - deduped across all
    users and sources - and returns newly-seen jobs paired with the set of
    user IDs whose subscription matched them (merged across every keyword
    that turned the job up, even within the same cycle)."""
    targets = database.get_distinct_search_targets()
    if not targets:
        print("No tracked keywords yet - nothing to search.")
        return []

    # (source_name, job_id) -> (ScrapedJob, {user_id, ...})
    found: dict[tuple[str, str], tuple] = {}

    for source in sources:
        for i, (keyword, location) in enumerate(targets):
            if i > 0:
                # Space individual requests out within the cycle instead of
                # firing them back-to-back - a burst of N rapid identical
                # requests is a much stronger bot signal than the same
                # volume spread over a few seconds.
                await asyncio.sleep(random.uniform(2, 5))

            jobs = await source.fetch(keyword, location)
            matched_users = set(database.get_users_tracking(keyword, location))

            for job in jobs:
                key = (job.source, job.job_id)
                if key in found:
                    found[key][1].update(matched_users)
                else:
                    found[key] = (job, set(matched_users))

    new_jobs: list[tuple[Job, set[str]]] = []
    for (source_name, job_id), (scraped, user_ids) in found.items():
        if database.is_job_known(source_name, job_id):
            continue
        persisted = database.upsert_job(scraped)
        new_jobs.append((persisted, user_ids))

    return new_jobs


_last_check_at: datetime | None = None


async def run_job_check():
    global _last_check_at
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"Error: Channel ID {CHANNEL_ID} not found.")
        return

    _last_check_at = datetime.now(UTC)
    print("Running search cycle across all sources...")
    new_jobs = await run_search_cycle()
    print(f"Found {len(new_jobs)} new unposted job(s).")

    for job, user_ids in reversed(new_jobs):  # post oldest first
        embed = discord.Embed(
            title=job.title,
            url=job.url,
            description=f"**Company:** {job.company}\n**Location:** {job.location}",
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"via {job.source}")

        mentions = " ".join(f"<@{uid}>" for uid in user_ids)
        await channel.send(content=mentions or None, embed=embed)


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def scheduled_job_check():
    # Small random jitter so requests aren't perfectly periodic
    await asyncio.sleep(random.uniform(0, 30))
    await run_job_check()


# --- KEYWORD TRACKING COMMANDS ---

@bot.tree.command(name="check", description="Manually trigger a job search across all sources")
async def check_cmd(interaction: discord.Interaction):
    if _last_check_at is not None:
        elapsed = (datetime.now(UTC) - _last_check_at).total_seconds()
        if elapsed < CHECK_COOLDOWN_SECONDS:
            wait_left = round(CHECK_COOLDOWN_SECONDS - elapsed)
            await interaction.response.send_message(
                f"A check just ran — try again in {wait_left}s. "
                "(Keeps us from hammering job sites if a few people mash /check.)",
                ephemeral=True,
            )
            return

    await interaction.response.defer(thinking=True)
    await run_job_check()
    await interaction.followup.send("✅ Check completed!")


@bot.tree.command(name="track", description="Get notified when new jobs match a keyword")
@app_commands.describe(
    keyword="e.g. 'python', 'backend', 'data engineer'",
    location=f"Defaults to {DEFAULT_LOCATION!r} if left blank",
)
async def track_cmd(interaction: discord.Interaction, keyword: str, location: str | None = None):
    database.upsert_user(interaction.user.id, interaction.user.display_name)

    existing = database.get_user_keywords(interaction.user.id)
    if len(existing) >= MAX_KEYWORDS_PER_USER:
        await interaction.response.send_message(
            f"You're already tracking {MAX_KEYWORDS_PER_USER} keywords (the max per person — "
            "keeps total search volume in check for everyone). Use `/untrack` to free one up.",
            ephemeral=True,
        )
        return

    location = location or DEFAULT_LOCATION
    added = database.add_tracked_keyword(interaction.user.id, keyword, location)

    if added:
        await interaction.response.send_message(
            f"🔔 Now tracking **{keyword}** in **{location}** — you'll be pinged here when a new match shows up.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"You're already tracking **{keyword}** in **{location}**.", ephemeral=True
        )


@bot.tree.command(name="untrack", description="Stop tracking a keyword")
@app_commands.describe(keyword="Pick one of your tracked keywords")
async def untrack_cmd(interaction: discord.Interaction, keyword: str):
    if not keyword.isdigit():
        await interaction.response.send_message(
            "Pick a keyword from the autocomplete list rather than typing it out.", ephemeral=True
        )
        return

    removed = database.remove_tracked_keyword(interaction.user.id, int(keyword))
    if removed:
        await interaction.response.send_message("🔕 Stopped tracking that keyword.", ephemeral=True)
    else:
        await interaction.response.send_message("Couldn't find that in your tracked list.", ephemeral=True)


@untrack_cmd.autocomplete("keyword")
async def untrack_autocomplete(interaction: discord.Interaction, current: str):
    current = current.lower()
    rows = database.get_user_keywords(interaction.user.id)
    return [
        app_commands.Choice(name=f"{r.keyword} ({r.location})", value=str(r.id))
        for r in rows
        if current in r.keyword.lower()
    ][:25]


@bot.tree.command(name="keywords", description="List the keywords you're tracking")
async def keywords_cmd(interaction: discord.Interaction):
    rows = database.get_user_keywords(interaction.user.id)
    if not rows:
        await interaction.response.send_message(
            "You're not tracking any keywords yet — try `/track`.", ephemeral=True
        )
        return

    lines = "\n".join(f"• **{r.keyword}** in **{r.location}**" for r in rows)
    embed = discord.Embed(title="Your tracked keywords", description=lines, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
