import os
import logging
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime
import aiohttp
import aiosqlite
import asyncio
import random

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
intents.members = True
intents.messages = True
intents.message_content = True

DB_PATH = "bot_data.db"
GUILD_ID = 984999848791126096

# Counting channels
COUNTING_CHANNEL_IDS = [1398545401598050425, 1411772929720586401]
FAILURE_ROLE_ID = 1210840031023988776

# Bonus entries per role
BONUS_ROLES = {
    1411126451163365437: 1,
    1412210602159378462: 2,
    1412212184792043530: 3,
    1412212463176388689: 4,
    1412212683515887710: 5,
    1412212741674106952: 6,
    1412212961338069022: 8
}

# Track giveaways
giveaways = {}

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
# Giveaway Commands
# -------------------------
@bot.tree.command(name="giveaway_start", description="Start a giveaway")
@app_commands.describe(prize="The prize for the giveaway", duration="Duration in seconds")
async def giveaway_start(interaction: discord.Interaction, prize: str, duration: int):
    if duration <= 0:
        await interaction.response.send_message("❌ Duration must be greater than 0 seconds!", ephemeral=True)
        return

    embed = discord.Embed(
        title="🎉 Giveaway! 🎉",
        description=f"Prize: **{prize}**\nReact with 🎉 to enter!\nEnds in {duration} seconds.",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Hosted by {interaction.user}", icon_url=interaction.user.display_avatar.url)

    await interaction.response.send_message("✅ Giveaway started!", ephemeral=True)
    msg = await interaction.channel.send(embed=embed)
    await msg.add_reaction("🎉")

    giveaways[msg.id] = {"prize": prize, "message": msg, "ended": False}

    await asyncio.sleep(duration)
    await end_giveaway(interaction.channel, msg.id)

async def end_giveaway(channel, message_id):
    if message_id not in giveaways or giveaways[message_id]["ended"]:
        return

    msg = await channel.fetch_message(message_id)
    reaction = discord.utils.get(msg.reactions, emoji="🎉")
    if not reaction:
        await channel.send("❌ No one entered the giveaway!")
        giveaways[message_id]["ended"] = True
        return

    users = await reaction.users().flatten()
    users = [u for u in users if not u.bot]
    if not users:
        await channel.send("❌ No valid users entered!")
        giveaways[message_id]["ended"] = True
        return

    weighted_entries = []
    for user in users:
        entries = 1
        member = channel.guild.get_member(user.id)
        if member:
            for role_id, bonus in BONUS_ROLES.items():
                if discord.utils.get(member.roles, id=role_id):
                    entries += bonus
        weighted_entries.extend([user] * entries)

    winner = random.choice(weighted_entries)
    await channel.send(f"🎉 Congratulations {winner.mention}! You won **{giveaways[message_id]['prize']}**!")
    giveaways[message_id]["ended"] = True

@bot.tree.command(name="giveaway_end", description="End an active giveaway")
@app_commands.describe(message_id="The message ID of the giveaway to end")
async def giveaway_end(interaction: discord.Interaction, message_id: str):
    try:
        message_id = int(message_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid message ID!", ephemeral=True)
        return
    await interaction.response.send_message("✅ Giveaway ending...", ephemeral=True)
    await end_giveaway(interaction.channel, message_id)

@bot.tree.command(name="giveaway_reroll", description="Reroll a giveaway winner")
@app_commands.describe(message_id="The message ID of the giveaway to reroll")
async def giveaway_reroll(interaction: discord.Interaction, message_id: str):
    try:
        message_id = int(message_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid message ID!", ephemeral=True)
        return

    if message_id not in giveaways or not giveaways[message_id]["ended"]:
        await interaction.response.send_message("❌ Giveaway not found or hasn't ended!", ephemeral=True)
        return

    msg = await interaction.channel.fetch_message(message_id)
    reaction = discord.utils.get(msg.reactions, emoji="🎉")
    if not reaction:
        await interaction.response.send_message("❌ No one entered the giveaway!", ephemeral=True)
        return

    users = await reaction.users().flatten()
    users = [u for u in users if not u.bot]
    weighted_entries = []
    for user in users:
        entries = 1
        member = interaction.guild.get_member(user.id)
        if member:
            for role_id, bonus in BONUS_ROLES.items():
                if discord.utils.get(member.roles, id=role_id):
                    entries += bonus
        weighted_entries.extend([user] * entries)

    winner = random.choice(weighted_entries)
    await interaction.response.send_message(f"🎉 New winner: {winner.mention}!", ephemeral=False)

# -------------------------
# Your existing commands
# -------------------------
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

# /profile
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

# /help
@bot.tree.command(name="help", description="Show all bot commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Tony Studios Bot — Help",
        description="Click commands to use them directly!",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="🕹️ /profile", value="View a Roblox user's profile", inline=False)
    embed.add_field(name="🐞 /report", value="Send a bug report", inline=False)
    embed.add_field(name="💡 /suggest", value="Send a suggestion", inline=False)
    embed.add_field(name="🎉 /giveaway_start", value="Start a giveaway", inline=False)
    embed.add_field(name="🛑 /giveaway_end", value="End a giveaway", inline=False)
    embed.add_field(name="🔄 /giveaway_reroll", value="Reroll a giveaway", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -------------------------
# Counting game
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
            await message.add_reaction("✅")
            next_num = number + 1
            bot_msg = await message.channel.send(str(next_num))
            await bot_msg.add_reaction("✅")
            await bot.db.execute("UPDATE counting SET last_number = ? WHERE channel_id = ?", (next_num, message.channel.id))
            await bot.db.commit()
        else:
            await message.add_reaction("❌")
            await bot.db.execute("UPDATE counting SET last_number = 0 WHERE channel_id = ?", (message.channel.id,))
            await bot.db.commit()
            await message.channel.send(f"❌ {message.author.mention} failed the counting game! Start again with **1**.")
            role = message.guild.get_role(FAILURE_ROLE_ID)
            if role:
                await message.author.add_roles(role, reason="Failed counting game")
    if bot.user in message.mentions:
        for emoji in ["🇾", "🇪", "🇸", "❓"]:
            await message.add_reaction(emoji)
    await bot.process_commands(message)

try:
    bot.run(TOKEN)
except discord.errors.PrivilegedIntentsRequired:
    logger.error("⚠️ Enable 'Message Content Intent' & 'Server Members Intent' in Discord Developer Portal.")
    raise
