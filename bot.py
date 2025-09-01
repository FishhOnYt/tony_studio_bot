import os
import logging
import re
import random
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timedelta
import aiohttp
import aiosqlite
from typing import Optional

# -------------------------
# Basic config
# -------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
if not TOKEN or not OWNER_ID:
    raise RuntimeError("DISCORD_TOKEN or OWNER_ID not set in .env")
OWNER_ID = int(OWNER_ID)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tony_bot")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

DB_PATH = "bot_data.db"
GUILD_ID = 984999848791126096

# Counting channels (multiple allowed)
COUNTING_CHANNEL_IDS = [1398545401598050425, 1411772929720586401]
FAILURE_ROLE_ID = 1210840031023988776

# Default bonus-role mapping (role_id: extra_entries)
BONUS_ROLES = {
    1411126451163365437: 1,
    1412210602159378462: 2,
    1412212184792043530: 3,
    1412212463176388689: 4,
    1412212683515887710: 5,
    1412212741674106952: 6,
    1412212961338069022: 8
}

# In-memory giveaways store: message_id -> giveaway data
giveaways = {}

# -------------------------
# Helpers
# -------------------------
def parse_duration_to_seconds(s: str) -> Optional[int]:
    """
    Accepts strings like '1d', '2h30m', '45m', '90s', '1h30m20s', or pure seconds '3600'.
    Returns seconds or None if invalid.
    """
    s = s.strip().lower()
    if s.isdigit():
        return int(s)
    pattern = r'(?:(?P<days>\d+)\s*d)?\s*(?:(?P<hours>\d+)\s*h)?\s*(?:(?P<minutes>\d+)\s*m)?\s*(?:(?P<seconds>\d+)\s*s)?'
    m = re.fullmatch(pattern, s)
    if not m:
        return None
    parts = {k: int(v) for k, v in m.groupdict().items() if v}
    seconds = parts.get("days", 0) * 86400 + parts.get("hours", 0) * 3600 + parts.get("minutes", 0) * 60 + parts.get("seconds", 0)
    return seconds if seconds > 0 else None

async def fetch_reaction_users(reaction: discord.Reaction):
    users = []
    # safe async iteration (works across discord.py versions)
    async for u in reaction.users():
        users.append(u)
    return users

def format_extra_entries_field(giveaway_extra_roles):
    # show combined of default BONUS_ROLES + any custom per-giveaway extras
    lines = []
    combined = BONUS_ROLES.copy()
    if giveaway_extra_roles:
        combined.update(giveaway_extra_roles)
    for rid, bonus in combined.items():
        lines.append(f"<@&{rid}>: +{bonus}")
    return "\n".join(lines) if lines else "None"

# -------------------------
# Bot class
# -------------------------
class TonyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=intents)
        self.session: aiohttp.ClientSession = None
        self.db: aiosqlite.Connection = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.db = await aiosqlite.connect(DB_PATH)
        await self._ensure_tables()
        guild = discord.Object(id=GUILD_ID)
        await self.tree.sync(guild=guild)
        logger.info("Commands synced to guild!")

    async def _ensure_tables(self):
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS counting (
                channel_id INTEGER PRIMARY KEY,
                last_number INTEGER DEFAULT 0
            )
        """)
        await self.db.commit()

    async def close(self):
        if self.session:
            await self.session.close()
        if self.db:
            await self.db.close()
        await super().close()

bot = TonyBot()

# Roblox API helper kept from your original
ROBLOX_USERS = "https://users.roblox.com/v1/usernames/users"
async def roblox_get_user(session, username):
    try:
        async with session.post(ROBLOX_USERS, json={"usernames": [username], "excludeBannedUsers": False}) as resp:
            data = await resp.json()
            if data.get("data"):
                return data["data"][0]
    except Exception:
        logger.exception("Failed Roblox user lookup")
    return None

async def send_to_owner(embed: discord.Embed):
    owner = await bot.fetch_user(OWNER_ID)
    if owner:
        try:
            await owner.send(embed=embed)
        except Exception:
            logger.exception("Failed to DM owner")

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

# -------------------------
# Giveaway: /giveaway (single command with many options)
# -------------------------
# Only users with manage_guild can use it (start/end/reroll)
giveaway_group = app_commands.Group(name="giveaway", description="Giveaway commands")

@giveaway_group.command(name="start", description="Start a giveaway")
@app_commands.describe(
    duration="Duration (e.g. '1h30m', '45m', '90s', or seconds)",
    winners="Number of winners (1-10)",
    prize="The prize text",
    channel="Channel to post giveaway in",
    host="Host (defaults to you)",
    required_role="Role required to enter (optional)",
    extra_entries="Optional extra entries in format roleid:bonus,roleid:bonus (e.g. 123:2,456:5)"
)
async def giveaway_start(interaction: discord.Interaction,
                         duration: str,
                         winners: int,
                         prize: str,
                         channel: discord.TextChannel,
                         host: Optional[discord.Member] = None,
                         required_role: Optional[discord.Role] = None,
                         extra_entries: Optional[str] = None):
    # permissions
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ You need Manage Server permission to start giveaways.", ephemeral=True)
        return

    seconds = parse_duration_to_seconds(duration)
    if seconds is None:
        await interaction.response.send_message("❌ Invalid duration. Examples: `1h30m`, `45m`, `90s`, or `3600`.", ephemeral=True)
        return
    if winners < 1 or winners > 10:
        await interaction.response.send_message("❌ Winners must be between 1 and 10.", ephemeral=True)
        return

    host = host or interaction.user

    # parse extra_entries param into dict
    giveaway_extra_roles = {}
    if extra_entries:
        # allow forms like "123:2,456:5" or "<@&123>:2"
        parts = [p.strip() for p in extra_entries.split(",") if p.strip()]
        for p in parts:
            p_clean = p.replace("<@&", "").replace(">", "")
            if ":" in p_clean:
                rid_str, bonus_str = p_clean.split(":", 1)
                try:
                    rid = int(rid_str.strip())
                    bonus = int(bonus_str.strip())
                    if bonus > 0:
                        giveaway_extra_roles[rid] = bonus
                except Exception:
                    continue

    ends_at = datetime.utcnow() + timedelta(seconds=seconds)
    ends_str = ends_at.strftime("%Y-%m-%d %H:%M:%S UTC")

    embed = discord.Embed(
        title="🎉 Giveaway 🎉",
        description=f"**Prize:** {prize}\nReact with 🎉 to join!\n**Ends:** {ends_str}\n**Winners:** {winners}",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Host", value=str(host), inline=True)
    embed.add_field(name="Required Role", value=(str(required_role) if required_role else "None"), inline=True)
    embed.add_field(name="Extra entries", value=format_extra_entries_field(giveaway_extra_roles), inline=False)
    embed.set_footer(text=f"Started by {interaction.user}", icon_url=interaction.user.display_avatar.url)

    await interaction.response.send_message(f"✅ Giveaway posted in {channel.mention}", ephemeral=True)
    gw_msg = await channel.send(embed=embed)
    try:
        await gw_msg.add_reaction("🎉")
    except Exception:
        logger.exception("Couldn't add reaction to giveaway message")

    # store giveaway info
    giveaways[gw_msg.id] = {
        "prize": prize,
        "channel_id": channel.id,
        "host_id": host.id if host else None,
        "required_role_id": required_role.id if required_role else None,
        "extra_roles": giveaway_extra_roles,  # per-giveaway extras
        "winners": winners,
        "ends_at": ends_at,
        "message": gw_msg,
        "ended": False,
        "task": None
    }

    # schedule end
    async def _auto_end(msg_id, chan, delay):
        await asyncio.sleep(delay)
        await end_giveaway(chan, msg_id)

    task = asyncio.create_task(_auto_end(gw_msg.id, channel, seconds))
    giveaways[gw_msg.id]["task"] = task

# end helper
async def end_giveaway(channel: discord.TextChannel, message_id: int):
    if message_id not in giveaways:
        return
    gw = giveaways[message_id]
    if gw["ended"]:
        return

    try:
        msg = await channel.fetch_message(message_id)
    except Exception:
        logger.exception("Failed to fetch giveaway message")
        gw["ended"] = True
        return

    reaction = discord.utils.get(msg.reactions, emoji="🎉")
    if not reaction:
        await channel.send("❌ No one entered the giveaway!")
        gw["ended"] = True
        return

    users = await fetch_reaction_users(reaction)
    users = [u for u in users if not u.bot]
    if not users:
        await channel.send("❌ No valid users entered!")
        gw["ended"] = True
        return

    # build weighted entries (respect required_role)
    weighted = []
    for user in users:
        member = channel.guild.get_member(user.id)
        if not member:
            continue
        # required role check
        if gw["required_role_id"] and not discord.utils.get(member.roles, id=gw["required_role_id"]):
            continue
        entries = 1
        # default global bonuses
        for rid, bonus in BONUS_ROLES.items():
            if discord.utils.get(member.roles, id=rid):
                entries += bonus
        # per-giveaway extras
        for rid, bonus in gw.get("extra_roles", {}).items():
            if discord.utils.get(member.roles, id=rid):
                entries += bonus
        if entries > 0:
            weighted.extend([member] * entries)

    if not weighted:
        await channel.send("❌ No eligible participants after role requirements/bonuses.")
        gw["ended"] = True
        return

    winners = []
    chosen_members = set()
    max_winners = min(gw["winners"], len(set(weighted)))  # can't pick more unique winners than unique entrants
    for _ in range(max_winners):
        winner_member = random.choice(weighted)
        winners.append(winner_member)
        chosen_members.add(winner_member.id)
        # remove all entries of this winner to avoid duplicates
        weighted = [m for m in weighted if m.id != winner_member.id]
        if not weighted:
            break

    mention_list = ", ".join(w.mention for w in winners)
    await channel.send(f"🎉 **Giveaway Ended!** Prize: **{gw['prize']}**\nWinner(s): {mention_list}")
    gw["ended"] = True

# /giveaway end
@giveaway_group.command(name="end", description="End an active giveaway early")
@app_commands.describe(message_id="Message ID of the giveaway to end")
async def giveaway_end(interaction: discord.Interaction, message_id: str):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ You need Manage Server permission to use this.", ephemeral=True)
        return
    try:
        mid = int(message_id)
    except Exception:
        await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)
        return
    gw = giveaways.get(mid)
    if not gw:
        await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)
        return
    await interaction.response.send_message("✅ Ending giveaway...", ephemeral=True)
    chan = interaction.guild.get_channel(gw["channel_id"])
    if chan:
        await end_giveaway(chan, mid)
    else:
        await interaction.followup.send("❌ Could not find the giveaway channel.", ephemeral=True)

# /giveaway reroll
@giveaway_group.command(name="reroll", description="Reroll winners for a finished giveaway")
@app_commands.describe(message_id="Message ID of the finished giveaway")
async def giveaway_reroll(interaction: discord.Interaction, message_id: str):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ You need Manage Server permission to use this.", ephemeral=True)
        return
    try:
        mid = int(message_id)
    except Exception:
        await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)
        return
    gw = giveaways.get(mid)
    if not gw or not gw["ended"]:
        await interaction.response.send_message("❌ Giveaway not found or hasn't ended yet.", ephemeral=True)
        return
    chan = interaction.guild.get_channel(gw["channel_id"])
    if not chan:
        await interaction.response.send_message("❌ Could not find channel.", ephemeral=True)
        return

    # fetch message and reactions same as end (but don't mark ended because it's already ended)
    try:
        msg = await chan.fetch_message(mid)
    except Exception:
        await interaction.response.send_message("❌ Could not fetch giveaway message.", ephemeral=True)
        return
    reaction = discord.utils.get(msg.reactions, emoji="🎉")
    if not reaction:
        await interaction.response.send_message("❌ No entries found.", ephemeral=True)
        return
    users = await fetch_reaction_users(reaction)
    users = [u for u in users if not u.bot]
    weighted = []
    for user in users:
        member = chan.guild.get_member(user.id)
        if not member:
            continue
        if gw["required_role_id"] and not discord.utils.get(member.roles, id=gw["required_role_id"]):
            continue
        entries = 1
        for rid, bonus in BONUS_ROLES.items():
            if discord.utils.get(member.roles, id=rid):
                entries += bonus
        for rid, bonus in gw.get("extra_roles", {}).items():
            if discord.utils.get(member.roles, id=rid):
                entries += bonus
        weighted.extend([member] * entries)

    if not weighted:
        await interaction.response.send_message("❌ No eligible participants for reroll.", ephemeral=True)
        return

    # pick single new winner (or pick as many as originally)
    winners = []
    chosen_members = set()
    max_winners = min(gw["winners"], len(set(weighted)))
    for _ in range(max_winners):
        winner_member = random.choice(weighted)
        winners.append(winner_member)
        weighted = [m for m in weighted if m.id != winner_member.id]
        if not weighted:
            break

    mention_list = ", ".join(w.mention for w in winners)
    await interaction.response.send_message(f"🔄 Reroll complete! New winner(s): {mention_list}", ephemeral=False)

# register group
bot.tree.add_command(giveaway_group)

# -------------------------
# Other commands (report, suggest, profile, help)
# -------------------------
@bot.tree.command(name="report", description="Send a bug report to the bot owner")
@app_commands.describe(bug="Describe the bug")
async def report(interaction: discord.Interaction, bug: str):
    embed = discord.Embed(
        title="🐞 Bug Report",
        description=bug,
        color=discord.Color.red(),
        timestamp=datetime.utcnow()
    )
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text=f"User ID: {interaction.user.id}")
    await interaction.response.send_message("✅ Your bug report was sent!", ephemeral=True)
    await bot.db.execute("INSERT INTO reports (user_id, username, content) VALUES (?, ?, ?)",
                         (interaction.user.id, str(interaction.user), bug))
    await bot.db.commit()
    await send_to_owner(embed)

@bot.tree.command(name="suggest", description="Send a suggestion to the bot owner")
@app_commands.describe(idea="Your suggestion")
async def suggest(interaction: discord.Interaction, idea: str):
    embed = discord.Embed(
        title="💡 Suggestion",
        description=idea,
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text=f"User ID: {interaction.user.id}")
    await interaction.response.send_message("✅ Your suggestion was sent!", ephemeral=True)
    await bot.db.execute("INSERT INTO suggestions (user_id, username, content) VALUES (?, ?, ?)",
                         (interaction.user.id, str(interaction.user), idea))
    await bot.db.commit()
    await send_to_owner(embed)

@bot.tree.command(name="profile", description="View a Roblox user's profile")
@app_commands.describe(username="Roblox username")
async def profile(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=False)
    user = await roblox_get_user(bot.session, username)
    if not user:
        await interaction.followup.send(f"❌ Could not find Roblox user `{username}`")
        return
    user_id = user.get("id")
    display_name = user.get("displayName") or username
    name = user.get("name")
    profile_link = f"https://www.roblox.com/users/{user_id}/profile"
    embed = discord.Embed(
        title=f"{display_name} — Roblox Profile",
        url=profile_link,
        color=discord.Color.blue(),
        description=f"Click [here]({profile_link}) to view the profile on Roblox!"
    )
    embed.set_image(url=f"https://www.roblox.com/headshot-thumbnail/image?userId={user_id}&width=420&height=420&format=png")
    embed.add_field(name="Username", value=name, inline=True)
    embed.add_field(name="Display Name", value=display_name, inline=True)
    embed.add_field(name="User ID", value=str(user_id), inline=True)
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.display_avatar.url)
    embed.timestamp = datetime.utcnow()
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="help", description="Show all bot commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Tony Studios Bot — Help",
        description="Use the slash commands. Giveaway example: `/giveaway start duration:1h30m winners:1 prize:Cool Stuff channel:#giveaways`",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="🕹️ /profile", value="View a Roblox user's profile", inline=False)
    embed.add_field(name="🐞 /report", value="Send a bug report", inline=False)
    embed.add_field(name="💡 /suggest", value="Send a suggestion", inline=False)
    embed.add_field(name="🎉 /giveaway", value="/giveaway start|end|reroll — Manage giveaways", inline=False)
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -------------------------
# Counting game (unchanged logic, supports multiple channels)
# -------------------------
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.id in COUNTING_CHANNEL_IDS:
        try:
            number = int(message.content.strip())
        except ValueError:
            await bot.process_commands(message)
            return

        await bot.db.execute("INSERT OR IGNORE INTO counting (channel_id, last_number) VALUES (?, ?)", (message.channel.id, 0))
        await bot.db.commit()

        async with bot.db.execute("SELECT last_number FROM counting WHERE channel_id = ?", (message.channel.id,)) as cursor:
            row = await cursor.fetchone()
            last_number = row[0] if row else 0

        if number == last_number + 1:
            await bot.db.execute("UPDATE counting SET last_number = ? WHERE channel_id = ?", (number, message.channel.id))
            await bot.db.commit()
            try:
                await message.add_reaction("✅")
            except Exception:
                logger.exception("Couldn't react to user's message")
            next_num = number + 1
            try:
                bot_msg = await message.channel.send(str(next_num))
                await bot_msg.add_reaction("✅")
                await bot.db.execute("UPDATE counting SET last_number = ? WHERE channel_id = ?", (next_num, message.channel.id))
                await bot.db.commit()
            except Exception:
                logger.exception("Failed to send next number or react")
        else:
            try:
                await message.add_reaction("❌")
            except Exception:
                logger.exception("Couldn't react with ❌")
            try:
                await bot.db.execute("UPDATE counting SET last_number = 0 WHERE channel_id = ?", (message.channel.id,))
                await bot.db.commit()
            except Exception:
                logger.exception("Failed to reset counting in DB")
            try:
                await message.channel.send(f"❌ {message.author.mention} failed the counting game! Start again with **1**.")
            except Exception:
                logger.exception("Failed to send failure announcement")
            guild = message.guild
            if guild:
                role = guild.get_role(FAILURE_ROLE_ID)
                if role:
                    try:
                        await message.author.add_roles(role, reason="Failed counting game")
                    except Exception:
                        logger.exception(f"Failed to give role {FAILURE_ROLE_ID} to {message.author}")

    # react to mentions like before
    if bot.user in message.mentions:
        try:
            for emoji in ["🇾", "🇪", "🇸", "❓"]:
                await message.add_reaction(emoji)
        except Exception:
            logger.exception("Failed to react to mention")

    await bot.process_commands(message)

# -------------------------
# Run bot
# -------------------------
try:
    bot.run(TOKEN)
except discord.errors.PrivilegedIntentsRequired:
    logger.error("⚠️ Enable 'Message Content Intent' & 'Server Members Intent' in the Discord Developer Portal.")
    raise
