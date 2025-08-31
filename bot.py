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
intents.members = True          # needed for role management
intents.messages = True         # needed for on_message
intents.message_content = True  # privileged intent for reading message content

DB_PATH = "bot_data.db"
GUILD_ID = 984999848791126096
COUNTING_CHANNEL_ID = 1398545401598050425
FAILURE_ROLE_ID = 1210840031023988776

class TonyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=intents)
        self.session: aiohttp.ClientSession = None
        self.db: aiosqlite.Connection = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.db = await aiosqlite.connect(DB_PATH)
        await self._ensure_tables()

        # Sync commands to specific guild for instant availability
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

# /profile command — Roblox lookup
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

# /help command
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


# -------------------------
# Counting game logic
# -------------------------
@bot.event
async def on_message(message):
    # Ignore non-guild or bot messages
    if message.author.bot:
        return

    # Only run counting logic in the specified channel
    if message.channel.id == COUNTING_CHANNEL_ID:
        try:
            number = int(message.content.strip())
        except ValueError:
            # ignore non-integer messages
            await bot.process_commands(message)
            return

        # Ensure a row exists for this channel (creates if missing)
        await bot.db.execute("INSERT OR IGNORE INTO counting (channel_id, last_number) VALUES (?, ?)", (message.channel.id, 0))
        await bot.db.commit()

        # Fetch last_number
        async with bot.db.execute("SELECT last_number FROM counting WHERE channel_id = ?", (message.channel.id,)) as cursor:
            row = await cursor.fetchone()
            last_number = row[0] if row else 0

        # If the user sent the expected next number (start at 1)
        if number == last_number + 1:
            # Update DB with user's number
            await bot.db.execute("UPDATE counting SET last_number = ? WHERE channel_id = ?", (number, message.channel.id))
            await bot.db.commit()

            # React to user's correct message
            try:
                await message.add_reaction("✅")
            except Exception:
                logger.exception("Couldn't react to user's message")

            # Bot posts the next number
            next_num = number + 1
            try:
                bot_msg = await message.channel.send(str(next_num))
            except Exception:
                logger.exception("Failed to send next number")
                bot_msg = None

            # If bot posted, record the bot's number as counted (so users should say next_num+1)
            if bot_msg:
                try:
                    # react to the bot message
                    await bot_msg.add_reaction("✅")
                except Exception:
                    logger.exception("Couldn't react to bot message")
                # store bot's number as last_number
                try:
                    await bot.db.execute("UPDATE counting SET last_number = ? WHERE channel_id = ?", (next_num, message.channel.id))
                    await bot.db.commit()
                except Exception:
                    logger.exception("Failed to update last_number to bot's number")

        else:
            # Wrong number: reset to 0, react, announce, give failure role
            try:
                await message.add_reaction("❌")
            except Exception:
                logger.exception("Couldn't react with ❌")

            # reset DB to 0 (so next correct start is 1)
            try:
                await bot.db.execute("UPDATE counting SET last_number = 0 WHERE channel_id = ?", (message.channel.id,))
                await bot.db.commit()
            except Exception:
                logger.exception("Failed to reset counting in DB")

            # announce who failed
            try:
                await message.channel.send(f"❌ {message.author.mention} failed the counting game! Start again with **1**.")
            except Exception:
                logger.exception("Failed to send failure announcement")

            # give failure role if possible
            guild = message.guild
            if guild:
                role = guild.get_role(FAILURE_ROLE_ID)
                if role:
                    try:
                        await message.author.add_roles(role, reason="Failed counting game")
                    except Exception:
                        logger.exception(f"Failed to give role {FAILURE_ROLE_ID} to {message.author}")

    # React if bot mentioned (keeps previous behavior)
    if bot.user in message.mentions:
        try:
            for emoji in ["🇾", "🇪", "🇸", "❓"]:
                await message.add_reaction(emoji)
        except Exception:
            logger.exception("Failed to react to mention")

    # allow commands to be processed as normal
    await bot.process_commands(message)


# Run the bot with helpful error if privileged intents are missing
try:
    bot.run(TOKEN)
except discord.errors.PrivilegedIntentsRequired:
    logger.error("⚠️ Privileged Intents are missing! Enable 'Message Content Intent' (and 'Server Members Intent' if needed) in the Discord Developer Portal for your bot, then restart.")
    raise
