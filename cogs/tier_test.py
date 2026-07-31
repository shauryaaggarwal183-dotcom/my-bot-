from __future__ import annotations
import logging
import re
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import PURPLE, PURPLE_DARK, ORANGE, GREY
import utils.embeds as E
# Reusing the SAME pipeline the support tickets use — this is what makes
# log channel, transcript channel, roles, claim/close/reopen/delete all
# work automatically for Tier Tester applications too. Nothing duplicated.
from cogs.tickets import create_ticket, TicketControlView
from cogs.access import require_admin_or_owner

logger = logging.getLogger('TicketBot.tier_test')

# NOTE: Every slash command in this cog is restricted to a server Admin or
# the bot's real Owner via require_admin_or_owner() (shared in cogs/access.py).

# Path to the panel banner image. Place your image file at this exact path
# inside your bot project: Mine-Front-main/assets/tier_testing_banner.png
BANNER_PATH = 'assets/tier_testing_banner.png'
BANNER_FILENAME = 'tier_testing_banner.png'

# ═══════════════════════════════════════════════════════════════════════════════
#  GAMEMODES — separate list per edition. Pick your edition first, then only
#  that edition's gamemodes show up in the dropdown.
#
#  These lists are only used as the DEFAULT seed the first time a server's
#  gamemodes are looked up. After that, each server's list lives in the
#  tier_gamemodes table and can be freely edited from /tieradminpanel without
#  ever touching this file again.
# ═══════════════════════════════════════════════════════════════════════════════

# Java Edition gamemodes
JAVA_GAMEMODES = [
    ('Overall (All Game Modes)', '🌐'),
    ('Nethpot', '🧪'),
    ('Axe', '🪓'),
    ('Dia Pot', '💎'),
    ('Mace', '<:z_mace:1523281275173998602>'),
    ('Spear Mace', '🔱'),
    ('Cart PvP', '🛒'),
    ('Build UHC', '<:z_builduhc:1523281258728394843>'),
    ('Crystal', '<:z_crystalpvp:1523281271122559076>'),
    ('SMP PvP', '🌍'),
    ('Sword PvP', '⚔️'),
]

# Bedrock Edition gamemodes — using your server's exact custom emojis.
BEDROCK_GAMEMODES = [
    ('Boxing', '<:z_boxing:1523281247328276540>'),
    ('MLG Rush', '<:z_mlgrush:1523281279578017812>'),
    ('No Debuff', '<:z_nodebuff:1523281283420258304>'),
    ('BedFight', '<:z_bedfight:1523281245184725154>'),
    ('Build UHC', '<:z_builduhc:1523281258728394843>'),
    ('SkyWars', '<:z_skywars:1523281287392133310>'),
    ('MidFight', '<:z_midfight:1523281277393043626>'),
    ('Battle Rush', '<:z_battlerush:1523281243242762330>'),
    ('Bridge', '<:z_bridge:1523281249194610800>'),
    ('Build', '<:z_build:1523281251484700753>'),
    ('Cave UHC', '<:z_caveuhc:1523281260871553176>'),
    ('Mace', '<:z_mace:1523281275173998602>'),
]

GAMEMODES_BY_EDITION = {
    'Java Edition': JAVA_GAMEMODES,
    'Bedrock Edition': BEDROCK_GAMEMODES,
}

EDITIONS = ('Java Edition', 'Bedrock Edition')


# ═══════════════════════════════════════════════════════════════════════════════
#  TIME FORMATTING — used for the "next slot opens in Xd Yh Zm" cooldown message
# ═══════════════════════════════════════════════════════════════════════════════

def format_duration(seconds: int) -> str:
    """172890 -> '2d 0h 1m' style countdown text."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f'{days}d')
    if hours or days:
        parts.append(f'{hours}h')
    parts.append(f'{minutes}m')
    return ' '.join(parts)


def format_cooldown_period(seconds: int) -> str:
    """172800 -> '2 days', 3600 -> '1 hour', 1800 -> '30 minutes'."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f'{days} day' + ('s' if days != 1 else '')
    if hours:
        return f'{hours} hour' + ('s' if hours != 1 else '')
    return f'{minutes} minute' + ('s' if minutes != 1 else '')


# ═══════════════════════════════════════════════════════════════════════════════
#  STORAGE — self-contained tables, created/managed only here.
#  Does not touch database.py or any table used by the ticket system.
# ═══════════════════════════════════════════════════════════════════════════════

async def _ensure_tier_table(bot):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS tier_settings (
                guild_id                INTEGER PRIMARY KEY,
                java_enabled            INTEGER DEFAULT 1,
                bedrock_enabled         INTEGER DEFAULT 1,
                banner_url              TEXT,
                tier_cooldown_seconds   INTEGER,
                result_channel_id       INTEGER,
                transcript_channel_id   INTEGER,
                log_channel_id          INTEGER,
                close_log_channel_id    INTEGER
            )
        ''')
        # Migration safety net in case an older version of this bot already
        # created tier_settings without the newer columns.
        for col, coltype in (
            ('banner_url', 'TEXT'),
            ('tier_cooldown_seconds', 'INTEGER'),
            ('result_channel_id', 'INTEGER'),
            ('transcript_channel_id', 'INTEGER'),
            ('log_channel_id', 'INTEGER'),
            ('close_log_channel_id', 'INTEGER'),
            ('ping_role_id', 'INTEGER'),
            ('ticket_category_id', 'INTEGER'),
        ):
            try:
                await db.execute(f'ALTER TABLE tier_settings ADD COLUMN {col} {coltype}')
            except Exception:
                pass  # column already exists

        await db.execute('''
            CREATE TABLE IF NOT EXISTS tier_gamemodes (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                edition  TEXT NOT NULL,
                name     TEXT NOT NULL,
                emoji    TEXT NOT NULL
            )
        ''')
        await db.commit()


async def get_tier_settings(bot, guild_id: int) -> dict:
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM tier_settings WHERE guild_id = ?', (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
        await db.execute('INSERT OR IGNORE INTO tier_settings (guild_id) VALUES (?)', (guild_id,))
        await db.commit()
    return {
        'guild_id': guild_id,
        'java_enabled': 1,
        'bedrock_enabled': 1,
        'banner_url': None,
        'tier_cooldown_seconds': None,
        'result_channel_id': None,
        'transcript_channel_id': None,
        'log_channel_id': None,
        'close_log_channel_id': None,
        'ping_role_id': None,
    }


async def set_tier_toggle(bot, guild_id: int, field: str, enabled: bool):
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO tier_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute(
            f'UPDATE tier_settings SET {field} = ? WHERE guild_id = ?',
            (1 if enabled else 0, guild_id)
        )
        await db.commit()


async def set_banner_url(bot, guild_id: int, url: str | None):
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO tier_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute('UPDATE tier_settings SET banner_url = ? WHERE guild_id = ?', (url, guild_id))
        await db.commit()


async def set_tier_cooldown(bot, guild_id: int, seconds: int | None):
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO tier_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute('UPDATE tier_settings SET tier_cooldown_seconds = ? WHERE guild_id = ?', (seconds, guild_id))
        await db.commit()


# Valid tier-tester-only channel fields. These are completely separate from
# the support ticket system's log/transcript channels (bot.db settings) —
# nothing here is shared with anyone/anything else.
TIER_CHANNEL_FIELDS = (
    'result_channel_id',
    'transcript_channel_id',
    'log_channel_id',
    'close_log_channel_id',
)


async def set_tier_channel(bot, guild_id: int, field: str, channel_id: int | None):
    if field not in TIER_CHANNEL_FIELDS:
        raise ValueError(f'Unknown tier channel field: {field}')
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO tier_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute(f'UPDATE tier_settings SET {field} = ? WHERE guild_id = ?', (channel_id, guild_id))
        await db.commit()


async def set_tier_ping_role(bot, guild_id: int, role_id: int | None):
    """The role pinged (and given channel access) whenever a NEW Tier Tester
    application or Tier Test request ticket is created. Independent from
    the support ticket system's own support_role_ids — set separately here
    so tier tester pings can go to a different role (e.g. @Tier Testers)."""
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO tier_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute('UPDATE tier_settings SET ping_role_id = ? WHERE guild_id = ?', (role_id, guild_id))
        await db.commit()


async def set_tier_ticket_category(bot, guild_id: int, category_id: int | None):
    """The Discord category (channel folder) Tier Tester ticket channels get
    created under. Independent from the support ticket system's own
    ticket_category_id — when unset, tier tickets fall back to that default
    category instead."""
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO tier_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute('UPDATE tier_settings SET ticket_category_id = ? WHERE guild_id = ?', (category_id, guild_id))
        await db.commit()


async def get_gamemodes(bot, guild_id: int, edition: str) -> list[tuple[str, str]]:
    """Returns this guild's gamemode list for an edition. The first time it's
    called for a guild, it seeds the table with the built-in defaults above,
    so every server starts out with the same list but can customize it
    independently from then on."""
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT name, emoji FROM tier_gamemodes WHERE guild_id = ? AND edition = ? ORDER BY id',
            (guild_id, edition)
        ) as cur:
            rows = await cur.fetchall()
        if rows:
            return [(r['name'], r['emoji']) for r in rows]

        defaults = GAMEMODES_BY_EDITION.get(edition, [])
        if defaults:
            await db.executemany(
                'INSERT INTO tier_gamemodes (guild_id, edition, name, emoji) VALUES (?, ?, ?, ?)',
                [(guild_id, edition, name, emoji) for name, emoji in defaults]
            )
            await db.commit()
        return list(defaults)


async def add_gamemode(bot, guild_id: int, edition: str, name: str, emoji: str) -> tuple[bool, str]:
    current = await get_gamemodes(bot, guild_id, edition)
    if len(current) >= 25:
        return False, 'You can have a maximum of **25** gamemodes per edition (Discord dropdown limit).'
    if any(n.lower() == name.lower() for n, _ in current):
        return False, f'**{name}** already exists in {edition}.'
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO tier_gamemodes (guild_id, edition, name, emoji) VALUES (?, ?, ?, ?)',
            (guild_id, edition, name, emoji)
        )
        await db.commit()
    return True, f'Added **{emoji} {name}** to {edition}.'


async def remove_gamemode(bot, guild_id: int, edition: str, name: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'DELETE FROM tier_gamemodes WHERE guild_id = ? AND edition = ? AND name = ?',
            (guild_id, edition, name)
        )
        await db.commit()


async def reset_gamemodes(bot, guild_id: int, edition: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'DELETE FROM tier_gamemodes WHERE guild_id = ? AND edition = ?',
            (guild_id, edition)
        )
        await db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
#  MODAL — Tier TESTER staff application (not "get my tier tested")
#  Same visual field style as the main ticket modal.
# ═══════════════════════════════════════════════════════════════════════════════

class TierApplicationModal(discord.ui.Modal):
    """Staff application — someone applying to BECOME a Tier Tester (i.e. to
    test OTHER players and assign them a tier), not to get their own tier
    tested. Questions are about their testing competence and availability,
    not their own rank — that's what TierTestModal below is for."""

    def __init__(self, bot, edition: str, gamemode: str):
        super().__init__(title='Tier Tester Staff Application', timeout=300)
        self.bot = bot
        self.edition = edition
        self.gamemode = gamemode

        self.ign_discord = discord.ui.TextInput(
            label='IGN & Discord Tag',
            placeholder='Gamertag | Discord username',
            required=True,
            max_length=100,
        )
        self.region = discord.ui.TextInput(
            label='Region',
            placeholder='e.g. NA, EU, AS, OCE',
            required=True,
            max_length=50,
        )
        self.experience = discord.ui.TextInput(
            label='Testing / PvP Experience',
            style=discord.TextStyle.paragraph,
            placeholder='Which tiers/gamemodes can you accurately test? Tested before, and where?',
            required=True,
            max_length=400,
        )
        self.why = discord.ui.TextInput(
            label='Why do you want to be a Tier Tester?',
            style=discord.TextStyle.paragraph,
            placeholder='Tell us why you\'d be a good fit for the role',
            required=True,
            max_length=400,
        )
        self.availability = discord.ui.TextInput(
            label='Availability',
            placeholder='e.g. Evenings EU time, ~5 tests/week',
            required=True,
            max_length=100,
        )

        self.add_item(self.ign_discord)
        self.add_item(self.region)
        self.add_item(self.experience)
        self.add_item(self.why)
        self.add_item(self.availability)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        answers = {
            'IGN & Discord Tag': self.ign_discord.value,
            'Gamemode': self.gamemode,
            'Region': self.region.value,
            'Testing / PvP Experience': self.experience.value,
            'Why do you want to be a Tier Tester?': self.why.value,
            'Availability': self.availability.value,
        }
        # Reuses the exact same ticket-creation pipeline as /newticket:
        # same channel setup, same welcome embed style, same DB row,
        # same log-channel logging, same auto-transcript-on-close,
        # same Claim/Close/Reopen/Transcript/Delete buttons.
        tier_settings = await get_tier_settings(self.bot, interaction.guild_id)
        ping_role_id = tier_settings.get('ping_role_id')
        await create_ticket(
            self.bot, interaction, f'Tier Tester App • {self.gamemode} • {self.edition}', answers,
            extra_ping_role_ids=[ping_role_id] if ping_role_id else None,
            category_id=tier_settings.get('ticket_category_id')
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  MODAL — Tier TEST request (a player wants THEIR skill tested/ranked —
#  not applying to become a Tier Tester). Same visual field style, different
#  questions, so it can't be confused with the staff application above.
# ═══════════════════════════════════════════════════════════════════════════════

class TierTestModal(discord.ui.Modal):
    def __init__(self, bot, edition: str, gamemode: str):
        super().__init__(title=f'🧪  {edition} Tier Test Request', timeout=300)
        self.bot = bot
        self.edition = edition
        self.gamemode = gamemode

        self.ign = discord.ui.TextInput(
            label='Minecraft IGN',
            placeholder='Your exact in-game username',
            required=True,
            max_length=32,
        )
        self.current_tier = discord.ui.TextInput(
            label='Current / Claimed Tier',
            placeholder='e.g. HT1, LT3, or "Untested"',
            required=True,
            max_length=50,
        )
        self.region = discord.ui.TextInput(
            label='Region / Ping',
            placeholder='e.g. EU, NA, Asia — helps us match a low-ping tester',
            required=True,
            max_length=100,
        )
        self.availability = discord.ui.TextInput(
            label='Availability for testing',
            style=discord.TextStyle.paragraph,
            placeholder='Best days/times you can hop on for the test',
            required=True,
            max_length=300,
        )

        self.add_item(self.ign)
        self.add_item(self.current_tier)
        self.add_item(self.region)
        self.add_item(self.availability)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        answers = {
            'Minecraft IGN': self.ign.value,
            'Gamemode': self.gamemode,
            'Current / Claimed Tier': self.current_tier.value,
            'Region / Ping': self.region.value,
            'Availability': self.availability.value,
        }
        # Same shared ticket-creation pipeline as everything else — different
        # category label only, so it shows up clearly separate from staff apps.
        tier_settings = await get_tier_settings(self.bot, interaction.guild_id)
        ping_role_id = tier_settings.get('ping_role_id')
        await create_ticket(
            self.bot, interaction, f'Tier Test • {self.gamemode} • {self.edition}', answers,
            extra_ping_role_ids=[ping_role_id] if ping_role_id else None,
            category_id=tier_settings.get('ticket_category_id')
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  PANEL VIEW  (what members see & use to apply)
#
#  Flow now matches the reference 3-step design:
#    1) Public panel has ONE button — "Apply for Tier Test" / "Request a
#       Tier Test" — nothing else visible on the main message.
#    2) Clicking it sends a PRIVATE (ephemeral) "Select Platform" message
#       with a Bedrock and a Java button.
#    3) Picking a platform sends a PRIVATE "Select Game Mode" message with
#       a native Discord multi-select dropdown (checkbox list) scoped to
#       that platform's gamemodes.
#    4) Confirming the dropdown opens the application/request modal, with
#       every gamemode the user ticked joined into one string.
# ═══════════════════════════════════════════════════════════════════════════════

async def _pick_edition(bot, interaction: discord.Interaction, edition: str, kind: str):
    """Shared gatekeeping (closed edition / blacklist / open ticket / cooldown /
    no gamemodes configured) then shows the gamemode multi-select screen."""
    db = bot.db
    label = 'Tier Tester applications' if kind == 'apply' else 'Tier Test requests'

    tier_settings = await get_tier_settings(bot, interaction.guild_id)
    field = 'java_enabled' if edition == 'Java Edition' else 'bedrock_enabled'
    if not tier_settings.get(field, 1):
        return await interaction.response.send_message(
            embed=E.error(f'{edition} {label} are currently closed.'),
            ephemeral=True
        )

    if await db.is_blacklisted(interaction.guild_id, interaction.user.id):
        return await interaction.response.send_message(
            embed=E.error('You are blacklisted from creating tickets.'), ephemeral=True
        )

    existing = await db.get_open_ticket(interaction.guild_id, interaction.user.id)
    if existing:
        ch = interaction.guild.get_channel(existing['channel_id'])
        msg = (f'You already have an open ticket: {ch.mention}'
               if ch else 'You already have an open ticket.')
        return await interaction.response.send_message(embed=E.error(msg), ephemeral=True)

    settings = await db.get_settings(interaction.guild_id)
    default_cooldown = settings.get('cooldown_seconds', 300) if settings else 300
    # Both flows can optionally use their own cooldown, set from
    # /tieradminpanel. Falls back to the ticket system default.
    cooldown = tier_settings.get('tier_cooldown_seconds') or default_cooldown
    remaining = await db.check_cooldown(interaction.guild_id, interaction.user.id, cooldown)
    if remaining > 0:
        period_text = format_cooldown_period(cooldown)
        remaining_text = format_duration(remaining)
        return await interaction.response.send_message(
            embed=E.error(
                f'You can only open **1 ticket every {period_text}**. '
                f'Your next slot opens in **{remaining_text}**.'
            ),
            ephemeral=True
        )

    gamemodes = await get_gamemodes(bot, interaction.guild_id, edition)
    if not gamemodes:
        return await interaction.response.send_message(
            embed=E.error(f'No gamemodes are configured for {edition} yet. Ask an admin to add some via /tieradminpanel.'),
            ephemeral=True
        )

    short = 'Bedrock' if edition == 'Bedrock Edition' else 'Java'
    platform_emoji = '🟢' if edition == 'Bedrock Edition' else '🍲'
    gm_description = (
        'Choose the game mode(s) you can test players in:' if kind == 'apply'
        else 'Choose the game mode you want to be tested in:'
    )
    embed = discord.Embed(
        title=f'{platform_emoji}  {short} — Select Game Mode',
        description=gm_description,
        color=PURPLE,
    )
    await interaction.response.send_message(
        embed=embed,
        view=GamemodeSelectView(bot, edition, gamemodes, kind=kind),
        ephemeral=True,
    )


class PlatformSelectView(discord.ui.View):
    """Ephemeral step 2 — shown after tapping the public 'Apply'/'Request'
    button. Bedrock (green) / Java (blurple), same as the reference panel.

    Only shows buttons for editions currently enabled via the
    Bedrock/Java toggles in /tieradminpanel — an edition that's toggled off
    simply doesn't appear as an option here at all."""

    def __init__(self, bot, kind: str = 'apply', java_enabled: bool = True, bedrock_enabled: bool = True):
        super().__init__(timeout=120)
        self.bot = bot
        self.kind = kind

        if not bedrock_enabled:
            self.remove_item(self.bedrock)
        if not java_enabled:
            self.remove_item(self.java)

    @discord.ui.button(label='Bedrock', emoji='🟢', style=discord.ButtonStyle.success)
    async def bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _pick_edition(self.bot, interaction, 'Bedrock Edition', self.kind)

    @discord.ui.button(label='Java', emoji='🍲', style=discord.ButtonStyle.primary)
    async def java(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _pick_edition(self.bot, interaction, 'Java Edition', self.kind)


class TierPanelView(discord.ui.View):
    """Public, persistent panel — a single 'Apply for Tier Test' button.
    kind='apply' -> staff application to BECOME a Tier Tester.
    kind='test'  -> a player requesting THEIR OWN skill/tier be tested."""

    def __init__(self, bot, kind: str = 'apply'):
        super().__init__(timeout=None)
        self.bot = bot
        self.kind = kind

    @discord.ui.button(label='Apply to be a Tier Tester', emoji='⚔️',
                       style=discord.ButtonStyle.primary, custom_id='tier:apply:start')
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        tier_settings = await get_tier_settings(self.bot, interaction.guild_id)
        java_on = bool(tier_settings.get('java_enabled', 1))
        bedrock_on = bool(tier_settings.get('bedrock_enabled', 1))

        if not java_on and not bedrock_on:
            return await interaction.response.send_message(
                embed=E.error('Tier Tester applications are currently closed for both editions.'),
                ephemeral=True
            )

        embed = discord.Embed(
            title='⚔️  Tier Tester Application — Select Platform',
            description='Which platform do you want to test players on — **Bedrock** or **Java**?',
            color=PURPLE,
        )
        await interaction.response.send_message(
            embed=embed,
            view=PlatformSelectView(self.bot, self.kind, java_enabled=java_on, bedrock_enabled=bedrock_on),
            ephemeral=True
        )


class TierTestPanelView(discord.ui.View):
    """Public, persistent panel for the 'Request a Tier Test' flow — its own
    custom_id so it can coexist with TierPanelView on the same server."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Request a Tier Test', emoji='🧪',
                       style=discord.ButtonStyle.primary, custom_id='tiertest:start')
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        tier_settings = await get_tier_settings(self.bot, interaction.guild_id)
        java_on = bool(tier_settings.get('java_enabled', 1))
        bedrock_on = bool(tier_settings.get('bedrock_enabled', 1))

        if not java_on and not bedrock_on:
            return await interaction.response.send_message(
                embed=E.error('Tier Test requests are currently closed for both editions.'),
                ephemeral=True
            )

        embed = discord.Embed(
            title='🧪  Tier Testing — Select Platform',
            description='Which platform do you want your tier tested on — **Bedrock** or **Java**?',
            color=PURPLE,
        )
        await interaction.response.send_message(
            embed=embed,
            view=PlatformSelectView(self.bot, kind='test', java_enabled=java_on, bedrock_enabled=bedrock_on),
            ephemeral=True
        )


class GamemodeSelectView(discord.ui.View):
    """Ephemeral step 3 — a native Discord multi-select (checkbox list) scoped
    to the chosen platform's gamemodes, e.g. 'Select 1 or more Bedrock game
    modes...'. Ticking one or more and confirming opens the modal, with every
    picked gamemode joined into a single string.

    kind='apply' -> opens the Tier Tester staff-application modal.
    kind='test'  -> opens the Tier Test request modal."""

    def __init__(self, bot, edition: str, gamemodes: list[tuple[str, str]], kind: str = 'apply'):
        super().__init__(timeout=120)
        self.bot = bot
        self.edition = edition
        self.kind = kind

        short = 'Bedrock' if edition == 'Bedrock Edition' else 'Java'
        options = [
            discord.SelectOption(label=name, value=name, emoji=emoji)
            for name, emoji in gamemodes
        ][:25]  # Discord hard cap of 25 options per select

        self.gamemode_select.options = options
        self.gamemode_select.placeholder = f'Select 1 or more {short} game modes...'
        self.gamemode_select.min_values = 1
        self.gamemode_select.max_values = len(options)

    @discord.ui.select()
    async def gamemode_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        gamemode = ', '.join(select.values)
        if self.kind == 'test':
            await interaction.response.send_modal(TierTestModal(self.bot, self.edition, gamemode))
        else:
            await interaction.response.send_modal(TierApplicationModal(self.bot, self.edition, gamemode))


# ═══════════════════════════════════════════════════════════════════════════════
#  PANEL EMBED  (same visual style as the support ticket panel + banner image)
# ═══════════════════════════════════════════════════════════════════════════════

async def tier_panel_embed(bot, guild: discord.Guild) -> discord.Embed:
    """Compact panel — just the intro + footer. The full gamemode list is no
    longer dumped here; it now only shows up later in the private
    'Select Game Mode' dropdown, matching the reference design."""
    settings = await get_tier_settings(bot, guild.id)
    ticket_settings = await bot.db.get_settings(guild.id) or {}
    default_cd = ticket_settings.get('cooldown_seconds', 300)
    cooldown = settings.get('tier_cooldown_seconds') or default_cd
    cooldown_text = format_cooldown_period(cooldown)

    e = discord.Embed(
        title=f'{guild.name} — Tier Tester Applications',
        description=(
            f'Want to **become a Tier Tester** at **{guild.name}**? '
            'Test other players and help assign them their tier.\n'
            'Click the button below to apply — pick **Bedrock** or **Java**.'
        ),
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)

    if settings.get('banner_url'):
        e.set_image(url=settings['banner_url'])
    else:
        e.set_image(url=f'attachment://{BANNER_FILENAME}')
    e.set_footer(text=f'{guild.name} | 1 Ticket every {cooldown_text} | Do not abuse the system')
    return e


async def tier_test_panel_embed(bot, guild: discord.Guild) -> discord.Embed:
    """Same compact layout as tier_panel_embed — wording changed to make
    clear this is 'get YOUR tier tested', not a staff application."""
    settings = await get_tier_settings(bot, guild.id)
    ticket_settings = await bot.db.get_settings(guild.id) or {}
    default_cd = ticket_settings.get('cooldown_seconds', 300)
    cooldown = settings.get('tier_cooldown_seconds') or default_cd
    cooldown_text = format_cooldown_period(cooldown)

    e = discord.Embed(
        title=f'{guild.name} — Tier Test Requests',
        description=(
            f'Want **your** skill tier tested at **{guild.name}**?\n'
            'Click the button below to get started.'
        ),
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)

    if settings.get('banner_url'):
        e.set_image(url=settings['banner_url'])
    else:
        e.set_image(url=f'attachment://{BANNER_FILENAME}')
    e.set_footer(text=f'{guild.name} | 1 Request every {cooldown_text} | Do not abuse the system')
    return e


# ═══════════════════════════════════════════════════════════════════════════════
#  TIER ADMIN PANEL — manage EVERY part of the tier tester system from one place
# ═══════════════════════════════════════════════════════════════════════════════

def tier_admin_embed(guild: discord.Guild, settings: dict,
                      java_gamemodes: list, bedrock_gamemodes: list,
                      default_cooldown: int) -> discord.Embed:
    java = '✅ Open' if settings.get('java_enabled', 1) else '❌ Closed'
    bedrock = '✅ Open' if settings.get('bedrock_enabled', 1) else '❌ Closed'
    banner = settings.get('banner_url') or f'Default local image (`{BANNER_PATH}`)'
    custom_cd = settings.get('tier_cooldown_seconds')
    cooldown_text = (f'**{custom_cd}s** (custom override)' if custom_cd
                      else f'**{default_cooldown}s** (using ticket system default)')

    e = discord.Embed(
        title='🛠️  Tier Tester — Admin Panel',
        description=(
            f'Full control over Tier Tester applications on **{guild.name}**.\n'
            'These edition toggles, gamemodes, banner, and cooldown are shared by '
            'both `/tierpanel` (Tier Tester applications) and `/tiertestpanel` '
            '(Tier Test requests).\n'
            'Use the buttons below to manage every part of the panel.\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━'
        ),
        color=PURPLE_DARK,
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)

    e.add_field(name='🟩  Java Edition', value=java, inline=True)
    e.add_field(name='🟦  Bedrock Edition', value=bedrock, inline=True)
    e.add_field(name='\u200b', value='\u200b', inline=True)
    e.add_field(name='⏱️  Cooldown', value=cooldown_text, inline=True)
    e.add_field(name='🖼️  Banner', value=banner[:1024], inline=True)
    e.add_field(name='\u200b', value='\u200b', inline=True)
    e.add_field(name='🟩  Java Gamemodes', value=f'{len(java_gamemodes)}/25 configured', inline=True)
    e.add_field(name='🟦  Bedrock Gamemodes', value=f'{len(bedrock_gamemodes)}/25 configured', inline=True)
    e.add_field(name='\u200b', value='\u200b', inline=True)

    def ch_text(ch_id):
        return f'<#{ch_id}>' if ch_id else '_Not set_'

    ping_role_id = settings.get('ping_role_id')
    e.add_field(
        name='🔔  Ticket Ping Role',
        value=f'<@&{ping_role_id}>' if ping_role_id else '_Not set — no role pinged on new tickets_',
        inline=True
    )

    ticket_cat_id = settings.get('ticket_category_id')
    e.add_field(
        name='🗂️  Ticket Category',
        value=f'<#{ticket_cat_id}>' if ticket_cat_id else '_Not set — using ticket system default_',
        inline=True
    )
    e.add_field(name='\u200b', value='\u200b', inline=True)

    e.add_field(
        name='📡  Tier Tester Channels',
        value=(
            f'**Result:** {ch_text(settings.get("result_channel_id"))}\n'
            f'**Transcript:** {ch_text(settings.get("transcript_channel_id"))}\n'
            f'**Logs:** {ch_text(settings.get("log_channel_id"))}\n'
            f'**Close Logs:** {ch_text(settings.get("close_log_channel_id"))}\n'
            '*(independent from the support ticket system\'s channels)*'
        ),
        inline=False
    )
    e.set_footer(text='Mine Front Ticket System  •  Tier Admin Panel')
    return e


def gamemode_manage_embed(edition: str, gamemodes: list[tuple[str, str]]) -> discord.Embed:
    lines = '\n'.join(f'{emoji} **{name}**' for name, emoji in gamemodes) or '_No gamemodes configured._'
    e = discord.Embed(
        title=f'🎮  Manage {edition} Gamemodes',
        description=(
            f'{lines}\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━\n'
            'Add a new gamemode, remove one from the dropdown, or reset back '
            'to the built-in default list.'
        ),
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    e.set_footer(text=f'Mine Front Ticket System  •  {len(gamemodes)}/25 gamemodes')
    return e


# ── Modals ───────────────────────────────────────────────────────────────────

class AddGamemodeModal(discord.ui.Modal):
    def __init__(self, bot, edition: str):
        super().__init__(title=f'➕  Add {edition} Gamemode', timeout=120)
        self.bot = bot
        self.edition = edition
        self.name_input = discord.ui.TextInput(
            label='Gamemode Name',
            placeholder='e.g. Sumo',
            required=True,
            max_length=50,
        )
        self.emoji_input = discord.ui.TextInput(
            label='Emoji (unicode or <:name:id>)',
            placeholder='e.g. ⚔️  or  <:z_sumo:123456789012345678>',
            required=False,
            max_length=100,
        )
        self.add_item(self.name_input)
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        name = self.name_input.value.strip()
        emoji = self.emoji_input.value.strip() or '🎮'
        ok, msg = await add_gamemode(self.bot, interaction.guild_id, self.edition, name, emoji)
        await interaction.followup.send(embed=(E.success(msg) if ok else E.error(msg)), ephemeral=True)


class BannerModal(discord.ui.Modal, title='🖼️  Set Panel Banner'):
    url = discord.ui.TextInput(
        label='Image URL',
        placeholder='https://example.com/banner.png',
        required=True,
        max_length=500,
    )

    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        value = self.url.value.strip()
        if not value.lower().startswith(('http://', 'https://')):
            return await interaction.followup.send(
                embed=E.error('Please provide a valid image URL starting with http:// or https://'),
                ephemeral=True)
        await set_banner_url(self.bot, interaction.guild_id, value)
        await interaction.followup.send(
            embed=E.success('Panel banner updated. Run `/tierpanel` again to post a fresh panel with it.'),
            ephemeral=True)


def parse_duration_input(text: str) -> int:
    """Parses a duration like '2d', '12h', '30m', '90s', '1d12h', or a
    plain number (treated as seconds). Raises ValueError on anything else."""
    text = text.strip().lower().replace(' ', '')
    if not text:
        raise ValueError

    if text.isdigit():
        return int(text)

    units = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
    matches = re.findall(r'(\d+)([dhms])', text)
    if not matches or len(''.join(f'{n}{u}' for n, u in matches)) != len(text):
        raise ValueError

    return sum(int(n) * units[u] for n, u in matches)


class TierCooldownModal(discord.ui.Modal, title='⏱️  Set Tier Cooldown Override'):
    seconds = discord.ui.TextInput(
        label='Cooldown (e.g. 2d, 12h, 30m, or seconds)',
        placeholder='e.g. 2d, 12h30m, 300s, or just 300',
        required=True,
        max_length=12,
    )

    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            val = parse_duration_input(self.seconds.value)
            if val < 0:
                raise ValueError
        except ValueError:
            return await interaction.followup.send(
                embed=E.error('Enter a valid duration — e.g. `2d`, `12h`, `30m`, `2d12h`, or a plain number of seconds.'),
                ephemeral=True)
        await set_tier_cooldown(self.bot, interaction.guild_id, val)
        await interaction.followup.send(
            embed=E.success(f'Tier Tester cooldown override set to **{format_duration(val)}** ({val} seconds).'),
            ephemeral=True)


# ── Sub-views ────────────────────────────────────────────────────────────────

class GamemodeResetConfirmView(discord.ui.View):
    def __init__(self, bot, edition: str):
        super().__init__(timeout=60)
        self.bot = bot
        self.edition = edition

    @discord.ui.button(label='Confirm Reset', style=discord.ButtonStyle.danger, emoji='🔄')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reset_gamemodes(self.bot, interaction.guild_id, self.edition)
        await interaction.response.send_message(
            embed=E.success(f'{self.edition} gamemodes have been reset to the default list.'), ephemeral=True)
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary, emoji='✖️')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base('✖️  Cancelled', 'Reset was cancelled.', color=GREY), ephemeral=True)
        self.stop()


class GamemodeManageView(discord.ui.View):
    def __init__(self, bot, edition: str, gamemodes: list[tuple[str, str]]):
        super().__init__(timeout=180)
        self.bot = bot
        self.edition = edition

        if gamemodes:
            self.remove_select.options = [
                discord.SelectOption(label=name, value=name, emoji=emoji)
                for name, emoji in gamemodes[:25]
            ]
            self.remove_select.placeholder = f'Select a {edition} gamemode to remove…'
        else:
            self.remove_select.disabled = True
            self.remove_select.options = [discord.SelectOption(label='No gamemodes configured', value='__none__')]
            self.remove_select.placeholder = 'No gamemodes to remove'

    @discord.ui.button(label='Add Gamemode', emoji='➕', style=discord.ButtonStyle.success, row=0)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddGamemodeModal(self.bot, self.edition))

    @discord.ui.select(row=1)
    async def remove_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        name = select.values[0]
        if name == '__none__':
            return await interaction.response.send_message(embed=E.error('Nothing to remove.'), ephemeral=True)
        await remove_gamemode(self.bot, interaction.guild_id, self.edition, name)
        await interaction.response.send_message(
            embed=E.success(f'Removed **{name}** from {self.edition}.'), ephemeral=True)

    @discord.ui.button(label='Reset to Default', emoji='🔄', style=discord.ButtonStyle.danger, row=2)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base(
                '⚠️  Confirm Reset',
                f'Reset **{self.edition}** gamemodes to the built-in default list?\n'
                'Any custom gamemodes you added will be lost.',
                color=ORANGE
            ),
            view=GamemodeResetConfirmView(self.bot, self.edition),
            ephemeral=True
        )


class TierChannelsView(discord.ui.View):
    """Lets an admin pick the 4 channels used ONLY by the Tier Tester system:
    result, transcript, logs, close-logs. Fully independent from the support
    ticket system's own log/transcript channel settings — nothing here is
    shared with anyone/anything else."""

    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot

    async def _save(self, interaction: discord.Interaction, field: str, label: str, select: discord.ui.ChannelSelect):
        channel = select.values[0] if select.values else None
        await set_tier_channel(self.bot, interaction.guild_id, field, channel.id if channel else None)
        mention = channel.mention if channel else 'Not set'
        await interaction.response.send_message(
            embed=E.success(f'**{label}** channel set to {mention}.'), ephemeral=True)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder='📤  Select the Post Result channel…',
        row=0,
    )
    async def result_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._save(interaction, 'result_channel_id', 'Post Result', select)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder='📄  Select the Transcript channel…',
        row=1,
    )
    async def transcript_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._save(interaction, 'transcript_channel_id', 'Transcript', select)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder='📜  Select the Logs channel…',
        row=2,
    )
    async def logs_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._save(interaction, 'log_channel_id', 'Logs', select)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder='🔒  Select the Close Logs channel…',
        row=3,
    )
    async def close_logs_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._save(interaction, 'close_log_channel_id', 'Close Logs', select)

    @discord.ui.button(label='Clear All', emoji='🧹', style=discord.ButtonStyle.danger, row=4)
    async def clear_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        for field in TIER_CHANNEL_FIELDS:
            await set_tier_channel(self.bot, interaction.guild_id, field, None)
        await interaction.response.send_message(
            embed=E.success('All Tier Tester channels have been cleared.'), ephemeral=True)


class TierPingRoleView(discord.ui.View):
    """Lets an admin pick the role that gets pinged (and given channel
    access) whenever a NEW Tier Tester application or Tier Test request
    ticket is created. Independent from the support ticket system's own
    support role list."""

    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder='🔔  Select the role to ping on new tier tickets…',
        row=0,
    )
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0] if select.values else None
        await set_tier_ping_role(self.bot, interaction.guild_id, role.id if role else None)
        mention = role.mention if role else 'Not set'
        await interaction.response.send_message(
            embed=E.success(f'Tier ticket ping role set to {mention}.'), ephemeral=True)

    @discord.ui.button(label='Clear Ping Role', emoji='🧹', style=discord.ButtonStyle.danger, row=1)
    async def clear_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_tier_ping_role(self.bot, interaction.guild_id, None)
        await interaction.response.send_message(
            embed=E.success('Tier ticket ping role cleared — no role will be pinged now.'), ephemeral=True)


class TierTicketCategoryView(discord.ui.View):
    """Lets an admin pick the Discord category (channel folder) that Tier
    Tester ticket channels get created under. Independent from the support
    ticket system's own ticket category — leave unset to keep using that
    default category instead."""

    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.category],
        placeholder='🗂️  Select the category for Tier Tester tickets…',
        row=0,
    )
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        category = select.values[0] if select.values else None
        await set_tier_ticket_category(self.bot, interaction.guild_id, category.id if category else None)
        mention = category.mention if category else 'Not set'
        await interaction.response.send_message(
            embed=E.success(f'Tier Tester ticket category set to **{mention}**.'), ephemeral=True)

    @discord.ui.button(label='Clear Category', emoji='🧹', style=discord.ButtonStyle.danger, row=1)
    async def clear_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_tier_ticket_category(self.bot, interaction.guild_id, None)
        await interaction.response.send_message(
            embed=E.success('Tier Tester ticket category cleared — using the ticket system default category again.'),
            ephemeral=True)


class TierAdminPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def _refresh(self, interaction: discord.Interaction):
        settings = await get_tier_settings(self.bot, interaction.guild_id)
        java_gm = await get_gamemodes(self.bot, interaction.guild_id, 'Java Edition')
        bedrock_gm = await get_gamemodes(self.bot, interaction.guild_id, 'Bedrock Edition')
        ticket_settings = await self.bot.db.get_settings(interaction.guild_id) or {}
        default_cd = ticket_settings.get('cooldown_seconds', 300)
        embed = tier_admin_embed(interaction.guild, settings, java_gm, bedrock_gm, default_cd)
        await interaction.response.edit_message(embed=embed, view=self)

    # ── Row 0: toggles ──────────────────────────────────────────────────────
    @discord.ui.button(label='Toggle Java', emoji='🟩', style=discord.ButtonStyle.success, row=0)
    async def toggle_java(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_tier_settings(self.bot, interaction.guild_id)
        await set_tier_toggle(self.bot, interaction.guild_id, 'java_enabled', not bool(settings.get('java_enabled', 1)))
        await self._refresh(interaction)

    @discord.ui.button(label='Toggle Bedrock', emoji='🟦', style=discord.ButtonStyle.primary, row=0)
    async def toggle_bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_tier_settings(self.bot, interaction.guild_id)
        await set_tier_toggle(self.bot, interaction.guild_id, 'bedrock_enabled', not bool(settings.get('bedrock_enabled', 1)))
        await self._refresh(interaction)

    @discord.ui.button(label='Refresh', emoji='🔄', style=discord.ButtonStyle.secondary, row=0)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._refresh(interaction)

    # ── Row 1: gamemode management ──────────────────────────────────────────
    @discord.ui.button(label='Java Gamemodes', emoji='🎮', style=discord.ButtonStyle.secondary, row=1)
    async def manage_java(self, interaction: discord.Interaction, button: discord.ui.Button):
        gamemodes = await get_gamemodes(self.bot, interaction.guild_id, 'Java Edition')
        await interaction.response.send_message(
            embed=gamemode_manage_embed('Java Edition', gamemodes),
            view=GamemodeManageView(self.bot, 'Java Edition', gamemodes),
            ephemeral=True
        )

    @discord.ui.button(label='Bedrock Gamemodes', emoji='🎮', style=discord.ButtonStyle.secondary, row=1)
    async def manage_bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        gamemodes = await get_gamemodes(self.bot, interaction.guild_id, 'Bedrock Edition')
        await interaction.response.send_message(
            embed=gamemode_manage_embed('Bedrock Edition', gamemodes),
            view=GamemodeManageView(self.bot, 'Bedrock Edition', gamemodes),
            ephemeral=True
        )

    # ── Row 2: banner ────────────────────────────────────────────────────────
    @discord.ui.button(label='Set Banner URL', emoji='🖼️', style=discord.ButtonStyle.secondary, row=2)
    async def set_banner(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BannerModal(self.bot))

    @discord.ui.button(label='Clear Banner', emoji='🧹', style=discord.ButtonStyle.secondary, row=2)
    async def clear_banner(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_banner_url(self.bot, interaction.guild_id, None)
        await interaction.response.send_message(
            embed=E.success('Banner reset — the default local image will be used again.'), ephemeral=True)

    # ── Row 3: cooldown ──────────────────────────────────────────────────────
    @discord.ui.button(label='Set Cooldown', emoji='⏱️', style=discord.ButtonStyle.secondary, row=3)
    async def set_cooldown(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TierCooldownModal(self.bot))

    @discord.ui.button(label='Clear Cooldown', emoji='♻️', style=discord.ButtonStyle.secondary, row=3)
    async def clear_cooldown(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_tier_cooldown(self.bot, interaction.guild_id, None)
        await interaction.response.send_message(
            embed=E.success('Cooldown override cleared — using the ticket system default again.'), ephemeral=True)

    # ── Row 4: dedicated tier-tester channels + ping role ───────────────────
    @discord.ui.button(label='Set Channels', emoji='📡', style=discord.ButtonStyle.primary, row=4)
    async def set_channels(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base(
                '📡  Tier Tester Channels',
                'Pick the channel for each purpose below. These are used **only** '
                'by the Tier Tester system and are independent of the support '
                'ticket system\'s channels.\n\n'
                '• **Post Result** — where tier test results get posted\n'
                '• **Transcript** — where closed ticket transcripts get sent\n'
                '• **Logs** — general Tier Tester activity logs\n'
                '• **Close Logs** — logged when a Tier Tester ticket is closed',
                color=PURPLE
            ),
            view=TierChannelsView(self.bot),
            ephemeral=True
        )

    @discord.ui.button(label='Set Ping Role', emoji='🔔', style=discord.ButtonStyle.secondary, row=4)
    async def set_ping_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base(
                '🔔  Tier Ticket Ping Role',
                'Pick the role that should be pinged (and automatically given '
                'access to the ticket channel) whenever a **new** Tier Tester '
                'application or Tier Test request comes in.\n\n'
                'This is independent from the support ticket system\'s own '
                'support roles — use it if you want tier tickets to ping a '
                'different role (e.g. `@Tier Testers`).',
                color=PURPLE
            ),
            view=TierPingRoleView(self.bot),
            ephemeral=True
        )

    @discord.ui.button(label='Set Ticket Category', emoji='🗂️', style=discord.ButtonStyle.secondary, row=4)
    async def set_ticket_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base(
                '🗂️  Tier Tester Ticket Category',
                'Pick the Discord category that **new Tier Tester ticket '
                'channels** should be created under.\n\n'
                'This is independent from the support ticket system\'s own '
                'ticket category — leave it unset to keep using that default '
                'category instead.',
                color=PURPLE
            ),
            view=TierTicketCategoryView(self.bot),
            ephemeral=True
        )


# ── Legacy quick-toggle settings (kept for backwards compatibility) ─────────

def tier_settings_embed(guild: discord.Guild, settings: dict) -> discord.Embed:
    java = '✅ Enabled' if settings.get('java_enabled', 1) else '❌ Disabled'
    bedrock = '✅ Enabled' if settings.get('bedrock_enabled', 1) else '❌ Disabled'
    e = discord.Embed(
        title='🎮  Tier Tester Settings',
        description=f'Managing Tier Tester applications for **{guild.name}**\n\n'
                    'Toggle which editions can currently apply to become a Tier Tester.\n'
                    '> For full control (gamemodes, banner, cooldown), use `/tieradminpanel`.',
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name='🟩 Java Edition', value=java, inline=True)
    e.add_field(name='🟦 Bedrock Edition', value=bedrock, inline=True)
    e.set_footer(text='Mine Front Ticket System  •  Tier Tester Settings')
    return e


class TierSettingsView(discord.ui.View):
    def __init__(self, bot, settings: dict):
        super().__init__(timeout=120)
        self.bot = bot
        self.settings = settings

    @discord.ui.button(label='Toggle Java', emoji='🟩', style=discord.ButtonStyle.success)
    async def toggle_java(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_val = not bool(self.settings.get('java_enabled', 1))
        await set_tier_toggle(self.bot, interaction.guild_id, 'java_enabled', new_val)
        self.settings['java_enabled'] = int(new_val)
        status = '✅ Enabled' if new_val else '❌ Disabled'
        await interaction.response.send_message(
            embed=E.success(f'Java Edition Tier Tester applications: **{status}**'), ephemeral=True)

    @discord.ui.button(label='Toggle Bedrock', emoji='🟦', style=discord.ButtonStyle.primary)
    async def toggle_bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_val = not bool(self.settings.get('bedrock_enabled', 1))
        await set_tier_toggle(self.bot, interaction.guild_id, 'bedrock_enabled', new_val)
        self.settings['bedrock_enabled'] = int(new_val)
        status = '✅ Enabled' if new_val else '❌ Disabled'
        await interaction.response.send_message(
            embed=E.success(f'Bedrock Edition Tier Tester applications: **{status}**'), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  COG
# ═══════════════════════════════════════════════════════════════════════════════

class TierPanel(commands.Cog, name='TierTest'):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(TierPanelView(bot, kind='apply'))
        bot.add_view(TierTestPanelView(bot))
        # NOTE: TicketControlView is already registered persistently by TicketsCog,
        # so it is not re-registered here to avoid touching that flow.

    @app_commands.command(name='tierpanel', description='(Admin/Owner only) Post the Tier Tester application panel.')
    async def tierpanel(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_tier_settings(self.bot, interaction.guild_id)
        embed = await tier_panel_embed(self.bot, interaction.guild)
        view = TierPanelView(self.bot, kind='apply')

        if settings.get('banner_url'):
            # Using a custom banner URL — no file attachment needed.
            await interaction.response.send_message(embed=embed, view=view)
            return

        try:
            file = discord.File(BANNER_PATH, filename=BANNER_FILENAME)
            await interaction.response.send_message(embed=embed, view=view, file=file)
        except FileNotFoundError:
            logger.warning(f'Banner image not found at {BANNER_PATH}, sending without image.')
            embed.set_image(url=None)
            await interaction.response.send_message(embed=embed, view=view)

    # ── /tiertestpanel — post the "request YOUR tier be tested" panel ─────────
    # Same UI/emojis/gamemode lists as /tierpanel, different questions asked
    # once a gamemode is picked (see TierTestModal).
    @app_commands.command(name='tiertestpanel', description='(Admin/Owner only) Post the Tier Test request panel (players requesting their tier be tested).')
    async def tiertestpanel(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_tier_settings(self.bot, interaction.guild_id)
        embed = await tier_test_panel_embed(self.bot, interaction.guild)
        view = TierTestPanelView(self.bot)

        if settings.get('banner_url'):
            await interaction.response.send_message(embed=embed, view=view)
            return

        try:
            file = discord.File(BANNER_PATH, filename=BANNER_FILENAME)
            await interaction.response.send_message(embed=embed, view=view, file=file)
        except FileNotFoundError:
            logger.warning(f'Banner image not found at {BANNER_PATH}, sending without image.')
            embed.set_image(url=None)
            await interaction.response.send_message(embed=embed, view=view)

    # /tiersettings — quick legacy toggle, still works exactly as before.
    @app_commands.command(name='tiersettings', description='(Admin/Owner only) Quickly toggle Java/Bedrock Tier Tester applications on-off.')
    async def tiersettings(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_tier_settings(self.bot, interaction.guild_id)
        await interaction.response.send_message(
            embed=tier_settings_embed(interaction.guild, settings),
            view=TierSettingsView(self.bot, settings),
            ephemeral=True
        )

    # /tieradminpanel — the full control center for everything tier-related:
    # edition toggles, per-edition gamemode add/remove/reset, custom banner
    # URL, and an optional cooldown override just for Tier Tester tickets.
    # Uses the SAME guild_settings (log channel, transcript channel, roles)
    # as your support tickets, because create_ticket() above is shared.
    @app_commands.command(name='tieradminpanel', description='(Admin/Owner only) Full admin panel to manage everything about the Tier Tester system.')
    async def tieradminpanel(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_tier_settings(self.bot, interaction.guild_id)
        java_gm = await get_gamemodes(self.bot, interaction.guild_id, 'Java Edition')
        bedrock_gm = await get_gamemodes(self.bot, interaction.guild_id, 'Bedrock Edition')
        ticket_settings = await self.bot.db.get_settings(interaction.guild_id) or {}
        default_cd = ticket_settings.get('cooldown_seconds', 300)

        embed = tier_admin_embed(interaction.guild, settings, java_gm, bedrock_gm, default_cd)
        await interaction.response.send_message(embed=embed, view=TierAdminPanelView(self.bot), ephemeral=True)


async def setup(bot):
    await bot.add_cog(TierPanel(bot))
