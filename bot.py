# tony_bot.py
import os
import logging
import re
import random
import asyncio
from typing import Optional, Dict

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timedelta
import aiohttp
import aiosqlite

# =========================================================
#                CONFIG (edit .env for secrets)
# =========================================================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
if not TOKEN or not OWNER_ID:
    raise RuntimeError("DISCORD_TOKEN or OWNER_ID missing in .env")
OWNER_ID = int(OWNER_ID)

# Guild to instantly sync slash commands to
GUILD_ID = 984999848791126096  # <-- change if needed

# Counting channels (multi)
COUNTING_CHANNEL_IDS = [1398545401598050425, 1411772929720586401]
FAILURE_ROLE_ID = 1210840031023988776

# The ONLY role allowed to manage giveaways
GIVEAWAY_HOST_ROLE_ID = 1402405882939048076

# Extra entries per role (role_id: bonus_entries)
BONUS_ROLES = {
    1411126451163365437: 1,
    1412210602159378462: 2,
    1412212184792043530: 3,
    1412212463176388689: 4,
    1412212683515887710: 5,
    1412212741674106952: 6,
    1412212961338069022: 8,
}

# Footer promo
ROBLOX_GROUP_URL = "https://www.roblox.com/share/g/84587582"
FOOTER_TEXT = f"Join my Roblox group ➜ {ROBLOX_GROUP_URL}"

DB_PATH = "bot_data.db"

# =========================================================
#                    LOGGING + INTENTS
# =========================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tony_bot")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

# =========================================================
#                        BOT
# =========================================================
class TonyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=intents)
        self.session: Optional[aiohttp.ClientSession] = None
        self.db: Optional[aiosqlite.Connection] = None

        # runtime store: giveaways (not persisted across restarts)
        self.giveaways: Dict[int, dict] = {}

    async def setup_hook(self):
        # http + db
        self.session = aiohttp.ClientSession()
        self.db = await aiosqlite.connect(DB_PATH)
        await self._ensure_tables()

        # register groups before syncing
        self.tree.add_command(giveaway_group)

        # guild-scoped sync for instant availability
        guild_obj = discord.Object(id=GUILD_ID)
        await self.tree.sync(guild=guild_obj)
        logger.info("Slash commands synced to guild %s", GUILD_ID)

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

# =========================================================
#                      HELPERS
# =========================================================
def has_giveaway_role(member: discord.Member) -> bool:
    return any(r.id == GIVEAWAY_HOST_ROLE_ID for r in member.roles)

def parse_duration_to_seconds(s: str) -> Optional[int]:
    """
    Accepts '1d', '2h30m', '45m', '90s', '1h30m20s' or raw seconds '3600'.
    """
    s = (s or "").strip().lower()
    if not s:
        return None
    if s.isdigit():
        sec = int(s)
        return sec if sec > 0 else None
    pattern = r'^\s*(?:(?P<days>\d+)\s*d)?\s*(?:(?P<hours>\d+)\s*h)?\s*(?:(?P<minutes>\d+)\s*m)?\s*(?:(?P<seconds>\d+)\s*s)?\s*$'
    m = re.fullmatch(pattern, s)
    if not m:
        return None
    parts = {k: int(v) for k, v in m.groupdict().items() if v}
    seconds = parts.get("days", 0) * 86400 + parts.get("hours", 0) * 3600 + parts.get("minutes", 0) * 60 + parts.get("seconds", 0)
    return seconds if seconds > 0 else None

async def fetch_reaction_users(reaction: discord.Reaction):
    users = []
    async for u in reaction.users():
        users.append(u)
    return users

def extra_entries_field_text(extra: Optional[Dict[int, int]]) -> str:
    merged = dict(BONUS_ROLES)
    if extra:
        merged.update(extra)
    if not merged:
        return "None"
    return "\n".join(f"<@&{rid}>: +{bonus}" for rid, bonus in merged.items())

# =========================================================
#                      GIVEAWAY GROUP
# =========================================================
giveaway_group = app_commands.Group(
    name="giveaway",
    description="Giveaway commands (🎉 to enter)",
    guild_ids=[GUILD_ID],  # fast register to this guild
)

@giveaway_group.command(name="start", description="Start a giveaway (role-locked)")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(
    duration="e.g. 1h30m, 45m, 90s, or 3600",
    winners="Number of winners (1-10)",
    prize="What are you giving away?",
    channel="Channel to post the giveaway",
    host="Giveaway host (defaults to you)",
    required_role="Role required to be eligible (optional)",
    extra_entries="Optional: roleid:bonus,roleid:bonus (IDs or <@&id>)"
)
async def giveaway_start(
    interaction: discord.Interaction,
    duration: str,
    winners: app_commands.Range[int, 1, 10],
    prize: str,
    channel: discord.TextChannel,
    host: Optional[discord.Member] = None,
    required_role: Optional[discord.Role] = None,
    extra_entries: Optional[str] = None
):
    # must be in a server
    if not interaction.guild:
        await interaction.response.send_message("❌ Use this in a server.", ephemeral=True)
        return

    # role lock
    if not has_giveaway_role(interaction.user):
        await interaction.response.send_message("❌ You need the giveaway host role to use this.", ephemeral=True)
        return

    seconds = parse_duration_to_seconds(duration)
    if seconds is None:
        await interaction.response.send_message(
            "❌ Invalid duration. Examples: `1h30m`, `45m`, `90s`, or `3600`.",
            ephemeral=True
        )
        return

    host = host or interaction.user

    # parse extra entries
    parsed_extra = {}
    if extra_entries:
        parts = [p.strip() for p in extra_entries.split(",") if p.strip()]
        for p in parts:
            cleaned = p.replace("<@&", "").replace(">", "").strip()
            if ":" in cleaned:
                rid_str, bonus_str = cleaned.split(":", 1)
                try:
                    rid = int(rid_str.strip())
                    bonus = int(bonus_str.strip())
                    if bonus > 0:
                        parsed_extra[rid] = bonus
                except Exception:
                    logger.warning("Couldn't parse extra entry: %s", p)

    # build embed
    ends_at = datetime.utcnow() + timedelta(seconds=seconds)
    ends_str = discord.utils.format_dt(ends_at, style="R")  # relative time
    embed = discord.Embed(
        title="🎉 Giveaway Started!",
        description=(
            f"**Prize:** {prize}\n"
            f"React with 🎉 to enter.\n"
            f"**Ends:** {ends_str}\n"
            f"**Winners:** {winners}"
        ),
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Host", value=host.mention, inline=True)
    embed.add_field(name="Required Role", value=(required_role.mention if required_role else "None"), inline=True)
    embed.add_field(name="Extra Entries", value=extra_entries_field_text(parsed_extra), inline=False)
    embed.add_field(name="Community", value=f"[Join our Roblox group]({ROBLOX_GROUP_URL})", inline=False)
    embed.set_footer(text=FOOTER_TEXT)

    # send confirmation + post
    await interaction.response.send_message(f"✅ Giveaway posted in {channel.mention}", ephemeral=True)

    try:
        gw_msg = await channel.send(embed=embed)
        await gw_msg.add_reaction("🎉")
    except Exception:
        logger.exception("Failed to send giveaway message or add reaction")
        await interaction.followup.send("❌ I couldn't post in that channel. Check my perms.", ephemeral=True)
        return

    # store runtime state
    bot.giveaways[gw_msg.id] = {
        "prize": prize,
        "channel_id": channel.id,
        "host_id": host.id,
        "required_role_id": required_role.id if required_role else None,
        "extra_roles": parsed_extra,
        "winners": int(winners),
        "ends_at": ends_at,
        "ended": False,
    }

    # schedule end
    async def _auto_end(mid: int, chan_id: int, wait_s: int):
        try:
            await asyncio.sleep(wait_s)
            chan = bot.get_channel(chan_id) or await bot.fetch_channel(chan_id)
            if chan:
                await end_giveaway(chan, mid)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Auto-end failed for giveaway %s", mid)

    bot.giveaways[gw_msg.id]["task"] = asyncio.create_task(_auto_end(gw_msg.id, channel.id, seconds))


async def end_giveaway(channel: discord.TextChannel, message_id: int):
    gw = bot.giveaways.get(message_id)
    if not gw or gw.get("ended"):
        return

    try:
        msg = await channel.fetch_message(message_id)
    except Exception:
        logger.exception("Failed to fetch giveaway message %s", message_id)
        gw["ended"] = True
        return

    reaction = discord.utils.get(msg.reactions, emoji="🎉")
    if not reaction:
        await channel.send("❌ No one entered the giveaway.")
        gw["ended"] = True
        return

    users = await fetch_reaction_users(reaction)
    users = [u for u in users if not u.bot]

    # build weighted list
    weighted = []
    for u in users:
        m = channel.guild.get_member(u.id)
        if not m:
            continue
        # required role gate
        req_id = gw.get("required_role_id")
        if req_id and not discord.utils.get(m.roles, id=req_id):
            continue

        entries = 1
        # global bonuses
        for rid, bonus in BONUS_ROLES.items():
            if discord.utils.get(m.roles, id=rid):
                entries += bonus
        # per-giveaway bonuses
        for rid, bonus in gw.get("extra_roles", {}).items():
            if discord.utils.get(m.roles, id=rid):
                entries += bonus

        if entries > 0:
            weighted.extend([m] * entries)

    if not weighted:
        await channel.send("❌ No eligible entries after requirements/bonuses.")
        gw["ended"] = True
        return

    winners_to_pick = min(gw["winners"], len(set(m.id for m in weighted)))
    winners = []
    for _ in range(winners_to_pick):
        pick = random.choice(weighted)
        winners.append(pick)
        # remove all of winner's entries to avoid duplicate wins
        weighted = [m for m in weighted if m.id != pick.id]
        if not weighted:
            break

    mentions = ", ".join(w.mention for w in winners)
    prize = gw["prize"]
    result_embed = discord.Embed(
        title="🎉 Giveaway Ended!",
        description=f"**Prize:** {prize}\n**Winner(s):** {mentions}",
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    result_embed.add_field(name="Community", value=f"[Join our Roblox group]({ROBLOX_GROUP_URL})", inline=False)
    result_embed.set_footer(text=FOOTER_TEXT)
    await channel.send(embed=result_embed)

    # mark ended + cancel task
    if t := gw.get("task"):
        try:
            t.cancel()
        except Exception:
            pass
    gw["ended"] = True


@giveaway_group.command(name="end", description="End a giveaway early (role-locked)")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(message_id="Message ID of the giveaway message")
async def giveaway_end(interaction: discord.Interaction, message_id: str):
    if not interaction.guild:
        await interaction.response.send_message("❌ Use this in a server.", ephemeral=True)
        return
    if not has_giveaway_role(interaction.user):
        await interaction.response.send_message("❌ You need the giveaway host role.", ephemeral=True)
        return
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)
        return

    gw = bot.giveaways.get(mid)
    if not gw:
        await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)
        return

    chan = interaction.guild.get_channel(gw["channel_id"])
    if not chan:
        await interaction.response.send_message("❌ Giveaway channel not found.", ephemeral=True)
        return

    await interaction.response.send_message("✅ Ending giveaway…", ephemeral=True)
    await end_giveaway(chan, mid)


@giveaway_group.command(name="reroll", description="Reroll winners (role-locked)")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(message_id="Message ID of a finished giveaway")
async def giveaway_reroll(interaction: discord.Interaction, message_id: str):
    if not interaction.guild:
        await interaction.response.send_message("❌ Use this in a server.", ephemeral=True)
        return
    if not has_giveaway_role(interaction.user):
        await interaction.response.send_message("❌ You need the giveaway host role.", ephemeral=True)
        return
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)
        return

    gw = bot.giveaways.get(mid)
    if not gw or not gw.get("ended"):
        await interaction.response.send_message("❌ Giveaway not found or not ended yet.", ephemeral=True)
        return

    chan = interaction.guild.get_channel(gw["channel_id"])
    if not chan:
        await interaction.response.send_message("❌ Giveaway channel not found.", ephemeral=True)
        return

    try:
        msg = await chan.fetch_message(mid)
    except Exception:
        await interaction.response.send_message("❌ Couldn't fetch the giveaway message.", ephemeral=True)
        return

    reaction = discord.utils.get(msg.reactions, emoji="🎉")
    if not reaction:
        await interaction.response.send_message("❌ No entries to reroll.", ephemeral=True)
        return

    users = await fetch_reaction_users(reaction)
    users = [u for u in users if not u.bot]

    weighted = []
    for u in users:
        m = chan.guild.get_member(u.id)
        if not m:
            continue
        req_id = gw.get("required_role_id")
        if req_id and not discord.utils.get(m.roles, id=req_id):
            continue

        entries = 1
        for rid, bonus in BONUS_ROLES.items():
            if discord.utils.get(m.roles, id=rid):
                entries += bonus
        for rid, bonus in gw.get("extra_roles", {}).items():
            if discord.utils.get(m.roles, id=rid):
                entries += bonus

        if entries > 0:
            weighted.extend([m] * entries)

    if not weighted:
        await interaction.response.send_message("❌ No eligible entries to reroll.", ephemeral=True)
        return

    winners_to_pick = min(gw["winners"], len(set(m.id for m in weighted)))
    winners = []
    for _ in range(winners_to_pick):
        pick = random.choice(weighted)
        winners.append(pick)
        weighted = [m for m in weighted if m.id != pick.id]
        if not weighted:
            break

    mentions = ", ".join(w.mention for w in winners)
    await interaction.response.send_message(f"🔄 New winner(s): {mentions}", ephemeral=False)

# =========================================================
#                      OTHER COMMANDS
# =========================================================
ROBLOX_USERS = "https://users.roblox.com/v1/usernames/users"

async def roblox_get_user(session, username):
    try:
        async with session.post(ROBLOX_USERS, json={"usernames": [username], "excludeBannedUsers": False}) as resp:
            data = await resp.json()
            if data.get("data"):
                return data["data"][0]
    except Exception:
        logger.exception("Roblox lookup failed")
    return None

@bot.tree.command(name="profile", description="View a Roblox user's profile")
@app_commands.guilds(discord.Object(id=GUILD_ID))
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
    link = f"https://www.roblox.com/users/{user_id}/profile"

    embed = discord.Embed(
        title=f"{display_name} — Roblox Profile",
        url=link,
        color=discord.Color.blurple(),
        description=f"[Open profile on Roblox]({link})"
    )
    embed.set_image(url=f"https://www.roblox.com/headshot-thumbnail/image?userId={user_id}&width=420&height=420&format=png")
    embed.add_field(name="Username", value=name, inline=True)
    embed.add_field(name="Display Name", value=display_name, inline=True)
    embed.add_field(name="User ID", value=str(user_id), inline=True)
    embed.add_field(name="Community", value=f"[Join our Roblox group]({ROBLOX_GROUP_URL})", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    embed.timestamp = datetime.utcnow()
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="report", description="Send a bug report to the bot owner")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(bug="Describe the bug")
async def report(interaction: discord.Interaction, bug: str):
    embed = discord.Embed(
        title="🐞 Bug Report",
        description=bug,
        color=discord.Color.red(),
        timestamp=datetime.utcnow()
    ).set_footer(text=FOOTER_TEXT)
    embed.add_field(name="Community", value=f"[Join our Roblox group]({ROBLOX_GROUP_URL})", inline=False)
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message("✅ Your bug report was sent!", ephemeral=True)
    await bot.db.execute("INSERT INTO reports (user_id, username, content) VALUES (?, ?, ?)",
                         (interaction.user.id, str(interaction.user), bug))
    await bot.db.commit()

    # DM owner
    try:
        owner = await bot.fetch_user(OWNER_ID)
        if owner:
            await owner.send(embed=embed)
    except Exception:
        logger.exception("Failed to DM owner")

@bot.tree.command(name="suggest", description="Send a suggestion to the bot owner")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(idea="Your suggestion")
async def suggest(interaction: discord.Interaction, idea: str):
    embed = discord.Embed(
        title="💡 Suggestion",
        description=idea,
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    ).set_footer(text=FOOTER_TEXT)
    embed.add_field(name="Community", value=f"[Join our Roblox group]({ROBLOX_GROUP_URL})", inline=False)
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message("✅ Your suggestion was sent!", ephemeral=True)
    await bot.db.execute("INSERT INTO suggestions (user_id, username, content) VALUES (?, ?, ?)",
                         (interaction.user.id, str(interaction.user), idea))
    await bot.db.commit()

    try:
        owner = await bot.fetch_user(OWNER_ID)
        if owner:
            await owner.send(embed=embed)
    except Exception:
        logger.exception("Failed to DM owner")

@bot.tree.command(name="help", description="Show all bot commands")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Tony Studios — Help",
        description=(
            "Use the slash commands below. Giveaway example:\n"
            "`/giveaway start duration:1h30m winners:2 prize:\"Nitro\" channel:#giveaways "
            "host:@You required_role:@Members extra_entries:123:2,456:5`"
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="🎉 /giveaway", value="start • end • reroll (host role required)", inline=False)
    embed.add_field(name="🕹️ /profile", value="View a Roblox user's profile", inline=False)
    embed.add_field(name="🐞 /report", value="Send a bug report (DMs owner)", inline=False)
    embed.add_field(name="💡 /suggest", value="Send a suggestion (DMs owner)", inline=False)
    embed.add_field(name="Community", value=f"[Join our Roblox group]({ROBLOX_GROUP_URL})", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# =========================================================
#                    COUNTING GAME
# =========================================================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.channel.id in COUNTING_CHANNEL_IDS:
        try:
            number = int(message.content.strip())
        except ValueError:
            await bot.process_commands(message)
            return

        # ensure row
        await bot.db.execute(
            "INSERT OR IGNORE INTO counting (channel_id, last_number) VALUES (?, ?)",
            (message.channel.id, 0)
        )
        await bot.db.commit()

        # get last
        async with bot.db.execute(
            "SELECT last_number FROM counting WHERE channel_id = ?",
            (message.channel.id,)
        ) as cur:
            row = await cur.fetchone()
            last = row[0] if row else 0

        if number == last + 1:
            await bot.db.execute(
                "UPDATE counting SET last_number = ? WHERE channel_id = ?",
                (number, message.channel.id)
            )
            await bot.db.commit()
            try:
                await message.add_reaction("✅")
            except Exception:
                logger.exception("Failed to react ✅")

            next_num = number + 1
            try:
                bot_msg = await message.channel.send(str(next_num))
                await bot_msg.add_reaction("✅")
                await bot.db.execute(
                    "UPDATE counting SET last_number = ? WHERE channel_id = ?",
                    (next_num, message.channel.id)
                )
                await bot.db.commit()
            except Exception:
                logger.exception("Failed to send next number")
        else:
            try:
                await message.add_reaction("❌")
            except Exception:
                logger.exception("Failed to react ❌")
            await bot.db.execute(
                "UPDATE counting SET last_number = 0 WHERE channel_id = ?",
                (message.channel.id,)
            )
            await bot.db.commit()
            try:
                await message.channel.send(f"❌ {message.author.mention} fumbled the count! Start again at **1**.")
            except Exception:
                logger.exception("Failed to send failure msg")

            role = message.guild.get_role(FAILURE_ROLE_ID) if message.guild else None
            if role:
                try:
                    await message.author.add_roles(role, reason="Failed counting game")
                except Exception:
                    logger.exception("Failed to add failure role")

    # react if bot mentioned
    if bot.user in message.mentions:
        try:
            for emoji in ["🇾", "🇪", "🇸", "❓"]:
                await message.add_reaction(emoji)
        except Exception:
            logger.exception("Failed to react to mention")

    await bot.process_commands(message)

# =========================================================
#                       LIFECYCLE
# =========================================================
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} ({bot.user.id})")

if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except discord.errors.PrivilegedIntentsRequired:
        logger.error("⚠️ Enable 'Message Content Intent' & 'Server Members Intent' in the Discord Developer Portal.")
        raise
