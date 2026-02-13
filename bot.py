#get bot token from environment variables
import os
#core library
import discord
#gives commands + cog loading
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")

#Intents VERY IMPORTANT!
intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True

# REQUIRED to receive message events at all
intents.messages = True
intents.guilds = True
# optional but useful if you ever DM-test:
intents.dm_messages = True

#The Bot Object
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")
    print("Guilds I can see:", [f"{g.name} ({g.id})" for g in bot.guilds])

@bot.event
async def on_message(message):
    # ignore ourselves
    if message.author.bot:
        return

    print(f"[MSG] #{getattr(message.channel, 'name', 'dm')} {message.author}: {message.content!r}")
    await bot.process_commands(message)  # IMPORTANT: keeps !commands working

async def main():
    async with bot:
        #load the JoinSound cog (with debug)
        try:
            await bot.load_extension("cogs.joinsound")
            print("Loaded cogs.joinsound OK")
        except Exception as e:
            print("Failed to load cogs.joinsound:", e)
            raise

        await bot.start(TOKEN)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
