'''
PURES OVERVIEW please dont touch
how will this flow?
starts
loads saved config
connects to configured vc (if exists)
waits for someone to join channel
checks cooldown
if outside of cooldown play audio
goes back to waiting
'''


#get bot token from environment variables
import os
#save/load which channel is configured
import json
#handle cooldown timings
import time
#required because Discord.py is async
import asyncio
#core library
import discord
#gives !setjoinchannel requested by Psykzz
from discord.ext import commands
#adds background loop (guard) for reconnect/leave behaviour
from discord.ext import tasks

from pathlib import Path



class JoinSound(commands.Cog):
    #we wrap everything in a cog class so it can be loaded/unloaded cleanly

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        #Config variables

        #easy tweak points for cooldown, audio or config filename
        self.AUDIO_FILE = "join.mp3"
        self.COOLDOWN_SECONDS = 15
        self.CONFIG_FILE = "join_config.json"

        #soundboard folder + per-user mapping file (Option A)
        #users will pick 1 sound from the approved list in the sounds folder
        self.SOUNDS_DIR = "sounds"
        self.USER_SOUNDS_FILE = "user_sounds.json"

        #stores user choices: user_id(str) -> filename(str)
        self.user_sounds = {}

        #ensure sounds folder exists
        Path(self.SOUNDS_DIR).mkdir(parents=True, exist_ok=True)

        #if we want to exclude nitro users (to avoid audio clash)
        #we need to make a new role in disc e.g. NitroUser
        #then find the id for NitroUser role
        #EXCLUDED_ROLE_IDS = [123456789012345678]  # replace with NitroUser role ID & uncomment
        #incase we go this route already added if statement to on_voice_state_update to not trigger for excluded members
        self.EXCLUDED_ROLE_IDS = []  # keep empty if not using

        #how often to check if bot should reconnect/leave
        self.GUARD_INTERVAL_SECONDS = 15
        #when channel becomes empty of humans, wait this long before leaving
        self.LEAVE_GRACE_SECONDS = 10

        # In-memory state
        self.config = {}  # guild_id (str) -> {"channel_id": int}
        self.last_play_time = {}  # guild_id (int) -> float
        self.play_lock = {}  # guild_id (int) -> asyncio.Lock

        #using guild ID on the off chance the bot is in multiple servers.
        #can resort back to last_play_time instead if guild.id doesnt function as intended
        #using guild ID provides each server with its own channel, cooldown & lock

        #new: used to schedule leaving after a short grace period
        self.leave_tasks = {}  # guild_id (int) -> asyncio.Task

        #load saved config when cog loads
        self.load_config()

        #load saved user sound choices
        self.load_user_sounds()


    #opens join_config.json, loads saved channel IDs into memory, if file doesnt exist starts empty
    def load_config(self):
        try:
            with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        except FileNotFoundError:
            self.config = {}
        except json.JSONDecodeError:
            self.config = {}


    #writes the config dictionary back to disk
    #removing this would reset the channel every time the bot restarts
    def save_config(self):
        with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)


    #opens user_sounds.json, loads saved user sound selections, if file doesnt exist starts empty
    def load_user_sounds(self):
        try:
            with open(self.USER_SOUNDS_FILE, "r", encoding="utf-8") as f:
                self.user_sounds = json.load(f)
        except FileNotFoundError:
            self.user_sounds = {}
        except json.JSONDecodeError:
            self.user_sounds = {}


    #writes the user_sounds dictionary back to disk
    def save_user_sounds(self):
        with open(self.USER_SOUNDS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.user_sounds, f, indent=2)


    #gets list of approved sound files from SOUNDS_DIR
    def list_available_sounds(self) -> list[str]:
        allowed = {".mp3", ".wav", ".ogg"}
        p = Path(self.SOUNDS_DIR)
        if not p.exists():
            return []

        files = []
        for f in p.iterdir():
            if f.is_file() and f.suffix.lower() in allowed:
                files.append(f.name)

        #sort for nicer output
        files.sort(key=str.lower)
        return files


    #resolve a user input like "bruh" into an actual filename in sounds folder
    def resolve_sound_choice(self, choice: str) -> str | None:
        choice = choice.strip()
        available = self.list_available_sounds()
        if not available:
            return None

        #allow user to type "bruh" without extension
        base = choice.lower()

        for filename in available:
            name_no_ext = Path(filename).stem.lower()
            if base == name_no_ext or base == filename.lower():
                return filename

        return None


    #channel lookup helper
    #this should safely retrive the stored voice channel for designated server
    #insteap of repeating config its cleaner
    def get_guild_channel_id(self, guild_id: int) -> int | None:
        entry = self.config.get(str(guild_id))
        if not entry:
            return None
        return entry.get("channel_id")


    #counts how many real people (non-bots) are in a voice channel
    #used for "leave when empty" and "reconnect if people are there"
    def count_humans_in_channel(self, channel: discord.VoiceChannel) -> int:
        return sum(1 for m in channel.members if not m.bot)



    #auto connect logic
    #important!

    #look up saved channel ID
    #get channel object from discord
    #check if bot is already connected
    #if connected somewhere else -> move
    #if not connected, connect.

    #if we want bot to leave when empty or reconnect if dropped, we modify this function
    async def ensure_connected_to_target(self, guild: discord.Guild) -> discord.VoiceClient | None:
        channel_id = self.get_guild_channel_id(guild.id)
        if not channel_id:
            return None

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            return None

        vc = guild.voice_client
        if vc and vc.is_connected():
            if vc.channel != channel:
                await vc.move_to(channel)
            return vc

        return await channel.connect()


    #disconnect helper (for leaving when empty)
    async def disconnect_if_connected(self, guild: discord.Guild):
        vc = guild.voice_client
        if vc and vc.is_connected():
            await vc.disconnect()


    #leave when empty (after a short grace period so we dont flap during mass moves)
    async def schedule_leave_if_empty(self, guild: discord.Guild):
        #cancel any existing leave timer for this guild
        existing = self.leave_tasks.get(guild.id)
        if existing and not existing.done():
            existing.cancel()

        async def _leave_later():
            try:
                await asyncio.sleep(self.LEAVE_GRACE_SECONDS)

                channel_id = self.get_guild_channel_id(guild.id)
                if not channel_id:
                    return

                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    return

                #if still no humans, leave
                if self.count_humans_in_channel(channel) == 0:
                    await self.disconnect_if_connected(guild)

            except asyncio.CancelledError:
                #cancelled because someone joined again
                pass

        self.leave_tasks[guild.id] = asyncio.create_task(_leave_later())



    #reconnect if dropped + leave when empty
    #periodically checks the configured channel for each guild
    @tasks.loop(seconds=15)  # value updated below in before_loop
    async def voice_guard(self):
        for guild in self.bot.guilds:
            target_id = self.get_guild_channel_id(guild.id)
            if not target_id:
                continue

            channel = guild.get_channel(target_id)
            if not isinstance(channel, discord.VoiceChannel):
                continue

            humans = self.count_humans_in_channel(channel)

            if humans > 0:
                #humans are in the channel, make sure the bot is connected (reconnect if dropped)
                try:
                    await self.ensure_connected_to_target(guild)
                except Exception as e:
                    print(f"voice_guard connect failed in guild {guild.id}: {e}")
            else:
                #no humans, schedule leaving (grace period)
                await self.schedule_leave_if_empty(guild)


    @voice_guard.before_loop
    async def before_voice_guard(self):
        #wait until the bot is ready before running the guard loop
        await self.bot.wait_until_ready()
        #set interval dynamically using your config variable
        self.voice_guard.change_interval(seconds=self.GUARD_INTERVAL_SECONDS)



    #runs once the bot logs in
    #loads saved config
    #loops through all guilds
    #auto connects to the saved voice channel
    #removing this would make the bot wait until someone joins before connecting
    @commands.Cog.listener()
    async def on_ready(self):
        # Auto-connect for every server the bot is in (if configured)
        for guild in self.bot.guilds:
            if self.get_guild_channel_id(guild.id):
                try:
                    await self.ensure_connected_to_target(guild)
                except Exception as e:
                    print(f"Auto-connect failed in guild {guild.id}: {e}")

        #start reconnect/leave guard loop
        if not self.voice_guard.is_running():
            self.voice_guard.start()



    #this creates the command Psykzz wanted us to add
    #!setjoinchannel
    #only users with manage server permissions can run it
    @commands.command(name="setjoinchannel")
    @commands.has_guild_permissions(manage_guild=True)
    async def set_join_channel(self, ctx: commands.Context):
        """
        Set the "join sound" voice channel to the channel you're currently in.
        Requires Manage Server permission.
        """
        if not ctx.guild:
            return await ctx.reply("This command must be used in a server.")

        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.reply("Join the voice channel you want first, then run `!setjoinchannel`.")

        channel = ctx.author.voice.channel
        self.config[str(ctx.guild.id)] = {"channel_id": channel.id}
        self.save_config()

        # Connect/move the bot there immediately
        try:
            await self.ensure_connected_to_target(ctx.guild)
        except Exception as e:
            return await ctx.reply(f"Saved channel as **{channel.name}**, but failed to connect: `{e}`")

        await ctx.reply(f"✅ Join channel set to **{channel.name}**. I’m connected and ready.")


    @set_join_channel.error
    async def set_join_channel_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You need **Manage Server** permission to run this.")
        else:
            await ctx.reply(f"Error: `{error}`")


    @commands.command(name="joinchannel")
    @commands.has_guild_permissions(manage_guild=True)
    async def join_channel(self, ctx: commands.Context):
        """
        Force the bot to connect to the configured channel (useful after a disconnect).
        """
        if not ctx.guild:
            return await ctx.reply("Use this in a server.")

        if not self.get_guild_channel_id(ctx.guild.id):
            return await ctx.reply("No join channel set. Run `!setjoinchannel` first.")

        vc = await self.ensure_connected_to_target(ctx.guild)
        if vc:
            await ctx.reply("✅ Connected.")
        else:
            await ctx.reply("Couldn’t connect — is the configured channel still valid?")


    #list available join sounds users can pick
    @commands.command(name="sounds")
    async def sounds(self, ctx: commands.Context):
        files = self.list_available_sounds()
        if not files:
            return await ctx.reply("No sounds found. Add files to the `sounds/` folder (mp3/wav/ogg).")

        #show without extensions for nicer UX
        names = [Path(f).stem for f in files]
        await ctx.reply("Available sounds:\n- " + "\n- ".join(names))


    #show what sound you currently have selected
    @commands.command(name="mysound")
    async def my_sound(self, ctx: commands.Context):
        selected = self.user_sounds.get(str(ctx.author.id))
        if not selected:
            return await ctx.reply("You don't have a join sound set. (Using default `join.mp3`)")

        await ctx.reply(f"Your join sound is set to: **{Path(selected).stem}**")


    #pick a sound from the approved list
    @commands.command(name="setsound")
    async def set_sound(self, ctx: commands.Context, *, sound_name: str = None):
        if not sound_name:
            return await ctx.reply("Usage: `!setsound <soundname>`\nTry `!sounds` to see options.")

        resolved = self.resolve_sound_choice(sound_name)
        if not resolved:
            return await ctx.reply("Sound not found. Use `!sounds` to see available options.")

        self.user_sounds[str(ctx.author.id)] = resolved
        self.save_user_sounds()
        await ctx.reply(f"✅ Your join sound is now: **{Path(resolved).stem}**")


    #reset to default join.mp3
    @commands.command(name="clearsound")
    async def clear_sound(self, ctx: commands.Context):
        if str(ctx.author.id) in self.user_sounds:
            self.user_sounds.pop(str(ctx.author.id), None)
            self.save_user_sounds()
        await ctx.reply("✅ Cleared. You will use default `join.mp3` again.")




    #this is the magic, the core trigger
    #this triggers when someone joins, leaves, switches channels, mutes, etc
    #but we are looking to heavily filter unnecessary triggers
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # ignore bots
        if member.bot or not member.guild:
            return

        #ignore users with excluded roles (e.g NitroUser)
        #only runs if you fill in EXCLUDED_ROLE_IDS above
        if self.EXCLUDED_ROLE_IDS and any(role.id in self.EXCLUDED_ROLE_IDS for role in member.roles):
            return

        guild = member.guild
        target_id = self.get_guild_channel_id(guild.id)
        if not target_id:
            return

        # only trigger when a user joins the configured channel
        joined_target = (before.channel != after.channel) and (after.channel and after.channel.id == target_id)
        if joined_target:
            # cancel pending leave if someone joined again
            pending = self.leave_tasks.get(guild.id)
            if pending and not pending.done():
                pending.cancel()

            # ensure connected in case we were dropped / restarted
            vc = await self.ensure_connected_to_target(guild)
            if not vc:
                return

            # Guild-specific lock + cooldown
            # adding a lock in advance
            #many ocassions where admin will move multiple people in 1 go
            #this is an attempt to stop it firing off multiple times for multiple people joining simultaneously.
            #only one join event can run at a time
            lock = self.play_lock.setdefault(guild.id, asyncio.Lock())
            async with lock:
                now = time.time()
                last = self.last_play_time.get(guild.id, 0.0)
                if now - last < self.COOLDOWN_SECONDS:
                    return

                # dont interrupt if already playing
                if vc.is_playing():
                    return  # don't interrupt

                self.last_play_time[guild.id] = now

                #pick user's custom sound if set, otherwise use default join.mp3
                chosen_file = self.user_sounds.get(str(member.id), self.AUDIO_FILE)

                #if it's a custom sound, it lives in sounds/ folder
                if chosen_file != self.AUDIO_FILE:
                    chosen_path = str(Path(self.SOUNDS_DIR) / chosen_file)
                else:
                    chosen_path = self.AUDIO_FILE

                try:
                    source = discord.FFmpegPCMAudio(chosen_path) #Uses FFmpeg to convert audio file
                    vc.play(source) #Streams raw PCM audio into the voice channel
                except Exception as e:
                    print("Playback error:", e)

            return

        # if someone left/switches out of the target channel, and now its empty of humans, schedule a leave
        left_target = (before.channel and before.channel.id == target_id) and (after.channel is None or after.channel.id != target_id)
        if left_target:
            channel = guild.get_channel(target_id)
            if isinstance(channel, discord.VoiceChannel) and self.count_humans_in_channel(channel) == 0:
                await self.schedule_leave_if_empty(guild)



async def setup(bot: commands.Bot):
    #this is required for bot.load_extension("cogs.joinsound")
    await bot.add_cog(JoinSound(bot))
