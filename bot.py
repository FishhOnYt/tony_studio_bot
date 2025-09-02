# tony_bot_final.py
import os
import logging
import random
import asyncio
import re
from typing import Optional, Dict, List

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timedelta
import aiohttp
import aiosqlite

# -------------------------
# CONFIG
# -------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
if not TOKEN or not OWNER_ID:
    raise RuntimeError("DISCORD_TOKEN or OWNER_ID missing in .env")
OWNER_ID = int(OWNER_ID)

# Counting channels (IDs you use)
COUNTING_CHANNEL_IDS = [1398545401598050425, 1411772929720586401]
FAILURE_ROLE_ID = 1210840031023988776

# Role required to manage giveaways (users must have this role in that server)
GIVEAWAY_HOST_ROLE_ID = 1402405882939048076

# GLOBAL extra entries per role (role_id: bonus_entries)
BONUS_ROLES: Dict[int, int] = {
    1411126451163365437: 1,
    1412210602159378462: 2,
    1412212184792043530: 3,
    1412212463176388689: 4,
    1412212683515887710: 5,
    1412212741674106952: 6,
    1412212961338069022: 8,
}

ROBLOX_GROUP_URL = "https://www.roblox.com/share/g/84587582"
FOOTER_TEXT = f"Join my Roblox group ➜ {ROBLOX_GROUP_URL}"

DB_PATH = "bot_data.db"

# -------------------------
# LOGGING & INTENTS
# -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tony_bot")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

# -------------------------
# BOT CLASS
# -------------------------
class TonyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=intents)
        self.session: Optional[aiohttp.ClientSession] = None
        self.db: Optional[aiosqlite.Connection] = None
        # runtime giveaways store: message_id -> giveaway data
        self.giveaways: Dict[int, dict] = {}

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.db = await aiosqlite.connect(DB_PATH)
        await self._ensure_tables()

        # register giveaway group before syncing
        self.tree.add_command(giveaway_group)

        # global sync (may take a bit to propagate to all servers)
        await self.tree.sync()
        logger.info("Slash commands synced (global)")

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

# -------------------------
# HELPERS
# -------------------------
def member_has_giveaway_role(member: discord.Member) -> bool:
    return any(r.id == GIVEAWAY_HOST_ROLE_ID for r in member.roles)

def parse_duration_to_seconds(s: str) -> Optional[int]:
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

async def fetch_reaction_users(reaction: discord.Reaction) -> List[discord.User]:
    users = []
    async for u in reaction.users():
        users.append(u)
    return users

def parse_extra_entries_string(s: Optional[str]) -> Dict[int, int]:
    """
    Parse "roleid:bonus,roleid:bonus" or "<@&id>:bonus"
    """
    parsed: Dict[int, int] = {}
    if not s:
        return parsed
    parts = [p.strip() for p in s.split(",") if p.strip()]
    for p in parts:
        cleaned = p.replace("<@&", "").replace(">", "").strip()
        if ":" in cleaned:
            left, right = cleaned.split(":", 1)
            try:
                rid = int(left.strip())
                bonus = int(right.strip())
                if bonus > 0:
                    parsed[rid] = bonus
            except Exception:
                logger.warning("Couldn't parse extra entry part: %s", p)
    return parsed

def calculate_entries_for_member(member: discord.Member, gw_extra: Dict[int, int]) -> int:
    """
    Base 1 entry + bonuses from global BONUS_ROLES and gw_extra
    """
    entries = 1
    # global bonuses
    for rid, bonus in BONUS_ROLES.items():
        if discord.utils.get(member.roles, id=rid):
            entries += bonus
    # giveaway-specific bonuses
    for rid, bonus in gw_extra.items():
        if discord.utils.get(member.roles, id=rid):
            entries += bonus
    return max(1, entries)

# -------------------------
# UI: Join button + Participants dropdown
# -------------------------
class JoinButton(discord.ui.Button):
    def __init__(self, message_id: int):
        super().__init__(style=discord.ButtonStyle.success, label="Join Giveaway", emoji="🎉")
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        try:
            gw = bot.giveaways.get(self.message_id)
            if not gw:
                await interaction.response.send_message("This giveaway isn't tracked (maybe the bot restarted).", ephemeral=True)
                return

            uid = interaction.user.id

            # required role check
            req = gw.get("required_role_id")
            if req:
                member = interaction.guild.get_member(uid)
                if not member or not discord.utils.get(member.roles, id=req):
                    await interaction.response.send_message("You're missing the required role to join.", ephemeral=True)
                    return

            participants: set = gw.setdefault("participants", set())
            if uid in participants:
                await interaction.response.send_message("You're already entered!", ephemeral=True)
                return

            participants.add(uid)
            gw["participants"] = participants
            await interaction.response.send_message("✅ You've been entered into the giveaway!", ephemeral=True)
        except Exception:
            logger.exception("JoinButton callback failed")
            await interaction.response.send_message("Failed to join giveaway.", ephemeral=True)

class ParticipantsSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption], participants_map: Dict[int, int]):
        super().__init__(placeholder="Select a participant...", min_values=1, max_values=1, options=options)
        self.participants_map = participants_map

    async def callback(self, interaction: discord.Interaction):
        try:
            uid = int(self.values[0])
            member = interaction.guild.get_member(uid) if interaction.guild else None
            entries = self.participants_map.get(uid, 1)
            text = f"{member.mention if member else str(uid)} — **{entries}** entry"
            await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            logger.exception("ParticipantsSelect callback failed")
            await interaction.response.send_message("Failed to fetch participant info.", ephemeral=True)

class ParticipantsView(discord.ui.View):
    def __init__(self, message_id: int):
        super().__init__(timeout=None)
        self.message_id = message_id
        # Add Join button (message_id will be set properly before edit)
        self.add_item(JoinButton(message_id))

    @discord.ui.button(label="View Participants", style=discord.ButtonStyle.primary, emoji="📋")
    async def view_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        try:
            gw = bot.giveaways.get(self.message_id)
            if not gw:
                await interaction.response.send_message("Giveaway not found (maybe bot restarted).", ephemeral=True)
                return

            # start with participants from JoinButton entries
            participants_set = set(gw.get("participants", set()))

            # also include reaction joiners
            chan = bot.get_channel(gw["channel_id"]) or await bot.fetch_channel(gw["channel_id"])
            if chan:
                try:
                    msg = await chan.fetch_message(self.message_id)
                    reaction = discord.utils.get(msg.reactions, emoji="🎉")
                    if reaction:
                        reacted_users = await fetch_reaction_users(reaction)
                        for u in reacted_users:
                            if not u.bot:
                                participants_set.add(u.id)
                except Exception:
                    logger.debug("Couldn't fetch message or reactions for participants list")

            if not participants_set:
                await interaction.response.send_message("No participants yet.", ephemeral=True)
                return

            # Build participants options and entries map (first 25)
            options = []
            participants_map: Dict[int, int] = {}
            gw_extra = gw.get("extra_roles", {}) or {}
            i = 0
            for uid in list(participants_set):
                if i >= 25:
                    break
                # try to get Member object (guild-only)
                member = interaction.guild.get_member(uid) if interaction.guild else None
                # calculate entries using roles
                entries = 1
                if member:
                    entries = calculate_entries_for_member(member, gw_extra)
                else:
                    entries = 1
                participants_map[uid] = entries
                label = (member.display_name if isinstance(member, discord.Member) else f"User {str(uid)}")[:100]
                desc = f"{entries} entry" if entries == 1 else f"{entries} entries"
                options.append(discord.SelectOption(label=label, description=desc, value=str(uid)))
                i += 1

            select = ParticipantsSelect(options=options, participants_map=participants_map)
            view = discord.ui.View(timeout=120)
            view.add_item(select)
            note = ""
            if len(participants_set) > 25:
                note = f"Showing first 25 participants ({len(participants_set)} total)."
            await interaction.response.send_message(content=note, view=view, ephemeral=True)
        except Exception:
            logger.exception("View Participants callback failed")
            await interaction.response.send_message("Could not load participants.", ephemeral=True)

# -------------------------
# GIVEAWAY GROUP
# -------------------------
giveaway_group = app_commands.Group(name="giveaway", description="Giveaway commands (button-based)")

@giveaway_group.command(name="start", description="Start a giveaway (host role required)")
@app_commands.describe(
    duration="Duration (e.g. 1h30m, 45m, 90s, or seconds)",
    winners="Winners (1-10)",
    prize="Prize text",
    channel="Channel to post giveaway in",
    host="Host (defaults to you)",
    required_role="Role required to enter (optional)",
    extra_entries="Optional extra entries roleid:bonus,roleid:bonus (IDs or <@&id>)"
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
    if not interaction.guild:
        await interaction.response.send_message("❌ Use this in a server.", ephemeral=True)
        return

    member = interaction.guild.get_member(interaction.user.id)
    if not member or not member_has_giveaway_role(member):
        await interaction.response.send_message("❌ You need the giveaway host role to use this.", ephemeral=True)
        return

    seconds = parse_duration_to_seconds(duration)
    if seconds is None:
        await interaction.response.send_message("❌ Invalid duration. Examples: `1h30m`, `45m`, `90s`, or `3600`.", ephemeral=True)
        return

    host = host or interaction.user
    gw_extra = parse_extra_entries_string(extra_entries)

    ends_at = datetime.utcnow() + timedelta(seconds=seconds)
    rel_ends = discord.utils.format_dt(ends_at, style="R")

    embed = discord.Embed(
        title="🎉 Giveaway Started!",
        description=f"**Prize:** {prize}\n**Ends:** {rel_ends}\n**Winners:** {winners}\nPress **Join Giveaway** to enter, or react with 🎉.",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Host", value=f"{host.mention}", inline=True)
    embed.add_field(name="Required Role", value=(required_role.mention if required_role else "None"), inline=True)
    # show extra entries summary if provided or global bonuses exist
    extras_text = "None"
    merged_extras = dict(BONUS_ROLES)
    merged_extras.update(gw_extra)
    if merged_extras:
        extras_text = "\n".join(f"<@&{rid}>: +{bonus}" for rid, bonus in merged_extras.items())
    embed.add_field(name="Extra Entries", value=extras_text, inline=False)
    embed.add_field(name="Community", value=f"[Join our Roblox group]({ROBLOX_GROUP_URL})", inline=False)
    embed.set_footer(text=FOOTER_TEXT)

    await interaction.response.send_message(f"✅ Giveaway posted in {channel.mention}", ephemeral=True)

    # send message first (without view), then attach a view with correct message id
    try:
        gw_msg = await channel.send(embed=embed)
    except Exception:
        logger.exception("Failed to post giveaway message")
        await interaction.followup.send("❌ I couldn't post in that channel. Check my perms.", ephemeral=True)
        return

    # add reaction for people who prefer reactions
    try:
        await gw_msg.add_reaction("🎉")
    except Exception:
        logger.debug("Could not add 🎉 reaction (missing perms?)")

    # build view and attach by editing message (so view has correct message id)
    view = ParticipantsView(message_id=gw_msg.id)
    # ensure JoinButton message_id is correct
    for item in view.children:
        if isinstance(item, JoinButton):
            item.message_id = gw_msg.id

    try:
        await gw_msg.edit(view=view)
    except Exception:
        # sometimes editing fails if view not allowed; still proceed
        logger.debug("Could not attach view to message by edit; trying to send view separately")
        # send view in channel so users have access (works but not attached to message)
        try:
            await channel.send("Giveaway controls:", view=view)
        except Exception:
            logger.debug("Couldn't send separate view either")

    # store giveaway state
    bot.giveaways[gw_msg.id] = {
        "prize": prize,
        "channel_id": channel.id,
        "host_id": getattr(host, "id", None),
        "required_role_id": required_role.id if required_role else None,
        "extra_roles": gw_extra,  # per-giveaway extra entries
        "winners": int(winners),
        "ends_at": ends_at,
        "participants": set(),  # user ids from JoinButton
        "ended": False,
        "task": None
    }

    # schedule auto-end
    async def _auto_end(mid: int, chan_id: int, wait_s: int):
        try:
            await asyncio.sleep(wait_s)
            chan = bot.get_channel(chan_id) or await bot.fetch_channel(chan_id)
            if chan:
                await end_giveaway(chan, mid)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Auto-end failed for giveaway %s", mid)

    task = asyncio.create_task(_auto_end(gw_msg.id, channel.id, seconds))
    bot.giveaways[gw_msg.id]["task"] = task

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

    # participants: union of JoinButton participants + reaction users
    participants_ids = set(gw.get("participants", set()))
    reaction = discord.utils.get(msg.reactions, emoji="🎉")
    if reaction:
        reacted_users = await fetch_reaction_users(reaction)
        for u in reacted_users:
            if not u.bot:
                participants_ids.add(u.id)

    # filter required role & compute entries
    eligible_list = []
    gw_extra = gw.get("extra_roles", {}) or {}
    for uid in participants_ids:
        m = channel.guild.get_member(uid)
        if not m:
            continue
        req_id = gw.get("required_role_id")
        if req_id and not discord.utils.get(m.roles, id=req_id):
            continue
        eligible_list.append((uid, calculate_entries_for_member(m, gw_extra)))

    if not eligible_list:
        await channel.send("❌ No eligible entries.")
        gw["ended"] = True
        return

    # build weighted pool
    pool = []
    for uid, entries in eligible_list:
        pool.extend([uid] * entries)

    winners_count = min(gw["winners"], len(set(pool)))
    winners_ids = []
    for _ in range(winners_count):
        pick = random.choice(pool)
        winners_ids.append(pick)
        pool = [x for x in pool if x != pick]
        if not pool:
            break

    mentions = []
    for uid in winners_ids:
        member = channel.guild.get_member(uid)
        mentions.append(member.mention if member else f"<@{uid}>")

    host_id = gw.get("host_id")
    host_mention = f"<@{host_id}>" if host_id else "the host"

    result_embed = discord.Embed(
        title="🎉 Giveaway Ended!",
        description=f"**Prize:** {gw['prize']}\n**Winner(s):** {', '.join(mentions)}",
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    result_embed.add_field(name="Claim", value=f"DM {host_mention} to claim your prize!", inline=False)
    result_embed.add_field(name="Community", value=f"[Join our Roblox group]({ROBLOX_GROUP_URL})", inline=False)
    result_embed.set_footer(text=FOOTER_TEXT)
    await channel.send(embed=result_embed)

    # cancel auto-task
    if t := gw.get("task"):
        try:
            t.cancel()
        except Exception:
            pass
    gw["ended"] = True

@giveaway_group.command(name="end", description="End a giveaway early (host role required)")
@app_commands.describe(message_id="Message ID of the giveaway message")
async def giveaway_end(interaction: discord.Interaction, message_id: str):
    if not interaction.guild:
        await interaction.response.send_message("❌ Use this in a server.", ephemeral=True)
        return
    member = interaction.guild.get_member(interaction.user.id)
    if not member or not member_has_giveaway_role(member):
        await interaction.response.send_message("❌ You need the giveaway host role to use this.", ephemeral=True)
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

@giveaway_group.command(name="reroll", description="Reroll winners for a finished giveaway (host role required)")
@app_commands.describe(message_id="Message ID of the finished giveaway")
async def giveaway_reroll(interaction: discord.Interaction, message_id: str):
    if not interaction.guild:
        await interaction.response.send_message("❌ Use this in a server.", ephemeral=True)
        return
    member = interaction.guild.get_member(interaction.user.id)
    if not member or not member_has_giveaway_role(member):
        await interaction.response.send_message("❌ You need the giveaway host role to use this.", ephemeral=True)
        return
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)
        return
    gw = bot.giveaways.get(mid)
    if not gw or not gw.get("ended"):
        await interaction.response.send_message("❌ Giveaway not found or hasn't ended yet.", ephemeral=True)
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

    participants_ids = set(gw.get("participants", set()))
    reaction = discord.utils.get(msg.reactions, emoji="🎉")
    if reaction:
        reacted_users = await fetch_reaction_users(reaction)
        for u in reacted_users:
            if not u.bot:
                participants_ids.add(u.id)

    gw_extra = gw.get("extra_roles", {}) or {}
    eligible_list = []
    for uid in participants_ids:
        m = chan.guild.get_member(uid)
        if not m:
            continue
        req_id = gw.get("required_role_id")
        if req_id and not discord.utils.get(m.roles, id=req_id):
            continue
        eligible_list.append((uid, calculate_entries_for_member(m, gw_extra)))

    if not eligible_list:
        await interaction.response.send_message("❌ No eligible entries to reroll.", ephemeral=True)
        return

    pool = []
    for uid, entries in eligible_list:
        pool.extend([uid] * entries)

    winners_count = min(gw["winners"], len(set(pool)))
    winners_ids = []
    for _ in range(winners_count):
        pick = random.choice(pool)
        winners_ids.append(pick)
        pool = [x for x in pool if x != pick]
        if not pool:
            break

    mentions = []
    for uid in winners_ids:
        m = chan.guild.get_member(uid)
        mentions.append(m.mention if m else f"<@{uid}>")

    await interaction.response.send_message(f"🔄 New winner(s): {', '.join(mentions)}", ephemeral=False)

# -------------------------
# Other commands (profile, report, suggest, help)
# -------------------------
ROBLOX_USERS = "https://users.roblox.com/v1/usernames/users"

async def roblox_get_user(session: aiohttp.ClientSession, username: str) -> Optional[dict]:
    try:
        async with session.post(ROBLOX_USERS, json={"usernames": [username], "excludeBannedUsers": False}) as resp:
            data = await resp.json()
            if data.get("data"):
                return data["data"][0]
    except Exception:
        logger.exception("Roblox lookup failed")
    return None

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
@app_commands.describe(bug="Describe the bug")
async def report(interaction: discord.Interaction, bug: str):
    embed = discord.Embed(title="🐞 Bug Report", description=bug, color=discord.Color.red(), timestamp=datetime.utcnow())
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="Community", value=f"[Join our Roblox group]({ROBLOX_GROUP_URL})", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    await interaction.response.send_message("✅ Your bug report was sent!", ephemeral=True)
    await bot.db.execute("INSERT INTO reports (user_id, username, content) VALUES (?, ?, ?)", (interaction.user.id, str(interaction.user), bug))
    await bot.db.commit()
    try:
        owner = await bot.fetch_user(OWNER_ID)
        if owner:
            await owner.send(embed=embed)
    except Exception:
        logger.exception("Failed to DM owner")

@bot.tree.command(name="suggest", description="Send a suggestion to the bot owner")
@app_commands.describe(idea="Your suggestion")
async def suggest(interaction: discord.Interaction, idea: str):
    embed = discord.Embed(title="💡 Suggestion", description=idea, color=discord.Color.green(), timestamp=datetime.utcnow())
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="Community", value=f"[Join our Roblox group]({ROBLOX_GROUP_URL})", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    await interaction.response.send_message("✅ Your suggestion was sent!", ephemeral=True)
    await bot.db.execute("INSERT INTO suggestions (user_id, username, content) VALUES (?, ?, ?)", (interaction.user.id, str(interaction.user), idea))
    await bot.db.commit()
    try:
        owner = await bot.fetch_user(OWNER_ID)
        if owner:
            await owner.send(embed=embed)
    except Exception:
        logger.exception("Failed to DM owner")

@bot.tree.command(name="help", description="Show all bot commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Tony Studios — Help",
        description=(
            "Giveaway example:\n"
            "`/giveaway start duration:1h winners:2 prize:\"Nitro\" channel:#giveaways host:@You required_role:@Members extra_entries:123:2,456:5`"
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

# -------------------------
# COUNTING GAME
# -------------------------
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

        await bot.db.execute("INSERT OR IGNORE INTO counting (channel_id, last_number) VALUES (?, ?)", (message.channel.id, 0))
        await bot.db.commit()

        async with bot.db.execute("SELECT last_number FROM counting WHERE channel_id = ?", (message.channel.id,)) as cur:
            row = await cur.fetchone()
            last = row[0] if row else 0

        if number == last + 1:
            await bot.db.execute("UPDATE counting SET last_number = ? WHERE channel_id = ?", (number, message.channel.id))
            await bot.db.commit()
            try:
                await message.add_reaction("✅")
            except Exception:
                logger.exception("Failed to react ✅")

            next_num = number + 1
            try:
                bot_msg = await message.channel.send(str(next_num))
                await bot_msg.add_reaction("✅")
                await bot.db.execute("UPDATE counting SET last_number = ? WHERE channel_id = ?", (next_num, message.channel.id))
                await bot.db.commit()
            except Exception:
                logger.exception("Failed to send next number")
        else:
            try:
                await message.add_reaction("❌")
            except Exception:
                logger.exception("Failed to react ❌")
            await bot.db.execute("UPDATE counting SET last_number = 0 WHERE channel_id = ?", (message.channel.id,))
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

# -------------------------
# LIFECYCLE
# -------------------------
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} ({bot.user.id})")

if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except discord.errors.PrivilegedIntentsRequired:
        logger.error("⚠️ Enable 'Message Content Intent' & 'Server Members Intent' in the Discord Developer Portal.")
        raise
