import aiosqlite
import json
import os
import datetime
from typing import Optional, List, Dict, Any

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'tickets.db')


class Database:
    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self.db_path = DB_PATH

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript('''
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id            INTEGER PRIMARY KEY,
                    support_role_ids    TEXT    DEFAULT '[]',
                    claim_role_ids      TEXT    DEFAULT '[]',
                    close_role_ids      TEXT    DEFAULT '[]',
                    reopen_role_ids     TEXT    DEFAULT '[]',
                    transcript_role_ids TEXT    DEFAULT '[]',
                    admin_role_ids      TEXT    DEFAULT '[]',
                    log_channel_id      INTEGER DEFAULT NULL,
                    transcript_channel_id INTEGER DEFAULT NULL,
                    ticket_category_id  INTEGER DEFAULT NULL,
                    setup_channel_id    INTEGER DEFAULT NULL,
                    cooldown_seconds    INTEGER DEFAULT 300
                );

                CREATE TABLE IF NOT EXISTS ticket_categories (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id            INTEGER NOT NULL,
                    name                TEXT    NOT NULL,
                    emoji               TEXT    DEFAULT '🎫',
                    description         TEXT    DEFAULT '',
                    enabled             INTEGER DEFAULT 1,
                    discord_category_id INTEGER DEFAULT NULL,
                    UNIQUE(guild_id, name)
                );

                CREATE TABLE IF NOT EXISTS tickets (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id    INTEGER NOT NULL,
                    user_id     INTEGER NOT NULL,
                    channel_id  INTEGER NOT NULL UNIQUE,
                    category    TEXT    NOT NULL,
                    status      TEXT    DEFAULT 'open',
                    claimed_by  INTEGER DEFAULT NULL,
                    created_at  TEXT    DEFAULT (datetime('now')),
                    closed_at   TEXT    DEFAULT NULL,
                    answers     TEXT    DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS blacklist (
                    guild_id    INTEGER NOT NULL,
                    user_id     INTEGER NOT NULL,
                    reason      TEXT    DEFAULT '',
                    added_at    TEXT    DEFAULT (datetime('now')),
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS ticket_logs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id    INTEGER NOT NULL,
                    ticket_id   INTEGER NOT NULL,
                    action      TEXT    NOT NULL,
                    actor_id    INTEGER NOT NULL,
                    timestamp   TEXT    DEFAULT (datetime('now')),
                    details     TEXT    DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS cooldowns (
                    guild_id    INTEGER NOT NULL,
                    user_id     INTEGER NOT NULL,
                    last_ticket TEXT    NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );
            ''')
            await db.commit()

    # ─────────────────────────── Guild Setup ────────────────────────────

    async def setup_guild(self, guild_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)',
                (guild_id,)
            )
            defaults = [
                ('General Support',  '🎫', 'Get help with general questions & issues'),
                ('Staff Apply',  '🧑‍💼', 'Staff Applications'),
                ('Report User',      '🚨', 'Report a member for breaking the rules'),
                ('Partnership',      '🤝', 'Discuss collaboration & partnership opportunities'),
                ('Support Ticket',      '🎫', 'Need? Support let us know how we can help.'),
            ]
            for name, emoji, desc in defaults:
                await db.execute(
                    'INSERT OR IGNORE INTO ticket_categories (guild_id, name, emoji, description) VALUES (?,?,?,?)',
                    (guild_id, name, emoji, desc)
                )
            await db.commit()

    # ─────────────────────────── Settings ───────────────────────────────

    async def get_settings(self, guild_id: int) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM guild_settings WHERE guild_id = ?', (guild_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                data = dict(row)
                for key in ['support_role_ids', 'claim_role_ids', 'close_role_ids',
                            'reopen_role_ids', 'transcript_role_ids', 'admin_role_ids']:
                    data[key] = json.loads(data.get(key) or '[]')
                return data

    async def update_setting(self, guild_id: int, key: str, value: Any):
        if isinstance(value, (list, dict)):
            value = json.dumps(value)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f'UPDATE guild_settings SET {key} = ? WHERE guild_id = ?',
                (value, guild_id)
            )
            await db.commit()

    async def reset_settings(self, guild_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('DELETE FROM guild_settings WHERE guild_id = ?', (guild_id,))
            await db.execute('DELETE FROM ticket_categories WHERE guild_id = ?', (guild_id,))
            await db.commit()
        await self.setup_guild(guild_id)

    # ─────────────────────────── Categories ─────────────────────────────

    async def get_categories(self, guild_id: int) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM ticket_categories WHERE guild_id = ? ORDER BY id',
                (guild_id,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_enabled_categories(self, guild_id: int) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM ticket_categories WHERE guild_id = ? AND enabled = 1 ORDER BY id',
                (guild_id,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def toggle_category(self, guild_id: int, name: str, enabled: bool):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'UPDATE ticket_categories SET enabled = ? WHERE guild_id = ? AND name = ?',
                (1 if enabled else 0, guild_id, name)
            )
            await db.commit()

    async def update_category_field(self, guild_id: int, name: str, key: str, value: Any):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f'UPDATE ticket_categories SET {key} = ? WHERE guild_id = ? AND name = ?',
                (value, guild_id, name)
            )
            await db.commit()

    # ─────────────────────────── Tickets ────────────────────────────────

    async def get_open_ticket(self, guild_id: int, user_id: int) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",
                (guild_id, user_id)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def create_ticket(self, guild_id: int, user_id: int, channel_id: int,
                            category: str, answers: dict) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                'INSERT INTO tickets (guild_id, user_id, channel_id, category, answers) VALUES (?,?,?,?,?)',
                (guild_id, user_id, channel_id, category, json.dumps(answers))
            )
            await db.commit()
            return cur.lastrowid

    async def get_ticket_by_channel(self, channel_id: int) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM tickets WHERE channel_id = ?', (channel_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                data = dict(row)
                data['answers'] = json.loads(data.get('answers') or '{}')
                return data

    async def get_ticket_by_id(self, ticket_id: int) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM tickets WHERE id = ?', (ticket_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                data = dict(row)
                data['answers'] = json.loads(data.get('answers') or '{}')
                return data

    async def update_ticket(self, channel_id: int, key: str, value: Any):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f'UPDATE tickets SET {key} = ? WHERE channel_id = ?',
                (value, channel_id)
            )
            await db.commit()

    async def get_ticket_messages(self, guild_id: int, limit: int = 50) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM tickets WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?',
                (guild_id, limit)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    # ─────────────────────────── Blacklist ──────────────────────────────

    async def is_blacklisted(self, guild_id: int, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                'SELECT 1 FROM blacklist WHERE guild_id=? AND user_id=?', (guild_id, user_id)
            ) as cur:
                return await cur.fetchone() is not None

    async def blacklist_user(self, guild_id: int, user_id: int, reason: str = ''):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT OR REPLACE INTO blacklist (guild_id, user_id, reason) VALUES (?,?,?)',
                (guild_id, user_id, reason)
            )
            await db.commit()

    async def unblacklist_user(self, guild_id: int, user_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'DELETE FROM blacklist WHERE guild_id=? AND user_id=?', (guild_id, user_id)
            )
            await db.commit()

    async def get_blacklist(self, guild_id: int) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM blacklist WHERE guild_id = ?', (guild_id,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    # ─────────────────────────── Logging ────────────────────────────────

    async def log_action(self, guild_id: int, ticket_id: int, action: str,
                         actor_id: int, details: str = ''):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT INTO ticket_logs (guild_id, ticket_id, action, actor_id, details) VALUES (?,?,?,?,?)',
                (guild_id, ticket_id, action, actor_id, details)
            )
            await db.commit()

    async def get_ticket_logs(self, ticket_id: int) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT * FROM ticket_logs WHERE ticket_id = ? ORDER BY timestamp',
                (ticket_id,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    # ─────────────────────────── Cooldowns ──────────────────────────────

    async def check_cooldown(self, guild_id: int, user_id: int,
                              cooldown_seconds: int = 300) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                'SELECT last_ticket FROM cooldowns WHERE guild_id=? AND user_id=?',
                (guild_id, user_id)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return 0
                last = datetime.datetime.fromisoformat(row[0])
                elapsed = (datetime.datetime.utcnow() - last).total_seconds()
                remaining = cooldown_seconds - elapsed
                return max(0, int(remaining))

    async def set_cooldown(self, guild_id: int, user_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT OR REPLACE INTO cooldowns (guild_id, user_id, last_ticket) VALUES (?,?,?)',
                (guild_id, user_id, datetime.datetime.utcnow().isoformat())
            )
            await db.commit()
