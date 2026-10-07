import os

import discord
from discord.ext import commands, voice_recv
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN was not found in .env")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


class SpeakerActivitySink(voice_recv.AudioSink):
    """Minimal sink used to prove Wigz can attribute incoming voice to members."""

    def wants_opus(self) -> bool:
        # For the speaker-attribution milestone we only need packet activity,
        # not decoded PCM. Keeping Opus encoded avoids unnecessary decoding.
        return True

    def write(self, user, data) -> None:
        # Receiving packets here proves the voice receive path is active.
        # We intentionally do not save or process audio yet.
        pass

    def cleanup(self) -> None:
        # Required by AudioSink. Nothing is being persisted yet, so there is
        # nothing to release at this milestone.
        pass

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_start(self, member):
        if member.bot:
            return
        print(f"[SPEAKING] {member.display_name} started speaking")

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_stop(self, member):
        if member.bot:
            return
        print(f"[SPEAKING] {member.display_name} stopped speaking")


def start_voice_listener(voice_client: voice_recv.VoiceRecvClient) -> None:
    if not voice_client.is_listening():
        voice_client.listen(
            SpeakerActivitySink(),
            after=lambda error: print(f"[VOICE LISTENER ERROR] {error}") if error else None,
        )
        print("[VOICE] Speaker activity listener started")


@bot.event
async def setup_hook():
    print("Syncing slash commands...")
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} slash command(s).")


@bot.event
async def on_ready():
    print()
    print("=" * 45)
    print("WIGZ")
    print("=" * 45)
    print(f"Logged in as: {bot.user}")
    print(f"Bot ID: {bot.user.id}")
    print(f"Connected servers: {len(bot.guilds)}")

    for guild in bot.guilds:
        print(f"  - {guild.name}")

    print()
    print("Wigz is online and ready.")
    print("=" * 45)


@bot.tree.command(name="join", description="Have Wigz join your current voice channel.")
async def join(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.", ephemeral=True
        )
        return

    member = interaction.user
    if not isinstance(member, discord.Member) or member.voice is None:
        await interaction.response.send_message(
            "Join a voice channel first, then use /join.", ephemeral=True
        )
        return

    voice_channel = member.voice.channel
    voice_client = interaction.guild.voice_client

    try:
        if voice_client is not None:
            if not isinstance(voice_client, voice_recv.VoiceRecvClient):
                await voice_client.disconnect()
                voice_client = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
            elif voice_client.channel != voice_channel:
                await voice_client.move_to(voice_channel)

            start_voice_listener(voice_client)
            await interaction.response.send_message(
                f"Listening in **{voice_channel.name}**. 🔊"
            )
            print(f"[VOICE] Listening in {voice_channel.name} in {interaction.guild.name}")
            return

        voice_client = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
        start_voice_listener(voice_client)

        await interaction.response.send_message(
            f"Joined **{voice_channel.name}** and started listening. 🔊"
        )
        print(f"[VOICE] Connected to {voice_channel.name} in {interaction.guild.name}")

    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to connect to that voice channel.", ephemeral=True
        )
    except Exception as exc:
        print(f"[VOICE ERROR] {type(exc).__name__}: {exc}")
        message = "I hit an error while trying to join voice. Check the Wigz console."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="leave", description="Disconnect Wigz from voice.")
async def leave(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.", ephemeral=True
        )
        return

    voice_client = interaction.guild.voice_client
    if voice_client is None:
        await interaction.response.send_message(
            "I'm not currently in a voice channel.", ephemeral=True
        )
        return

    channel_name = voice_client.channel.name

    if isinstance(voice_client, voice_recv.VoiceRecvClient) and voice_client.is_listening():
        voice_client.stop_listening()

    await voice_client.disconnect()
    await interaction.response.send_message(f"Left **{channel_name}**. 👋")
    print(f"[VOICE] Disconnected from {channel_name} in {interaction.guild.name}")


bot.run(TOKEN)
