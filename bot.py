import asyncio
import io
import logging
from logging.handlers import RotatingFileHandler
import tempfile
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import wave
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands, voice_recv
from dotenv import load_dotenv
from faster_whisper import WhisperModel

load_dotenv()

# Keep a persistent rotating log when Wigz runs without a visible console.
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "wigz.log")
_file_handler = RotatingFileHandler(
    LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_file_handler)

# Mirror print-based Wigz diagnostics into the persistent log without
# removing console output during manual development runs.
_builtin_print = print
def print(*args, **kwargs):
    _builtin_print(*args, **kwargs)
    try:
        logging.getLogger("wigz").info(" ".join(str(arg) for arg in args))
    except Exception:
        pass

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
    "cheating",
    "cheater",
    "hacks",
    "hacking",
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
WATCHED_VOICE_CHANNEL_ID = 442196862607425536  # WHO
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
_db_lock = threading.Lock()
AFK_INACTIVITY_MINUTES = 25
AFK_THRESHOLD_SECONDS = AFK_INACTIVITY_MINUTES * 60
_voice_activity_lock = threading.Lock()
_voice_activity = {}


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

    # Record each distinct matched profanity/slur separately so score and
    # leaderboard breakdowns can show exactly which word was detected.
    # Repeating the same word within one utterance still counts only once.
    words = set(normalized.split())
    for term in sorted(words.intersection(PROFANITY_AND_SLURS)):
        matches.append(term)

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
        conn.execute("""CREATE TABLE IF NOT EXISTS afk_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
            afk_started_at TEXT NOT NULL, afk_ended_at TEXT NOT NULL, duration_seconds INTEGER NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_afk_sessions_user ON afk_sessions(user_id)")
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


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def finish_afk_session_locked(guild_id: int, user_id: int, ended_at: datetime) -> None:
    state = _voice_activity.get((guild_id, user_id))
    if not state or state["afk_started"] is None:
        return
    started = state["afk_started"]
    duration = max(0, int((ended_at - started).total_seconds()))
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute("""INSERT INTO afk_sessions
            (user_id, display_name, guild_id, channel_id, afk_started_at, afk_ended_at, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, state["display_name"], guild_id, state["channel_id"],
             started.isoformat(), ended_at.isoformat(), duration))
        conn.commit()
    print(f"[AFK] {state['display_name']} active again after {format_duration(duration)} AFK")
    state["afk_started"] = None


def note_voice_activity(member: discord.Member) -> None:
    now = datetime.now(timezone.utc)
    with _voice_activity_lock:
        state = _voice_activity.setdefault((member.guild.id, member.id), {
            "display_name": member.display_name, "channel_id": WATCHED_VOICE_CHANNEL_ID,
            "last_spoke": now, "afk_started": None})
        state["display_name"] = member.display_name
        if state["afk_started"] is not None:
            finish_afk_session_locked(member.guild.id, member.id, now)
        state["last_spoke"] = now


def close_voice_activity(member: discord.Member) -> None:
    with _voice_activity_lock:
        finish_afk_session_locked(member.guild.id, member.id, datetime.now(timezone.utc))
        _voice_activity.pop((member.guild.id, member.id), None)


async def afk_monitor_loop() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(timezone.utc)
        with _voice_activity_lock:
            for state in _voice_activity.values():
                if state["afk_started"] is None:
                    threshold = state["last_spoke"] + timedelta(seconds=AFK_THRESHOLD_SECONDS)
                    if now >= threshold:
                        state["afk_started"] = threshold
                        print(f"[AFK] {state['display_name']} marked AFK after {AFK_INACTIVITY_MINUTES}m silence")
        await asyncio.sleep(30)


def afk_period_stats(user_id: int, guild_id: int, period: str):
    cutoff = period_cutoff(period)
    where, params = "WHERE user_id=? AND guild_id=?", [user_id, guild_id]
    if cutoff:
        where += " AND afk_ended_at >= ?"
        params.append(cutoff)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        return conn.execute(f"""SELECT COALESCE(SUM(duration_seconds),0), COUNT(*),
            COALESCE(MAX(duration_seconds),0) FROM afk_sessions {where}""", params).fetchone()


def period_cutoff(period: str):
    now_local = datetime.now(LOCAL_TZ)
    if period == "today":
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = (now_local - timedelta(days=now_local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif period == "month":
        start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        return None
    return start.astimezone(timezone.utc).isoformat()


def period_where(period: str):
    cutoff = period_cutoff(period)
    if cutoff is None:
        return "", []
    return " AND occurred_at >= ?", [cutoff]


async def ensure_watched_voice_state(guild: discord.Guild) -> None:
    """Keep Wigz silently connected to WHO while the bot is online."""
    channel = guild.get_channel(WATCHED_VOICE_CHANNEL_ID)
    if not isinstance(channel, discord.VoiceChannel):
        return

    voice_client = guild.voice_client
    try:
        if voice_client is None:
            voice_client = await channel.connect(
                cls=voice_recv.VoiceRecvClient,
                self_deaf=False,
            )
            start_voice_listener(voice_client)
            print(f"[AUTO VOICE] Connected silently to {channel.name}; staying connected")
        elif isinstance(voice_client, voice_recv.VoiceRecvClient):
            if voice_client.channel != channel:
                await voice_client.move_to(channel)
            start_voice_listener(voice_client)
    except Exception as exc:
        print(f"[AUTO VOICE ERROR] {type(exc).__name__}: {exc}")


def get_top_trigger(user_id: int, guild_id: int, period: str):
    extra_where, extra_params = period_where(period)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            f"""SELECT trigger, COUNT(*) AS count
                FROM trigger_events
                WHERE user_id = ? AND guild_id = ?
                  AND trigger != 'profanity/slur'{extra_where}
                GROUP BY trigger
                ORDER BY count DESC, trigger COLLATE NOCASE
                LIMIT 1""",
            [user_id, guild_id, *extra_params],
        ).fetchone()


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
        note_voice_activity(member)

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


def display_trigger(trigger: str) -> str:
    return trigger.replace("_", " ").title()


def stat_bar(value: int, maximum: int, width: int = 10) -> str:
    if maximum <= 0:
        return "░" * width
    filled = max(1, round((value / maximum) * width)) if value > 0 else 0
    filled = min(width, filled)
    return "█" * filled + "░" * (width - filled)


def member_avatar_url(member) -> str | None:
    avatar = getattr(member, "display_avatar", None)
    return avatar.url if avatar else None


@bot.event
async def setup_hook():
    init_database()
    print(f"[DATABASE] Ready: {DB_PATH}")
    print("Syncing slash commands...")
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} slash command(s).")
    bot.loop.create_task(afk_monitor_loop())


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

    # Keep Wigz parked silently in WHO whenever the bot is online.
    for guild in bot.guilds:
        await ensure_watched_voice_state(guild)
        channel = guild.get_channel(WATCHED_VOICE_CHANNEL_ID)
        if isinstance(channel, discord.VoiceChannel):
            now = datetime.now(timezone.utc)
            with _voice_activity_lock:
                for member in channel.members:
                    if not member.bot:
                        _voice_activity[(guild.id, member.id)] = {
                            "display_name": member.display_name, "channel_id": channel.id,
                            "last_spoke": now, "afk_started": None}


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    was_watched = before.channel is not None and before.channel.id == WATCHED_VOICE_CHANNEL_ID
    is_watched = after.channel is not None and after.channel.id == WATCHED_VOICE_CHANNEL_ID
    if not was_watched and is_watched:
        with _voice_activity_lock:
            _voice_activity[(member.guild.id, member.id)] = {
                "display_name": member.display_name, "channel_id": after.channel.id,
                "last_spoke": datetime.now(timezone.utc), "afk_started": None}
        print(f"[AFK] Tracking {member.display_name}")
    elif was_watched and not is_watched:
        close_voice_activity(member)
        print(f"[AFK] Stopped tracking {member.display_name}")


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
@app_commands.choices(period=[
    app_commands.Choice(name="Today", value="today"),
    app_commands.Choice(name="This week", value="week"),
    app_commands.Choice(name="This month", value="month"),
    app_commands.Choice(name="All time", value="all"),
])
async def score(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
    period: app_commands.Choice[str] | None = None,
):
    target = member or interaction.user
    selected = period.value if period else "all"
    extra_where, extra_params = period_where(selected)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            f"""SELECT trigger, COUNT(*) AS count
                FROM trigger_events
                WHERE user_id = ? AND guild_id = ? AND trigger != 'profanity/slur'{extra_where}
                GROUP BY trigger
                ORDER BY count DESC, trigger COLLATE NOCASE""",
            [target.id, interaction.guild_id, *extra_params],
        ).fetchall()

    total = sum(count for _, count in rows)
    label = {"today":"Today","week":"This week","month":"This month","all":"All time"}[selected]
    if not rows:
        await interaction.response.send_message(
            f"**{target.display_name}** has no Wigz trigger points for **{label}**."
        )
        return

    max_count = max(count for _, count in rows)
    breakdown = "\n".join(
        f"**{display_trigger(trigger)}**  \`{stat_bar(count, max_count)}\` **{count}**"
        for trigger, count in rows[:15]
    )
    embed = discord.Embed(
        title="🎯 WIGZ • SCORECARD",
        description=f"## {target.display_name}\n**{label.upper()}**  •  **{total}** TOTAL HITS",
        color=discord.Color.from_rgb(88, 101, 242),
    )
    embed.add_field(name="📊 TRIGGER BREAKDOWN", value=breakdown, inline=False)
    embed.set_thumbnail(url=member_avatar_url(target))
    embed.set_footer(text="WIGZ • Every word leaves a mark")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="Show detailed Wigz stats for a member.")
async def stats(interaction: discord.Interaction, member: discord.Member | None = None):
    target = member or interaction.user
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT trigger, COUNT(*) AS count
               FROM trigger_events
               WHERE user_id = ? AND guild_id = ? AND trigger != 'profanity/slur'
               GROUP BY trigger
               ORDER BY count DESC, trigger COLLATE NOCASE""",
            (target.id, interaction.guild_id),
        ).fetchall()
        total = sum(count for _, count in rows)
        today_cutoff = period_cutoff("today")
        week_cutoff = period_cutoff("week")
        month_cutoff = period_cutoff("month")
        today = conn.execute(
            "SELECT COUNT(*) FROM trigger_events WHERE user_id=? AND guild_id=? AND occurred_at>=?",
            (target.id, interaction.guild_id, today_cutoff),
        ).fetchone()[0]
        week = conn.execute(
            "SELECT COUNT(*) FROM trigger_events WHERE user_id=? AND guild_id=? AND occurred_at>=?",
            (target.id, interaction.guild_id, week_cutoff),
        ).fetchone()[0]
        month = conn.execute(
            "SELECT COUNT(*) FROM trigger_events WHERE user_id=? AND guild_id=? AND occurred_at>=?",
            (target.id, interaction.guild_id, month_cutoff),
        ).fetchone()[0]

    if not rows:
        await interaction.response.send_message(f"**{target.display_name}** has no Wigz stats yet.")
        return

    top_today = get_top_trigger(target.id, interaction.guild_id, "today")
    top_week = get_top_trigger(target.id, interaction.guild_id, "week")
    top_month = get_top_trigger(target.id, interaction.guild_id, "month")

    favorite, favorite_count = rows[0]
    max_count = max(count for _, count in rows)
    breakdown = "\n".join(
        f"**{display_trigger(trigger)}**  \`{stat_bar(count, max_count)}\` **{count}**"
        for trigger, count in rows[:10]
    )
    embed = discord.Embed(
        title="📈 WIGZ • PLAYER PROFILE",
        description=f"## {target.display_name}\nLifetime trigger report",
        color=discord.Color.from_rgb(88, 101, 242),
    )
    overview = (
        f"**TODAY**  {today}    •    **WEEK**  {week}\n"
        f"**MONTH**  {month}    •    **ALL TIME**  {total}"
    )
    hot_words = (
        f"☀️ **TODAY**  {display_trigger(top_today[0])} · {top_today[1] if top_today else 0}\n"
        if top_today else "☀️ **TODAY**  —\n"
    )
    hot_words += (
        f"📅 **WEEK**  {display_trigger(top_week[0])} · {top_week[1]}\n"
        if top_week else "📅 **WEEK**  —\n"
    )
    hot_words += (
        f"🗓️ **MONTH**  {display_trigger(top_month[0])} · {top_month[1]}"
        if top_month else "🗓️ **MONTH**  —"
    )
    embed.add_field(name="📊  SCOREBOARD", value=overview, inline=False)
    embed.add_field(
        name="🏅  SIGNATURE WORD",
        value=f"### {display_trigger(favorite)}\n**{favorite_count} lifetime hits**",
        inline=False,
    )
    embed.add_field(name="🔥  HOT WORDS", value=hot_words, inline=False)
    embed.add_field(name="📈  TRIGGER BREAKDOWN", value=breakdown, inline=False)
    embed.set_thumbnail(url=member_avatar_url(target))
    embed.set_footer(text="WIGZ • Every word leaves a mark")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Show the Wigz trigger leaderboard.")
@app_commands.choices(period=[
    app_commands.Choice(name="Today", value="today"),
    app_commands.Choice(name="This week", value="week"),
    app_commands.Choice(name="This month", value="month"),
    app_commands.Choice(name="All time", value="all"),
])
async def leaderboard(
    interaction: discord.Interaction,
    period: app_commands.Choice[str] | None = None,
):
    selected = period.value if period else "all"
    extra_where, extra_params = period_where(selected)
    label = {"today":"Today","week":"This week","month":"This month","all":"All time"}[selected]

    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            f"""SELECT user_id, MAX(display_name), COUNT(*) AS score
                FROM trigger_events
                WHERE guild_id = ? AND trigger != 'profanity/slur'{extra_where}
                GROUP BY user_id
                ORDER BY score DESC, MAX(display_name) COLLATE NOCASE
                LIMIT 10""",
            [interaction.guild_id, *extra_params],
        ).fetchall()

        sections = []
        for index, (user_id, name, points) in enumerate(rows, start=1):
            breakdown_rows = conn.execute(
                f"""SELECT trigger, COUNT(*) AS count
                    FROM trigger_events
                    WHERE user_id = ? AND guild_id = ? AND trigger != 'profanity/slur'{extra_where}
                    GROUP BY trigger
                    ORDER BY count DESC, trigger COLLATE NOCASE""",
                [user_id, interaction.guild_id, *extra_params],
            ).fetchall()
            breakdown = "  •  ".join(
                f"**{display_trigger(trigger)}** {count}" for trigger, count in breakdown_rows[:6]
            )
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"#{index}")
            sections.append((medal, name, points, breakdown))

    if not rows:
        await interaction.response.send_message(f"No Wigz trigger scores for **{label}** yet.")
        return

    max_points = max(points for _, _, points, _ in sections)
    lines = []
    for medal, name, points, breakdown in sections:
        bar = stat_bar(points, max_points, 16)
        lines.append(
            f"### {medal}  {name}\n"
            f"\`{bar}\`  **{points} HITS**\n"
            f"{breakdown or 'No trigger breakdown'}"
        )

    embed = discord.Embed(
        title="🏆  W I G Z   L E A D E R B O A R D",
        description=(
            f"**{label.upper()}**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            + "\n\n".join(lines)
            + "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="WIGZ  •  Rankings update live")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="recap", description="Show today's server-wide Wigz recap.")
async def recap(interaction: discord.Interaction):
    cutoff = period_cutoff("today")

    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        total = conn.execute(
            """SELECT COUNT(*)
               FROM trigger_events
               WHERE guild_id = ? AND trigger != 'profanity/slur'
                 AND occurred_at >= ?""",
            (interaction.guild_id, cutoff),
        ).fetchone()[0]

        top_word = conn.execute(
            """SELECT trigger, COUNT(*) AS count
               FROM trigger_events
               WHERE guild_id = ? AND trigger != 'profanity/slur'
                 AND occurred_at >= ?
               GROUP BY trigger
               ORDER BY count DESC, trigger COLLATE NOCASE
               LIMIT 1""",
            (interaction.guild_id, cutoff),
        ).fetchone()

        leaders = conn.execute(
            """SELECT user_id, MAX(display_name), COUNT(*) AS score
               FROM trigger_events
               WHERE guild_id = ? AND trigger != 'profanity/slur'
                 AND occurred_at >= ?
               GROUP BY user_id
               ORDER BY score DESC, MAX(display_name) COLLATE NOCASE
               LIMIT 3""",
            (interaction.guild_id, cutoff),
        ).fetchall()

    top_word_text = (
        f"**{display_trigger(top_word[0])}**\n### {top_word[1]} HITS"
        if top_word else "No triggers yet"
    )
    if leaders:
        medals = ["🥇", "🥈", "🥉"]
        standings = "\n".join(
            f"{medals[index]}  **{name}**  ·  **{points}**"
            for index, (_, name, points) in enumerate(leaders)
        )
        mvp_text = f"**{leaders[0][1]}**\n### {leaders[0][2]} HITS"
    else:
        standings = "No scores yet"
        mvp_text = "Nobody yet"

    embed = discord.Embed(
        title="⚡  W I G Z   •   D A I L Y   R E C A P",
        description=(
            "## TODAY'S DAMAGE REPORT\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"# {total}\n"
            "**TOTAL HITS TODAY**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="🔥  WORD OF THE DAY", value=top_word_text, inline=False)
    embed.add_field(name="👑  TODAY'S MVP", value=mvp_text, inline=False)
    embed.add_field(name="🏆  TODAY'S PODIUM", value=standings, inline=False)
    embed.set_footer(text="WIGZ  •  Resets at midnight Pacific")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="afk", description="Show a member's Wigz voice-inactivity stats.")
async def afk(interaction: discord.Interaction, member: discord.Member | None = None):
    target = member or interaction.user
    today_seconds, _, _ = afk_period_stats(target.id, interaction.guild_id, "today")
    week_seconds, _, _ = afk_period_stats(target.id, interaction.guild_id, "week")
    month_seconds, _, _ = afk_period_stats(target.id, interaction.guild_id, "month")
    all_seconds, all_trips, longest = afk_period_stats(target.id, interaction.guild_id, "all")
    current = "⚪ **NOT CURRENTLY TRACKED**"
    with _voice_activity_lock:
        state = _voice_activity.get((interaction.guild_id, target.id))
        if state:
            now = datetime.now(timezone.utc)
            silent = int((now - state["last_spoke"]).total_seconds())
            if state["afk_started"]:
                current_afk = int((now - state["afk_started"]).total_seconds())
                current = f"💤 **WIGZ AFK** · {format_duration(current_afk)}\nLast spoke {format_duration(silent)} ago"
            else:
                current = f"🟢 **ACTIVE**\nLast spoke {format_duration(silent)} ago"
    embed = discord.Embed(title="💤  W I G Z   •   A F K   R E P O R T",
        description=f"## {target.display_name}\n{current}\n\n*AFK begins after {AFK_INACTIVITY_MINUTES} minutes without speaking.*",
        color=discord.Color.gold())
    embed.add_field(name="⏱️  AFK TIME", value=f"**TODAY**  {format_duration(today_seconds)}\n**THIS WEEK**  {format_duration(week_seconds)}\n**THIS MONTH**  {format_duration(month_seconds)}\n**ALL TIME**  {format_duration(all_seconds)}", inline=False)
    embed.add_field(name="😴  LONGEST NAP", value=f"### {format_duration(longest)}", inline=True)
    embed.add_field(name="🚪  AFK TRIPS", value=f"### {all_trips}", inline=True)
    embed.set_thumbnail(url=member_avatar_url(target))
    embed.set_footer(text="WIGZ • Voice inactivity tracker")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="afkleaderboard", description="Rank members by Wigz AFK time.")
@app_commands.choices(period=[
    app_commands.Choice(name="Today", value="today"), app_commands.Choice(name="This week", value="week"),
    app_commands.Choice(name="This month", value="month"), app_commands.Choice(name="All time", value="all")])
async def afkleaderboard(interaction: discord.Interaction, period: app_commands.Choice[str] | None = None):
    selected = period.value if period else "all"
    cutoff = period_cutoff(selected)
    where, params = "WHERE guild_id=?", [interaction.guild_id]
    if cutoff:
        where += " AND afk_ended_at >= ?"
        params.append(cutoff)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(f"""SELECT user_id, MAX(display_name), SUM(duration_seconds) AS total
            FROM afk_sessions {where} GROUP BY user_id ORDER BY total DESC LIMIT 10""", params).fetchall()
    label = {"today":"Today","week":"This week","month":"This month","all":"All time"}[selected]
    if not rows:
        await interaction.response.send_message(f"No completed Wigz AFK sessions for **{label}** yet.")
        return
    maximum = rows[0][2]
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for index, (_, name, seconds) in enumerate(rows):
        rank = medals[index] if index < 3 else f"#{index + 1}"
        lines.append(f"### {rank}  {name}\n\`{stat_bar(seconds, maximum, 16)}\`  **{format_duration(seconds)}**")
    embed = discord.Embed(title="💤  A F K   L E A D E R B O A R D",
        description=f"**{label.upper()}**\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(lines),
        color=discord.Color.gold())
    embed.set_footer(text=f"WIGZ • AFK begins after {AFK_INACTIVITY_MINUTES}m of silence")
    await interaction.response.send_message(embed=embed)


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
