import os
import logging
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime
import aiohttp
import aiosqlite

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
GUILD_ID = 984999848791126096  # Your server ID for instant sync

class TonyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=intents)
        self.session: aiohttp.ClientSession = None
        self.db: aiosqlite.Connection = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.db = await aiosqlite.connect(DB_PATH)
        await self._ensure_tables()

        # Sync commands to your server instantly
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
        await self.db.commit()

    async def close(self):
        if self.session:
            await self.session.close()
        if self.db:
            await self.db.close()
        await super().close()

bot = TonyBot()

# Roblox API
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

# /profile command — premium look
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

# /help command — premium style
@bot.tree.command(name="help", description="Show all bot commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Tony Studios Bot — Help",
        description="Click commands to use them directly!",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="🕹️ /profile <username>", value="View a Roblox user's profile with avatar & link", inline=False)
    embed.add_field(name="🐞 /report <bug>", value="Send a bug report to the bot owner via DM", inline=False)
    embed.add_field(name="💡 /suggest <idea>", value="Send a suggestion to the bot owner via DM", inline=False)
    embed.add_field(name="❓ /help", value="Show this help message", inline=False)
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.display_avatar.url)

    await interaction.response.send_message(embed=embed, ephemeral=True)
@bot.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == bot.user:
        return

    # Check if the bot was mentioned
    if bot.user in message.mentions:
        try:
            # React with the letters: Y, E, S
            emojis = ["🇾", "🇪", "🇸", "?"]
            for emoji in emojis:
                await message.add_reaction(emoji)
        except Exception:
            logger.exception("Failed to react to mention")

    # Process commands normally
    await bot.process_commands(message)

# Run the bot
bot.run(TOKEN)


