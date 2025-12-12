import discord
from discord.ext import commands, tasks
from discord import Embed, app_commands
import aiohttp
import json
import os
import asyncpg


# ============================================================
# CONFIGURATION
# ============================================================
## postgres DB save snapshot
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL (Railway Postgres).")


# OWNER_ID = os.getenv("OWNER_ID")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
if not DISCORD_CHANNEL_ID:
    raise RuntimeError("Missing DISCORD_CHANNEL_ID environment variable.")
TARGET_CHANNEL_ID = int(DISCORD_CHANNEL_ID)

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_APP_ACCESS_TOKEN = os.getenv("TWITCH_ACCESS_TOKEN")

ALERT_ROLE_ID = os.getenv("ALERT_ROLE_ID")

# SNAPSHOT_FILE = "badges_snapshot.json"

if not TWITCH_CLIENT_ID or not TWITCH_APP_ACCESS_TOKEN:
    raise RuntimeError("Missing TWITCH_CLIENT_ID or TWITCH_ACCESS_TOKEN environment variables.")


# ============================================================
# DISCORD BOT INITIALIZATION
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=[">/", "/"], intents=intents)

# Set of all previously known badge IDs (set_id:version_id)
known_badge_ids: set[str] = set()


# # ============================================================
# # SNAPSHOT DATABASE FUNCTIONS
# # ============================================================

# def load_snapshot() -> set[str]:
#     """Load previously known badge IDs from JSON file."""
#     if not os.path.exists(SNAPSHOT_FILE):
#         return set()

#     try:
#         with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
#             data = json.load(f)
#         return set(data.get("badge_ids", []))
#     except Exception:
#         return set()


# def save_snapshot(ids: set[str]) -> None:
#     """Save known badge IDs to JSON file."""
#     payload = {"badge_ids": sorted(list(ids))}
#     try:
#         with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
#             json.dump(payload, f, indent=2)
#     except Exception:
#         pass

# ============================================================
# SNAPSHOT DATABASE (Postgres) FUNCTIONS
# ============================================================

async def db_init():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS twitch_badges_seen (
            badge_id TEXT PRIMARY KEY
        );
    """)
    await conn.close()

async def db_get_seen_ids() -> set[str]:
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT badge_id FROM twitch_badges_seen;")
    await conn.close()
    return {r["badge_id"] for r in rows}

async def db_mark_seen(ids: set[str]) -> None:
    if not ids:
        return
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.executemany(
        "INSERT INTO twitch_badges_seen (badge_id) VALUES ($1) ON CONFLICT DO NOTHING;",
        [(i,) for i in ids]
    )
    await conn.close()


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
            badge_desc = version.get("description")
            image = (
                version.get("image_url_4x")
                or version.get("image_url_2x")
                or version.get("image_url_1x")
            )

            results.append(
                {
                    "id": badge_id,
                    "name": badge_name,
                    "description": badge_desc,
                    "type": "Global",
                    "image_url": image,
                }
            )

    return results


# ============================================================
# EMBED BUILDER
# ============================================================

def build_badge_embed(badge: dict) -> Embed:
    desc_line = ""
    if badge.get("description"):
        desc_line = f"\n**Description:** {badge['description']}"

    embed = Embed(
        title="New TTV Global Badge Detected",
        description=(
            f"**Name:** {badge['name']}\n"
            f"**Type:** {badge['type']}"
            f"{desc_line}"
        ),
        color=0x7A3CEB
    )

    if badge.get("image_url"):
        embed.set_thumbnail(url=badge["image_url"])

    return embed


# ============================================================
# DISCORD BOT EVENTS
# ============================================================

# @bot.event
# async def on_ready():
#     global known_badge_ids

#     # Load previously known badges
#     known_badge_ids = load_snapshot()

#     # If first run → initialize database with current Twitch badges
#     if not known_badge_ids:
#         current = await fetch_global_badges()
#         known_badge_ids = {b["id"] for b in current}
#         save_snapshot(known_badge_ids)

#     # Start periodic checks
#     if not check_for_badges.is_running():
#         check_for_badges.start()

#     # Set presence to show prefix
#     activity = discord.Activity(
#         type=discord.ActivityType.watching,
#         name="{>/} – command prefix",
#     )
#     await bot.change_presence(status=discord.Status.online, activity=activity)

#     # Startup message
#     channel = bot.get_channel(TARGET_CHANNEL_ID)
#     if channel:
#         embed = Embed(
#             title="Alertium is now online",
#             description=(
#                 "Monitoring Twitch **global badges** for new releases.\n"
#                 "Prefix: `>/`  |  Try: `>/status`"
#             ),
#             color=0x7A3CEB,
#         )
#         await channel.send(embed=embed)

#     print("Alertium is running")

#---------------------------Postgres(rootVer UP)--------
@bot.event
async def on_ready():
    global known_badge_ids

    await db_init()

    # Load previously known badges from DB
    known_badge_ids = await db_get_seen_ids()

    # If first run (DB empty) → initialize DB with current Twitch badges
    if not known_badge_ids:
        current = await fetch_global_badges()
        known_badge_ids = {b["id"] for b in current}
        await db_mark_seen(known_badge_ids)

    if not check_for_badges.is_running():
        check_for_badges.start()

    activity = discord.Activity(
        type=discord.ActivityType.watching,
        name="{>/} – command prefix",
    )
    await bot.change_presence(status=discord.Status.online, activity=activity)

    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if channel:
        embed = Embed(
            title="Alertium is now online",
            description=(
                "Monitoring Twitch **global badges** for new releases.\n"
                "Prefix: `>/`  |  Try: `>/status`"
            ),
            color=0x7A3CEB,
        )
        await channel.send(embed=embed)

    print("Alertium is running")
    try:
        await bot.tree.sync()
        print("Slash commands synced")
    except Exception as e:
        print(f"Slash sync failed: {e}")



@bot.event
async def on_message(message: discord.Message):
    # Ignore messages sent by the bot itself
    if message.author == bot.user:
        return

    # If bot is mentioned
    if bot.user in message.mentions:
        user_id = message.author.id
        content = message.content.lower()

        # ------------------------------
        # SPECIAL CASE: You mention bot
        # ------------------------------
        if user_id == OWNER_ID and "mal hada" in content:
            await message.channel.send(f"{message.author.mention} khas li ygadha lih")
            await bot.process_commands(message)
            return

        # ------------------------------
        # NORMAL ROTATING RESPONSES
        # ------------------------------
        current_count = mention_counts.get(user_id, 0)
        index = current_count % len(MENTION_REPLIES)
        reply_text = MENTION_REPLIES[index]

        mention_counts[user_id] = current_count + 1

        await message.channel.send(f"{message.author.mention} {reply_text}")

    # Allow prefix commands to still work
    await bot.process_commands(message)



# ============================================================
# PERIODIC BADGE CHECKING
# ============================================================

@tasks.loop(seconds=10)  # Change to 1 during testing
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
    # known_badge_ids = current_ids
    # save_snapshot(known_badge_ids)
#----------------------------------------------------
    
    # Save new badges to DB so they persist across restarts
    await db_mark_seen(new_badges)
    
    # Update in-memory cache
    known_badge_ids = current_ids



@check_for_badges.before_loop
async def before_loop():
    await bot.wait_until_ready()


# ============================================================
# REACTION ROLE HANDLERS
# ============================================================

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """
    Handle reactions on the opt-in message:
    - Only ✅ is allowed.
    - Any other reaction is removed.
    - ✅ grants the alert role once.
    """
    # Ignore bot's own reactions
    if payload.user_id == bot.user.id:
        return

    if not ALERT_ROLE_ID:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    channel = guild.get_channel(payload.channel_id)
    if channel is None:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return

    # Only handle messages sent by this bot
    if message.author.id != bot.user.id:
        return

    # Only act on your opt-in embed
    if not message.embeds:
        return

    title = message.embeds[0].title or ""
    if title != "Alertium Notifications Opt-in":
        return

    # At this point we know: this is the opt-in message
    emoji_str = str(payload.emoji)

    member = guild.get_member(payload.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.NotFound:
            return

    # If the emoji is NOT ✅ → remove it and stop
    if emoji_str != "✅":
        try:
            await message.remove_reaction(payload.emoji, member)
            print(f"Removed non-✅ reaction from {member} on opt-in message.")
        except Exception as e:
            print(f"Failed to remove reaction: {e}")
        return

    # Emoji IS ✅ → give the alert role
    role = guild.get_role(int(ALERT_ROLE_ID))
    if role is None:
        print(f"Could not find alert role with ID {ALERT_ROLE_ID}")
        return

    if role not in member.roles:
        await member.add_roles(role, reason="Opted into Alertium notifications via reaction")
        print(f"Assigned alert role to {member}.")



@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    """Remove the alert role when a user removes ✅ from the opt-in message."""
    if not ALERT_ROLE_ID:
        return

    if str(payload.emoji) != "✅":
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    role = guild.get_role(int(ALERT_ROLE_ID))
    if role is None:
        return

    member = guild.get_member(payload.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.NotFound:
            return

    channel = guild.get_channel(payload.channel_id)
    if channel is None:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return

    if message.author.id != bot.user.id:
        return

    if message.embeds:
        if message.embeds[0].title != "Alertium Notifications Opt-in":
            return

    if role in member.roles:
        await member.remove_roles(role, reason="Opted out of Alertium notifications via reaction")



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
async def status(ctx):
    """Check if Alertium is online."""
    message = (
        "Alertium is online.\n"
    )
    await ctx.send(message)

@bot.command()
@commands.has_permissions(manage_roles=True)
async def setup_alert_role(ctx):
    """
    Post the opt-in message so users can react to get the alert role.
    Run this in the channel where you want people to subscribe.
    """
    if not ALERT_ROLE_ID:
        await ctx.send("ALERT_ROLE_ID is not configured in the environment.")
        return

    description = (
        "React with ✅ to receive the @AlertiumMe role.\n"
        "You will be pinged whenever a new Twitch global badge is detected."
    )

    embed = Embed(
        title="Alertium Notifications Opt-in",
        description=description,
        color=0x7A3CEB,
    )

    msg = await ctx.send(embed=embed)
    await msg.add_reaction("✅")


@bot.command()
async def simulate_new(ctx):
    global known_badge_ids

    fake_id = "simulated_set:simulated_version_" + str(len(known_badge_ids) + 1)

    fake_badge = {
        "id": fake_id,
        "name": "Simulated Test Badge",
        "type": "Global",
    }

    known_badge_ids.add(fake_id)
    # save_snapshot(known_badge_ids)
    await db_mark_seen({fake_id})

    embed = discord.Embed(
        title="Simulated Test Badge",
        description=f"**Name:** Simulated Test Badge\n**Type:** Global",
        color=0x7A3CEB,
    )

    file = discord.File("ali.png", filename="ali.png")

    embed.set_thumbnail(url="attachment://ali.png")

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
# MENTION REPLY CONFIG
# ============================================================

MENTION_REPLIES = [
    "tagi mok",
    "chi qalwa?",   
    "nod liya men fogo",
    "T9awed",
]

# Track how many times each user mentioned the bot
mention_counts: dict[int, int] = {}

    
# ============================================================
# BOT LAUNCH
# ============================================================

DISCORD_BOT_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
bot.run(DISCORD_BOT_TOKEN)
