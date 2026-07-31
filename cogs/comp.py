from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import PURPLE, PURPLE_DARK, GREEN, RED, ORANGE, GREY, BLUE
import utils.embeds as E
from cogs.access import require_admin_or_owner, is_admin_or_owner
# Reuse the exact same gamemode lists/editions Tier Testing already uses so
# both systems stay visually/conceptually consistent. Comp Fight keeps its
# OWN copy of each server's list in comp_gamemodes though, so editing one
# system's gamemodes from its own admin panel never touches the other.
from cogs.tier_test import GAMEMODES_BY_EDITION, EDITIONS

logger = logging.getLogger('TicketBot.comp')

# NOTE: Every admin-only slash command / button in this cog is gated with
# require_admin_or_owner() (shared in cogs/access.py) — same permission
# model as every other cog in this bot.

# ═══════════════════════════════════════════════════════════════════════════════
#  HOW THIS SYSTEM WORKS (no ELO, no auto-posted public matches)
#
#  1) Member clicks "Join Queue" on the public panel.
#  2) Picks their platform  → Java or Bedrock.
#  3) Picks their game mode → whatever's configured for that platform.
#  4) A modal pops up asking for their exact Minecraft username.
#  5) They pick their current/claimed tier from a dropdown.
#  6) They're placed in queue. The instant another member is waiting in the
#     SAME platform + game mode queue, both are pulled out and a private
#     MATCH TICKET channel is created for just the two of them + staff —
#     exactly like big competitive PvP servers run their comp fight/duel
#     queues, instead of a public embed with Ready buttons and ELO math.
#  7) Inside the ticket, staff report the result (or a no-show), which
#     updates each player's win/loss record, streak, and (optionally) tier.
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
#  TUNABLES
# ═══════════════════════════════════════════════════════════════════════════════

DODGE_THRESHOLD = 3          # no-shows within the window before a cooldown kicks in
DODGE_WINDOW_HOURS = 24      # rolling window for counting no-shows
DODGE_COOLDOWN_MINUTES = 30  # how long queueing is blocked once threshold is hit
STREAK_FIRE_THRESHOLD = 3    # win/loss streak length before showing the badge

# Default tier ladder seeded the first time a server uses this system. Fully
# editable afterwards from /compadminpanel → Manage Tiers, without ever
# touching this file again. Order matters — first = highest tier.
DEFAULT_TIERS = ['HT1', 'LT1', 'HT2', 'LT2', 'HT3', 'LT3', 'HT4', 'LT4', 'HT5', 'LT5']
UNRANKED = 'Unranked'


# ═══════════════════════════════════════════════════════════════════════════════
#  STORAGE — fully self-contained tables, only touched from this file.
# ═══════════════════════════════════════════════════════════════════════════════

async def _ensure_tables(bot):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_settings (
                guild_id                INTEGER PRIMARY KEY,
                java_enabled            INTEGER DEFAULT 1,
                bedrock_enabled         INTEGER DEFAULT 1,
                queue_channel_id        INTEGER,
                ticket_category_id      INTEGER,
                log_channel_id          INTEGER,
                ping_role_id            INTEGER,
                queue_status_message_id INTEGER,
                season_number           INTEGER DEFAULT 1
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_gamemodes (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                edition  TEXT NOT NULL,
                name     TEXT NOT NULL,
                emoji    TEXT NOT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_tiers (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name     TEXT NOT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_players (
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                edition    TEXT NOT NULL,
                gamemode   TEXT NOT NULL,
                ign        TEXT DEFAULT '',
                tier_label TEXT DEFAULT 'Unranked',
                wins       INTEGER DEFAULT 0,
                losses     INTEGER DEFAULT 0,
                streak     INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, edition, gamemode)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_queue (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                edition    TEXT NOT NULL,
                gamemode   TEXT NOT NULL,
                ign        TEXT NOT NULL,
                tier_label TEXT DEFAULT 'Unranked',
                queued_at  TEXT DEFAULT (datetime('now'))
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_matches (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id     INTEGER NOT NULL,
                edition      TEXT NOT NULL,
                gamemode     TEXT NOT NULL,
                channel_id   INTEGER,
                message_id   INTEGER,
                player1_id   INTEGER NOT NULL,
                player1_ign  TEXT NOT NULL,
                player1_tier TEXT NOT NULL DEFAULT 'Unranked',
                player2_id   INTEGER NOT NULL,
                player2_ign  TEXT NOT NULL,
                player2_tier TEXT NOT NULL DEFAULT 'Unranked',
                status       TEXT DEFAULT 'open',
                winner_id    INTEGER,
                score        TEXT,
                notes        TEXT,
                created_at   TEXT DEFAULT (datetime('now')),
                completed_at TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_tier_roles (
                guild_id   INTEGER NOT NULL,
                tier_label TEXT NOT NULL,
                role_id    INTEGER NOT NULL,
                PRIMARY KEY (guild_id, tier_label)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_dodge_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                match_id INTEGER,
                logged_at TEXT DEFAULT (datetime('now'))
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_cooldowns (
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                until_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_season_archive (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                season     INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                edition    TEXT NOT NULL,
                gamemode   TEXT NOT NULL,
                tier_label TEXT,
                wins       INTEGER,
                losses     INTEGER,
                streak     INTEGER,
                archived_at TEXT DEFAULT (datetime('now'))
            )
        ''')
        await db.commit()


# ── Settings ─────────────────────────────────────────────────────────────────

async def get_comp_settings(bot, guild_id: int) -> dict:
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM comp_settings WHERE guild_id = ?', (guild_id,)) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
        await db.execute('INSERT OR IGNORE INTO comp_settings (guild_id) VALUES (?)', (guild_id,))
        await db.commit()
    return {
        'guild_id': guild_id, 'java_enabled': 1, 'bedrock_enabled': 1,
        'queue_channel_id': None, 'ticket_category_id': None,
        'log_channel_id': None, 'ping_role_id': None,
        'queue_status_message_id': None, 'season_number': 1,
    }


async def set_comp_toggle(bot, guild_id: int, field: str, enabled: bool):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO comp_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute(f'UPDATE comp_settings SET {field} = ? WHERE guild_id = ?', (1 if enabled else 0, guild_id))
        await db.commit()


COMP_CHANNEL_FIELDS = ('queue_channel_id', 'log_channel_id')


async def set_comp_channel(bot, guild_id: int, field: str, channel_id: int | None):
    if field not in COMP_CHANNEL_FIELDS:
        raise ValueError(f'Unknown comp channel field: {field}')
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO comp_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute(f'UPDATE comp_settings SET {field} = ? WHERE guild_id = ?', (channel_id, guild_id))
        await db.commit()


async def set_comp_ticket_category(bot, guild_id: int, category_id: int | None):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO comp_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute('UPDATE comp_settings SET ticket_category_id = ? WHERE guild_id = ?', (category_id, guild_id))
        await db.commit()


async def set_comp_ping_role(bot, guild_id: int, role_id: int | None):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO comp_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute('UPDATE comp_settings SET ping_role_id = ? WHERE guild_id = ?', (role_id, guild_id))
        await db.commit()


async def set_queue_status_message(bot, guild_id: int, message_id: int | None):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO comp_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute('UPDATE comp_settings SET queue_status_message_id = ? WHERE guild_id = ?', (message_id, guild_id))
        await db.commit()


# ── Gamemodes (own copy, seeded from tier_test's default lists) ────────────

async def get_comp_gamemodes(bot, guild_id: int, edition: str) -> list[tuple[str, str]]:
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT name, emoji FROM comp_gamemodes WHERE guild_id = ? AND edition = ? ORDER BY id',
            (guild_id, edition)
        ) as cur:
            rows = await cur.fetchall()
        if rows:
            return [(r['name'], r['emoji']) for r in rows]
        defaults = GAMEMODES_BY_EDITION.get(edition, [])
        if defaults:
            await db.executemany(
                'INSERT INTO comp_gamemodes (guild_id, edition, name, emoji) VALUES (?, ?, ?, ?)',
                [(guild_id, edition, name, emoji) for name, emoji in defaults]
            )
            await db.commit()
        return list(defaults)


async def add_comp_gamemode(bot, guild_id: int, edition: str, name: str, emoji: str) -> tuple[bool, str]:
    current = await get_comp_gamemodes(bot, guild_id, edition)
    if len(current) >= 25:
        return False, 'You can have a maximum of **25** gamemodes per edition (Discord dropdown limit).'
    if any(n.lower() == name.lower() for n, _ in current):
        return False, f'**{name}** already exists in {edition}.'
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO comp_gamemodes (guild_id, edition, name, emoji) VALUES (?, ?, ?, ?)',
            (guild_id, edition, name, emoji)
        )
        await db.commit()
    return True, f'Added **{emoji} {name}** to {edition}.'


async def remove_comp_gamemode(bot, guild_id: int, edition: str, name: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('DELETE FROM comp_gamemodes WHERE guild_id=? AND edition=? AND name=?',
                          (guild_id, edition, name))
        await db.commit()


async def reset_comp_gamemodes(bot, guild_id: int, edition: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('DELETE FROM comp_gamemodes WHERE guild_id=? AND edition=?', (guild_id, edition))
        await db.commit()
    await get_comp_gamemodes(bot, guild_id, edition)  # re-seeds defaults


# ── Tiers (own configurable ladder, per guild) ──────────────────────────────

async def get_comp_tiers(bot, guild_id: int) -> list[str]:
    """Returns the tier ladder in rank order (best first). Seeds the
    DEFAULT_TIERS the first time a guild uses the system."""
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT name FROM comp_tiers WHERE guild_id = ? ORDER BY id', (guild_id,)
        ) as cur:
            rows = await cur.fetchall()
        if rows:
            return [r['name'] for r in rows]
        await db.executemany(
            'INSERT INTO comp_tiers (guild_id, name) VALUES (?, ?)',
            [(guild_id, name) for name in DEFAULT_TIERS]
        )
        await db.commit()
    return list(DEFAULT_TIERS)


async def add_comp_tier(bot, guild_id: int, name: str) -> tuple[bool, str]:
    current = await get_comp_tiers(bot, guild_id)
    if len(current) >= 24:  # leave room for the always-appended "Unranked" option
        return False, 'You can have a maximum of **24** tiers (Discord dropdown limit).'
    if any(t.lower() == name.lower() for t in current):
        return False, f'**{name}** already exists.'
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT INTO comp_tiers (guild_id, name) VALUES (?, ?)', (guild_id, name))
        await db.commit()
    return True, f'Added tier **{name}**.'


async def remove_comp_tier(bot, guild_id: int, name: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('DELETE FROM comp_tiers WHERE guild_id=? AND name=?', (guild_id, name))
        await db.commit()


async def reset_comp_tiers(bot, guild_id: int):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('DELETE FROM comp_tiers WHERE guild_id=?', (guild_id,))
        await db.commit()
    await get_comp_tiers(bot, guild_id)


def tier_rank(tiers: list[str], tier_label: str) -> int:
    """Lower = better. Anything not on the ladder (e.g. Unranked) sorts last."""
    try:
        return tiers.index(tier_label)
    except ValueError:
        return len(tiers) + 1


# ── Players ──────────────────────────────────────────────────────────────

async def get_player(bot, guild_id: int, user_id: int, edition: str, gamemode: str) -> dict:
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM comp_players WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=?',
            (guild_id, user_id, edition, gamemode)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
    return {'guild_id': guild_id, 'user_id': user_id, 'edition': edition, 'gamemode': gamemode,
            'ign': '', 'tier_label': UNRANKED, 'wins': 0, 'losses': 0, 'streak': 0}


async def upsert_player_queue_info(bot, guild_id: int, user_id: int, edition: str, gamemode: str,
                                    ign: str, tier_label: str):
    """Called the moment someone joins queue — records their claimed IGN and
    tier for this edition/gamemode, creating the player row if needed."""
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO comp_players (guild_id, user_id, edition, gamemode, ign, tier_label) VALUES (?,?,?,?,?,?) '
            'ON CONFLICT(guild_id, user_id, edition, gamemode) DO UPDATE SET ign=excluded.ign, tier_label=excluded.tier_label',
            (guild_id, user_id, edition, gamemode, ign, tier_label)
        )
        await db.commit()


async def set_player_tier(bot, guild_id: int, user_id: int, edition: str, gamemode: str, tier_label: str):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO comp_players (guild_id, user_id, edition, gamemode, tier_label) VALUES (?,?,?,?,?) '
            'ON CONFLICT(guild_id, user_id, edition, gamemode) DO UPDATE SET tier_label=excluded.tier_label',
            (guild_id, user_id, edition, gamemode, tier_label)
        )
        await db.commit()


async def record_match_result(bot, guild_id: int, edition: str, gamemode: str,
                               winner_id: int, loser_id: int,
                               winner_new_tier: str | None, loser_new_tier: str | None):
    """Applies a win/loss and a streak update to both players. No ELO — pure
    win/loss record + tier, exactly like a real tier-test ladder. If a new
    tier is supplied it's applied to that player."""
    await _ensure_tables(bot)
    winner = await get_player(bot, guild_id, winner_id, edition, gamemode)
    loser = await get_player(bot, guild_id, loser_id, edition, gamemode)

    winner_streak = winner['streak'] + 1 if winner['streak'] >= 0 else 1
    loser_streak = loser['streak'] - 1 if loser['streak'] <= 0 else -1

    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO comp_players (guild_id, user_id, edition, gamemode, wins, streak) VALUES (?,?,?,?,1,?) '
            'ON CONFLICT(guild_id, user_id, edition, gamemode) DO UPDATE SET wins = wins + 1, streak = ?',
            (guild_id, winner_id, edition, gamemode, winner_streak, winner_streak)
        )
        if winner_new_tier:
            await db.execute(
                'UPDATE comp_players SET tier_label = ? WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=?',
                (winner_new_tier, guild_id, winner_id, edition, gamemode)
            )
        await db.execute(
            'INSERT INTO comp_players (guild_id, user_id, edition, gamemode, losses, streak) VALUES (?,?,?,?,1,?) '
            'ON CONFLICT(guild_id, user_id, edition, gamemode) DO UPDATE SET losses = losses + 1, streak = ?',
            (guild_id, loser_id, edition, gamemode, loser_streak, loser_streak)
        )
        if loser_new_tier:
            await db.execute(
                'UPDATE comp_players SET tier_label = ? WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=?',
                (loser_new_tier, guild_id, loser_id, edition, gamemode)
            )
        await db.commit()


# ── Tier roles (auto-assigned Discord role per claimed tier label) ─────────

async def get_tier_roles(bot, guild_id: int) -> dict[str, int]:
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT tier_label, role_id FROM comp_tier_roles WHERE guild_id = ?', (guild_id,)) as cur:
            return {r['tier_label']: r['role_id'] for r in await cur.fetchall()}


async def set_tier_role(bot, guild_id: int, tier_label: str, role_id: int):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO comp_tier_roles (guild_id, tier_label, role_id) VALUES (?,?,?) '
            'ON CONFLICT(guild_id, tier_label) DO UPDATE SET role_id=excluded.role_id',
            (guild_id, tier_label, role_id)
        )
        await db.commit()


async def remove_tier_role(bot, guild_id: int, tier_label: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('DELETE FROM comp_tier_roles WHERE guild_id=? AND tier_label=?', (guild_id, tier_label))
        await db.commit()


async def sync_tier_roles(bot, guild: discord.Guild, user_ids: list[int]):
    """Gives each member the Discord role mapped to their *best-ranked*
    current tier across all edition/gamemode combos (best = earliest in the
    configured tier ladder) and strips every other mapped tier role — keeps
    someone from accumulating every tier role they ever touched. Silently
    skips anyone missing perms/roles/not in guild."""
    mapping = await get_tier_roles(bot, guild.id)
    if not mapping:
        return
    tiers = await get_comp_tiers(bot, guild.id)
    mapped_role_ids = set(mapping.values())
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        for uid in user_ids:
            member = guild.get_member(uid)
            if not member:
                continue
            async with db.execute(
                'SELECT tier_label FROM comp_players WHERE guild_id=? AND user_id=? AND tier_label IS NOT NULL',
                (guild.id, uid)
            ) as cur:
                rows = await cur.fetchall()
            tiers_held = {r['tier_label'] for r in rows} & set(mapping.keys())
            target_role_id = None
            if tiers_held:
                best_tier = min(tiers_held, key=lambda t: tier_rank(tiers, t))
                target_role_id = mapping[best_tier]
            to_remove = [r for r in member.roles if r.id in mapped_role_ids and r.id != target_role_id]
            try:
                if to_remove:
                    await member.remove_roles(*to_remove, reason='Comp Fight tier role sync')
                if target_role_id and not any(r.id == target_role_id for r in member.roles):
                    role = guild.get_role(target_role_id)
                    if role:
                        await member.add_roles(role, reason='Comp Fight tier role sync')
            except (discord.Forbidden, discord.HTTPException):
                pass


# ── Anti-dodge (no-shows inside match tickets) ──────────────────────────────

async def record_dodge(bot, guild: discord.Guild, user_id: int, match_id: int | None = None) -> int:
    """Logs a no-show and, if the user has crossed DODGE_THRESHOLD within the
    window, places them on a queueing cooldown. Returns the current count
    within the window (after logging this one)."""
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO comp_dodge_log (guild_id, user_id, match_id) VALUES (?,?,?)',
            (guild.id, user_id, match_id)
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM comp_dodge_log WHERE guild_id=? AND user_id=? "
            "AND logged_at >= datetime('now', ?)",
            (guild.id, user_id, f'-{DODGE_WINDOW_HOURS} hours')
        ) as cur:
            count = (await cur.fetchone())[0]
        if count >= DODGE_THRESHOLD:
            await db.execute(
                "INSERT INTO comp_cooldowns (guild_id, user_id, until_at) VALUES (?,?, datetime('now', ?)) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET until_at=excluded.until_at",
                (guild.id, user_id, f'+{DODGE_COOLDOWN_MINUTES} minutes')
            )
            await db.commit()
    return count


async def get_cooldown_remaining(bot, guild_id: int, user_id: int) -> int | None:
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT until_at FROM comp_cooldowns WHERE guild_id=? AND user_id=?', (guild_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        until = datetime.fromisoformat(row['until_at'].replace(' ', 'T')).replace(tzinfo=timezone.utc)
        remaining = (until - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            await db.execute('DELETE FROM comp_cooldowns WHERE guild_id=? AND user_id=?', (guild_id, user_id))
            await db.commit()
            return None
        return max(1, round(remaining / 60))


async def clear_cooldown(bot, guild_id: int, user_id: int):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('DELETE FROM comp_cooldowns WHERE guild_id=? AND user_id=?', (guild_id, user_id))
        await db.commit()


# ── Queue ────────────────────────────────────────────────────────────────

async def get_queue_entry(bot, guild_id: int, user_id: int, edition: str, gamemode: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM comp_queue WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=?',
            (guild_id, user_id, edition, gamemode)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user_queue_entries(bot, guild_id: int, user_id: int) -> list[dict]:
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM comp_queue WHERE guild_id=? AND user_id=? ORDER BY queued_at', (guild_id, user_id)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_full_queue(bot, guild_id: int) -> list[dict]:
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM comp_queue WHERE guild_id=? ORDER BY edition, gamemode, queued_at', (guild_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def leave_queue(bot, guild_id: int, user_id: int, edition: str, gamemode: str) -> bool:
    async with aiosqlite.connect(bot.db.db_path) as db:
        cur = await db.execute(
            'DELETE FROM comp_queue WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=?',
            (guild_id, user_id, edition, gamemode)
        )
        await db.commit()
        return cur.rowcount > 0


async def purge_queue_by_edition(bot, guild_id: int, edition: str) -> int:
    """Kicks everyone currently queued for an edition out of queue — used
    when staff toggles that edition off, so a closed platform truly
    disappears everywhere instead of leaving stale queue entries behind."""
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        cur = await db.execute('DELETE FROM comp_queue WHERE guild_id=? AND edition=?', (guild_id, edition))
        await db.commit()
        return cur.rowcount


async def try_join_queue(bot, guild_id: int, user_id: int, edition: str, gamemode: str,
                          ign: str, tier_label: str):
    """Adds the user to queue. The instant a SECOND player is waiting for the
    same edition + gamemode, both are pulled out (FIFO) and a match record is
    created. Returns ('queued', None) or ('matched', match_row_dict)."""
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            'INSERT INTO comp_queue (guild_id, user_id, edition, gamemode, ign, tier_label) VALUES (?,?,?,?,?,?)',
            (guild_id, user_id, edition, gamemode, ign, tier_label)
        )
        await db.commit()

        async with db.execute(
            'SELECT * FROM comp_queue WHERE guild_id=? AND edition=? AND gamemode=? ORDER BY queued_at LIMIT 2',
            (guild_id, edition, gamemode)
        ) as cur:
            waiting = await cur.fetchall()

        if len(waiting) < 2:
            return 'queued', None

        p1, p2 = dict(waiting[0]), dict(waiting[1])
        await db.execute('DELETE FROM comp_queue WHERE id IN (?, ?)', (p1['id'], p2['id']))

        cur2 = await db.execute(
            'INSERT INTO comp_matches (guild_id, edition, gamemode, player1_id, player1_ign, player1_tier, '
            'player2_id, player2_ign, player2_tier) VALUES (?,?,?,?,?,?,?,?,?)',
            (guild_id, edition, gamemode, p1['user_id'], p1['ign'], p1['tier_label'],
             p2['user_id'], p2['ign'], p2['tier_label'])
        )
        await db.commit()
        return 'matched', await get_match(bot, cur2.lastrowid)


# ── Matches (tickets) ────────────────────────────────────────────────────

async def get_match(bot, match_id: int) -> dict | None:
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM comp_matches WHERE id = ?', (match_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_match_by_channel(bot, channel_id: int) -> dict | None:
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM comp_matches WHERE channel_id = ?', (channel_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_active_matches(bot, guild_id: int) -> list[dict]:
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM comp_matches WHERE guild_id=? AND status='open' ORDER BY created_at", (guild_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def set_match_message(bot, match_id: int, channel_id: int, message_id: int):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('UPDATE comp_matches SET channel_id=?, message_id=? WHERE id=?',
                          (channel_id, message_id, match_id))
        await db.commit()


async def complete_match(bot, match_id: int, winner_id: int, score: str, notes: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            "UPDATE comp_matches SET status='completed', winner_id=?, score=?, notes=?, "
            "completed_at=datetime('now') WHERE id=?",
            (winner_id, score, notes, match_id)
        )
        await db.commit()


async def cancel_match(bot, match_id: int, notes: str = ''):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            "UPDATE comp_matches SET status='cancelled', notes=?, completed_at=datetime('now') WHERE id=?",
            (notes, match_id)
        )
        await db.commit()


async def get_match_history(bot, guild_id: int, user_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM comp_matches WHERE guild_id=? AND status='completed' "
            "AND (player1_id=? OR player2_id=?) ORDER BY completed_at DESC LIMIT ?",
            (guild_id, user_id, user_id, limit)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Leaderboard ──────────────────────────────────────────────────────────

async def get_leaderboard(bot, guild_id: int, edition: str, gamemode: str, limit: int = 10) -> list[dict]:
    """Ranked by tier (best ladder position first), then wins, then fewest
    losses — no ELO, purely record + claimed tier, same as a real tier
    ladder on a big PvP server."""
    await _ensure_tables(bot)
    tiers = await get_comp_tiers(bot, guild_id)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM comp_players WHERE guild_id=? AND edition=? AND gamemode=?',
            (guild_id, edition, gamemode)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    rows.sort(key=lambda p: (tier_rank(tiers, p['tier_label']), -p['wins'], p['losses']))
    return rows[:limit]


# ── Season reset ─────────────────────────────────────────────────────────

async def do_season_reset(bot, guild_id: int) -> int:
    """Archives every player's current stats into comp_season_archive, then
    resets wins/losses/streak/tier for a fresh season. Returns the new
    season number."""
    await _ensure_tables(bot)
    settings = await get_comp_settings(bot, guild_id)
    season = settings.get('season_number', 1)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM comp_players WHERE guild_id=?', (guild_id,)) as cur:
            players = [dict(r) for r in await cur.fetchall()]
        for p in players:
            await db.execute(
                'INSERT INTO comp_season_archive (guild_id, season, user_id, edition, gamemode, '
                'tier_label, wins, losses, streak) VALUES (?,?,?,?,?,?,?,?,?)',
                (guild_id, season, p['user_id'], p['edition'], p['gamemode'],
                 p['tier_label'], p['wins'], p['losses'], p['streak'])
            )
        await db.execute(
            f"UPDATE comp_players SET wins=0, losses=0, streak=0, tier_label='{UNRANKED}' WHERE guild_id=?",
            (guild_id,)
        )
        await db.execute(
            'UPDATE comp_settings SET season_number = season_number + 1 WHERE guild_id=?', (guild_id,)
        )
        await db.commit()
    return season + 1


# ═══════════════════════════════════════════════════════════════════════════════
#  EMBEDS
# ═══════════════════════════════════════════════════════════════════════════════

def queue_join_embed(guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(
        title=f'⚔️  {guild.name} — Comp Fight Queue',
        description=(
            'Looking for a **Comp Fight**? Hop in the queue below.\n\n'
            '**How it works:**\n'
            '1️⃣ Click **Join Queue** and pick your platform\n'
            '2️⃣ Pick the game mode you want to fight in\n'
            '3️⃣ Tell us your exact **Minecraft username**\n'
            '4️⃣ Pick your current/claimed **tier**\n'
            '5️⃣ The moment another player queues for the same mode, a '
            'private **match ticket** opens for just the two of you\n'
            '6️⃣ Arrange & play your set in the ticket — staff will log the '
            'result and update tiers from there\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━'
        ),
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    e.set_footer(text=f'{guild.name} • Comp Fight System')
    return e


def streak_badge(streak: int) -> str:
    if streak >= STREAK_FIRE_THRESHOLD:
        return f' 🔥{streak}'
    if streak >= 1:
        return f' (W{streak})'
    if streak <= -STREAK_FIRE_THRESHOLD:
        return f' 🧊{abs(streak)}'
    if streak <= -1:
        return f' (L{abs(streak)})'
    return ''


def match_ticket_welcome_embed(guild: discord.Guild, match: dict, gamemode_emoji: str) -> discord.Embed:
    short = 'Bedrock' if match['edition'] == 'Bedrock Edition' else 'Java'
    e = discord.Embed(
        title=f'{gamemode_emoji}  {match["gamemode"]} — Comp Fight Match',
        description=(
            f'<@{match["player1_id"]}> **VS** <@{match["player2_id"]}>\n\n'
            f'**Platform:** {short} Edition\n\n'
            'Agree on a time, hop in-game, and play your set. Once you\'re '
            'done, a staff member will report the result below.'
        ),
        color=BLUE,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name=f'🔹 {"Player 1"}', value=f'<@{match["player1_id"]}>\nIGN: `{match["player1_ign"]}`\nTier: `{match["player1_tier"]}`', inline=True)
    e.add_field(name=f'🔸 {"Player 2"}', value=f'<@{match["player2_id"]}>\nIGN: `{match["player2_ign"]}`\nTier: `{match["player2_tier"]}`', inline=True)
    e.set_footer(text=f'{guild.name} • Comp Fight • Match #{match["id"]}')
    return e


def match_result_embed(guild: discord.Guild, match: dict, winner_id: int, score: str,
                        gamemode_emoji: str, notes: str) -> discord.Embed:
    loser_id = match['player2_id'] if winner_id == match['player1_id'] else match['player1_id']
    e = discord.Embed(
        title=f'{gamemode_emoji}  {match["gamemode"]} — Comp Result',
        color=GREEN,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name='📊  Score', value=f'**{score}**', inline=True)
    e.add_field(name='🏆  Winner', value=f'<@{winner_id}>', inline=True)
    e.add_field(name='💀  Loser', value=f'<@{loser_id}>', inline=True)
    if notes:
        e.add_field(name='📝  Notes', value=notes, inline=False)
    e.set_footer(text=f'{guild.name} • Comp Fight • Match #{match["id"]}')
    return e


def leaderboard_embed(guild: discord.Guild, edition: str, gamemode: str, rows: list[dict]) -> discord.Embed:
    e = discord.Embed(
        title=f'🏅  Leaderboard — {gamemode} ({edition})',
        color=ORANGE,
        timestamp=datetime.now(timezone.utc)
    )
    if not rows:
        e.description = '_No ranked players yet for this mode._'
    else:
        medals = ['🥇', '🥈', '🥉']
        lines = []
        for i, p in enumerate(rows):
            medal = medals[i] if i < 3 else f'`#{i+1}`'
            lines.append(
                f'{medal} <@{p["user_id"]}> — `{p["tier_label"]}` • '
                f'{p["wins"]}W/{p["losses"]}L{streak_badge(p["streak"])}'
            )
        e.description = '\n'.join(lines)
    e.set_footer(text=f'{guild.name} • Comp Fight Leaderboard')
    return e


def history_embed(guild: discord.Guild, member: discord.abc.User, matches: list[dict]) -> discord.Embed:
    e = discord.Embed(
        title=f'📜  Match History — {member.display_name}',
        color=BLUE,
        timestamp=datetime.now(timezone.utc)
    )
    if not matches:
        e.description = '_No completed matches yet._'
    else:
        lines = []
        for m in matches:
            on_p1 = member.id == m['player1_id']
            won = m['winner_id'] == member.id
            result = '🟢 Won' if won else '🔴 Lost'
            opp_id = m['player2_id'] if on_p1 else m['player1_id']
            lines.append(f'**#{m["id"]}** {m["gamemode"]} — {result} • `{m["score"]}` vs <@{opp_id}>')
        e.description = '\n'.join(lines)
    e.set_footer(text=f'{guild.name} • Last {len(matches)} completed match(es)')
    return e


def queue_status_embed(guild: discord.Guild, entries: list[dict]) -> discord.Embed:
    e = discord.Embed(
        title='📡  Live Comp Queue',
        color=ORANGE,
        timestamp=datetime.now(timezone.utc)
    )
    if not entries:
        e.description = '_Nobody is currently queued. Be the first — click **Join Queue** above!_'
    else:
        grouped: dict[str, list[dict]] = {}
        for entry in entries:
            key = f'{entry["edition"]} • {entry["gamemode"]}'
            grouped.setdefault(key, []).append(entry)
        lines = []
        for key, group in grouped.items():
            names = ', '.join(f'<@{g["user_id"]}> (`{g["tier_label"]}`)' for g in group)
            lines.append(f'**{key}**  —  {len(group)} waiting\n{names}')
        e.description = '\n\n'.join(lines)
    e.set_footer(text=f'{guild.name} • Updates live as players queue')
    return e


def comp_admin_embed(guild: discord.Guild, settings: dict, java_gm: list, bedrock_gm: list,
                      tiers: list[str], active_count: int, queued_count: int, tier_role_count: int = 0) -> discord.Embed:
    java = '✅ Open' if settings.get('java_enabled', 1) else '❌ Closed'
    bedrock = '✅ Open' if settings.get('bedrock_enabled', 1) else '❌ Closed'

    def ch_text(ch_id):
        return f'<#{ch_id}>' if ch_id else '_Not set_'

    e = discord.Embed(
        title='🛠️  Comp Fight — Admin Panel',
        description=(
            f'Full control over the Comp Fight queue system on **{guild.name}**.\n'
            'Matched players get a **private match ticket** — no public '
            'embeds, no ELO, just a clean 1v1 ladder.\n\n'
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
    e.add_field(name='🟩  Java Gamemodes', value=f'{len(java_gm)}/25 configured', inline=True)
    e.add_field(name='🟦  Bedrock Gamemodes', value=f'{len(bedrock_gm)}/25 configured', inline=True)
    e.add_field(name='🎚️  Tiers', value=f'{len(tiers)} configured', inline=True)
    e.add_field(name='⚔️  Open Match Tickets', value=str(active_count), inline=True)
    e.add_field(name='⏳  Players Queued', value=str(queued_count), inline=True)
    e.add_field(name='\u200b', value='\u200b', inline=True)

    ping_role_id = settings.get('ping_role_id')
    e.add_field(
        name='🔔  Staff Ping Role',
        value=f'<@&{ping_role_id}>' if ping_role_id else '_Not set — no staff role pinged in new tickets_',
        inline=False
    )
    e.add_field(
        name='📡  Channels',
        value=(
            f'**Queue Panel:** {ch_text(settings.get("queue_channel_id"))}\n'
            f'**Logs:** {ch_text(settings.get("log_channel_id"))}\n'
            f'**Ticket Category:** {("<#" + str(settings["ticket_category_id"]) + ">") if settings.get("ticket_category_id") else "_Not set — created without a category_"}'
        ),
        inline=False
    )
    e.add_field(name='📅  Season', value=f'Season **{settings.get("season_number", 1)}**', inline=True)
    e.add_field(name='🎭  Tier Roles', value=f'{tier_role_count} mapped', inline=True)
    e.add_field(name='\u200b', value='\u200b', inline=True)
    e.set_footer(text='Mine Front • Comp Fight Admin Panel')
    return e


def gamemode_manage_embed(edition: str, gamemodes: list[tuple[str, str]]) -> discord.Embed:
    lines = '\n'.join(f'{emoji} {name}' for name, emoji in gamemodes) or '_No gamemodes configured._'
    return E.base(f'🎮  {edition} Gamemodes', f'{lines}\n\nUse the buttons below to add, remove, or reset.', color=PURPLE)


def tier_manage_embed(tiers: list[str]) -> discord.Embed:
    lines = '\n'.join(f'`#{i+1}` {t}' for i, t in enumerate(tiers)) or '_No tiers configured._'
    return E.base('🎚️  Tier Ladder', f'{lines}\n\nOrder = rank (top = best). Use the buttons below to add, remove, or reset.', color=PURPLE)


# ═══════════════════════════════════════════════════════════════════════════════
#  QUEUE FLOW — Edition → Gamemode → Username modal → Tier select → Queue
# ═══════════════════════════════════════════════════════════════════════════════

class TierSelectView(discord.ui.View):
    """Final step before joining queue — pick current/claimed tier."""

    def __init__(self, bot, edition: str, gamemode: str, ign: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.edition = edition
        self.gamemode = gamemode
        self.ign = ign

    async def _build(self, guild_id: int):
        tiers = await get_comp_tiers(self.bot, guild_id)
        options = [discord.SelectOption(label=t, value=t) for t in tiers][:24]
        options.append(discord.SelectOption(label=UNRANKED, value=UNRANKED, emoji='❔'))
        self.tier_select.options = options
        self.tier_select.placeholder = 'Select your current / claimed tier…'

    @discord.ui.select(placeholder='Loading tiers…')
    async def tier_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        tier_label = select.values[0]
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _join_queue_flow(self.bot, interaction, self.edition, self.gamemode, self.ign, tier_label)

    async def send(self, interaction: discord.Interaction):
        await self._build(interaction.guild_id)
        await interaction.response.send_message(
            embed=E.base('🎚️  Select Your Tier', f'IGN locked in: **{self.ign}**\n\nNow pick your current / claimed tier:', color=PURPLE),
            view=self, ephemeral=True)


class QueueUsernameModal(discord.ui.Modal):
    """Step after picking a gamemode — asks for the exact Minecraft username."""

    def __init__(self, bot, edition: str, gamemode: str):
        super().__init__(title=f'⚔️  {gamemode} — Join Queue', timeout=300)
        self.bot = bot
        self.edition = edition
        self.gamemode = gamemode
        self.ign = discord.ui.TextInput(
            label='Minecraft Username',
            placeholder='Your exact in-game username',
            required=True,
            max_length=32,
        )
        self.add_item(self.ign)

    async def on_submit(self, interaction: discord.Interaction):
        ign = self.ign.value.strip()
        if not ign:
            return await interaction.response.send_message(embed=E.error('Username cannot be empty.'), ephemeral=True)
        await TierSelectView(self.bot, self.edition, self.gamemode, ign).send(interaction)


async def _join_queue_flow(bot, interaction: discord.Interaction, edition: str, gamemode: str, ign: str, tier_label: str):
    guild = interaction.guild

    # Re-check in case staff toggled the edition off while the flow was open.
    settings = await get_comp_settings(bot, guild.id)
    field = 'bedrock_enabled' if edition == 'Bedrock Edition' else 'java_enabled'
    if not bool(settings.get(field, 1)):
        return await interaction.followup.send(embed=E.error(f'**{edition}** Comp Fight is currently closed.'), ephemeral=True)

    remaining = await get_cooldown_remaining(bot, guild.id, interaction.user.id)
    if remaining:
        return await interaction.followup.send(
            embed=E.error(f'You\'ve had too many no-shows recently — you\'re on a queue cooldown for '
                           f'**{remaining} more minute(s)**.'), ephemeral=True)

    existing = await get_queue_entry(bot, guild.id, interaction.user.id, edition, gamemode)
    if existing:
        return await interaction.followup.send(
            embed=E.error(f'You are already queued for **{gamemode}** ({edition}).'), ephemeral=True)

    await upsert_player_queue_info(bot, guild.id, interaction.user.id, edition, gamemode, ign, tier_label)
    await sync_tier_roles(bot, guild, [interaction.user.id])

    status, match = await try_join_queue(bot, guild.id, interaction.user.id, edition, gamemode, ign, tier_label)

    if status == 'matched':
        channel = await _create_match_ticket(bot, guild, match)
        await _refresh_queue_status(bot, guild)
        ch_text = channel.mention if channel else 'a private match ticket'
        await interaction.followup.send(
            embed=E.success(f'Opponent found! Your **{gamemode}** ({edition}) match ticket is ready: {ch_text}'),
            ephemeral=True)
    else:
        await _refresh_queue_status(bot, guild)
        await interaction.followup.send(
            embed=E.success(
                f'You\'ve joined the **{gamemode}** ({edition}) queue as **{ign}** at tier **{tier_label}**. '
                'You\'ll get a ticket the moment an opponent queues up.'
            ), ephemeral=True)


class CompGamemodeSelectView(discord.ui.View):
    def __init__(self, bot, edition: str, gamemodes: list[tuple[str, str]]):
        super().__init__(timeout=120)
        self.bot = bot
        self.edition = edition
        short = 'Bedrock' if edition == 'Bedrock Edition' else 'Java'
        options = [discord.SelectOption(label=name, value=name, emoji=emoji) for name, emoji in gamemodes][:25]
        self.gamemode_select.options = options
        self.gamemode_select.placeholder = f'Select the {short} game mode to queue for…'

    @discord.ui.select()
    async def gamemode_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        gamemode = select.values[0]
        await interaction.response.send_modal(QueueUsernameModal(self.bot, self.edition, gamemode))


class CompEditionSelectView(discord.ui.View):
    def __init__(self, bot, java_enabled: bool = True, bedrock_enabled: bool = True):
        super().__init__(timeout=120)
        self.bot = bot
        if not bedrock_enabled:
            self.remove_item(self.bedrock)
        if not java_enabled:
            self.remove_item(self.java)

    async def _pick(self, interaction: discord.Interaction, edition: str):
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        field = 'bedrock_enabled' if edition == 'Bedrock Edition' else 'java_enabled'
        if not bool(settings.get(field, 1)):
            return await interaction.response.send_message(embed=E.error(f'**{edition}** Comp Fight is currently closed.'), ephemeral=True)

        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, edition)
        if not gamemodes:
            return await interaction.response.send_message(
                embed=E.error(f'No gamemodes are configured for {edition} yet. Ask an admin to add some via /compadminpanel.'),
                ephemeral=True)

        short = 'Bedrock' if edition == 'Bedrock Edition' else 'Java'
        platform_emoji = '🟢' if edition == 'Bedrock Edition' else '🍲'
        embed = discord.Embed(
            title=f'{platform_emoji}  {short} — Select Game Mode',
            description='Choose the game mode you want to queue for:',
            color=PURPLE,
        )
        await interaction.response.send_message(
            embed=embed, view=CompGamemodeSelectView(self.bot, edition, gamemodes), ephemeral=True)

    @discord.ui.button(label='Bedrock', emoji='🟢', style=discord.ButtonStyle.success)
    async def bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, 'Bedrock Edition')

    @discord.ui.button(label='Java', emoji='🍲', style=discord.ButtonStyle.primary)
    async def java(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, 'Java Edition')


class LeaveQueueSelectView(discord.ui.View):
    def __init__(self, bot, entries: list[dict]):
        super().__init__(timeout=120)
        self.bot = bot
        self.leave_select.options = [
            discord.SelectOption(label=f'{e["edition"]} • {e["gamemode"]}', value=f'{e["edition"]}||{e["gamemode"]}')
            for e in entries[:25]
        ]

    @discord.ui.select(placeholder='Select a queue to leave…')
    async def leave_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        edition, gamemode = select.values[0].split('||', 1)
        ok = await leave_queue(self.bot, interaction.guild_id, interaction.user.id, edition, gamemode)
        await _refresh_queue_status(self.bot, interaction.guild)
        if ok:
            await interaction.response.send_message(embed=E.success(f'Left the **{gamemode}** ({edition}) queue.'), ephemeral=True)
        else:
            await interaction.response.send_message(embed=E.error('You were not in that queue.'), ephemeral=True)


class CompQueuePanelView(discord.ui.View):
    """Public, persistent panel posted via /comppanel."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Join Queue', emoji='⚔️', style=discord.ButtonStyle.primary, custom_id='comp:queue:join')
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        java_on = bool(settings.get('java_enabled', 1))
        bedrock_on = bool(settings.get('bedrock_enabled', 1))
        if not java_on and not bedrock_on:
            return await interaction.response.send_message(
                embed=E.error('Comp Fight queueing is currently closed for both editions.'), ephemeral=True)
        embed = discord.Embed(
            title='⚔️  Comp Fight — Select Platform',
            description='Which platform do you want to queue on — **Bedrock** or **Java**?',
            color=PURPLE,
        )
        await interaction.response.send_message(embed=embed, view=CompEditionSelectView(self.bot, java_on, bedrock_on), ephemeral=True)

    @discord.ui.button(label='Leave Queue', emoji='🚪', style=discord.ButtonStyle.secondary, custom_id='comp:queue:leave')
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        entries = await get_user_queue_entries(self.bot, interaction.guild_id, interaction.user.id)
        if not entries:
            return await interaction.response.send_message(embed=E.error('You are not currently in any Comp Fight queue.'), ephemeral=True)
        await interaction.response.send_message(
            embed=E.base('🚪  Leave Queue', 'Pick which queue to leave:', color=ORANGE),
            view=LeaveQueueSelectView(self.bot, entries), ephemeral=True)


async def _refresh_queue_status(bot, guild: discord.Guild):
    settings = await get_comp_settings(bot, guild.id)
    ch_id = settings.get('queue_channel_id')
    if not ch_id:
        return
    channel = guild.get_channel(ch_id)
    if not channel:
        return
    entries = await get_full_queue(bot, guild.id)
    embed = queue_status_embed(guild, entries)
    msg_id = settings.get('queue_status_message_id')
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed)
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    try:
        msg = await channel.send(embed=embed)
        await set_queue_status_message(bot, guild.id, msg.id)
    except discord.HTTPException:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  MATCH TICKET — private channel created the moment two players are matched
# ═══════════════════════════════════════════════════════════════════════════════

async def _create_match_ticket(bot, guild: discord.Guild, match: dict) -> discord.TextChannel | None:
    settings = await get_comp_settings(bot, guild.id)
    p1 = guild.get_member(match['player1_id'])
    p2 = guild.get_member(match['player2_id'])

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                               manage_channels=True, manage_permissions=True),
    }
    for m in (p1, p2):
        if m:
            overwrites[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    ping_role_id = settings.get('ping_role_id')
    if ping_role_id:
        role = guild.get_role(ping_role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    category_id = settings.get('ticket_category_id')
    category = guild.get_channel(category_id) if category_id else None

    p1_name = p1.name if p1 else str(match['player1_id'])
    p2_name = p2.name if p2 else str(match['player2_id'])
    channel_name = f'comp-{p1_name}-{p2_name}'[:90].lower().replace(' ', '-')

    try:
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f'Comp Fight match #{match["id"]} • {match["gamemode"]} ({match["edition"]})',
            reason='Comp Fight — players matched in queue'
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error(f'Failed to create comp match ticket: {e}')
        return None

    gamemodes = await get_comp_gamemodes(bot, guild.id, match['edition'])
    emoji = next((em for n, em in gamemodes if n == match['gamemode']), '⚔️')
    embed = match_ticket_welcome_embed(guild, match, emoji)
    view = MatchTicketControlView(bot)

    ping = f'{p1.mention if p1 else ""} {p2.mention if p2 else ""} {f"<@&{ping_role_id}>" if ping_role_id else ""}'.strip()
    try:
        msg = await channel.send(content=ping or None, embed=embed, view=view,
                                  allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await msg.pin()
    except discord.HTTPException:
        msg = await channel.send(embed=embed, view=view)

    await set_match_message(bot, match['id'], channel.id, msg.id)

    log_id = settings.get('log_channel_id')
    if log_id:
        log_channel = guild.get_channel(log_id)
        if log_channel:
            try:
                await log_channel.send(embed=E.base(
                    '⚔️  Comp Match Ticket Opened',
                    f'**{match["gamemode"]}** ({match["edition"]}) — {channel.mention}\n'
                    f'{p1.mention if p1 else match["player1_id"]} vs {p2.mention if p2 else match["player2_id"]}',
                    color=PURPLE))
            except discord.HTTPException:
                pass

    return channel


class WinnerPickView(discord.ui.View):
    def __init__(self, bot, match: dict, guild: discord.Guild):
        super().__init__(timeout=180)
        self.bot = bot
        self.match = match
        p1 = guild.get_member(match['player1_id'])
        p2 = guild.get_member(match['player2_id'])
        self.p1_button.label = f'{(p1.display_name if p1 else match["player1_ign"])} Won'[:80]
        self.p2_button.label = f'{(p2.display_name if p2 else match["player2_ign"])} Won'[:80]

    @discord.ui.button(style=discord.ButtonStyle.success, emoji='🏆')
    async def p1_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MatchResultModal(self.bot, self.match, self.match['player1_id']))

    @discord.ui.button(style=discord.ButtonStyle.success, emoji='🏆')
    async def p2_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MatchResultModal(self.bot, self.match, self.match['player2_id']))


class MatchResultModal(discord.ui.Modal):
    def __init__(self, bot, match: dict, winner_id: int):
        super().__init__(title='📊  Post Comp Result', timeout=300)
        self.bot = bot
        self.match = match
        self.winner_id = winner_id

        self.score = discord.ui.TextInput(label='Score', placeholder='e.g. 3-0, 3-1, 3-2', required=True, max_length=20)
        self.winner_tier = discord.ui.TextInput(
            label="Winner's New Tier (blank = unchanged)", placeholder='e.g. HT2', required=False, max_length=50)
        self.loser_tier = discord.ui.TextInput(
            label="Loser's New Tier (blank = unchanged)", placeholder='e.g. LT3', required=False, max_length=50)
        self.notes = discord.ui.TextInput(
            label='Notes (optional)', style=discord.TextStyle.paragraph, required=False, max_length=300)

        self.add_item(self.score)
        self.add_item(self.winner_tier)
        self.add_item(self.loser_tier)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        bot, guild, match = self.bot, interaction.guild, self.match
        loser_id = match['player2_id'] if self.winner_id == match['player1_id'] else match['player1_id']

        winner_tier = self.winner_tier.value.strip() or None
        loser_tier = self.loser_tier.value.strip() or None
        notes = self.notes.value.strip()
        if not notes:
            parts = []
            if winner_tier:
                parts.append(f'<@{self.winner_id}> : → **{winner_tier}**')
            if loser_tier:
                parts.append(f'<@{loser_id}> : → **{loser_tier}**')
            notes = '\n'.join(parts)

        await record_match_result(bot, match['guild_id'], match['edition'], match['gamemode'],
                                   self.winner_id, loser_id, winner_tier, loser_tier)
        await complete_match(bot, match['id'], self.winner_id, self.score.value.strip(), notes)
        await sync_tier_roles(bot, guild, [self.winner_id, loser_id])

        gamemodes = await get_comp_gamemodes(bot, guild.id, match['edition'])
        emoji = next((em for n, em in gamemodes if n == match['gamemode']), '⚔️')
        embed = match_result_embed(guild, match, self.winner_id, self.score.value.strip(), emoji, notes)

        await interaction.followup.send(embed=embed)

        settings = await get_comp_settings(bot, guild.id)
        log_id = settings.get('log_channel_id')
        if log_id:
            log_channel = guild.get_channel(log_id)
            if log_channel:
                try:
                    await log_channel.send(embed=E.base(
                        '📋  Comp Result Logged',
                        f'{interaction.user.mention} logged the result for **{match["gamemode"]}** Match #{match["id"]}: '
                        f'<@{self.winner_id}> defeated <@{loser_id}> ({self.score.value.strip()}).',
                        color=PURPLE))
                except discord.HTTPException:
                    pass


class NoShowSelectView(discord.ui.View):
    def __init__(self, bot, match: dict, guild: discord.Guild):
        super().__init__(timeout=120)
        self.bot = bot
        self.match = match
        p1 = guild.get_member(match['player1_id'])
        p2 = guild.get_member(match['player2_id'])
        self.p1_btn.label = f'{(p1.display_name if p1 else match["player1_ign"])} No-Showed'[:80]
        self.p2_btn.label = f'{(p2.display_name if p2 else match["player2_ign"])} No-Showed'[:80]

    async def _mark(self, interaction: discord.Interaction, user_id: int):
        count = await record_dodge(self.bot, interaction.guild, user_id, self.match['id'])
        await cancel_match(self.bot, self.match['id'], f'No-show: <@{user_id}>')
        note = ''
        if count >= DODGE_THRESHOLD:
            note = (f'\n⚠️ <@{user_id}> has {count} no-shows in the last {DODGE_WINDOW_HOURS}h and is now on a '
                    f'**{DODGE_COOLDOWN_MINUTES}-minute** queue cooldown.')
        await interaction.response.send_message(
            embed=E.base('🚫  Match Cancelled', f'Logged a no-show for <@{user_id}>.{note}', color=RED))

    @discord.ui.button(style=discord.ButtonStyle.danger, emoji='🚫')
    async def p1_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._mark(interaction, self.match['player1_id'])

    @discord.ui.button(style=discord.ButtonStyle.danger, emoji='🚫')
    async def p2_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._mark(interaction, self.match['player2_id'])


class CloseTicketConfirmView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=60)
        self.bot = bot

    @discord.ui.button(label='Confirm Close', style=discord.ButtonStyle.danger, emoji='🔒')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=E.base('🔒  Closing…', 'This channel will be deleted in 5 seconds.', color=RED))
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f'Comp match ticket closed by {interaction.user}')
        except (discord.Forbidden, discord.HTTPException):
            pass

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary, emoji='✖️')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=E.base('✖️  Cancelled', color=GREY), ephemeral=True)


class MatchTicketControlView(discord.ui.View):
    """Persistent buttons shown on every Comp Fight match ticket."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _get_match(self, interaction: discord.Interaction) -> dict | None:
        return await get_match_by_channel(self.bot, interaction.channel_id)

    @discord.ui.button(label='Report Result', emoji='🏆', style=discord.ButtonStyle.success, custom_id='comp:ticket:result')
    async def report_result(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = await self._get_match(interaction)
        if not match:
            return await interaction.response.send_message(embed=E.error('This is not an active match ticket.'), ephemeral=True)
        if match['status'] != 'open':
            return await interaction.response.send_message(embed=E.error('This match has already been resolved.'), ephemeral=True)
        if not await is_admin_or_owner(self.bot, interaction):
            return await interaction.response.send_message(embed=E.error('Only staff can report a result.'), ephemeral=True)
        await interaction.response.send_message(
            embed=E.base('🏆  Who Won?', 'Pick the winner:', color=PURPLE),
            view=WinnerPickView(self.bot, match, interaction.guild), ephemeral=True)

    @discord.ui.button(label='No-Show', emoji='🚫', style=discord.ButtonStyle.danger, custom_id='comp:ticket:noshow')
    async def no_show(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = await self._get_match(interaction)
        if not match:
            return await interaction.response.send_message(embed=E.error('This is not an active match ticket.'), ephemeral=True)
        if match['status'] != 'open':
            return await interaction.response.send_message(embed=E.error('This match has already been resolved.'), ephemeral=True)
        if not await is_admin_or_owner(self.bot, interaction):
            return await interaction.response.send_message(embed=E.error('Only staff can log a no-show.'), ephemeral=True)
        await interaction.response.send_message(
            embed=E.base('🚫  Who No-Showed?', 'Pick which player failed to show up:', color=ORANGE),
            view=NoShowSelectView(self.bot, match, interaction.guild), ephemeral=True)

    @discord.ui.button(label='Close Ticket', emoji='🔒', style=discord.ButtonStyle.secondary, custom_id='comp:ticket:close')
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = await self._get_match(interaction)
        is_participant = match and interaction.user.id in (match['player1_id'], match['player2_id'])
        if not is_participant and not await is_admin_or_owner(self.bot, interaction):
            return await interaction.response.send_message(embed=E.error('Only the matched players or staff can close this ticket.'), ephemeral=True)
        await interaction.response.send_message(
            embed=E.base('⚠️  Confirm Close', 'Close (and delete) this match ticket?', color=ORANGE),
            view=CloseTicketConfirmView(self.bot), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN PANEL
# ═══════════════════════════════════════════════════════════════════════════════

class AddCompGamemodeModal(discord.ui.Modal):
    def __init__(self, bot, edition: str):
        super().__init__(title=f'➕  Add {edition} Gamemode', timeout=120)
        self.bot = bot
        self.edition = edition
        self.name_input = discord.ui.TextInput(label='Gamemode Name', placeholder='e.g. Boxing', required=True, max_length=50)
        self.emoji_input = discord.ui.TextInput(label='Emoji', placeholder='e.g. 🥊', required=False, max_length=100)
        self.add_item(self.name_input)
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await add_comp_gamemode(self.bot, interaction.guild_id, self.edition,
                                           self.name_input.value.strip(), self.emoji_input.value.strip() or '🎮')
        await interaction.followup.send(embed=(E.success(msg) if ok else E.error(msg)), ephemeral=True)


class CompGamemodeResetConfirmView(discord.ui.View):
    def __init__(self, bot, edition: str):
        super().__init__(timeout=60)
        self.bot = bot
        self.edition = edition

    @discord.ui.button(label='Confirm Reset', style=discord.ButtonStyle.danger, emoji='🔄')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reset_comp_gamemodes(self.bot, interaction.guild_id, self.edition)
        await interaction.response.send_message(embed=E.success(f'{self.edition} comp gamemodes reset to default.'), ephemeral=True)
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary, emoji='✖️')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=E.base('✖️  Cancelled', color=GREY), ephemeral=True)
        self.stop()


class CompGamemodeManageView(discord.ui.View):
    def __init__(self, bot, edition: str, gamemodes: list[tuple[str, str]]):
        super().__init__(timeout=180)
        self.bot = bot
        self.edition = edition
        if gamemodes:
            self.remove_select.options = [discord.SelectOption(label=name, value=name, emoji=emoji) for name, emoji in gamemodes[:25]]
            self.remove_select.placeholder = f'Select a {edition} gamemode to remove…'
        else:
            self.remove_select.disabled = True
            self.remove_select.options = [discord.SelectOption(label='No gamemodes configured', value='__none__')]

    @discord.ui.select(row=0)
    async def remove_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        name = select.values[0]
        await remove_comp_gamemode(self.bot, interaction.guild_id, self.edition, name)
        await interaction.response.send_message(embed=E.success(f'Removed **{name}** from {self.edition}.'), ephemeral=True)

    @discord.ui.button(label='Add Gamemode', emoji='➕', style=discord.ButtonStyle.success, row=1)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddCompGamemodeModal(self.bot, self.edition))

    @discord.ui.button(label='Reset to Default', emoji='🔄', style=discord.ButtonStyle.danger, row=1)
    async def reset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base('⚠️  Confirm Reset', f'Reset {self.edition} comp gamemodes to the built-in default list?', color=ORANGE),
            view=CompGamemodeResetConfirmView(self.bot, self.edition), ephemeral=True)


class AddCompTierModal(discord.ui.Modal, title='➕  Add Tier'):
    name_input = discord.ui.TextInput(label='Tier Name', placeholder='e.g. HT1', required=True, max_length=50)

    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await add_comp_tier(self.bot, interaction.guild_id, self.name_input.value.strip())
        await interaction.followup.send(embed=(E.success(msg) if ok else E.error(msg)), ephemeral=True)


class CompTierResetConfirmView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=60)
        self.bot = bot

    @discord.ui.button(label='Confirm Reset', style=discord.ButtonStyle.danger, emoji='🔄')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reset_comp_tiers(self.bot, interaction.guild_id)
        await interaction.response.send_message(embed=E.success('Tier ladder reset to default.'), ephemeral=True)
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary, emoji='✖️')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=E.base('✖️  Cancelled', color=GREY), ephemeral=True)
        self.stop()


class CompTierManageView(discord.ui.View):
    def __init__(self, bot, tiers: list[str]):
        super().__init__(timeout=180)
        self.bot = bot
        if tiers:
            self.remove_select.options = [discord.SelectOption(label=t, value=t) for t in tiers[:25]]
            self.remove_select.placeholder = 'Select a tier to remove…'
        else:
            self.remove_select.disabled = True
            self.remove_select.options = [discord.SelectOption(label='No tiers configured', value='__none__')]

    @discord.ui.select(row=0)
    async def remove_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await remove_comp_tier(self.bot, interaction.guild_id, select.values[0])
        await interaction.response.send_message(embed=E.success(f'Removed tier **{select.values[0]}**.'), ephemeral=True)

    @discord.ui.button(label='Add Tier', emoji='➕', style=discord.ButtonStyle.success, row=1)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddCompTierModal(self.bot))

    @discord.ui.button(label='Reset to Default', emoji='🔄', style=discord.ButtonStyle.danger, row=1)
    async def reset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base('⚠️  Confirm Reset', 'Reset the tier ladder to the built-in default list?', color=ORANGE),
            view=CompTierResetConfirmView(self.bot), ephemeral=True)


class CompChannelsView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot

    async def _save(self, interaction, field, label, select):
        channel = select.values[0] if select.values else None
        await set_comp_channel(self.bot, interaction.guild_id, field, channel.id if channel else None)
        mention = channel.mention if channel else 'Not set'
        await interaction.response.send_message(embed=E.success(f'{label} channel set to {mention}.'), ephemeral=True)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder='📡  Select the Queue Panel channel…', row=0)
    async def queue_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._save(interaction, 'queue_channel_id', 'Queue Panel', select)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder='📋  Select the Logs channel…', row=1)
    async def logs_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._save(interaction, 'log_channel_id', 'Logs', select)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.category],
                       placeholder='🗂️  Select the category for match tickets…', row=2)
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        category = select.values[0] if select.values else None
        await set_comp_ticket_category(self.bot, interaction.guild_id, category.id if category else None)
        await interaction.response.send_message(
            embed=E.success(f'Match ticket category set to {category.mention if category else "Not set"}.'), ephemeral=True)

    @discord.ui.button(label='Clear All', emoji='🧹', style=discord.ButtonStyle.danger, row=3)
    async def clear_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        for field in COMP_CHANNEL_FIELDS:
            await set_comp_channel(self.bot, interaction.guild_id, field, None)
        await set_comp_ticket_category(self.bot, interaction.guild_id, None)
        await interaction.response.send_message(embed=E.success('All Comp Fight channels cleared.'), ephemeral=True)


class CompPingRoleView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder='🔔  Select the staff role pinged in new tickets…', row=0)
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0] if select.values else None
        await set_comp_ping_role(self.bot, interaction.guild_id, role.id if role else None)
        await interaction.response.send_message(
            embed=E.success(f'Comp Fight staff ping role set to {role.mention if role else "Not set"}.'), ephemeral=True)

    @discord.ui.button(label='Clear Ping Role', emoji='🧹', style=discord.ButtonStyle.danger, row=1)
    async def clear_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_comp_ping_role(self.bot, interaction.guild_id, None)
        await interaction.response.send_message(embed=E.success('Comp Fight staff ping role cleared.'), ephemeral=True)


class AddTierRoleModal(discord.ui.Modal):
    def __init__(self, bot):
        super().__init__(title='🎭  Map a Tier → Role', timeout=180)
        self.bot = bot
        self.tier_input = discord.ui.TextInput(label='Exact Tier Label', placeholder='e.g. HT2', required=True, max_length=100)
        self.add_item(self.tier_input)

    async def on_submit(self, interaction: discord.Interaction):
        embed = E.base('🎭  Pick the Role', f'Which role should be auto-assigned for tier **{self.tier_input.value.strip()}**?', color=PURPLE)
        await interaction.response.send_message(embed=embed, view=TierRolePickView(self.bot, self.tier_input.value.strip()), ephemeral=True)


class TierRolePickView(discord.ui.View):
    def __init__(self, bot, tier_label: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.tier_label = tier_label

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder='Select the role for this tier…')
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        await set_tier_role(self.bot, interaction.guild_id, self.tier_label, role.id)
        await interaction.response.send_message(embed=E.success(f'**{self.tier_label}** will now auto-assign {role.mention}.'), ephemeral=True)


class RemoveTierRoleSelectView(discord.ui.View):
    def __init__(self, bot, mapping: dict[str, int]):
        super().__init__(timeout=180)
        self.bot = bot
        options = [discord.SelectOption(label=tier[:100], value=tier) for tier in list(mapping.keys())[:25]]
        self.remove_select.options = options or [discord.SelectOption(label='No tier roles mapped', value='__none__')]
        if not options:
            self.remove_select.disabled = True

    @discord.ui.select(placeholder='Select a tier-role mapping to remove…')
    async def remove_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await remove_tier_role(self.bot, interaction.guild_id, select.values[0])
        await interaction.response.send_message(embed=E.success(f'Removed the role mapping for **{select.values[0]}**.'), ephemeral=True)


class CompTierRolesView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot

    @discord.ui.button(label='Add Mapping', emoji='➕', style=discord.ButtonStyle.success, row=0)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddTierRoleModal(self.bot))

    @discord.ui.button(label='Remove Mapping', emoji='🗑️', style=discord.ButtonStyle.danger, row=0)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        mapping = await get_tier_roles(self.bot, interaction.guild_id)
        await interaction.response.send_message(embed=E.base('🗑️  Remove Mapping', color=ORANGE),
                                                  view=RemoveTierRoleSelectView(self.bot, mapping), ephemeral=True)


class SeasonResetConfirmView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=60)
        self.bot = bot

    @discord.ui.button(label='Confirm Season Reset', style=discord.ButtonStyle.danger, emoji='📅')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        new_season = await do_season_reset(self.bot, interaction.guild_id)
        await interaction.followup.send(
            embed=E.success(f'Season reset complete — everyone\'s stats were archived and **Season {new_season}** has begun.'),
            ephemeral=True)
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary, emoji='✖️')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=E.base('✖️  Cancelled', color=GREY), ephemeral=True)
        self.stop()


class CompAdminPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def _refresh(self, interaction: discord.Interaction):
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        java_gm = await get_comp_gamemodes(self.bot, interaction.guild_id, 'Java Edition')
        bedrock_gm = await get_comp_gamemodes(self.bot, interaction.guild_id, 'Bedrock Edition')
        tiers = await get_comp_tiers(self.bot, interaction.guild_id)
        active = await get_active_matches(self.bot, interaction.guild_id)
        queue = await get_full_queue(self.bot, interaction.guild_id)
        tier_roles = await get_tier_roles(self.bot, interaction.guild_id)
        embed = comp_admin_embed(interaction.guild, settings, java_gm, bedrock_gm, tiers, len(active), len(queue), len(tier_roles))
        await interaction.response.edit_message(embed=embed, view=self)

    async def _toggle_edition(self, interaction: discord.Interaction, field: str, edition: str):
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        new_state = not bool(settings.get(field, 1))
        await set_comp_toggle(self.bot, interaction.guild_id, field, new_state)

        if not new_state:
            # Edition just got closed — rip it out of the queue so a closed
            # platform truly disappears everywhere instead of leaving stale
            # queue entries behind. Open match tickets are left alone since
            # staff may still want to finish them out.
            await purge_queue_by_edition(self.bot, interaction.guild_id, edition)
            await _refresh_queue_status(self.bot, interaction.guild)

        await self._refresh(interaction)

    # Row 0 — toggles
    @discord.ui.button(label='Toggle Java', emoji='🟩', style=discord.ButtonStyle.success, row=0)
    async def toggle_java(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_edition(interaction, 'java_enabled', 'Java Edition')

    @discord.ui.button(label='Toggle Bedrock', emoji='🟦', style=discord.ButtonStyle.primary, row=0)
    async def toggle_bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_edition(interaction, 'bedrock_enabled', 'Bedrock Edition')

    @discord.ui.button(label='Refresh', emoji='🔄', style=discord.ButtonStyle.secondary, row=0)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._refresh(interaction)

    # Row 1 — gamemodes + tiers
    @discord.ui.button(label='Java Gamemodes', emoji='🎮', style=discord.ButtonStyle.secondary, row=1)
    async def manage_java(self, interaction: discord.Interaction, button: discord.ui.Button):
        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, 'Java Edition')
        await interaction.response.send_message(embed=gamemode_manage_embed('Java Edition', gamemodes),
                                                 view=CompGamemodeManageView(self.bot, 'Java Edition', gamemodes), ephemeral=True)

    @discord.ui.button(label='Bedrock Gamemodes', emoji='🎮', style=discord.ButtonStyle.secondary, row=1)
    async def manage_bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, 'Bedrock Edition')
        await interaction.response.send_message(embed=gamemode_manage_embed('Bedrock Edition', gamemodes),
                                                 view=CompGamemodeManageView(self.bot, 'Bedrock Edition', gamemodes), ephemeral=True)

    @discord.ui.button(label='Manage Tiers', emoji='🎚️', style=discord.ButtonStyle.secondary, row=1)
    async def manage_tiers(self, interaction: discord.Interaction, button: discord.ui.Button):
        tiers = await get_comp_tiers(self.bot, interaction.guild_id)
        await interaction.response.send_message(embed=tier_manage_embed(tiers),
                                                 view=CompTierManageView(self.bot, tiers), ephemeral=True)

    # Row 2 — channels + ping role
    @discord.ui.button(label='Set Channels', emoji='📡', style=discord.ButtonStyle.primary, row=2)
    async def set_channels(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base('📡  Comp Fight Channels',
                          'Pick the channel/category for each purpose:\n\n'
                          '• **Queue Panel** — public panel + live queue status\n'
                          '• **Logs** — staff activity logs\n'
                          '• **Ticket Category** — where match tickets get created',
                          color=PURPLE),
            view=CompChannelsView(self.bot), ephemeral=True)

    @discord.ui.button(label='Set Ping Role', emoji='🔔', style=discord.ButtonStyle.secondary, row=2)
    async def set_ping_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base('🔔  Staff Ping Role', 'Pick the role pinged whenever a new match ticket opens.', color=PURPLE),
            view=CompPingRoleView(self.bot), ephemeral=True)

    # Row 3 — queue view
    @discord.ui.button(label='View Queue', emoji='📋', style=discord.ButtonStyle.secondary, row=3)
    async def view_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        entries = await get_full_queue(self.bot, interaction.guild_id)
        await interaction.response.send_message(embed=queue_status_embed(interaction.guild, entries), ephemeral=True)

    # Row 4 — tier roles / season
    @discord.ui.button(label='Tier Roles', emoji='🎭', style=discord.ButtonStyle.primary, row=4)
    async def tier_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        mapping = await get_tier_roles(self.bot, interaction.guild_id)
        lines = '\n'.join(f'**{tier}** → <@&{role_id}>' for tier, role_id in mapping.items()) or '_No mappings yet._'
        await interaction.response.send_message(
            embed=E.base('🎭  Auto Tier Roles',
                          f'{lines}\n\n_A member auto-gets the role for their best-ranked current tier and loses any other mapped role._',
                          color=PURPLE),
            view=CompTierRolesView(self.bot), ephemeral=True)

    @discord.ui.button(label='Season Reset', emoji='📅', style=discord.ButtonStyle.danger, row=4)
    async def season_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        await interaction.response.send_message(
            embed=E.base('⚠️  Confirm Season Reset',
                          f'This archives **every** player\'s stats (wins/losses/streak/tier) from '
                          f'Season **{settings.get("season_number", 1)}** and resets everyone to Unranked '
                          f'for a fresh season. This cannot be undone from here.',
                          color=ORANGE),
            view=SeasonResetConfirmView(self.bot), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  COG
# ═══════════════════════════════════════════════════════════════════════════════

class CompFight(commands.Cog, name='CompFight'):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(CompQueuePanelView(bot))
        bot.add_view(MatchTicketControlView(bot))

    @app_commands.command(name='comppanel', description='(Admin/Owner only) Post the Comp Fight queue panel.')
    async def comppanel(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        embed = queue_join_embed(interaction.guild)
        # Sent as a plain channel message (not interaction.response) so Discord
        # doesn't tag it with "Username used /comppanel" above the panel.
        await interaction.channel.send(embed=embed, view=CompQueuePanelView(self.bot))
        await interaction.response.send_message(embed=E.success('Comp Fight panel posted.'), ephemeral=True)
        await _refresh_queue_status(self.bot, interaction.guild)

    @app_commands.command(name='compadminpanel', description='(Admin/Owner only) Full admin panel for the Comp Fight system.')
    async def compadminpanel(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        java_gm = await get_comp_gamemodes(self.bot, interaction.guild_id, 'Java Edition')
        bedrock_gm = await get_comp_gamemodes(self.bot, interaction.guild_id, 'Bedrock Edition')
        tiers = await get_comp_tiers(self.bot, interaction.guild_id)
        active = await get_active_matches(self.bot, interaction.guild_id)
        queue = await get_full_queue(self.bot, interaction.guild_id)
        tier_roles = await get_tier_roles(self.bot, interaction.guild_id)
        embed = comp_admin_embed(interaction.guild, settings, java_gm, bedrock_gm, tiers, len(active), len(queue), len(tier_roles))
        await interaction.response.send_message(embed=embed, view=CompAdminPanelView(self.bot), ephemeral=True)

    @app_commands.command(name='compstats', description='Check your (or another player\'s) Comp Fight tier & record.')
    @app_commands.describe(edition='Java or Bedrock Edition', gamemode='Gamemode name', member='Whose stats to check')
    async def compstats(self, interaction: discord.Interaction, edition: str, gamemode: str, member: discord.Member = None):
        if edition not in EDITIONS:
            return await interaction.response.send_message(embed=E.error(f'Edition must be one of: {", ".join(EDITIONS)}'), ephemeral=True)
        member = member or interaction.user
        player = await get_player(self.bot, interaction.guild_id, member.id, edition, gamemode)
        e = E.base(f'📊  {member.display_name} — {gamemode}',
                    f'**Tier:** {player["tier_label"]}\n'
                    f'**Record:** {player["wins"]}W / {player["losses"]}L\n'
                    f'**Streak:**{streak_badge(player["streak"]) or " —"}',
                    color=BLUE)
        e.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @compstats.autocomplete('edition')
    async def edition_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=e, value=e) for e in EDITIONS if current.lower() in e.lower()][:25]

    @compstats.autocomplete('gamemode')
    async def gamemode_autocomplete(self, interaction: discord.Interaction, current: str):
        edition = interaction.namespace.edition if interaction.namespace.edition in EDITIONS else 'Bedrock Edition'
        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, edition)
        return [app_commands.Choice(name=n, value=n) for n, _ in gamemodes if current.lower() in n.lower()][:25]

    @app_commands.command(name='compleaderboard', description='Top Comp Fight players for a gamemode by tier & record.')
    @app_commands.describe(edition='Java or Bedrock Edition', gamemode='Gamemode name', top='How many to show (default 10, max 25)')
    async def compleaderboard(self, interaction: discord.Interaction, edition: str, gamemode: str, top: int = 10):
        if edition not in EDITIONS:
            return await interaction.response.send_message(embed=E.error(f'Edition must be one of: {", ".join(EDITIONS)}'), ephemeral=True)
        top = max(1, min(top, 25))
        rows = await get_leaderboard(self.bot, interaction.guild_id, edition, gamemode, top)
        await interaction.response.send_message(embed=leaderboard_embed(interaction.guild, edition, gamemode, rows))

    @compleaderboard.autocomplete('edition')
    async def leaderboard_edition_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=e, value=e) for e in EDITIONS if current.lower() in e.lower()][:25]

    @compleaderboard.autocomplete('gamemode')
    async def leaderboard_gamemode_autocomplete(self, interaction: discord.Interaction, current: str):
        edition = interaction.namespace.edition if interaction.namespace.edition in EDITIONS else 'Bedrock Edition'
        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, edition)
        return [app_commands.Choice(name=n, value=n) for n, _ in gamemodes if current.lower() in n.lower()][:25]

    @app_commands.command(name='comphistory', description='See recent Comp Fight match history for yourself or another player.')
    @app_commands.describe(member='Whose history to check', amount='How many recent matches (default 10, max 25)')
    async def comphistory(self, interaction: discord.Interaction, member: discord.Member = None, amount: int = 10):
        member = member or interaction.user
        amount = max(1, min(amount, 25))
        matches = await get_match_history(self.bot, interaction.guild_id, member.id, amount)
        await interaction.response.send_message(embed=history_embed(interaction.guild, member, matches), ephemeral=True)

    @app_commands.command(name='compclearcooldown', description='(Admin/Owner only) Clear a player\'s anti-dodge queue cooldown.')
    @app_commands.describe(member='The player to clear the cooldown for')
    async def compclearcooldown(self, interaction: discord.Interaction, member: discord.Member):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await clear_cooldown(self.bot, interaction.guild_id, member.id)
        await interaction.response.send_message(embed=E.success(f'Cleared {member.mention}\'s queue cooldown.'), ephemeral=True)


async def setup(bot):
    await bot.add_cog(CompFight(bot))
