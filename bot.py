import discord
from discord.ext import commands, tasks
from discord import Embed, app_commands
import aiohttp
import json
import os

# ============================================================
# CONFIGURATION
# ============================================================

DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
if not DISCORD_CHANNEL_ID:
    raise RuntimeError("Missing DISCORD_CHANNEL_ID environment variable.")
TARGET_CHANNEL_ID = int(DISCORD_CHANNEL_ID)

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_APP_ACCESS_TOKEN = os.getenv("TWITCH_ACCESS_TOKEN")

ALERT_ROLE_ID = os.getenv("ALERT_ROLE_ID")

SNAPSHOT_FILE = "badges_snapshot.json"

if not TWITCH_CLIENT_ID or not TWITCH_APP_ACCESS_TOKEN:
    raise RuntimeError("Missing TWITCH_CLIENT_ID or TWITCH_ACCESS_TOKEN environment variables.")


# ============================================================
# DISCORD BOT INITIALIZATION
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=">/", intents=intents)

# Set of all previously known badge IDs (set_id:version_id)
known_badge_ids: set[str] = set()


# ============================================================
# SNAPSHOT DATABASE FUNCTIONS
# ============================================================

def load_snapshot() -> set[str]:
    """Load previously known badge IDs from JSON file."""
    if not os.path.exists(SNAPSHOT_FILE):
        return set()

    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("badge_ids", []))
    except Exception:
        return set()


def save_snapshot(ids: set[str]) -> None:
    """Save known badge IDs to JSON file."""
    payload = {"badge_ids": sorted(list(ids))}
    try:
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        pass


# ============================================================
# TWITCH API FETCHING
# ============================================================

async def fetch_global_badges():
    """
    Fetch all Twitch global badge sets and versions.
    Returns:
        List of dicts formatted consistently:
        {
          "id": "set_id:version_id",
          "name": "...",
          "type": "Global",
          "image_url": ...
        }
    """
    url = "https://api.twitch.tv/helix/chat/badges/global"
    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {TWITCH_APP_ACCESS_TOKEN}",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                return []
            data = await response.json()

    sets = data.get("data", [])
    results = []

    for badge_set in sets:
        set_id = badge_set.get("set_id", "unknown")
        for version in badge_set.get("versions", []):
            badge_id = f"{set_id}:{version.get('id', 'unknown')}"
            badge_name = version.get("title") or version.get("description") or "Unnamed Badge"
            image = (
                version.get("image_url_4x")
                or version.get("image_url_2x")
                or version.get("image_url_1x")
            )

            results.append(
                {
                    "id": badge_id,
                    "name": badge_name,
                    "type": "Global",
                    "image_url": image,
                }
            )

    return results


# ============================================================
# EMBED BUILDER
# ============================================================

def build_badge_embed(badge: dict) -> Embed:
    embed = Embed(
        title="New TTV Global Badge Detected",
        description=(
            f"**Name:** {badge['name']}\n"
            f"**Type:** {badge['type']}"
        ),
        color=0x7A3CEB
    )

    if badge.get("image_url"):
        embed.set_thumbnail(url=badge["image_url"])

    embed.set_footer(text="")  # You can fill this later with a URL or signature
    return embed


# ============================================================
# DISCORD BOT EVENTS
# ============================================================

@bot.event
async def on_ready():
    global known_badge_ids

    # Load previously known badges
    known_badge_ids = load_snapshot()

    # If first run → initialize database with current Twitch badges
    if not known_badge_ids:
        current = await fetch_global_badges()
        known_badge_ids = {b["id"] for b in current}
        save_snapshot(known_badge_ids)

    # Start periodic checks
    if not check_for_badges.is_running():
        check_for_badges.start()

    # Set presence to show prefix
    activity = discord.Activity(
        type=discord.ActivityType.watching,
        name="{>/} – command prefix",
    )
    await bot.change_presence(status=discord.Status.online, activity=activity)

    # Startup message
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if channel:
        await channel.send("Alertium is now online.")
    print("Alertium is running")


# ============================================================
# PERIODIC BADGE CHECKING
# ============================================================

@tasks.loop(seconds=30)  # Change to 1 during testing
async def check_for_badges():
    """Compare Twitch badge list with local snapshot and detect new entries."""
    global known_badge_ids

    badges = await fetch_global_badges()
    if not badges:
        return

    current_ids = {b["id"] for b in badges}
    new_badges = current_ids - known_badge_ids

    if not new_badges:
        return

    # Notify about each new badge
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if channel:
        for badge in badges:
            if badge["id"] in new_badges:
                embed = build_badge_embed(badge)
                if ALERT_ROLE_ID:
                    mention = f"<@&{ALERT_ROLE_ID}>"
                    await channel.send(content=mention, embed=embed)
                else:
                    await channel.send(embed=embed)

    # Update snapshot
    known_badge_ids = current_ids
    save_snapshot(known_badge_ids)


@check_for_badges.before_loop
async def before_loop():
    await bot.wait_until_ready()


# ============================================================
# OPTIONAL MANUAL COMMANDS
# ============================================================

@bot.command()
async def ping(ctx):
    await ctx.send("7ader")


@bot.command()
async def testbadge(ctx):
    """Manually fetch the latest badge and display it (for testing)."""
    badges = await fetch_global_badges()
    if not badges:
        await ctx.send("Could not fetch Twitch badges.")
        return

    embed = build_badge_embed(badges[-1])
    await ctx.send(embed=embed)

@bot.command()
async def simulate_new(ctx):
    """
    Simulate the detection of a new global badge.
    """
    global known_badge_ids

    fake_id = "simulated_set:simulated_version_" + str(len(known_badge_ids) + 1)

    fake_badge = {
        "id": fake_id,
        "name": "Simulated Test Badge",
        "type": "Global",
    }

    known_badge_ids.add(fake_id)
    save_snapshot(known_badge_ids)

    embed = discord.Embed(
        title=fake_badge["name"],
        description=f"ID: {fake_badge['id']}\nType: {fake_badge['type']}",
        color=0x00ff00,
    )

    # Attach the file
    file = discord.File("ali.png", filename="ali.png")

    # Point the embed to the attached image
    embed.set_image(url="attachment://ali.png")

    if ALERT_ROLE_ID:
        mention = f"<@&{ALERT_ROLE_ID}>"
        await ctx.send(content=mention, embed=embed, file=file)
    else:
        await ctx.send(embed=embed, file=file)
        
# ============================================================
# SLASH COMMANDS {>/}
# ============================================================

@bot.tree.command(name="status", description="Check if Alertium is online and running.")
async def status_command(interaction: discord.Interaction):
    message = (
        "Alertium is online and monitoring Twitch global badges.\n"
        "Prefix commands start with `>/`."
    )
    await interaction.response.send_message(message, ephemeral=True)
    
# ============================================================
# BOT LAUNCH
# ============================================================

DISCORD_BOT_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
bot.run(DISCORD_BOT_TOKEN)
