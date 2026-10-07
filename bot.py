import os

import discord
from discord.ext import commands
from dotenv import load_dotenv


# Load secrets from .env
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN was not found in .env")


# Discord intents
intents = discord.Intents.default()
intents.members = True
intents.message_content = True


# Create bot
bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


@bot.event
async def on_ready():
    print()
    print("=" * 45)
    print("WIGS")
    print("=" * 45)
    print(f"Logged in as: {bot.user}")
    print(f"Bot ID: {bot.user.id}")
    print(f"Connected servers: {len(bot.guilds)}")

    for guild in bot.guilds:
        print(f"  - {guild.name}")

    print()
    print("Wigs is online and ready.")
    print("=" * 45)


bot.run(TOKEN)