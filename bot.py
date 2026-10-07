import asyncio
import io
import logging
import tempfile
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
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

# Tracked phrases requested for Wigz.
TRIGGER_PHRASES = [
    "cheers",
    "dabby time",
    "my bullets do nothing",
    "trash",
    "ragebait",
    "stinky",
]

# Keep sensitive vocabulary internal. These are grouped into a single
# profanity/slur score category instead of being displayed by commands.
PROFANITY_AND_SLURS = {
    "fuck", "fucking", "fucked", "fucker", "motherfucker",
    "shit", "shitty", "bullshit", "damn", "goddamn",
    "bitch", "bitches", "bastard", "asshole", "dick", "cunt",
    # Common identity-based slurs are intentionally stored only for detection.
    "nigger", "nigga", "faggot", "fag", "chink", "gook", "kike",
    "spic", "wetback", "beaner", "coon", "raghead", "towelhead",
    "tranny", "retard",
}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database", "wigz.db")
_db_lock = threading.Lock()


def normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9']+", " ", text.lower()).strip()


def detect_phrases(text: str) -> list[str]:
    """Return each configured trigger/category at most once per utterance."""
    normalized = normalize_text(text)
    padded = f" {normalized} "
    matches = []

    for phrase in TRIGGER_PHRASES:
        normalized_phrase = normalize_text(phrase)
        if normalized_phrase and f" {normalized_phrase} " in padded:
            matches.append(phrase)

    words = set(normalized.split())
    if words.intersection(PROFANITY_AND_SLURS):
        matches.append("profanity/slur")

    return matches


def init_database() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trigger_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                trigger TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                guild_name TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trigger_events_user ON trigger_events(user_id)"
        )
        conn.commit()


def record_trigger(user_id: int, display_name: str, trigger: str,
                   guild_id: int, guild_name: str,
                   channel_id: int, channel_name: str) -> None:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO trigger_events
               (user_id, display_name, trigger, occurred_at,
                guild_id, guild_name, channel_id, channel_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, display_name, trigger,
                datetime.now(timezone.utc).isoformat(),
                guild_id, guild_name, channel_id, channel_name,
            ),
        )
        conn.commit()


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


def transcribe_pcm(user_id: int, display_name: str, guild_id: int, guild_name: str, channel_id: int, channel_name: str, pcm: bytes) -> None:
    # Ignore extremely short bursts/noise.
    if len(pcm) < 48000:
        return

    try:
        model = get_whisper_model()
        wav_bytes = pcm_to_wav_bytes(pcm)

        # On Windows, the PyAV build used by faster-whisper can mis-handle
        # BytesIO objects. A short-lived WAV file is more reliable. It is
        # deleted immediately after transcription and is never retained.
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_audio:
                temp_audio.write(wav_bytes)
                temp_path = temp_audio.name

            segments, info = model.transcribe(
                temp_path,
                language="en",
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        if text:
            print(f"[TRANSCRIPT] {display_name}: {text}")
            for phrase in detect_phrases(text):
                record_trigger(
                    user_id, display_name, phrase,
                    guild_id, guild_name, channel_id, channel_name,
                )
                print(f'[TRIGGER] {display_name} -> "{phrase}" [SAVED]')
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
                args=(
                    member.id,
                    display_name,
                    member.guild.id,
                    member.guild.name,
                    member.voice.channel.id if member.voice and member.voice.channel else 0,
                    member.voice.channel.name if member.voice and member.voice.channel else "unknown",
                    pcm,
                ),
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
    init_database()
    print(f"[DATABASE] Ready: {DB_PATH}")
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


@bot.tree.command(name="score", description="Show a member's Wigz trigger score.")
async def score(interaction: discord.Interaction, member: discord.Member | None = None):
    target = member or interaction.user
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT trigger, COUNT(*) AS count
               FROM trigger_events
               WHERE user_id = ? AND guild_id = ?
               GROUP BY trigger
               ORDER BY count DESC, trigger COLLATE NOCASE""",
            (target.id, interaction.guild_id),
        ).fetchall()

    total = sum(count for _, count in rows)
    if not rows:
        await interaction.response.send_message(
            f"**{target.display_name}** has no Wigz trigger points yet."
        )
        return

    breakdown = "\n".join(
        f"• {trigger.title()}: **{count}**" for trigger, count in rows
    )
    await interaction.response.send_message(
        f"📊 **{target.display_name} — {total} total**\n{breakdown}"
    )


@bot.tree.command(name="leaderboard", description="Show the Wigz trigger leaderboard.")
async def leaderboard(interaction: discord.Interaction):
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT user_id, MAX(display_name), COUNT(*) AS score
               FROM trigger_events
               WHERE guild_id = ?
               GROUP BY user_id
               ORDER BY score DESC, MAX(display_name) COLLATE NOCASE
               LIMIT 10""",
            (interaction.guild_id,),
        ).fetchall()

    if not rows:
        await interaction.response.send_message("No Wigz trigger scores yet.")
        return

    sections = []
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        for index, (user_id, name, points) in enumerate(rows, start=1):
            breakdown_rows = conn.execute(
                """SELECT trigger, COUNT(*) AS count
                   FROM trigger_events
                   WHERE user_id = ? AND guild_id = ?
                   GROUP BY trigger
                   ORDER BY count DESC, trigger COLLATE NOCASE""",
                (user_id, interaction.guild_id),
            ).fetchall()
            breakdown = " • ".join(
                f"{trigger.title()}: {count}" for trigger, count in breakdown_rows
            )
            sections.append(
                f"**{index}. {name} — {points} total**\n{breakdown}"
            )

    await interaction.response.send_message(
        "🏆 **Wigz Leaderboard**\n\n" + "\n\n".join(sections)
    )


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
