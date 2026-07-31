from __future__ import annotations
import logging
import re
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import utils.embeds as E

logger = logging.getLogger('TicketBot.access')

# ═══════════════════════════════════════════════════════════════════════════
#  /access  —  ACCESS SYSTEM  (hardened)
#
#  Only the bot's REAL owner (the actual owner of the application in the
#  Discord Developer Portal — discord.py verifies this itself, it cannot
#  be spoofed by any role or permission) can grant or revoke access.
#  Server admins, people with Manage Server, or any regular member can
#  never use this, no matter what Discord permissions they have.
#
#  Security hardening in this version:
#    • Access is scoped PER SERVER by default — access granted in one
#      guild no longer silently works in every other guild the bot is in.
#      (This was the biggest risk in the previous version.) Owners can
#      still explicitly grant cross-server access with `global_access`,
#      but it's opt-in and clearly labelled, never the default.
#    • Grants can be temporary (auto-expiring) instead of forever, so a
#      forgotten grant doesn't stay live indefinitely.
#    • Every grant/revoke/expiry is written to a permanent, append-only
#      audit log (separate from the live access_list table) and, if a
#      log channel is configured, posted there too — so access changes
#      are never silent.
#    • A confirmation step is required before a grant is finalized, to
#      avoid fat-fingering the wrong user.
#    • Access can never be granted to a bot account.
#    • All permission checks fail CLOSED — any unexpected DB error is
#      treated as "no access" rather than accidentally letting someone in.
#
#  To gate any feature in another cog/command as "access-only", import
#  the `has_access()` helper below and use it like this:
#
#      from cogs.access import has_access
#      if not await has_access(bot, interaction.guild_id, interaction.user.id):
#          return await interaction.response.send_message(
#              embed=E.error("This feature is only available to users with access."),
#              ephemeral=True
#          )
# ═══════════════════════════════════════════════════════════════════════════

GLOBAL_SCOPE = 0  # sentinel guild_id meaning "works in every guild"
DURATION_RE = re.compile(r'^\s*(\d+)\s*([mhdw])\s*$', re.IGNORECASE)
DURATION_UNITS = {'m': 'minutes', 'h': 'hours', 'd': 'days', 'w': 'weeks'}
MAX_DURATION_DAYS = 365  # hard ceiling so nobody can grant a 100-year "temporary" pass
MAX_NOTE_LEN = 300


def _parse_duration(raw: str) -> tuple[timedelta | None, str | None]:
    """Parses '30m' / '12h' / '7d' / '2w' into a timedelta.
    Returns (timedelta, None) on success or (None, error_message) on failure.
    Empty/None input means "no expiry" (permanent) -> (None, None)."""
    if not raw or not raw.strip():
        return None, None
    m = DURATION_RE.match(raw)
    if not m:
        return None, ('Invalid duration format. Use a number followed by '
                       'm/h/d/w, e.g. `30m`, `12h`, `7d`, `2w`.')
    amount, unit = int(m.group(1)), m.group(2).lower()
    if amount <= 0:
        return None, 'Duration must be a positive number.'
    delta = timedelta(**{DURATION_UNITS[unit]: amount})
    if delta > timedelta(days=MAX_DURATION_DAYS):
        return None, f'Duration cannot exceed {MAX_DURATION_DAYS} days.'
    return delta, None


# ═══════════════════════════════════════════════════════════════════════════
#  STORAGE
# ═══════════════════════════════════════════════════════════════════════════

async def _ensure_table(bot: commands.Bot):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS access_list (
                guild_id    INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                granted_by  INTEGER NOT NULL,
                granted_at  TEXT    DEFAULT (datetime('now')),
                expires_at  TEXT    DEFAULT NULL,
                note        TEXT    DEFAULT '',
                PRIMARY KEY (guild_id, user_id)
            )
        ''')

        # ── Migration: older versions of this bot used a single global
        # PRIMARY KEY(user_id) table with no guild_id column at all. If that
        # old table is detected, migrate its rows forward as GLOBAL_SCOPE
        # grants (so nobody who previously had access silently loses it),
        # then continue on the new per-guild schema from now on.
        async with db.execute("PRAGMA table_info(access_list)") as cur:
            cols = {row[1] for row in await cur.fetchall()}

        if 'guild_id' not in cols:
            logger.info('Migrating legacy access_list table (no guild_id column) …')
            await db.execute('ALTER TABLE access_list RENAME TO access_list_legacy')
            await db.execute('''
                CREATE TABLE access_list (
                    guild_id    INTEGER NOT NULL,
                    user_id     INTEGER NOT NULL,
                    granted_by  INTEGER NOT NULL,
                    granted_at  TEXT    DEFAULT (datetime('now')),
                    expires_at  TEXT    DEFAULT NULL,
                    note        TEXT    DEFAULT '',
                    PRIMARY KEY (guild_id, user_id)
                )
            ''')
            await db.execute('''
                INSERT INTO access_list (guild_id, user_id, granted_by, granted_at, note)
                SELECT ?, user_id, granted_by, granted_at, note FROM access_list_legacy
            ''', (GLOBAL_SCOPE,))
            await db.execute('DROP TABLE access_list_legacy')
            logger.info('Legacy access_list migrated to per-guild schema (as global-scope grants).')
        elif 'expires_at' not in cols:
            # Table already had guild_id (e.g. mid-migration) but not expires_at yet.
            await db.execute('ALTER TABLE access_list ADD COLUMN expires_at TEXT DEFAULT NULL')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS access_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                action      TEXT    NOT NULL,
                target_id   INTEGER NOT NULL,
                actor_id    INTEGER NOT NULL,
                detail      TEXT    DEFAULT '',
                created_at  TEXT    DEFAULT (datetime('now'))
            )
        ''')
        await db.commit()


async def _audit(bot: commands.Bot, guild_id: int, action: str, target_id: int,
                 actor_id: int, detail: str = ''):
    """Append-only audit trail. Never updated or deleted, even when the
    underlying access grant is later revoked or expires — so there's always
    a record of who did what and when."""
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO access_audit_log (guild_id, action, target_id, actor_id, detail) '
            'VALUES (?, ?, ?, ?, ?)',
            (guild_id, action, target_id, actor_id, detail[:500])
        )
        await db.commit()
    logger.info(f'[access] {action} guild={guild_id} target={target_id} actor={actor_id} {detail}')


async def _notify_log_channel(bot: commands.Bot, guild: discord.Guild | None, embed: discord.Embed):
    """Best-effort: if this guild has a log channel configured (reusing the
    same log_channel_id ticket system already uses), post the access change
    there too, so admins see it without needing to run /access list."""
    if guild is None:
        return
    try:
        settings = await bot.db.get_settings(guild.id)
    except Exception:
        return
    if not settings:
        return
    channel_id = settings.get('log_channel_id')
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


async def _purge_expired(bot: commands.Bot, guild_id: int | None = None):
    """Lazily deletes expired grants. Called before every access check/list
    so expired access never lingers even if nobody explicitly checks."""
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        if guild_id is None:
            async with db.execute(
                "SELECT * FROM access_list WHERE expires_at IS NOT NULL AND expires_at <= datetime('now')"
            ) as cur:
                expired = await cur.fetchall()
            await db.execute("DELETE FROM access_list WHERE expires_at IS NOT NULL AND expires_at <= datetime('now')")
        else:
            async with db.execute(
                "SELECT * FROM access_list WHERE expires_at IS NOT NULL AND expires_at <= datetime('now') "
                "AND (guild_id = ? OR guild_id = ?)", (guild_id, GLOBAL_SCOPE)
            ) as cur:
                expired = await cur.fetchall()
            await db.execute(
                "DELETE FROM access_list WHERE expires_at IS NOT NULL AND expires_at <= datetime('now') "
                "AND (guild_id = ? OR guild_id = ?)", (guild_id, GLOBAL_SCOPE)
            )
        await db.commit()
    for row in expired:
        await _audit(bot, row['guild_id'], 'expired', row['user_id'], row['user_id'],
                     detail='Automatically expired')


async def has_access(bot: commands.Bot, guild_id: int | None, user_id: int) -> bool:
    """Return True if the given user currently has NON-expired access in
    this guild (or a global grant). Fails CLOSED on any error."""
    try:
        await _ensure_table(bot)
        gid = guild_id if guild_id is not None else GLOBAL_SCOPE
        await _purge_expired(bot, gid)
        async with aiosqlite.connect(bot.db.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM access_list WHERE user_id = ? AND (guild_id = ? OR guild_id = ?) "
                "AND (expires_at IS NULL OR expires_at > datetime('now'))",
                (user_id, gid, GLOBAL_SCOPE)
            ) as cur:
                return await cur.fetchone() is not None
    except Exception:
        logger.exception('has_access() failed — denying access (fail-closed).')
        return False


async def grant_access(bot: commands.Bot, guild_id: int, user_id: int, granted_by: int,
                       note: str = '', expires_at: datetime | None = None):
    await _ensure_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT OR REPLACE INTO access_list (guild_id, user_id, granted_by, granted_at, expires_at, note) '
            "VALUES (?, ?, ?, datetime('now'), ?, ?)",
            (guild_id, user_id, granted_by,
             expires_at.strftime('%Y-%m-%d %H:%M:%S') if expires_at else None,
             note[:MAX_NOTE_LEN])
        )
        await db.commit()


async def revoke_access(bot: commands.Bot, guild_id: int, user_id: int) -> bool:
    await _ensure_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        cur = await db.execute(
            'DELETE FROM access_list WHERE user_id = ? AND (guild_id = ? OR guild_id = ?)',
            (user_id, guild_id, GLOBAL_SCOPE)
        )
        await db.commit()
        return cur.rowcount > 0


async def list_access(bot: commands.Bot, guild_id: int) -> list[dict]:
    await _ensure_table(bot)
    await _purge_expired(bot, guild_id)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM access_list WHERE guild_id = ? OR guild_id = ? ORDER BY granted_at DESC',
            (guild_id, GLOBAL_SCOPE)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ═══════════════════════════════════════════════════════════════════════════
#  SHARED PERMISSION HELPERS
#
#  Every command in this bot (across every cog) is locked down to only
#  the bot's real Owner or a server Admin who's been explicitly granted
#  access. Import these two helpers into any other cog to apply the exact
#  same rule — signatures are unchanged from before, so no other file
#  needs to be touched:
#
#      from cogs.access import require_admin_or_owner
#
#      async def mycommand(self, interaction: discord.Interaction):
#          if not await require_admin_or_owner(self.bot, interaction):
#              return
#          ...
# ═══════════════════════════════════════════════════════════════════════════

async def is_admin_or_owner(bot: commands.Bot, interaction: discord.Interaction) -> bool:
    """Return True only if the user is the bot's real Owner, or a user who
    has been explicitly granted access via /access grant for THIS guild
    (or a global grant). Fails closed on any unexpected error.

    NOTE: having Discord's Administrator permission (or an Admin role
    configured via /setrole) is NOT enough on its own — the bot Owner
    must grant access with /access grant for anyone else to use commands.
    """
    try:
        if await bot.is_owner(interaction.user):
            return True
    except Exception:
        logger.exception('bot.is_owner() failed — falling back to access_list only.')
    return await has_access(bot, interaction.guild_id, interaction.user.id)


async def require_admin_or_owner(bot: commands.Bot, interaction: discord.Interaction) -> bool:
    """Check is_admin_or_owner() and, if it fails, send the standard denial
    message and log the denied attempt (useful for spotting probing/abuse).
    Returns True/False so callers can just do:
        if not await require_admin_or_owner(self.bot, interaction):
            return
    """
    if await is_admin_or_owner(bot, interaction):
        return True

    logger.warning(
        f'[access] DENIED — user={interaction.user} ({interaction.user.id}) '
        f'guild={interaction.guild_id} command={getattr(interaction.command, "qualified_name", "?")}'
    )
    await interaction.response.send_message(
        embed=E.error(
            '❌ Only users who have been granted **access** in this server, or the '
            '**main Owner** of the bot, can use this command.'
        ),
        ephemeral=True
    )
    return False


# Static reference used by /access commands below. Update this list if you
# add, rename, or remove commands elsewhere in the bot.
COMMAND_PERMISSIONS = {
    'Owner Only': [
        '/access grant', '/access revoke', '/access list', '/access audit',
    ],
    'Admin + Owner': [
        '/access check', '/access commands',
        '/adminpanel', '/blacklist', '/unblacklist', '/setrole', '/removerole',
        '/settings', '/botping',
        '/setup', '/newticket', '/close', '/claim', '/transcript', '/add', '/remove',
        '/tierpanel', '/tiertestpanel', '/tiersettings', '/tieradminpanel',
    ],
}


# ═══════════════════════════════════════════════════════════════════════════
#  CONFIRMATION VIEW — grants are privilege escalation, so require an
#  explicit confirm click before writing anything to the DB.
# ═══════════════════════════════════════════════════════════════════════════

class GrantConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild_id: int, target: discord.User,
                actor_id: int, note: str, expires_at: datetime | None, is_global: bool):
        super().__init__(timeout=60)
        self.bot = bot
        self.guild_id = guild_id
        self.target = target
        self.actor_id = actor_id
        self.note = note
        self.expires_at = expires_at
        self.is_global = is_global
        self.done = False

    async def on_timeout(self):
        self.done = True

    @discord.ui.button(label='Confirm Grant', style=discord.ButtonStyle.success, emoji='✅')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message(
                embed=E.error('Only the person who ran this command can confirm it.'), ephemeral=True)
        if self.done:
            return
        self.done = True

        scope_id = GLOBAL_SCOPE if self.is_global else self.guild_id
        await grant_access(self.bot, scope_id, self.target.id, self.actor_id, self.note, self.expires_at)

        scope_label = 'ALL servers (global)' if self.is_global else f'this server ({interaction.guild.name if interaction.guild else self.guild_id})'
        expiry_label = f"until <t:{int(self.expires_at.timestamp())}:F>" if self.expires_at else 'permanently'
        detail = f'scope={"global" if self.is_global else self.guild_id} expires={self.expires_at} note={self.note}'
        await _audit(self.bot, scope_id, 'grant', self.target.id, self.actor_id, detail)

        log_embed = E.base(
            '✅ Access Granted',
            f'{self.target.mention} (`{self.target.id}`) was granted access {expiry_label}.\n'
            f'Scope: **{scope_label}**\n'
            f'Granted by: <@{self.actor_id}>' + (f'\n📝 Note: {self.note}' if self.note else '')
        )
        await _notify_log_channel(self.bot, interaction.guild, log_embed)

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=E.success(
                f'✅ {self.target.mention} has been granted access {expiry_label} '
                f'({scope_label}).' + (f'\n📝 Note: {self.note}' if self.note else '')
            ),
            view=self
        )
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary, emoji='✖️')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message(
                embed=E.error('Only the person who ran this command can cancel it.'), ephemeral=True)
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=E.base('✖️  Cancelled', 'No access was granted.'), view=self)
        self.stop()


class RevokeConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild_id: int, target: discord.User, actor_id: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.guild_id = guild_id
        self.target = target
        self.actor_id = actor_id
        self.done = False

    @discord.ui.button(label='Confirm Revoke', style=discord.ButtonStyle.danger, emoji='🗑️')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message(
                embed=E.error('Only the person who ran this command can confirm it.'), ephemeral=True)
        if self.done:
            return
        self.done = True

        removed = await revoke_access(self.bot, self.guild_id, self.target.id)
        if removed:
            await _audit(self.bot, self.guild_id, 'revoke', self.target.id, self.actor_id)
            log_embed = E.base(
                '🗑️ Access Revoked',
                f'{self.target.mention} (`{self.target.id}`) had their access revoked.\n'
                f'Revoked by: <@{self.actor_id}>'
            )
            await _notify_log_channel(self.bot, interaction.guild, log_embed)

        for child in self.children:
            child.disabled = True
        msg = (f'✅ Access has been removed from {self.target.mention}.' if removed
               else f'{self.target.mention} did not have access to begin with.')
        await interaction.response.edit_message(
            embed=(E.success(msg) if removed else E.error(msg)), view=self)
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary, emoji='✖️')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message(
                embed=E.error('Only the person who ran this command can cancel it.'), ephemeral=True)
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=E.base('✖️  Cancelled', 'No access was revoked.'), view=self)
        self.stop()


class AccessCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await _ensure_table(self.bot)

    # ─────────────────────────── Owner Gate ──────────────────────────────
    # is_owner() fetches application_info() internally and caches the
    # real owner's (or team owner's) ID — this cannot be overridden by
    # any role, server permission, or config file.
    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        try:
            is_owner = await self.bot.is_owner(interaction.user)
        except Exception:
            logger.exception('bot.is_owner() failed during owner-gated command — denying (fail-closed).')
            is_owner = False

        if is_owner:
            return True

        logger.warning(
            f'[access] Owner-only command denied — user={interaction.user} ({interaction.user.id}) '
            f'guild={interaction.guild_id} command={getattr(interaction.command, "qualified_name", "?")}'
        )
        await interaction.response.send_message(
            embed=E.error(
                '❌ This command can only be used by the **real owner** of the bot.\n'
                'Server admins or any regular member cannot use this.'
            ),
            ephemeral=True
        )
        return False

    access_group = app_commands.Group(
        name='access',
        description='Access system (bot owner only).'
    )

    # ─────────────────────────── /access grant ───────────────────────────
    @access_group.command(name='grant', description='(Owner only) Grant access to a user in this server.')
    @app_commands.describe(
        user='User to grant access to',
        note='Optional note (reason/plan/etc.)',
        duration='Optional: how long access lasts, e.g. 30m, 12h, 7d, 2w. Leave empty for permanent.',
        global_access='Grant access in EVERY server this bot is in, not just this one. Default: No.',
    )
    async def grant(self, interaction: discord.Interaction, user: discord.User,
                    note: str = '', duration: str = '', global_access: bool = False):
        if not await self._require_owner(interaction):
            return

        if user.bot:
            return await interaction.response.send_message(
                embed=E.error('❌ Access cannot be granted to bot accounts.'), ephemeral=True)

        if global_access is False and interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=E.error('Run this inside a server to grant server-scoped access, '
                              'or set `global_access: True` to grant it everywhere.'),
                ephemeral=True)

        if len(note) > MAX_NOTE_LEN:
            return await interaction.response.send_message(
                embed=E.error(f'Note is too long (max {MAX_NOTE_LEN} characters).'), ephemeral=True)

        delta, err = _parse_duration(duration)
        if err:
            return await interaction.response.send_message(embed=E.error(err), ephemeral=True)
        expires_at = (datetime.now(timezone.utc) + delta) if delta else None

        scope_label = 'ALL servers (global)' if global_access else (interaction.guild.name if interaction.guild else str(interaction.guild_id))
        expiry_label = f"until <t:{int(expires_at.timestamp())}:F>" if expires_at else 'permanently'

        view = GrantConfirmView(self.bot, interaction.guild_id, user, interaction.user.id,
                                note, expires_at, global_access)
        await interaction.response.send_message(
            embed=E.base(
                '⚠️  Confirm Access Grant',
                f'Grant access to {user.mention} (`{user.id}`) {expiry_label}?\n'
                f'Scope: **{scope_label}**' + (f'\n📝 Note: {note}' if note else '') +
                '\n\nThis lets them use every Admin/Owner-gated command in that scope. Confirm?'
            ),
            view=view,
            ephemeral=True
        )

    # ─────────────────────────── /access revoke ──────────────────────────
    @access_group.command(name='revoke', description='(Owner only) Revoke access from a user in this server.')
    @app_commands.describe(user='User to revoke access from')
    async def revoke(self, interaction: discord.Interaction, user: discord.User):
        if not await self._require_owner(interaction):
            return

        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=E.error('Run this inside a server to revoke server-scoped access.'), ephemeral=True)

        view = RevokeConfirmView(self.bot, interaction.guild_id, user, interaction.user.id)
        await interaction.response.send_message(
            embed=E.base(
                '⚠️  Confirm Access Revoke',
                f'Revoke access from {user.mention} (`{user.id}`) in this server?'
            ),
            view=view,
            ephemeral=True
        )

    # ─────────────────────────── /access list ────────────────────────────
    @access_group.command(name='list', description='(Owner only) List all users with access in this server.')
    async def list_(self, interaction: discord.Interaction):
        if not await self._require_owner(interaction):
            return

        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=E.error('Run this inside a server to list server-scoped access.'), ephemeral=True)

        rows = await list_access(self.bot, interaction.guild_id)
        if not rows:
            return await interaction.response.send_message(
                embed=E.base('Access List', 'No one has been granted access yet.'),
                ephemeral=True
            )

        lines = []
        for r in rows:
            scope = 'global' if r['guild_id'] == GLOBAL_SCOPE else 'this server'
            expiry = f" — expires {r['expires_at']} UTC" if r['expires_at'] else ' — permanent'
            lines.append(
                f"<@{r['user_id']}> — granted by <@{r['granted_by']}> on {r['granted_at']} "
                f"({scope}{expiry})" + (f" — _{r['note']}_" if r['note'] else '')
            )

        await interaction.response.send_message(
            embed=E.base('Access List', '\n'.join(lines)),
            ephemeral=True
        )

    # ─────────────────────────── /access check ───────────────────────────
    @access_group.command(name='check', description='(Admin/Owner only) Check if a user currently has access.')
    @app_commands.describe(user='User to check (defaults to yourself)')
    async def check(self, interaction: discord.Interaction, user: discord.User = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return

        target = user or interaction.user
        allowed = await has_access(self.bot, interaction.guild_id, target.id)

        await interaction.response.send_message(
            embed=(E.success(f'{target.mention} **has** access.')
                   if allowed else
                   E.error(f'{target.mention} does **not** have access.')),
            ephemeral=True
        )

    # ─────────────────────────── /access audit ───────────────────────────
    @access_group.command(name='audit', description='(Owner only) Show the recent access audit trail for this server.')
    @app_commands.describe(limit='How many recent entries to show (default 15, max 50)')
    async def audit(self, interaction: discord.Interaction, limit: int = 15):
        if not await self._require_owner(interaction):
            return
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=E.error('Run this inside a server to view its audit trail.'), ephemeral=True)

        limit = max(1, min(limit, 50))
        await _ensure_table(self.bot)
        async with aiosqlite.connect(self.bot.db.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM access_audit_log WHERE guild_id = ? OR guild_id = ? '
                'ORDER BY id DESC LIMIT ?',
                (interaction.guild_id, GLOBAL_SCOPE, limit)
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message(
                embed=E.base('Access Audit Log', 'No audit entries yet.'), ephemeral=True)

        icon = {'grant': '✅', 'revoke': '🗑️', 'expired': '⏰'}
        lines = [
            f"{icon.get(r['action'], '•')} `{r['created_at']}` **{r['action']}** — "
            f"target <@{r['target_id']}>, actor <@{r['actor_id']}>"
            for r in rows
        ]
        await interaction.response.send_message(
            embed=E.base('Access Audit Log', '\n'.join(lines)), ephemeral=True)

    # ─────────────────────────── /access commands ─────────────────────────
    @access_group.command(name='commands', description='(Admin/Owner only) Show which commands can be used by whom.')
    async def commands_(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return

        lines = []
        for group_name, cmds in COMMAND_PERMISSIONS.items():
            lines.append(f'**{group_name}**')
            lines.append(', '.join(cmds))
            lines.append('')

        await interaction.response.send_message(
            embed=E.base('Command Permissions', '\n'.join(lines).strip()),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AccessCog(bot))
