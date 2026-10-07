import asyncio
import io
import logging
import os
import threading
import wave
from collections import defaultdict

import discord
from discord.ext import commands, voice_recv
from dotenv import load_dotenv
from faster_whisper import WhisperModel

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN was not found in .env")

# Keep the experimental voice-receive library's protocol chatter out of the
# console while still allowing real warnings/errors through.
logging.getLogger("discord.ext.voice_recv.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL", "base.en")
_whisper_model = None
_whisper_lock = threading.Lock()


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                print(f"[WHISPER] Loading {WHISPER_MODEL_SIZE} model...")
                _whisper_model = WhisperModel(
                    WHISPER_MODEL_SIZE,
                    device="cpu",
                    compute_type="int8",
                )
                print("[WHISPER] Model ready.")
    return _whisper_model


def pcm_to_wav_bytes(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(48000)
        wav_file.writeframes(pcm)
    return output.getvalue()


def transcribe_pcm(display_name: str, pcm: bytes) -> None:
    # Ignore extremely short bursts/noise.
    if len(pcm) < 48000:
        return

    try:
        model = get_whisper_model()
        wav_bytes = pcm_to_wav_bytes(pcm)

        # faster-whisper accepts a binary file-like object through PyAV.
        audio_file = io.BytesIO(wav_bytes)
        segments, info = model.transcribe(
            audio_file,
            language="en",
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )

        text = " ".join(segment.text.strip() for segment in segments).strip()
        if text:
            print(f"[TRANSCRIPT] {display_name}: {text}")
        else:
            print(f"[TRANSCRIPT] {display_name}: (no speech detected)")

    except Exception as exc:
        print(f"[TRANSCRIBE ERROR] {display_name}: {type(exc).__name__}: {exc}")


class TranscriptionSink(voice_recv.AudioSink):
    """Collect PCM separately per Discord member and transcribe each utterance."""

    def __init__(self):
        super().__init__()
        self.buffers = defaultdict(bytearray)
        self.names = {}

    def wants_opus(self) -> bool:
        # Whisper needs decoded PCM, so this milestone switches decoding back on.
        return False

    def write(self, user, data) -> None:
        if user is None or user.bot:
            return

        pcm = getattr(data, "pcm", None)
        if not pcm:
            return

        self.buffers[user.id].extend(pcm)
        self.names[user.id] = user.display_name

    def cleanup(self) -> None:
        self.buffers.clear()
        self.names.clear()

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_start(self, member):
        if member.bot:
            return

        # Start a clean utterance for this member.
        self.buffers[member.id] = bytearray()
        self.names[member.id] = member.display_name
        print(f"[SPEAKING] {member.display_name} started speaking")

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_stop(self, member):
        if member.bot:
            return

        print(f"[SPEAKING] {member.display_name} stopped speaking")
        pcm = bytes(self.buffers.pop(member.id, b""))
        display_name = self.names.pop(member.id, member.display_name)

        if pcm:
            threading.Thread(
                target=transcribe_pcm,
                args=(display_name, pcm),
                daemon=True,
            ).start()


def start_voice_listener(voice_client: voice_recv.VoiceRecvClient) -> None:
    if not voice_client.is_listening():
        voice_client.listen(
            TranscriptionSink(),
            after=lambda error: print(f"[VOICE LISTENER ERROR] {error}") if error else None,
        )
        print("[VOICE] Speaker listener + local transcription started")


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
