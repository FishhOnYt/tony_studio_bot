import os
import logging
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import aiosqlite
from datetime import datetime

# ---------------------- Environment ----------------------
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")

if not TOKEN or not OWNER_ID:
    raise RuntimeError("DISCORD_TOKEN or OWNER_ID not set in environment variables")
OWNER_ID = int(OWNER_ID)

# ---------------------- Logging -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tony_bot")

# ---------------------- Intents ------------------------
intents = discord.Intents.default()
intents.guilds = True

DB_PATH = "bot_data.db"

# ---------------------- Bot Class ----------------------
class TonyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=intents)
        self.session: aiohttp.ClientSession = None
        self.db: aiosqlite.Connection = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.db = await aiosqlite.connect(DB_PATH)
        await self._ensure_tables()
        await self.tree.sync()
        logger.info("Commands synced")

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
        await self.db.commit()

    async def close(self):
        if self.session:
            await self.session.close()
        if self.db:
            await self.db.close()
        await super().close()

bot = TonyBot()

# ---------------------- Roblox API URLs ----------------------
ROBLOX_USERS = "https://users.roblox.com/v1/usernames/users"
ROBLOX_PRESENCE = "https://presence.roblox.com/v1/presence/users"
ROBLOX_BADGES = "https://badges.roblox.com/v1/users/{}/badges?limit=100"

# ---------------------- Roblox Helpers ----------------------
async def roblox_get_user(session, username):
    try:
        async with session.post(ROBLOX_USERS, json={"usernames": [username], "excludeBannedUsers": False}) as resp:
            data = await resp.json()
            if data.get("data"):
                return data["data"][0]
    except Exception:
        logger.exception("Failed Roblox user lookup")
    return None

async def roblox_get_presence(session, user_id):
    try:
        async with session.post(ROBLOX_PRESENCE, json={"userIds": [user_id]}) as resp:
            return await resp.json()
    except Exception:
        logger.exception("Failed Roblox presence")
    return None

async def roblox_get_badges(session, user_id):
    try:
        async with session.get(ROBLOX_BADGES.format(user_id)) as resp:
            return await resp.json()
    except Exception:
        logger.exception("Failed Roblox badges")
    return None

# ---------------------- DM Helper ----------------------
async def send_to_owner(embed: discord.Embed):
    owner = await bot.fetch_user(OWNER_ID)
    if owner:
        try:
            await owner.send(embed=embed)
        except Exception:
            logger.exception("Failed to DM owner")

# ---------------------- Events ----------------------
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ---------------------- Commands ----------------------

# /report
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

# /suggest
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

# /profile command with avatar + badge images
@bot.tree.command(name="profile", description="Get Roblox profile info with avatar and badges")
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

    # Presence
    presence_data = await roblox_get_presence(bot.session, user_id)
    online_text = "Unknown"
    try:
        if presence_data and presence_data.get("userPresences"):
            up = presence_data["userPresences"][0]
            online_text = "Online" if up.get("userPresenceType") != 0 else "Offline"
    except Exception:
        pass

    # Badges
    badges_data = await roblox_get_badges(bot.session, user_id)
    badges = badges_data.get("data", []) if badges_data else []

    # Main embed
    embed = discord.Embed(
        title=f"Roblox Profile — {display_name}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=f"https://www.roblox.com/headshot-thumbnail/image?userId={user_id}&width=420&height=420&format=png")
    embed.add_field(name="Username", value=name, inline=True)
    embed.add_field(name="Display Name", value=display_name, inline=True)
    embed.add_field(name="Presence", value=online_text, inline=True)
    
    # Badge images as separate fields
    if badges:
        for badge in badges[:10]:  # show first 10 badges
            embed.add_field(
                name=badge.get("name"),
                value=f"[Badge Link](https://www.roblox.com/badges/{badge.get('id')})",
                inline=True
            )
    
    await interaction.followup.send(embed=embed)

# /robloxinfo new command
@bot.tree.command(name="robloxinfo", description="Quick overview of a Roblox user")
@app_commands.describe(username="Roblox username")
async def robloxinfo(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=False)
    user = await roblox_get_user(bot.session, username)
    if not user:
        await interaction.followup.send(f"❌ Could not find Roblox user `{username}`")
        return

    user_id = user.get("id")
    display_name = user.get("displayName") or username
    name = user.get("name")

    presence_data = await roblox_get_presence(bot.session, user_id)
    online_text = "Unknown"
    try:
        if presence_data and presence_data.get("userPresences"):
            up = presence_data["userPresences"][0]
            online_text = "Online" if up.get("userPresenceType") != 0 else "Offline"
    except Exception:
        pass

    embed = discord.Embed(
        title=f"{display_name} — Quick Info",
        color=discord.Color.purple()
    )
    embed.set_thumbnail(url=f"https://www.roblox.com/headshot-thumbnail/image?userId={user_id}&width=420&height=420&format=png")
    embed.add_field(name="Username", value=name, inline=True)
    embed.add_field(name="Display Name", value=display_name, inline=True)
    embed.add_field(name="Presence", value=online_text, inline=True)
    embed.set_footer(text=f"User ID: {user_id}")

    await interaction.followup.send(embed=embed)

# /help
@bot.tree.command(name="help", description="Show all bot commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Tony Studios Bot — Help",
        description="Here are all available commands:",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="🐞 /report <bug>", value="Send a bug report to the bot owner (DM)", inline=False)
    embed.add_field(name="💡 /suggest <idea>", value="Send a suggestion to the bot owner (DM)", inline=False)
    embed.add_field(name="🕹️ /profile <username>", value="View Roblox profile with avatar and badges", inline=False)
    embed.add_field(name="📊 /robloxinfo <username>", value="Quick overview of a Roblox user", inline=False)
    embed.add_field(name="❓ /help", value="Show this help message", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------------------- Run Bot ----------------------
bot.run(TOKEN)
