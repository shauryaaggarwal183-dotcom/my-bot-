from __future__ import annotations
import os
import re
import time
import logging

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from groq import AsyncGroq

import utils.embeds as E
from cogs.access import require_admin_or_owner

logger = logging.getLogger('TicketBot.ai_qa')

# ═══════════════════════════════════════════════════════════════════════════
#  AI Q&A  —  Auto-answers ANY question in chat using Groq (not just
#  Minecraft — general knowledge, coding, advice, whatever gets asked).
#
#  • If someone @mentions the bot, ANY question they ask gets answered.
#  • If someone just types a message in an enabled channel that looks like
#    a question (matches QUESTION_HINTS), the bot answers automatically —
#    no mention needed, no topic restriction.
#  • Per-guild toggle + optional channel lock, managed via /aiqa command.
#  • Needs GROQ_API_KEY set as an environment variable (Railway →
#    Variables tab, or your local .env file). Requires the `groq` package
#    (pip install groq).
# ═══════════════════════════════════════════════════════════════════════════

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# "groq/compound" is Groq's agentic model — it automatically triggers a real
# web search (and code execution) tool call server-side whenever a question
# needs live/current info (news, prices, "who is", scores, dates, etc), then
# folds the results into its answer. No extra search API/key needed on our
# side. Can be overridden with a GROQ_MODEL env var if you ever want to pin
# it back to a plain (non-searching) model like "llama-3.3-70b-versatile".
GROQ_MODEL = os.getenv("GROQ_MODEL", "groq/compound")

COOLDOWN_SECONDS = 12          # per-user cooldown so one person can't spam it
MAX_REPLY_CHARS = 1900         # keep replies under discord's 2000 char limit

BASE_SYSTEM_PROMPT = (
    "You are the in-server AI assistant for a Minecraft community (tier-testing "
    "/ PvP / SMP server). Answer ANY question the member asks — Minecraft related "
    "or not (general knowledge, coding, homework, advice, random chat, etc). "
    "You have access to a real-time web search tool — use it automatically "
    "whenever a question depends on current/live information (news, prices, "
    "scores, who currently holds a role, today's date, recent releases, etc) "
    "instead of guessing from memory. "
    "Keep answers short (usually under 100 words) and use Discord markdown "
    "(backticks for commands, bullet points for steps) when useful. If you "
    "genuinely don't know something server-specific (like this server's exact "
    "rules), say so instead of guessing."
)

QUESTION_HINTS = [
    "?", "kya", "kaise", "kaisy", "kese", "kyu", "kyun", "kaun", "kab",
    "kitna", "kitne", "how", "what", "why", "when", "where", "which",
    "can i", "can you", "is it", "does", "do i", "should i",
]


def _looks_like_question(text: str) -> bool:
    t = text.lower().strip()
    if not t:
        return False
    return any(hint in t for hint in QUESTION_HINTS)


class AIQACog(commands.Cog, name="AI Q&A"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client: AsyncGroq | None = None
        self._cooldowns: dict[int, float] = {}
        self._owner_label: str | None = None  # cached "Name (id)" of the real bot owner

    async def cog_load(self):
        await self._ensure_table()
        if GROQ_API_KEY:
            self.client = AsyncGroq(api_key=GROQ_API_KEY)
        else:
            self.client = None
        await self._refresh_owner_label()

    async def cog_unload(self):
        if self.client:
            await self.client.close()

    # ── Owner info ───────────────────────────────────────────────────────
    async def _refresh_owner_label(self):
        """Fetches the REAL bot owner (Discord Developer Portal application
        owner, or team owner) — same trusted source cogs/access.py uses via
        bot.is_owner() — and caches a human-readable label for the AI's
        system prompt. Never guessable/spoofable by server roles."""
        try:
            app_info = await self.bot.application_info()
            if app_info.team:
                owner = app_info.team.owner or app_info.owner
            else:
                owner = app_info.owner
            if owner:
                self._owner_label = f"{owner.name} (Discord ID {owner.id})"
        except Exception:
            logger.exception("Could not fetch application_info() for owner label.")

    def _system_prompt(self, guild: discord.Guild | None) -> str:
        """Builds the system prompt fresh per-request so it always carries
        current, real context: today's date/time, this server's name, and
        who the bot's real owner is — instead of a static prompt."""
        now = discord.utils.utcnow().strftime("%A, %B %d, %Y %H:%M UTC")
        parts = [BASE_SYSTEM_PROMPT, f"Right now it is {now}."]
        if guild:
            parts.append(f"This Discord server is called \"{guild.name}\".")
        if self._owner_label:
            parts.append(
                f"The real owner/developer of this bot is {self._owner_label}. "
                "If a member asks who owns/made/runs the bot, answer with this "
                "person — do not guess or say you don't know."
            )
        return " ".join(parts)

    # ── DB ────────────────────────────────────────────────────────────────
    async def _ensure_table(self):
        async with aiosqlite.connect(self.bot.db.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS aiqa_settings (
                    guild_id    INTEGER PRIMARY KEY,
                    enabled     INTEGER DEFAULT 0,
                    channel_id  INTEGER
                )
            ''')
            await db.commit()

    async def _get_settings(self, guild_id: int) -> dict:
        async with aiosqlite.connect(self.bot.db.db_path) as db:
            async with db.execute(
                'SELECT enabled, channel_id FROM aiqa_settings WHERE guild_id = ?',
                (guild_id,)
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return {'enabled': False, 'channel_id': None}
        return {'enabled': bool(row[0]), 'channel_id': row[1]}

    async def _set_settings(self, guild_id: int, **kwargs):
        current = await self._get_settings(guild_id)
        current.update(kwargs)
        async with aiosqlite.connect(self.bot.db.db_path) as db:
            await db.execute('''
                INSERT INTO aiqa_settings (guild_id, enabled, channel_id)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    channel_id = excluded.channel_id
            ''', (guild_id, int(current['enabled']), current['channel_id']))
            await db.commit()

    # ── Groq call ────────────────────────────────────────────────────────
    async def _ask_ai(self, question: str, author_name: str,
                       guild: discord.Guild | None = None) -> str | None:
        if not GROQ_API_KEY or not self.client:
            logger.error("GROQ_API_KEY is not set — cannot answer AI Q&A questions.")
            return None

        if not self._owner_label:
            # Best-effort refresh in case cog_load's fetch failed earlier
            # (e.g. transient API hiccup on startup).
            await self._refresh_owner_label()

        try:
            completion = await self.client.chat.completions.create(
                model=GROQ_MODEL,
                max_tokens=400,
                messages=[
                    {"role": "system", "content": self._system_prompt(guild)},
                    {"role": "user", "content": f"({author_name} asks) {question}"},
                ],
            )
        except Exception:
            logger.exception("Groq API request failed")
            return None

        if not completion.choices:
            return None

        answer = (completion.choices[0].message.content or "").strip()
        return answer or None

    @staticmethod
    def _chunks(text: str, size: int):
        for i in range(0, len(text), size):
            yield text[i:i + size]

    # ── Listener ─────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        settings = await self._get_settings(message.guild.id)
        if not settings['enabled']:
            return

        mentioned = self.bot.user in message.mentions
        content = message.content.strip()

        if settings['channel_id'] and message.channel.id != settings['channel_id'] and not mentioned:
            return

        clean = re.sub(rf'<@!?{self.bot.user.id}>', '', content).strip() if mentioned else content
        if not clean:
            return

        if not mentioned:
            if not _looks_like_question(clean):
                return

        now = time.monotonic()
        last = self._cooldowns.get(message.author.id, 0)
        if now - last < COOLDOWN_SECONDS:
            return
        self._cooldowns[message.author.id] = now

        async with message.channel.typing():
            answer = await self._ask_ai(clean, message.author.display_name, message.guild)

        if not answer:
            if mentioned:
                await message.reply(
                    embed=E.error("I couldn't reach the AI service right now. Try again in a bit."),
                    mention_author=False
                )
            return

        first = True
        for chunk in self._chunks(answer, MAX_REPLY_CHARS):
            if first:
                await message.reply(chunk, mention_author=False)
                first = False
            else:
                await message.channel.send(chunk)

    # ── /aiqa ────────────────────────────────────────────────────────────
    aiqa = app_commands.Group(name="aiqa", description="Manage the AI auto-answer system.")

    @aiqa.command(name="enable", description="Enable AI auto-answering in this server.")
    async def aiqa_enable(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await self._set_settings(interaction.guild_id, enabled=True)
        await interaction.response.send_message(
            embed=E.success("✅ AI Q&A enabled. I'll now answer any question, not just Minecraft ones."),
            ephemeral=True
        )

    @aiqa.command(name="disable", description="Disable AI auto-answering in this server.")
    async def aiqa_disable(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await self._set_settings(interaction.guild_id, enabled=False)
        await interaction.response.send_message(
            embed=E.success("🚫 AI Q&A disabled."),
            ephemeral=True
        )

    @aiqa.command(name="setchannel", description="Restrict auto-answering to one channel (leave empty = all channels).")
    @app_commands.describe(channel="Channel to restrict to, or omit to allow all channels")
    async def aiqa_setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await self._set_settings(interaction.guild_id, channel_id=channel.id if channel else None)
        msg = f"Channel restricted to {channel.mention}." if channel else "Channel restriction removed — works everywhere now."
        await interaction.response.send_message(embed=E.success(msg), ephemeral=True)

    @aiqa.command(name="status", description="Show current AI Q&A settings.")
    async def aiqa_status(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await self._get_settings(interaction.guild_id)
        chan = f"<#{settings['channel_id']}>" if settings['channel_id'] else "All channels"
        key_status = "✅ Set" if GROQ_API_KEY else "❌ Missing (set GROQ_API_KEY!)"
        owner_status = self._owner_label or "⚠️ Not detected yet"
        await interaction.response.send_message(
            embed=E.base(
                "🤖 AI Q&A Status",
                f"**Enabled:** {'✅ Yes' if settings['enabled'] else '❌ No'}\n"
                f"**Channel:** {chan}\n"
                f"**API Key:** {key_status}\n"
                f"**Model:** `{GROQ_MODEL}` {'(real-time web search)' if 'compound' in GROQ_MODEL else ''}\n"
                f"**Detected Owner:** {owner_status}\n"
                f"**Cooldown:** {COOLDOWN_SECONDS}s per user"
            ),
            ephemeral=True
        )

    @aiqa.command(name="ask", description="Directly ask the AI anything.")
    @app_commands.describe(question="Your question")
    async def aiqa_ask(self, interaction: discord.Interaction, question: str):
        await interaction.response.defer(thinking=True)
        answer = await self._ask_ai(question, interaction.user.display_name, interaction.guild)
        if not answer:
            await interaction.followup.send(
                embed=E.error("Couldn't reach the AI service right now. Try again in a bit.")
            )
            return
        chunks = list(self._chunks(answer, MAX_REPLY_CHARS))
        await interaction.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIQACog(bot))
