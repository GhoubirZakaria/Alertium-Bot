import discord
from discord.ext import commands, tasks
from discord import Embed, app_commands
import aiohttp
import json
import os

# ------------------------------------------------------------
from datetime import timedelta, datetime

# Forbidden combinations of emoji reactions
FORBIDDEN_COMBOS = [
    {"🇳", "I", "G"},
    {"🇳", "I", "G", "🇬", "E", "R"},
    {"🇳", "I", "G", "🇬", "A"},
    {"😡", "🤬"}
]

# Timeout escalation in minutes
OFFENSE_TIMEOUTS = [1, 5, 15, 60]  # 1 min → 5 min → 15 min → 1 hour

# Track offense count per user
offense_counts: dict[int, int] = {}

# Track user reactions on each message
# Key: (message_id, user_id) → Set of emojis
reactions_on_message: dict[tuple[int, int], set[str]] = {}
last_punished_at: dict[int, datetime] = {}



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

LOG_CHANNEL_ID_ENV = os.getenv("LOG_CHANNEL_ID")
LOG_CHANNEL_ID = int(LOG_CHANNEL_ID_ENV) if LOG_CHANNEL_ID_ENV else None

SNAPSHOT_FILE = "badges_snapshot.json"

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
#---------------------logging to log channel----------------------

async def log_timeout_action(
    guild: discord.Guild,
    member: discord.Member,
    minutes: int,
    count: int,
    combo: set[str],
    channel_id: int,
    message_id: int,
):
    """Send a log entry to the log channel, if configured."""
    if LOG_CHANNEL_ID is None:
        return

    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if log_channel is None:
        return

    jump_url = f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"

    embed = Embed(
        title="Alertium – Forbidden Reaction combo Timeout",
        description="A user has been timed out for a forbidden emoji combo.",
        color=0xFF5555,
    )
    embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)
    embed.add_field(name="Duration", value=f"{minutes} minute(s)", inline=True)
    embed.add_field(name="Offense Count", value=str(count), inline=True)
    embed.add_field(name="Emoji Combo", value=", ".join(combo), inline=False)
    embed.add_field(name="Message", value=f"[Jump to message]({jump_url})", inline=False)

    await log_channel.send(embed=embed)


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
# REACTION ROLE HANDLERS
# ============================================================

# @bot.event
# (payload: discord.RawReactionActionEvent):
#     """Give the alert role when a user reacts with ✅ on the opt-in message."""
#     # Ignore bot's own reactions
#     if payload.user_id == bot.user.id:
#         return

#     if str(payload.emoji) != "✅":
#         return

#     if not ALERT_ROLE_ID:
#         return

#     guild = bot.get_guild(payload.guild_id)
#     if guild is None:
#         return

#     role = guild.get_role(int(ALERT_ROLE_ID))
#     if role is None:
#         return

#     # Fetch the member
#     member = guild.get_member(payload.user_id)
#     if member is None:
#         try:
#             member = await guild.fetch_member(payload.user_id)
#         except discord.NotFound:
#             return

#     # Fetch the message to ensure it's one of our opt-in messages
#     channel = guild.get_channel(payload.channel_id)
#     if channel is None:
#         return

#     try:
#         message = await channel.fetch_message(payload.message_id)
#     except discord.NotFound:
#         return

#     # Only react to messages sent by this bot and (optionally) with the opt-in title
#     if message.author.id != bot.user.id:
#         return

#     # Optional: check embed title to be extra safe
#     if message.embeds:
#         if message.embeds[0].title != "Alertium Notifications Opt-in":
#             return

#     # Finally, add the role if the user doesn't have it
#     if role not in member.roles:
#         await member.add_roles(role, reason="Opted into Alertium notifications via reaction")


# ============================================================
# REACTION HANDLER: moderation + opt-in role
# ============================================================

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Handle reaction-based moderation and the opt-in alert role."""

    # Ignore bot's own reactions
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    emoji_str = str(payload.emoji)
    key = (payload.message_id, payload.user_id)

    # --------------------------------------------------------
    # 1) Track reactions for combo detection
    # --------------------------------------------------------
    if key not in reactions_on_message:
        reactions_on_message[key] = set()
    reactions_on_message[key].add(emoji_str)

    # --------------------------------------------------------
    # 2) Moderation: check forbidden combos
    # --------------------------------------------------------
    for combo in FORBIDDEN_COMBOS:
        if combo.issubset(reactions_on_message[key]):
            # User has used all emojis in this combo on this message
            member = guild.get_member(payload.user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(payload.user_id)
                except discord.NotFound:
                    return

            # Cooldown: avoid punishing multiple times within 10 seconds
            now = datetime.utcnow()
            last = last_punished_at.get(member.id)
            if last and (now - last).total_seconds() < 10:
                # Within cooldown; do not punish again
                print(f"Skipping punishment for {member} due to cooldown.")
                break

            # Escalate offense count
            count = offense_counts.get(member.id, 0) + 1
            offense_counts[member.id] = count
            last_punished_at[member.id] = now

            index = min(count - 1, len(OFFENSE_TIMEOUTS) - 1)
            minutes = OFFENSE_TIMEOUTS[index]

            try:
                await member.timeout(
                    timedelta(minutes=minutes),
                    reason=f"Forbidden emoji combo detected: {combo} (offense {count})",
                )
                print(f"Timed out {member} for {minutes} minute(s) (combo offense {count}).")
            except Exception as e:
                print(f"Failed to timeout {member}: {e}")

            # Optional: remove all their reactions from this message
            channel = guild.get_channel(payload.channel_id)
            if channel is not None:
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    for reaction in msg.reactions:
                        async for user in reaction.users():
                            if user.id == member.id:
                                await reaction.remove(user)
                except Exception:
                    pass

            # Log to moderation channel
            await log_timeout_action(
                guild=guild,
                member=member,
                minutes=minutes,
                count=count,
                combo=combo,
                channel_id=payload.channel_id,
                message_id=payload.message_id,
            )

            # Clear stored reactions for this (message, user) to avoid repeated triggers
            reactions_on_message[key].clear()

            break  # Stop checking combos

    # --------------------------------------------------------
    # 3) Opt-in Alert Role: ✅ on opt-in message
    # --------------------------------------------------------
    if emoji_str != "✅":
        return

    if not ALERT_ROLE_ID:
        return

    role = guild.get_role(int(ALERT_ROLE_ID))
    if role is None:
        print(f"Could not find alert role with ID {ALERT_ROLE_ID}")
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

    # Only handle messages sent by this bot
    if message.author.id != bot.user.id:
        return

    # Only act on your opt-in embed
    if message.embeds:
        title = message.embeds[0].title or ""
        if "Alertium Notifications Opt-in" not in title:
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
@commands.has_permissions(manage_guild=True)
async def offenses(ctx, member: discord.Member | None = None):
    """
    Show how many reaction offenses a user has.
    If no user is provided, show for yourself.
    """
    if member is None:
        member = ctx.author

    count = offense_counts.get(member.id, 0)
    await ctx.send(f"{member.mention} has **{count}** recorded reaction offense(s).")


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
    save_snapshot(known_badge_ids)

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
# BOT LAUNCH
# ============================================================

DISCORD_BOT_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
bot.run(DISCORD_BOT_TOKEN)
