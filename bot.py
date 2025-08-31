import os
import logging
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import aiosqlite
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
if not TOKEN or not OWNER_ID:
    raise RuntimeError("DISCORD_TOKEN or OWNER_ID not set in .env")
OWNER_ID = int(OWNER_ID)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tony_bot")

# Intents
intents = discord.Intents.default()
intents.guilds = True

DB_PATH = "bot_data.db"

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

# Roblox API URLs
ROBLOX_USERS = "https://users.roblox.com/v1/usernames/users"
ROBLOX_PRESENCE = "https://presence.roblox.com/v1/presence/users"
ROBLOX_GROUPS = "https://groups.roblox.com/v1/groups/{}/roles"
ROBLOX_BADGES = "https://badges.roblox.com/v1/users/{}/badges?limit=100"
ROBLOX_GAMES = "https://games.roblox.com/v1/users/{}/games?limit=100"

# Roblox helpers
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

async def roblox_get_group_roles(session, group_id):
    try:
        async with session.get(ROBLOX_GROUPS.format(group_id)) as resp:
            return await resp.json()
    except Exception:
        logger.exception("Failed Roblox group roles")
    return None

async def roblox_get_badges(session, user_id):
    try:
        async with session.get(ROBLOX_BADGES.format(user_id)) as resp:
            return await resp.json()
    except Exception:
        logger.exception("Failed Roblox badges")
    return None

async def roblox_get_games(session, user_id):
    try:
        async with session.get(ROBLOX_GAMES.format(user_id)) as resp:
            return await resp.json()
    except Exception:
        logger.exception("Failed Roblox games")
    return None

# DM helper
async def send_to_owner(embed: discord.Embed):
    owner = await bot.fetch_user(OWNER_ID)
    if owner:
        try:
            await owner.send(embed=embed)
        except Exception:
            logger.exception("Failed to DM owner")

# Events
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

# /report command
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

# /suggest command
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

# /help command
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
    embed.add_field(name="🕹️ /profile <username>", value="View Roblox profile with avatar, badges, and games (scrollable)", inline=False)
    embed.add_field(name="👥 /grouproles <group_id>", value="View Roblox group roles", inline=False)
    embed.add_field(name="❓ /help", value="Show this help message", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# /profile command with avatar, badges & games pagination
@bot.tree.command(name="profile", description="Get Roblox profile info")
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

    # Games
    games_data = await roblox_get_games(bot.session, user_id)
    games = games_data.get("data", []) if games_data else []

    # Build embeds
    embeds = []

    # Main profile embed
    main_embed = discord.Embed(
        title=f"Roblox Profile — {display_name}",
        color=discord.Color.blue()
    )
    main_embed.set_thumbnail(url=f"https://www.roblox.com/headshot-thumbnail/image?userId={user_id}&width=420&height=420&format=png")
    main_embed.add_field(name="Username", value=name, inline=True)
    main_embed.add_field(name="Display Name", value=display_name, inline=True)
    main_embed.add_field(name="User ID", value=str(user_id), inline=True)
    main_embed.add_field(name="Presence", value=online_text, inline=True)
    main_embed.add_field(name="Badges", value=str(len(badges)), inline=True)
    main_embed.add_field(name="Games Played", value=str(len(games)), inline=True)
    embeds.append(main_embed)

    # Badges embeds
    for i in range(0, len(badges), 20):
        embed = discord.Embed(
            title=f"{display_name}'s Badges",
            color=discord.Color.gold()
        )
        for badge in badges[i:i+20]:
            embed.add_field(name=badge.get("name"), value=f"Badge ID: {badge.get('id')}", inline=True)
        embeds.append(embed)

    # Games embeds
    for i in range(0, len(games), 20):
        embed = discord.Embed(
            title=f"{display_name}'s Games",
            color=discord.Color.green()
        )
        for game in games[i:i+20]:
            embed.add_field(name=game.get("name"), value=f"Place ID: {game.get('id')}", inline=True)
        embeds.append(embed)

    # Send first embed
    message = await interaction.followup.send(embed=embeds[0])

    # Pagination
    if len(embeds) > 1:
        await message.add_reaction("⬅️")
        await message.add_reaction("➡️")

        current_page = 0

        def check(reaction, user):
            return user == interaction.user and str(reaction.emoji) in ["⬅️", "➡️"] and reaction.message.id == message.id

        while True:
            try:
                reaction, user = await bot.wait_for("reaction_add", timeout=120.0, check=check)
                if str(reaction.emoji) == "➡️":
                    current_page = (current_page + 1) % len(embeds)
                elif str(reaction.emoji) == "⬅️":
                    current_page = (current_page - 1) % len(embeds)
                await message.edit(embed=embeds[current_page])
                await message.remove_reaction(reaction, user)
            except Exception:
                break

# /grouproles command
@bot.tree.command(name="grouproles", description="List Roblox group roles")
@app_commands.describe(group_id="Roblox group ID")
async def grouproles(interaction: discord.Interaction, group_id: int):
    await interaction.response.defer(ephemeral=True)
    data = await roblox_get_group_roles(bot.session, group_id)
    if not data or not data.get("roles"):
        await interaction.followup.send("❌ Could not fetch group roles or no roles found.")
        return
    roles = data["roles"]
    embed = discord.Embed(title=f"Roblox Group Roles — {group_id}", color=discord.Color.purple())
    for r in roles[:10]:
        embed.add_field(name=r.get("name"), value=f"Rank: {r.get('rank')} — RoleId: {r.get('id')}", inline=False)
    if len(roles) > 10:
        embed.set_footer(text=f"And {len(roles)-10} more roles...")
    await interaction.followup.send(embed=embed)

# Run the bot
bot.run(TOKEN)
