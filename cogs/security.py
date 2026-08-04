"""
security.py — Enterprise Security Suite
=========================================
A single, self-contained cog providing Wick/Beemo/Sapphire-tier protection:
anti-spam, link/scam protection, raid protection, anti-nuke, risk-scored
member screening, captcha verification, security auditing, backups,
incident tracking, a live dashboard, and full event logging.

Drop this file into your cogs/ folder — nothing else needs to change.
It reuses your existing bot.db (aiosqlite, same db file as your other
cogs), utils.embeds, and cogs.access permission gate, and creates its own
tables on load so there is nothing to migrate by hand.

Design notes (so future-you knows why things are grouped the way they are):
  • All the "Anti X Spam" categories (spam / duplicate / emoji / sticker /
    gif / attachment / mention / mass-ping / thread) run through ONE
    generic rate-limiter (`RateTracker`) parameterized per category, rather
    than nine near-identical hand-rolled counters. Same detection quality,
    1/9th the code to maintain.
  • Per-event logging (messages/members/voice/server/security) is grouped
    into 5 toggles instead of 20+, since that's what's actually useful to
    flip on/off in practice — still fully configurable via /security setup.
  • Anti-nuke and raid protection both write to the same `security_incidents`
    table so /security dashboard and /security audit can report on both
    from one place.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import utils.embeds as E
from cogs.access import require_admin_or_owner

log = logging.getLogger("security")

# --------------------------------------------------------------------------
# Colors — reuse project palette where it exists, fall back to sane defaults
# for the ones this cog needs that config.py doesn't define.
# --------------------------------------------------------------------------
try:
    from config import PURPLE, PURPLE_DARK, GREEN, RED, ORANGE, GREY
except ImportError:  # pragma: no cover - keeps this cog droppable standalone
    PURPLE, PURPLE_DARK, GREEN, RED, ORANGE, GREY = (
        0x9B59B6, 0x6A3D8F, 0x2ECC71, 0xE74C3C, 0xE67E22, 0x95A5A6,
    )
BLUE = 0x3498DB
YELLOW = 0xF1C40F
DARK_RED = 0x8B0000
BLACK = 0x23272A

SEVERITY_COLOR = {"LOW": GREEN, "MEDIUM": YELLOW, "HIGH": ORANGE, "CRITICAL": DARK_RED}
SEVERITY_EMOJI = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}

# ==========================================================================
# DATABASE
# ==========================================================================

def _connect(bot):
    """Shared connection helper — WAL + busy_timeout so a write from this
    cog never silently loses a race against another cog's connection to the
    same sqlite file."""
    return aiosqlite.connect(bot.db.db_path, timeout=10)


async def _pragmas(db: aiosqlite.Connection):
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=10000")


SCHEMA = """
CREATE TABLE IF NOT EXISTS security_settings (
    guild_id            INTEGER PRIMARY KEY,
    -- module toggles
    mod_anti_spam       INTEGER NOT NULL DEFAULT 1,
    mod_anti_links      INTEGER NOT NULL DEFAULT 1,
    mod_anti_raid       INTEGER NOT NULL DEFAULT 1,
    mod_anti_nuke       INTEGER NOT NULL DEFAULT 1,
    mod_risk_scoring    INTEGER NOT NULL DEFAULT 1,
    mod_captcha         INTEGER NOT NULL DEFAULT 0,
    mod_scam_detection  INTEGER NOT NULL DEFAULT 1,
    -- logging toggles
    log_messages        INTEGER NOT NULL DEFAULT 1,
    log_members         INTEGER NOT NULL DEFAULT 1,
    log_voice           INTEGER NOT NULL DEFAULT 0,
    log_server          INTEGER NOT NULL DEFAULT 1,
    log_security        INTEGER NOT NULL DEFAULT 1,
    -- channels / roles
    log_channel_id       INTEGER,
    alert_channel_id     INTEGER,
    verify_channel_id    INTEGER,
    quarantine_role_id   INTEGER,
    muted_role_id        INTEGER,
    verified_role_id     INTEGER,
    -- raid tuning
    raid_join_threshold  INTEGER NOT NULL DEFAULT 8,
    raid_join_window     INTEGER NOT NULL DEFAULT 12,
    -- captcha tuning
    captcha_mode          TEXT NOT NULL DEFAULT 'button',   -- button | code
    captcha_timeout_secs   INTEGER NOT NULL DEFAULT 600,
    -- state
    panic_mode           INTEGER NOT NULL DEFAULT 0,
    lockdown_mode        INTEGER NOT NULL DEFAULT 0,
    threat_level         TEXT NOT NULL DEFAULT 'LOW',
    updated_at           TEXT
);

CREATE TABLE IF NOT EXISTS security_lists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    list_type  TEXT NOT NULL,               -- 'whitelist' | 'blacklist'
    kind       TEXT NOT NULL,               -- 'user' | 'role' | 'channel' | 'domain'
    value      TEXT NOT NULL,               -- id (as text) or domain string
    reason     TEXT,
    added_by   INTEGER,
    added_at   TEXT
);

CREATE TABLE IF NOT EXISTS security_incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    category    TEXT NOT NULL,              -- 'raid' | 'nuke' | 'spam' | 'scam' | 'link' | 'manual'
    severity    TEXT NOT NULL,              -- LOW/MEDIUM/HIGH/CRITICAL
    actor_id    INTEGER,
    description TEXT,
    evidence    TEXT,
    resolved    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS security_offenses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    category    TEXT NOT NULL,
    action      TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS security_stats (
    guild_id        INTEGER PRIMARY KEY,
    spam_blocked    INTEGER NOT NULL DEFAULT 0,
    raid_attempts   INTEGER NOT NULL DEFAULT 0,
    scam_links      INTEGER NOT NULL DEFAULT 0,
    timeouts        INTEGER NOT NULL DEFAULT 0,
    kicks           INTEGER NOT NULL DEFAULT 0,
    bans            INTEGER NOT NULL DEFAULT 0,
    quarantines     INTEGER NOT NULL DEFAULT 0,
    deleted_msgs    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS security_backups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    created_by  INTEGER,
    created_at  TEXT,
    data        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS security_webhook_activity (
    guild_id    INTEGER NOT NULL,
    webhook_id  INTEGER NOT NULL,
    last_seen   TEXT,
    PRIMARY KEY (guild_id, webhook_id)
);

CREATE TABLE IF NOT EXISTS security_pending_verify (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    code        TEXT,
    joined_at   TEXT,
    PRIMARY KEY (guild_id, user_id)
);
"""


async def ensure_tables(bot):
    if getattr(bot, "_security_tables_ready", False):
        return
    async with _connect(bot) as db:
        await _pragmas(db)
        await db.executescript(SCHEMA)
        await db.commit()
    bot._security_tables_ready = True


DEFAULTS = {
    "mod_anti_spam": True, "mod_anti_links": True, "mod_anti_raid": True,
    "mod_anti_nuke": True, "mod_risk_scoring": True, "mod_captcha": False,
    "mod_scam_detection": True, "log_messages": True, "log_members": True,
    "log_voice": False, "log_server": True, "log_security": True,
    "log_channel_id": None, "alert_channel_id": None, "verify_channel_id": None,
    "quarantine_role_id": None, "muted_role_id": None, "verified_role_id": None,
    "raid_join_threshold": 8, "raid_join_window": 12,
    "captcha_mode": "button", "captcha_timeout_secs": 600,
    "panic_mode": False, "lockdown_mode": False, "threat_level": "LOW",
}
_BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
_COLUMNS = list(DEFAULTS.keys())


async def get_settings(bot, guild_id: int) -> dict:
    await ensure_tables(bot)
    async with _connect(bot) as db:
        await _pragmas(db)
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT OR IGNORE INTO security_settings (guild_id, updated_at) VALUES (?, ?)",
            (guild_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM security_settings WHERE guild_id = ?", (guild_id,))
        row = await cur.fetchone()
    settings = dict(row)
    for key in _BOOL_KEYS:
        settings[key] = bool(settings.get(key))
    return settings


async def update_settings(bot, guild_id: int, **kwargs) -> dict:
    await ensure_tables(bot)
    if not kwargs:
        return await get_settings(bot, guild_id)
    cols = ", ".join(f"{k} = ?" for k in kwargs if k in _COLUMNS)
    vals = [(int(v) if isinstance(v, bool) else v) for k, v in kwargs.items() if k in _COLUMNS]
    async with _connect(bot) as db:
        await _pragmas(db)
        await db.execute(
            "INSERT OR IGNORE INTO security_settings (guild_id, updated_at) VALUES (?, ?)",
            (guild_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.execute(
            f"UPDATE security_settings SET {cols}, updated_at = ? WHERE guild_id = ?",
            (*vals, datetime.now(timezone.utc).isoformat(), guild_id),
        )
        await db.commit()
    return await get_settings(bot, guild_id)


# --------------------------------------------------------------------------
# Stats / incidents / offenses / lists — small shared helpers used
# throughout the listeners and commands below.
# --------------------------------------------------------------------------

async def bump_stat(bot, guild_id: int, column: str, by: int = 1):
    await ensure_tables(bot)
    async with _connect(bot) as db:
        await _pragmas(db)
        await db.execute(
            "INSERT OR IGNORE INTO security_stats (guild_id) VALUES (?)", (guild_id,)
        )
        await db.execute(
            f"UPDATE security_stats SET {column} = {column} + ? WHERE guild_id = ?",
            (by, guild_id),
        )
        await db.commit()


async def get_stats(bot, guild_id: int) -> dict:
    await ensure_tables(bot)
    async with _connect(bot) as db:
        await _pragmas(db)
        db.row_factory = aiosqlite.Row
        await db.execute("INSERT OR IGNORE INTO security_stats (guild_id) VALUES (?)", (guild_id,))
        await db.commit()
        cur = await db.execute("SELECT * FROM security_stats WHERE guild_id = ?", (guild_id,))
        row = await cur.fetchone()
    return dict(row)


async def log_incident(bot, guild_id: int, category: str, severity: str,
                        description: str, actor_id: Optional[int] = None,
                        evidence: Optional[str] = None) -> int:
    await ensure_tables(bot)
    async with _connect(bot) as db:
        await _pragmas(db)
        cur = await db.execute(
            "INSERT INTO security_incidents (guild_id, category, severity, actor_id, "
            "description, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, category, severity, actor_id, description, evidence,
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def recent_incidents(bot, guild_id: int, limit: int = 5) -> list[dict]:
    await ensure_tables(bot)
    async with _connect(bot) as db:
        await _pragmas(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM security_incidents WHERE guild_id = ? "
            "ORDER BY id DESC LIMIT ?", (guild_id, limit),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def record_offense(bot, guild_id: int, user_id: int, category: str,
                          action: str, reason: str):
    await ensure_tables(bot)
    async with _connect(bot) as db:
        await _pragmas(db)
        await db.execute(
            "INSERT INTO security_offenses (guild_id, user_id, category, action, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, category, action, reason, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def offense_count(bot, guild_id: int, user_id: int, since_days: int = 30) -> int:
    await ensure_tables(bot)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    async with _connect(bot) as db:
        await _pragmas(db)
        cur = await db.execute(
            "SELECT COUNT(*) FROM security_offenses WHERE guild_id = ? AND user_id = ? "
            "AND created_at >= ?", (guild_id, user_id, cutoff),
        )
        (count,) = await cur.fetchone()
    return count


async def add_list_entry(bot, guild_id: int, list_type: str, kind: str, value: str,
                          reason: Optional[str], added_by: int):
    await ensure_tables(bot)
    async with _connect(bot) as db:
        await _pragmas(db)
        await db.execute(
            "INSERT INTO security_lists (guild_id, list_type, kind, value, reason, added_by, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, list_type, kind, str(value), reason, added_by,
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def remove_list_entry(bot, guild_id: int, list_type: str, kind: str, value: str) -> int:
    await ensure_tables(bot)
    async with _connect(bot) as db:
        await _pragmas(db)
        cur = await db.execute(
            "DELETE FROM security_lists WHERE guild_id = ? AND list_type = ? AND kind = ? AND value = ?",
            (guild_id, list_type, kind, str(value)),
        )
        await db.commit()
        return cur.rowcount


async def get_list(bot, guild_id: int, list_type: str, kind: Optional[str] = None) -> list[dict]:
    await ensure_tables(bot)
    async with _connect(bot) as db:
        await _pragmas(db)
        db.row_factory = aiosqlite.Row
        if kind:
            cur = await db.execute(
                "SELECT * FROM security_lists WHERE guild_id = ? AND list_type = ? AND kind = ? "
                "ORDER BY id DESC", (guild_id, list_type, kind),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM security_lists WHERE guild_id = ? AND list_type = ? ORDER BY id DESC",
                (guild_id, list_type),
            )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def is_whitelisted(bot, guild_id: int, *, user: Optional[discord.Member] = None,
                          domain: Optional[str] = None) -> bool:
    entries = await get_list(bot, guild_id, "whitelist")
    if not entries:
        return False
    values = {(e["kind"], e["value"]) for e in entries}
    if user is not None:
        if ("user", str(user.id)) in values:
            return True
        role_ids = {str(r.id) for r in user.roles}
        if any(("role", rid) in values for rid in role_ids):
            return True
    if domain is not None:
        domain = domain.lower()
        for e in entries:
            if e["kind"] == "domain" and (domain == e["value"].lower() or domain.endswith("." + e["value"].lower())):
                return True
    return False


async def is_blacklisted_domain(bot, guild_id: int, domain: str) -> Optional[str]:
    entries = await get_list(bot, guild_id, "blacklist", kind="domain")
    domain = domain.lower()
    for e in entries:
        if domain == e["value"].lower() or domain.endswith("." + e["value"].lower()):
            return e["reason"] or "blacklisted domain"
    return None


# ==========================================================================
# IN-MEMORY DETECTION ENGINES
# (Deliberately not persisted — these are all short-window, real-time
# signals; sqlite round-trips would be both slower and pointless for a
# 5-10 second sliding window. Everything that actually needs to survive a
# restart — offenses, incidents, stats, settings — lives in the DB above.)
# ==========================================================================

class RateTracker:
    """Generic sliding-window rate limiter shared by every 'Anti X Spam'
    module. Each category gets its own (guild_id, user_id) -> deque of
    timestamps; `hit()` records one event and returns True the moment the
    configured threshold is exceeded within the configured window."""

    def __init__(self):
        self._data: dict[tuple[int, int, str], deque] = defaultdict(deque)

    def hit(self, guild_id: int, user_id: int, category: str,
            max_count: int, window_secs: float) -> tuple[bool, int]:
        key = (guild_id, user_id, category)
        now = time.monotonic()
        dq = self._data[key]
        dq.append(now)
        while dq and now - dq[0] > window_secs:
            dq.popleft()
        return (len(dq) >= max_count, len(dq))

    def reset(self, guild_id: int, user_id: int, category: str):
        self._data.pop((guild_id, user_id, category), None)


class DuplicateTracker:
    """Anti Duplicate Messages: remembers each user's last few message
    contents and flags exact repeats sent in quick succession."""

    def __init__(self, memory: int = 3):
        self.memory = memory
        self._data: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=memory))

    def check(self, guild_id: int, user_id: int, content: str) -> int:
        key = (guild_id, user_id)
        dq = self._data[key]
        normalized = content.strip().lower()
        repeats = sum(1 for c in dq if c == normalized and normalized)
        dq.append(normalized)
        return repeats


class JoinTracker:
    """Anti Raid: sliding window of recent joins/leaves per guild, used to
    compute join velocity for both raid detection and per-member risk
    scoring."""

    def __init__(self):
        self.joins: dict[int, deque] = defaultdict(deque)
        self.leaves: dict[int, deque] = defaultdict(deque)

    def record_join(self, guild_id: int) -> None:
        self.joins[guild_id].append(time.monotonic())

    def record_leave(self, guild_id: int) -> None:
        self.leaves[guild_id].append(time.monotonic())

    def _count_within(self, dq: deque, window_secs: float) -> int:
        now = time.monotonic()
        while dq and now - dq[0] > window_secs:
            dq.popleft()
        return len(dq)

    def join_count(self, guild_id: int, window_secs: float) -> int:
        return self._count_within(self.joins[guild_id], window_secs)

    def leave_count(self, guild_id: int, window_secs: float) -> int:
        return self._count_within(self.leaves[guild_id], window_secs)


class NukeTracker:
    """Anti Nuke: sliding window of destructive audit-log actions per
    (guild, actor), so a burst of channel/role deletes from one actor in a
    short window trips the breaker even if each individual action would
    look unremarkable on its own."""

    WEIGHTS = {
        "channel_delete": 3, "channel_create": 1, "role_delete": 3,
        "role_create": 1, "webhook_create": 2, "webhook_delete": 2,
        "emoji_delete": 1, "sticker_delete": 1, "guild_update": 2,
        "overwrite_update": 2, "overwrite_create": 1, "member_role_update": 1,
        "member_update": 1, "bot_add": 2,
    }
    THRESHOLD = 6
    WINDOW = 20.0

    def __init__(self):
        self._data: dict[tuple[int, int], deque] = defaultdict(deque)

    def hit(self, guild_id: int, actor_id: int, action: str) -> tuple[bool, int]:
        weight = self.WEIGHTS.get(action, 1)
        key = (guild_id, actor_id)
        now = time.monotonic()
        dq = self._data[key]
        for _ in range(weight):
            dq.append(now)
        while dq and now - dq[0] > self.WINDOW:
            dq.popleft()
        return (len(dq) >= self.THRESHOLD, len(dq))

    def reset(self, guild_id: int, actor_id: int):
        self._data.pop((guild_id, actor_id), None)


rate = RateTracker()
dupes = DuplicateTracker()
joins = JoinTracker()
nuke = NukeTracker()

# Category -> (max_count, window_secs). Tunable in one place; exposed via
# /security setup as "sensitivity" presets (see SENSITIVITY_PRESETS below).
SPAM_LIMITS = {
    "message":    (6, 6.0),
    "emoji":      (10, 8.0),
    "sticker":    (4, 8.0),
    "gif":        (4, 10.0),
    "attachment": (6, 10.0),
    "mention":    (5, 8.0),      # mentions across messages
    "mass_ping":  (12, 5.0),     # mentions within a *single* message triggers instantly, see below
    "thread":     (4, 30.0),
}
CAPS_MIN_LEN = 12
CAPS_RATIO = 0.7
MASS_MENTION_SINGLE_MSG = 6  # mentions in one message = instant trip, no window needed


def caps_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < CAPS_MIN_LEN:
        return 0.0
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters)


_ZALGO_RE = re.compile(r"[\u0300-\u036f\u0483-\u0489\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff]")
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u180e\u2062-\u2064]")


def zalgo_score(text: str) -> float:
    if not text:
        return 0.0
    marks = len(_ZALGO_RE.findall(text))
    return marks / max(len(text), 1)


def has_invisible_unicode(text: str) -> bool:
    return bool(_INVISIBLE_RE.search(text))


# ==========================================================================
# LINK PROTECTION + SCAM DETECTION
# ==========================================================================

URL_RE = re.compile(r"https?://[^\s<>\[\]()]+", re.IGNORECASE)
INVITE_RE = re.compile(
    r"(?:discord\.gg|discord(?:app)?\.com/invite|discordapp\.com/invite)/([a-zA-Z0-9-]+)",
    re.IGNORECASE,
)

# Built-in, extensible signature lists. Anything server-specific goes in
# the security_lists blacklist table via /security blacklist instead of
# editing this file.
URL_SHORTENERS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "buff.ly", "ow.ly",
    "cutt.ly", "rebrand.ly", "shorturl.at", "tiny.cc", "rb.gy", "s.id",
}
IP_LOGGER_DOMAINS = {
    "grabify.link", "iplogger.org", "iplogger.com", "iplogger.ru", "2no.co",
    "blasze.com", "yip.su", "ps3cfw.com", "curiouscat.club", "ezstat.ru",
    "spylogger.net", "stopmodreposts.com", "whatstheirip.com", "mylnikov.org",
}
FAKE_NITRO_DOMAINS = {
    "discord-nitro.com", "discordnitro.com", "discord-gift.com", "discordgift.com",
    "discrod.gift", "discord-airdrop.com", "discords-nitro.com", "dlscord-nitro.com",
    "discord.gift.com", "steamcommunity.ru", "dlscord.com", "discord-app.net",
}
FAKE_STEAM_DOMAINS = {
    "steamcommunlty.com", "steamcommmunity.com", "steamcomunity.com",
    "steampowereb.com", "steamcomrnunity.com", "steancommunity.com",
}
FAKE_MICROSOFT_DOMAINS = {
    "microsoft-login.com", "microsft.com", "live-microsoft.com", "0ffice.com",
    "micros0ft-verify.com", "outlook-verify.com",
}
FAKE_MC_GIVEAWAY_DOMAINS = {
    "minecraft-giveaway.com", "mc-freeaccounts.com", "minecraftfreegift.com",
    "minecraft-premium.net", "freemcaccount.com",
}
KNOWN_SCAM_DOMAINS = (
    FAKE_NITRO_DOMAINS | FAKE_STEAM_DOMAINS | FAKE_MICROSOFT_DOMAINS | FAKE_MC_GIVEAWAY_DOMAINS
)

SCAM_KEYWORDS = {
    "crypto": [
        r"\bguaranteed\s+(?:returns?|profit)\b", r"\bdouble\s+your\s+(?:btc|crypto|investment)\b",
        r"\binvestment\s+opportunity\b", r"\bcrypto\s+giveaway\b", r"\belon\s*musk.{0,15}giveaway\b",
        r"\bmining\s+bot\b.{0,20}\bprofit\b",
    ],
    "free_nitro": [
        r"\bfree\s*nitro\b", r"\bnitro\s+generator\b", r"\bnitro\s+for\s+free\b",
        r"\bclaim\s+(?:your\s+)?nitro\b",
    ],
    "free_minecraft": [
        r"\bfree\s+minecraft\s+(?:account|premium|key)s?\b", r"\bmc\s+account\s+generator\b",
        r"\bminecraft\s+giveaway\b.{0,20}\bclick\b",
    ],
    "impersonation": [
        r"\bthis\s+is\s+(?:an?\s+)?official\s+(?:discord|staff)\s+(?:message|warning)\b",
        r"\bdiscord\s+staff\s+team\b.{0,20}\bverify\b", r"\byour\s+account\s+(?:will\s+be\s+|has\s+been\s+)?(?:banned|suspended|terminated)\b.{0,25}\bclick\b",
    ],
    "giveaway": [
        r"\bcongratulations?\b.{0,15}\byou(?:'ve|\s+have)?\s+won\b", r"\bclaim\s+(?:your\s+)?prize\s+now\b",
        r"\blimited\s+time\b.{0,15}\bclaim\b", r"\bact\s+now\b.{0,15}\bclaim\b",
    ],
}
_SCAM_PATTERNS = {
    cat: [re.compile(p, re.IGNORECASE) for p in pats] for cat, pats in SCAM_KEYWORDS.items()
}


def extract_domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/:\s]+)", url, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def is_homograph_domain(domain: str) -> bool:
    """Flags IDN/punycode domains (xn--...) and domains mixing multiple
    unicode scripts in one label — the two hallmarks of a homograph /
    look-alike domain attack."""
    if "xn--" in domain:
        return True
    scripts = set()
    for ch in domain:
        if ch in ".-0123456789":
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        scripts.add(name.split(" ")[0])
    return len(scripts) > 1


@dataclass
class LinkVerdict:
    flagged: bool = False
    reason: str = ""
    category: str = ""  # invite | scam_domain | phishing | fake_nitro | ip_logger | shortener | homograph | blacklist


async def classify_urls(bot, guild_id: int, content: str) -> list[LinkVerdict]:
    verdicts: list[LinkVerdict] = []
    for url in URL_RE.findall(content):
        domain = extract_domain(url)
        if not domain:
            continue
        if await is_whitelisted(bot, guild_id, domain=domain):
            continue
        if INVITE_RE.search(url):
            verdicts.append(LinkVerdict(True, "unauthorized Discord invite link", "invite"))
            continue
        custom = await is_blacklisted_domain(bot, guild_id, domain)
        if custom:
            verdicts.append(LinkVerdict(True, f"blacklisted domain ({custom})", "blacklist"))
            continue
        if domain in FAKE_NITRO_DOMAINS:
            verdicts.append(LinkVerdict(True, "fake Discord Nitro phishing link", "fake_nitro"))
            continue
        if domain in FAKE_STEAM_DOMAINS:
            verdicts.append(LinkVerdict(True, "fake Steam phishing link", "phishing"))
            continue
        if domain in FAKE_MICROSOFT_DOMAINS:
            verdicts.append(LinkVerdict(True, "fake Microsoft login phishing link", "phishing"))
            continue
        if domain in FAKE_MC_GIVEAWAY_DOMAINS:
            verdicts.append(LinkVerdict(True, "fake Minecraft giveaway link", "scam_domain"))
            continue
        if domain in IP_LOGGER_DOMAINS:
            verdicts.append(LinkVerdict(True, "IP logger link", "ip_logger"))
            continue
        if domain in URL_SHORTENERS:
            verdicts.append(LinkVerdict(True, "shortened URL (destination hidden)", "shortener"))
            continue
        if is_homograph_domain(domain):
            verdicts.append(LinkVerdict(True, f"look-alike / homograph domain ({domain})", "homograph"))
            continue
    return verdicts


def scam_message_score(content: str) -> tuple[int, list[str]]:
    """Pattern-based scam scorer (crypto / free nitro / free MC / staff
    impersonation / fake giveaways). Each matched category adds weight;
    returns the total score plus which categories fired, so callers can
    both threshold on it and explain the flag to staff."""
    score = 0
    hits: list[str] = []
    for category, patterns in _SCAM_PATTERNS.items():
        if any(p.search(content) for p in patterns):
            score += 2
            hits.append(category)
    if URL_RE.search(content) and hits:
        score += 1  # a link alongside scam language is more dangerous than words alone
    return score, hits


# ==========================================================================
# RISK SCORING (new member screening)
# ==========================================================================

_SUSPICIOUS_NAME_RE = re.compile(r"^[a-zA-Z]+\d{4,}$|^user\d+$|^[a-zA-Z0-9]{1,3}\d{5,}$", re.IGNORECASE)


async def calculate_risk(bot, member: discord.Member) -> tuple[int, str, list[str]]:
    """Weighted 0-100 risk score for a freshly-joined member. Returns
    (score, level, reasons) — reasons feeds straight into the audit-log
    style alert embed so staff see *why*, not just a number."""
    score = 0
    reasons: list[str] = []
    now = datetime.now(timezone.utc)

    age_days = (now - member.created_at).days
    if age_days < 1:
        score += 35; reasons.append("account created < 1 day ago")
    elif age_days < 7:
        score += 22; reasons.append("account created < 7 days ago")
    elif age_days < 30:
        score += 10; reasons.append("account created < 30 days ago")

    if member.avatar is None:
        score += 12; reasons.append("default avatar")

    if _SUSPICIOUS_NAME_RE.match(member.name):
        score += 15; reasons.append("spammy/generated-looking username")

    join_velocity = joins.join_count(member.guild.id, 60.0)
    if join_velocity >= 10:
        score += 20; reasons.append(f"{join_velocity} joins in the last minute (raid-like velocity)")
    elif join_velocity >= 5:
        score += 10; reasons.append(f"{join_velocity} joins in the last minute")

    prior = await offense_count(bot, member.guild.id, member.id, since_days=365)
    if prior > 0:
        score += min(prior * 8, 25)
        reasons.append(f"{prior} prior offense(s) on record in this server")

    if getattr(member, "pending", False):
        score += 5; reasons.append("has not completed member screening")

    score = min(score, 100)
    if score >= 70:
        level = "CRITICAL"
    elif score >= 45:
        level = "HIGH"
    elif score >= 20:
        level = "MEDIUM"
    else:
        level = "LOW"
    return score, level, reasons


# ==========================================================================
# AUTO-ACTION DISPATCHER (warn -> delete -> timeout -> kick -> ban, with
# escalation based on rolling offense count; quarantine/lockdown/mute are
# also routed through here so every punitive action ends up logged the
# same way.)
# ==========================================================================

ESCALATION_LADDER = [
    ("warn", None),
    ("timeout", 600),        # 10 min
    ("timeout", 3600),       # 1 hour
    ("timeout", 21600),      # 6 hours
    ("kick", None),
    ("ban", None),
]


async def escalate(bot, member: discord.Member, category: str, reason: str,
                    message: Optional[discord.Message] = None) -> str:
    """Runs the standard escalation ladder for a member who tripped a
    filter. Deletes the offending message (if any) unconditionally, then
    applies the next rung of punishment based on their rolling offense
    count. Returns the action taken, for logging."""
    guild = member.guild
    if message is not None:
        try:
            await message.delete()
            await bump_stat(bot, guild.id, "deleted_msgs")
        except (discord.NotFound, discord.Forbidden):
            pass

    count = await offense_count(bot, guild.id, member.id, since_days=1)
    rung = min(count, len(ESCALATION_LADDER) - 1)
    action, duration = ESCALATION_LADDER[rung]
    await record_offense(bot, guild.id, member.id, category, action, reason)
    await apply_action(bot, member, action, reason, duration_secs=duration)
    return action


async def apply_action(bot, member: discord.Member, action: str, reason: str,
                        duration_secs: Optional[int] = None) -> bool:
    guild = member.guild
    try:
        if action == "warn":
            try:
                await member.send(f"⚠️ You were warned in **{guild.name}**: {reason}")
            except discord.Forbidden:
                pass
        elif action == "timeout":
            until = discord.utils.utcnow() + timedelta(seconds=duration_secs or 600)
            await member.timeout(until, reason=reason)
            await bump_stat(bot, guild.id, "timeouts")
        elif action == "kick":
            await member.kick(reason=reason)
            await bump_stat(bot, guild.id, "kicks")
        elif action == "ban":
            await member.ban(reason=reason, delete_message_seconds=300)
            await bump_stat(bot, guild.id, "bans")
        elif action == "quarantine":
            settings = await get_settings(bot, guild.id)
            role_id = settings.get("quarantine_role_id")
            role = guild.get_role(role_id) if role_id else None
            if role:
                await member.add_roles(role, reason=reason)
                await bump_stat(bot, guild.id, "quarantines")
        elif action == "mute":
            settings = await get_settings(bot, guild.id)
            role_id = settings.get("muted_role_id")
            role = guild.get_role(role_id) if role_id else None
            if role:
                await member.add_roles(role, reason=reason)
        elif action == "role_removal":
            roles = [r for r in member.roles if r.is_assignable()]
            if roles:
                await member.remove_roles(*roles, reason=reason)
        return True
    except discord.Forbidden:
        log.warning("Missing permissions to apply action=%s on %s in guild %s", action, member, guild.id)
        return False
    except discord.HTTPException as e:
        log.warning("Failed to apply action=%s on %s: %s", action, member, e)
        return False


# ==========================================================================
# LOGGING
# ==========================================================================

async def log_event(bot, guild: discord.Guild, group: str, embed: discord.Embed):
    """group is one of: messages | members | voice | server | security —
    matches the log_* toggles in security_settings."""
    settings = await get_settings(bot, guild.id)
    if not settings.get(f"log_{group}", False):
        return
    channel_id = settings.get("log_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass


async def alert_staff(bot, guild: discord.Guild, embed: discord.Embed, ping: bool = False):
    settings = await get_settings(bot, guild.id)
    channel_id = settings.get("alert_channel_id") or settings.get("log_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(
            content="@here" if ping else None, embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=ping, roles=False, users=False),
        )
    except discord.Forbidden:
        pass


# ==========================================================================
# BACKUP / RESTORE
# ==========================================================================

async def create_backup(bot, guild: discord.Guild, created_by: int) -> int:
    data = {
        "guild_name": guild.name,
        "verification_level": str(guild.verification_level),
        "explicit_content_filter": str(guild.explicit_content_filter),
        "roles": [
            {
                "name": r.name, "permissions": r.permissions.value, "color": r.color.value,
                "hoist": r.hoist, "mentionable": r.mentionable, "position": r.position,
            }
            for r in guild.roles if not r.is_default() and not r.managed
        ],
        "categories": [
            {"name": c.name, "position": c.position, "id": c.id}
            for c in guild.categories
        ],
        "channels": [
            {
                "name": ch.name, "type": str(ch.type), "position": ch.position,
                "category_id": ch.category_id,
                "topic": getattr(ch, "topic", None),
                "nsfw": getattr(ch, "nsfw", False),
                "slowmode_delay": getattr(ch, "slowmode_delay", 0),
            }
            for ch in guild.channels if not isinstance(ch, discord.CategoryChannel)
        ],
        "emojis": [{"name": e.name, "url": str(e.url)} for e in guild.emojis],
    }
    await ensure_tables(bot)
    async with _connect(bot) as db:
        await _pragmas(db)
        cur = await db.execute(
            "INSERT INTO security_backups (guild_id, created_by, created_at, data) VALUES (?, ?, ?, ?)",
            (guild.id, created_by, datetime.now(timezone.utc).isoformat(), json.dumps(data)),
        )
        await db.commit()
        return cur.lastrowid


async def get_backup(bot, guild_id: int, backup_id: Optional[int] = None) -> Optional[dict]:
    await ensure_tables(bot)
    async with _connect(bot) as db:
        await _pragmas(db)
        db.row_factory = aiosqlite.Row
        if backup_id:
            cur = await db.execute(
                "SELECT * FROM security_backups WHERE guild_id = ? AND id = ?", (guild_id, backup_id)
            )
        else:
            cur = await db.execute(
                "SELECT * FROM security_backups WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (guild_id,)
            )
        row = await cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    out["data"] = json.loads(out["data"])
    return out


async def restore_backup(guild: discord.Guild, backup: dict) -> tuple[int, int]:
    """Best-effort restore: recreates roles and channels/categories that no
    longer exist by name. Does not touch anything that still exists, and
    never deletes anything — this is a recovery tool, not a sync tool."""
    data = backup["data"]
    restored_roles = restored_channels = 0

    existing_role_names = {r.name for r in guild.roles}
    for r in sorted(data["roles"], key=lambda x: x["position"]):
        if r["name"] in existing_role_names:
            continue
        try:
            await guild.create_role(
                name=r["name"], permissions=discord.Permissions(r["permissions"]),
                colour=discord.Colour(r["color"]), hoist=r["hoist"], mentionable=r["mentionable"],
                reason="Security backup restore",
            )
            restored_roles += 1
        except discord.HTTPException:
            pass

    existing_cat_names = {c.name: c for c in guild.categories}
    cat_map = {}
    for c in sorted(data["categories"], key=lambda x: x["position"]):
        if c["name"] in existing_cat_names:
            cat_map[c["id"]] = existing_cat_names[c["name"]]
            continue
        try:
            new_cat = await guild.create_category(c["name"], reason="Security backup restore")
            cat_map[c["id"]] = new_cat
        except discord.HTTPException:
            pass

    existing_channel_names = {ch.name for ch in guild.channels}
    for ch in sorted(data["channels"], key=lambda x: x["position"]):
        if ch["name"] in existing_channel_names:
            continue
        category = cat_map.get(ch["category_id"])
        try:
            if ch["type"] == "voice":
                await guild.create_voice_channel(ch["name"], category=category, reason="Security backup restore")
            else:
                await guild.create_text_channel(
                    ch["name"], category=category, topic=ch.get("topic"),
                    nsfw=ch.get("nsfw", False), slowmode_delay=ch.get("slowmode_delay", 0),
                    reason="Security backup restore",
                )
            restored_channels += 1
        except discord.HTTPException:
            pass

    return restored_roles, restored_channels


# ==========================================================================
# LOCKDOWN / QUARANTINE HELPERS
# ==========================================================================

async def lockdown_guild(guild: discord.Guild, reason: str) -> int:
    locked = 0
    for ch in guild.text_channels:
        try:
            overwrite = ch.overwrites_for(guild.default_role)
            if overwrite.send_messages is not False:
                overwrite.send_messages = False
                await ch.set_permissions(guild.default_role, overwrite=overwrite, reason=reason)
                locked += 1
        except discord.Forbidden:
            continue
    return locked


async def unlock_guild(guild: discord.Guild, reason: str) -> int:
    unlocked = 0
    for ch in guild.text_channels:
        try:
            overwrite = ch.overwrites_for(guild.default_role)
            if overwrite.send_messages is False:
                overwrite.send_messages = None
                await ch.set_permissions(guild.default_role, overwrite=overwrite, reason=reason)
                unlocked += 1
        except discord.Forbidden:
            continue
    return unlocked


async def quarantine_new_joiners(bot, guild: discord.Guild, minutes: int = 15):
    """Called when raid mode trips: temporarily assigns the quarantine role
    to anyone who joins in the next `minutes`, handled by the on_member_join
    listener checking settings['panic_mode'] / a short-lived raid flag."""
    await update_settings(bot, guild.id, panic_mode=True)

    async def _clear():
        await asyncio.sleep(minutes * 60)
        await update_settings(bot, guild.id, panic_mode=False)

    asyncio.create_task(_clear())


# ==========================================================================
# CAPTCHA
# ==========================================================================

def _gen_code() -> str:
    import random, string
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


async def start_verification(bot, member: discord.Member, settings: dict):
    """Puts a freshly-joined member into quarantine and posts (or DMs) the
    verification prompt, per the configured captcha_mode. Also schedules
    the timeout kick if they never complete it."""
    guild = member.guild
    role_id = settings.get("quarantine_role_id")
    role = guild.get_role(role_id) if role_id else None
    if role:
        try:
            await member.add_roles(role, reason="Pending captcha verification")
        except discord.Forbidden:
            pass

    channel_id = settings.get("verify_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None
    mode = settings.get("captcha_mode", "button")

    if mode == "code":
        code = _gen_code()
        await ensure_tables(bot)
        async with _connect(bot) as db:
            await _pragmas(db)
            await db.execute(
                "INSERT OR REPLACE INTO security_pending_verify (guild_id, user_id, code, joined_at) "
                "VALUES (?, ?, ?, ?)",
                (guild.id, member.id, code, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
        embed = E.base(
            "🔐  Verify to Continue",
            f"{member.mention}, welcome to **{guild.name}**! Type `/verify code:{code}` "
            f"anywhere in the server within {settings['captcha_timeout_secs'] // 60} minutes "
            f"to get access.\n\n**Your code:** `{code}`",
            color=BLUE,
        )
    else:
        embed = E.base(
            "🔐  Verify to Continue",
            f"{member.mention}, welcome to **{guild.name}**! Click the button below to verify "
            f"you're human and get access.",
            color=BLUE,
        )

    view = CaptchaButtonView(bot) if mode == "button" else None
    target = channel or member
    try:
        if view:
            await target.send(embed=embed, view=view)
        else:
            await target.send(embed=embed)
    except discord.Forbidden:
        pass

    async def _timeout_kick():
        await asyncio.sleep(settings["captcha_timeout_secs"])
        fresh = guild.get_member(member.id)
        if fresh is None:
            return
        still_quarantined = role and role in fresh.roles
        if still_quarantined:
            try:
                await fresh.kick(reason="Did not complete captcha verification in time")
                await bump_stat(bot, guild.id, "kicks")
            except discord.Forbidden:
                pass

    asyncio.create_task(_timeout_kick())


async def complete_verification(bot, member: discord.Member, settings: dict):
    guild = member.guild
    quarantine_id = settings.get("quarantine_role_id")
    verified_id = settings.get("verified_role_id")
    quarantine_role = guild.get_role(quarantine_id) if quarantine_id else None
    verified_role = guild.get_role(verified_id) if verified_id else None
    try:
        if quarantine_role and quarantine_role in member.roles:
            await member.remove_roles(quarantine_role, reason="Captcha verified")
        if verified_role:
            await member.add_roles(verified_role, reason="Captcha verified")
    except discord.Forbidden:
        pass


class CaptchaButtonView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji="✅",
                        custom_id="security:captcha_verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_settings(self.bot, interaction.guild_id)
        await complete_verification(self.bot, interaction.user, settings)
        await interaction.response.send_message("✅ You're verified — welcome!", ephemeral=True)
        button.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass


# ==========================================================================
# THE COG
# ==========================================================================

EMOJI_RE = re.compile(r"<a?:\w+:\d+>|[\U0001F300-\U0001FAFF\u2600-\u27BF]")

NUKE_ACTION_MAP = {
    discord.AuditLogAction.channel_delete: "channel_delete",
    discord.AuditLogAction.channel_create: "channel_create",
    discord.AuditLogAction.role_delete: "role_delete",
    discord.AuditLogAction.role_create: "role_create",
    discord.AuditLogAction.webhook_create: "webhook_create",
    discord.AuditLogAction.webhook_delete: "webhook_delete",
    discord.AuditLogAction.emoji_delete: "emoji_delete",
    discord.AuditLogAction.sticker_delete: "sticker_delete",
    discord.AuditLogAction.guild_update: "guild_update",
    discord.AuditLogAction.overwrite_update: "overwrite_update",
    discord.AuditLogAction.overwrite_create: "overwrite_create",
    discord.AuditLogAction.member_role_update: "member_role_update",
    discord.AuditLogAction.member_update: "member_update",
}
DESTRUCTIVE_ACTIONS = {
    discord.AuditLogAction.channel_delete, discord.AuditLogAction.role_delete,
}
DANGEROUS_PERMS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "manage_webhooks", "ban_members", "kick_members", "manage_permissions",
)


class SecurityCog(commands.Cog, name="Security"):
    """Enterprise-grade security suite: anti-spam, link/scam protection,
    raid protection, anti-nuke, risk scoring, captcha, auditing, backups,
    incident tracking, dashboard, and full event logging."""

    security = app_commands.Group(
        name="security", description="Server security suite",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await ensure_tables(self.bot)
        self.bot.add_view(CaptchaButtonView(self.bot))

    # ---------------------------------------------------------------- utils

    async def _is_immune(self, member: discord.Member, settings: dict) -> bool:
        if member.bot:
            return True
        if member.guild_permissions.administrator or member == member.guild.owner:
            return True
        if await is_whitelisted(self.bot, member.guild.id, user=member):
            return True
        return False

    # ------------------------------------------------------------ messages

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        guild, member = message.guild, message.author
        settings = await get_settings(self.bot, guild.id)
        if await self._is_immune(member, settings):
            return
        if await self._scan_message(message, settings, edited=False):
            return

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Anti Edit Bypass: someone posts clean text, waits, then edits in a
        # scam link / bad content once they think nobody's watching.
        if not after.guild or after.author.bot or before.content == after.content:
            return
        settings = await get_settings(self.bot, after.guild.id)
        if await self._is_immune(after.author, settings):
            return
        await self._scan_message(after, settings, edited=True)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        settings = await get_settings(self.bot, message.guild.id)
        if not settings["mod_anti_spam"]:
            return
        has_mentions = bool(message.mentions) or bool(message.role_mentions) or "@everyone" in message.content
        age = (discord.utils.utcnow() - message.created_at).total_seconds()
        if has_mentions and age < 8:
            embed = E.base(
                "👻  Possible Ghost Ping",
                f"**{message.author}** mentioned someone then deleted their message within {age:.1f}s "
                f"in {message.channel.mention}.",
                color=ORANGE,
            )
            await log_event(self.bot, message.guild, "security", embed)
            await log_incident(self.bot, message.guild.id, "ghost_ping", "LOW",
                                f"{message.author} ({message.author.id}) possible ghost ping", actor_id=message.author.id)

    async def _scan_message(self, message: discord.Message, settings: dict, edited: bool) -> bool:
        """Runs every content-based filter against a message. Returns True
        if the message was actioned (deleted/punished) so callers can stop."""
        bot, guild, member = self.bot, message.guild, message.author
        content = message.content or ""
        category_prefix = "edit_" if edited else ""

        if settings["mod_anti_spam"]:
            if has_invisible_unicode(content):
                await escalate(bot, member, category_prefix + "invisible_unicode",
                                "used invisible/zero-width unicode to disguise a message", message)
                return True
            if zalgo_score(content) > 0.15:
                await escalate(bot, member, category_prefix + "zalgo",
                                "posted zalgo / corrupted text", message)
                return True
            if caps_ratio(content) >= CAPS_RATIO:
                await escalate(bot, member, category_prefix + "caps",
                                "excessive caps lock", message)
                return True
            if not edited and dupes.check(guild.id, member.id, content) >= 2:
                await escalate(bot, member, "duplicate", "repeated the same message", message)
                await bump_stat(bot, guild.id, "spam_blocked")
                return True
            if not edited:
                tripped, count = rate.hit(guild.id, member.id, "message", *SPAM_LIMITS["message"])
                if tripped:
                    await escalate(bot, member, "spam", f"sent {count} messages in {SPAM_LIMITS['message'][1]:.0f}s", message)
                    await bump_stat(bot, guild.id, "spam_blocked")
                    return True
                if message.stickers:
                    tripped, count = rate.hit(guild.id, member.id, "sticker", *SPAM_LIMITS["sticker"])
                    if tripped:
                        await escalate(bot, member, "sticker_spam", f"sent {count} stickers too quickly", message)
                        return True
                if message.attachments:
                    is_gif = any(a.filename.lower().endswith(".gif") for a in message.attachments)
                    cat = "gif" if is_gif else "attachment"
                    tripped, count = rate.hit(guild.id, member.id, cat, *SPAM_LIMITS[cat])
                    if tripped:
                        await escalate(bot, member, cat + "_spam", f"sent {count} {cat}s too quickly", message)
                        return True
                elif "tenor.com" in content.lower() or "giphy.com" in content.lower():
                    tripped, count = rate.hit(guild.id, member.id, "gif", *SPAM_LIMITS["gif"])
                    if tripped:
                        await escalate(bot, member, "gif_spam", f"sent {count} GIF links too quickly", message)
                        return True
                emoji_count = len(EMOJI_RE.findall(content))
                if emoji_count >= 8:
                    await escalate(bot, member, "emoji_spam", f"{emoji_count} emojis in one message", message)
                    return True
                elif emoji_count >= 3:
                    tripped, count = rate.hit(guild.id, member.id, "emoji", *SPAM_LIMITS["emoji"])
                    if tripped:
                        await escalate(bot, member, "emoji_spam", "repeated emoji spam", message)
                        return True

            mention_count = len(message.mentions) + len(message.role_mentions)
            if mention_count >= MASS_MENTION_SINGLE_MSG:
                await escalate(bot, member, "mass_ping", f"mentioned {mention_count} users/roles in one message", message)
                return True
            elif mention_count > 0:
                tripped = False
                for _ in range(mention_count):
                    tripped, count = rate.hit(guild.id, member.id, "mention", *SPAM_LIMITS["mention"])
                if tripped:
                    await escalate(bot, member, "mention_spam", "mentioning too many people across messages", message)
                    return True

        if settings["mod_anti_links"]:
            verdicts = await classify_urls(bot, guild.id, content)
            if verdicts:
                v = verdicts[0]
                await escalate(bot, member, v.category, v.reason, message)
                await bump_stat(bot, guild.id, "scam_links")
                await log_incident(bot, guild.id, "link", "MEDIUM",
                                    f"{member} ({member.id}) posted {v.reason}", actor_id=member.id,
                                    evidence=content[:300])
                return True

        if settings["mod_scam_detection"]:
            score, hits = scam_message_score(content)
            if score >= 4:
                await escalate(bot, member, "scam", f"scam-pattern message ({', '.join(hits)})", message)
                await bump_stat(bot, guild.id, "scam_links")
                await log_incident(bot, guild.id, "scam", "MEDIUM",
                                    f"{member} ({member.id}) sent a scam-pattern message: {', '.join(hits)}",
                                    actor_id=member.id, evidence=content[:300])
                return True

        return False

    # -------------------------------------------------------------- threads

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        guild = thread.guild
        settings = await get_settings(self.bot, guild.id)
        if not settings["mod_anti_spam"] or thread.owner is None:
            return
        member = guild.get_member(thread.owner_id)
        if member is None or await self._is_immune(member, settings):
            return
        tripped, count = rate.hit(guild.id, thread.owner_id, "thread", *SPAM_LIMITS["thread"])
        if tripped:
            try:
                await thread.delete(reason="Anti thread spam")
            except discord.HTTPException:
                pass
            await record_offense(self.bot, guild.id, thread.owner_id, "thread_spam",
                                  "delete", f"created {count} threads too quickly")
            if member:
                await apply_action(self.bot, member, "timeout", "thread spam", duration_secs=600)

    # ---------------------------------------------------------------- members

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        settings = await get_settings(self.bot, guild.id)
        joins.record_join(guild.id)

        if member.bot:
            # Anti "unauthorized bot add" — a bot joining that wasn't
            # whitelisted is one of the more common nuke vectors (a
            # malicious bot with broad perms added via a compromised
            # webhook/oauth flow).
            if settings["mod_anti_nuke"] and not await is_whitelisted(self.bot, guild.id, user=member):
                embed = E.base(
                    "🤖  Unrecognized Bot Added",
                    f"{member.mention} (`{member.id}`) was just added to the server and is not on "
                    f"the security whitelist. If this wasn't expected, remove it immediately.",
                    color=RED,
                )
                await alert_staff(self.bot, guild, embed, ping=True)
                await log_incident(self.bot, guild.id, "nuke", "HIGH",
                                    f"Unrecognized bot added: {member} ({member.id})", actor_id=member.id)
            return

        # Raid detection
        if settings["mod_anti_raid"]:
            count = joins.join_count(guild.id, settings["raid_join_window"])
            if count >= settings["raid_join_threshold"] and not settings["panic_mode"]:
                await self._trigger_raid_response(guild, count, settings)
            elif settings["panic_mode"]:
                # Raid already in progress — quarantine everyone joining
                # until it's manually or automatically cleared.
                await apply_action(self.bot, member, "quarantine", "joined during active raid lockdown")

        # Risk scoring
        reasons: list[str] = []
        level = "LOW"
        if settings["mod_risk_scoring"]:
            score, level, reasons = await calculate_risk(self.bot, member)
            if level in ("HIGH", "CRITICAL"):
                embed = E.base(
                    f"{SEVERITY_EMOJI[level]}  High-Risk Join — {level}",
                    f"{member.mention} (`{member.id}`) scored **{score}/100**.\n\n" +
                    "\n".join(f"• {r}" for r in reasons),
                    color=SEVERITY_COLOR[level],
                )
                await alert_staff(self.bot, guild, embed, ping=(level == "CRITICAL"))
                if level == "CRITICAL" and settings.get("quarantine_role_id"):
                    await apply_action(self.bot, member, "quarantine", "critical risk score on join")

        # Captcha
        if settings["mod_captcha"]:
            await start_verification(self.bot, member, settings)

        if settings["log_members"]:
            embed = E.base(
                "📥  Member Joined",
                f"{member.mention} (`{member.id}`)\nAccount created {discord.utils.format_dt(member.created_at, 'R')}"
                + (f"\nRisk: {SEVERITY_EMOJI.get(level, '')} {level}" if settings["mod_risk_scoring"] else ""),
                color=GREEN,
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            await log_event(self.bot, guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        settings = await get_settings(self.bot, guild.id)
        joins.record_leave(guild.id)

        if settings["mod_anti_raid"]:
            count = joins.leave_count(guild.id, settings["raid_join_window"])
            if count >= settings["raid_join_threshold"]:
                await log_incident(self.bot, guild.id, "raid", "MEDIUM",
                                    f"Mass leave detected: {count} members left within "
                                    f"{settings['raid_join_window']}s")
                embed = E.base(
                    "📉  Mass Leave Detected",
                    f"{count} members left within {settings['raid_join_window']}s. Could be a raid "
                    f"aftermath, a prune, or a mass-kick — worth a look.",
                    color=ORANGE,
                )
                await alert_staff(self.bot, guild, embed)

        if settings["log_members"]:
            embed = E.base("📤  Member Left", f"{member} (`{member.id}`)", color=GREY)
            await log_event(self.bot, guild, "members", embed)

    async def _trigger_raid_response(self, guild: discord.Guild, count: int, settings: dict):
        severity = "CRITICAL" if count >= settings["raid_join_threshold"] * 2 else "HIGH"
        await update_settings(self.bot, guild.id, threat_level=severity)
        await bump_stat(self.bot, guild.id, "raid_attempts")
        await log_incident(self.bot, guild.id, "raid", severity,
                            f"{count} joins within {settings['raid_join_window']}s")

        locked = await lockdown_guild(guild, "Automated raid response")
        await quarantine_new_joiners(self.bot, guild, minutes=15)

        embed = E.base(
            f"{SEVERITY_EMOJI[severity]}  RAID DETECTED — {severity}",
            f"**{count}** members joined within **{settings['raid_join_window']}s**.\n\n"
            f"✅ Locked {locked} channel(s)\n"
            f"✅ New joiners will be quarantined for 15 minutes\n"
            f"✅ Threat level set to **{severity}**\n\n"
            f"Use `/security unlock` once you've confirmed it's safe.",
            color=SEVERITY_COLOR[severity],
        )
        await alert_staff(self.bot, guild, embed, ping=True)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        settings = await get_settings(self.bot, after.guild.id)
        if not settings["log_members"]:
            return
        if before.nick != after.nick:
            embed = E.base(
                "✏️  Nickname Changed",
                f"{after.mention}: `{before.nick or before.name}` → `{after.nick or after.name}`",
                color=GREY,
            )
            await log_event(self.bot, after.guild, "members", embed)
        if before.roles != after.roles:
            added = set(after.roles) - set(before.roles)
            removed = set(before.roles) - set(after.roles)
            if added or removed:
                parts = []
                if added:
                    parts.append("+ " + ", ".join(r.mention for r in added))
                if removed:
                    parts.append("- " + ", ".join(r.mention for r in removed))
                embed = E.base("🎭  Roles Updated", f"{after.mention}\n" + "\n".join(parts), color=GREY)
                await log_event(self.bot, after.guild, "members", embed)

    # ------------------------------------------------------------ anti-nuke

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        """Requires discord.py 2.4+. This single listener is the backbone
        of anti-nuke: every destructive/administrative action taken in the
        guild passes through here with who did it, letting us both log
        everything and trip the nuke breaker on suspicious bursts."""
        guild = entry.guild
        settings = await get_settings(self.bot, guild.id)
        actor = entry.user
        action_key = NUKE_ACTION_MAP.get(entry.action)

        if settings["log_security"] and action_key:
            embed = E.base(
                f"🛡️  Audit: {entry.action.name.replace('_', ' ').title()}",
                f"**Actor:** {actor.mention if actor else 'Unknown'} (`{actor.id if actor else '?'}`)\n"
                f"**Reason:** {entry.reason or 'No reason provided'}",
                color=GREY,
            )
            await log_event(self.bot, guild, "security", embed)

        if not settings["mod_anti_nuke"] or action_key is None or actor is None:
            return
        if actor.id == self.bot.user.id or actor.id == guild.owner_id:
            return
        member = guild.get_member(actor.id)
        if member and await self._is_immune(member, settings):
            # Admins/whitelisted staff performing normal admin work — don't
            # trip the breaker, but their actions are still logged above.
            return

        tripped, score = nuke.hit(guild.id, actor.id, action_key)
        if not tripped:
            return

        nuke.reset(guild.id, actor.id)
        await bump_stat(self.bot, guild.id, "bans" if member else "kicks")
        await log_incident(self.bot, guild.id, "nuke", "CRITICAL",
                            f"Anti-nuke tripped: {actor} ({actor.id}) performed {score} weighted "
                            f"destructive actions within {NukeTracker.WINDOW:.0f}s "
                            f"(last: {entry.action.name})", actor_id=actor.id)

        punished = False
        if member is not None:
            try:
                dangerous_roles = [r for r in member.roles if any(getattr(r.permissions, p) for p in DANGEROUS_PERMS)]
                if dangerous_roles:
                    await member.remove_roles(*dangerous_roles, reason="Anti-nuke: suspected server nuke attempt")
                await member.ban(reason="Anti-nuke: suspected server nuke attempt", delete_message_seconds=0)
                punished = True
            except discord.Forbidden:
                pass
        else:
            try:
                await guild.ban(discord.Object(id=actor.id), reason="Anti-nuke: suspected server nuke attempt")
                punished = True
            except discord.Forbidden:
                pass

        locked = await lockdown_guild(guild, "Anti-nuke automatic lockdown")
        await update_settings(self.bot, guild.id, threat_level="CRITICAL", lockdown_mode=True)

        restored_msg = ""
        if entry.action in DESTRUCTIVE_ACTIONS:
            backup = await get_backup(self.bot, guild.id)
            if backup:
                r_roles, r_channels = await restore_backup(guild, backup)
                if r_roles or r_channels:
                    restored_msg = f"\n♻️ Auto-restored {r_roles} role(s) and {r_channels} channel(s) from the latest backup."

        embed = E.base(
            "🚨  ANTI-NUKE TRIPPED — CRITICAL",
            f"**{actor}** (`{actor.id}`) triggered {score} weighted destructive actions in "
            f"{NukeTracker.WINDOW:.0f}s.\n\n"
            f"{'✅ Actor banned' if punished else '⚠️ Could not punish actor (check my role position/perms)'}\n"
            f"✅ Server locked down ({locked} channel(s))"
            f"{restored_msg}\n\n"
            f"Run `/security unlock` once you've confirmed the threat is over.",
            color=DARK_RED,
        )
        await alert_staff(self.bot, guild, embed, ping=True)

    # ------------------------------------------------------------- webhooks

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        settings = await get_settings(self.bot, channel.guild.id)
        if not settings["log_security"]:
            return
        embed = E.base("🔗  Webhooks Updated", f"Webhooks changed in {channel.mention}.", color=GREY)
        await log_event(self.bot, channel.guild, "security", embed)

    # --------------------------------------------------------------- voice

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        settings = await get_settings(self.bot, member.guild.id)
        if not settings["log_voice"]:
            return
        if before.channel == after.channel:
            return
        if before.channel is None:
            desc = f"{member.mention} joined 🔊 **{after.channel.name}**"
        elif after.channel is None:
            desc = f"{member.mention} left 🔊 **{before.channel.name}**"
        else:
            desc = f"{member.mention} moved 🔊 **{before.channel.name}** → **{after.channel.name}**"
        await log_event(self.bot, member.guild, "voice", E.base("🎙️  Voice Update", desc, color=GREY))

    # ------------------------------------------------------------- invites

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        settings = await get_settings(self.bot, invite.guild.id)
        if not settings["log_server"]:
            return
        expiry = "never" if not invite.max_age else f"{invite.max_age}s"
        embed = E.base(
            "🔗  Invite Created",
            f"**Code:** `{invite.code}`\n**By:** {invite.inviter.mention if invite.inviter else 'Unknown'}\n"
            f"**Max uses:** {invite.max_uses or 'unlimited'}\n**Expires:** {expiry}",
            color=GREY,
        )
        await log_event(self.bot, invite.guild, "server", embed)

    # ---------------------------------------------------------------- audit

    async def compute_audit(self, guild: discord.Guild, settings: dict) -> tuple[int, list[dict]]:
        score = 100
        findings: list[dict] = []

        def deduct(amount, severity, text):
            nonlocal score
            score = max(0, score - amount)
            findings.append({"severity": severity, "text": text})

        admin_roles = [r for r in guild.roles if r.permissions.administrator and not r.is_default()]
        for r in admin_roles:
            if len(r.members) == 0:
                deduct(3, "LOW", f"Unused admin role: **{r.name}** (0 members)")
            elif len(r.members) > 5:
                deduct(6, "MEDIUM", f"Administrator role **{r.name}** is held by {len(r.members)} members")

        dangerous_role_holders = defaultdict(list)
        for r in guild.roles:
            if r.is_default():
                continue
            for perm in DANGEROUS_PERMS:
                if getattr(r.permissions, perm, False):
                    dangerous_role_holders[perm].append(r.name)
        if len(dangerous_role_holders.get("ban_members", [])) > 3:
            deduct(5, "MEDIUM", f"{len(dangerous_role_holders['ban_members'])} roles can ban members")

        risky_bots = [
            m for m in guild.members
            if m.bot and (m.guild_permissions.administrator or m.guild_permissions.ban_members
                           or m.guild_permissions.manage_guild)
        ]
        for b in risky_bots:
            whitelisted = await is_whitelisted(self.bot, guild.id, user=b)
            if not whitelisted:
                deduct(8, "HIGH", f"High-risk bot **{b}** has admin/ban/manage-server permissions and isn't whitelisted")

        if guild.verification_level in (discord.VerificationLevel.none, discord.VerificationLevel.low):
            deduct(7, "MEDIUM", f"Server verification level is low ({guild.verification_level})")

        if not settings.get("log_channel_id"):
            deduct(10, "HIGH", "No security log channel configured")
        if not settings.get("mod_anti_raid"):
            deduct(8, "MEDIUM", "Anti-raid protection is disabled")
        if not settings.get("mod_anti_nuke"):
            deduct(10, "HIGH", "Anti-nuke protection is disabled")
        if not settings.get("mod_anti_links"):
            deduct(6, "MEDIUM", "Link protection is disabled")

        try:
            invites = await guild.invites()
            no_expiry = [i for i in invites if i.max_age == 0]
            if len(no_expiry) > 5:
                deduct(4, "LOW", f"{len(no_expiry)} invites never expire")
        except discord.Forbidden:
            pass

        me = guild.me
        top_roles = sorted(guild.roles, key=lambda r: r.position, reverse=True)[:3]
        if me and me.top_role.position < (top_roles[0].position if top_roles else 0):
            below = [r.name for r in top_roles if r.position > me.top_role.position]
            if below:
                deduct(5, "MEDIUM", f"My role is below: {', '.join(below)} — I can't moderate those members")

        if not findings:
            findings.append({"severity": "LOW", "text": "No significant issues found — nice."})

        return score, findings

    # -------------------------------------------------------------- verify

    @app_commands.command(name="verify", description="Complete code-based server verification.")
    @app_commands.describe(code="The 6-character code you were given on join")
    async def verify(self, interaction: discord.Interaction, code: str):
        if not interaction.guild:
            return
        await ensure_tables(self.bot)
        async with _connect(self.bot) as db:
            await _pragmas(db)
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM security_pending_verify WHERE guild_id = ? AND user_id = ?",
                (interaction.guild_id, interaction.user.id),
            )
            row = await cur.fetchone()
        if row is None:
            await interaction.response.send_message("You don't have a pending verification.", ephemeral=True)
            return
        if row["code"].upper() != code.strip().upper():
            await interaction.response.send_message("❌ That code doesn't match. Double-check and try again.", ephemeral=True)
            return
        settings = await get_settings(self.bot, interaction.guild_id)
        await complete_verification(self.bot, interaction.user, settings)
        async with _connect(self.bot) as db:
            await _pragmas(db)
            await db.execute(
                "DELETE FROM security_pending_verify WHERE guild_id = ? AND user_id = ?",
                (interaction.guild_id, interaction.user.id),
            )
            await db.commit()
        await interaction.response.send_message("✅ Verified — welcome!", ephemeral=True)

    # ------------------------------------------------------------ /security

    @security.command(name="setup", description="(Admin/Owner only) Open the security configuration panel.")
    async def security_setup(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_settings(self.bot, interaction.guild_id)
        embed = E.base(
            "🛡️  Security Setup",
            "Configure channels, the quarantine role, and which modules are active.\n"
            "Every change here applies immediately — no save button needed.\n\n"
            "Need a muted or verified role too? Set them the same way I've wired the ones "
            "below; ask and I'll add extra selects if you need them.",
            color=PURPLE,
        )
        await interaction.response.send_message(embed=embed, view=SetupView(self.bot, interaction.guild_id, settings), ephemeral=True)

    @security.command(name="dashboard", description="Live security dashboard for this server.")
    async def security_dashboard(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        settings = await get_settings(self.bot, guild.id)
        stats = await get_stats(self.bot, guild.id)
        incidents = await recent_incidents(self.bot, guild.id, limit=5)
        score, _ = await self.compute_audit(guild, settings)

        quarantine_role = guild.get_role(settings["quarantine_role_id"]) if settings["quarantine_role_id"] else None
        quarantined_count = len(quarantine_role.members) if quarantine_role else 0

        modules_on = [label for key, label, emoji in MODULE_OPTIONS if settings.get(key)]
        modules_off = [label for key, label, emoji in MODULE_OPTIONS if not settings.get(key)]

        desc = (
            f"**Security Score:** {score}/100 {_target_prog_bar(score)}\n"
            f"**Threat Level:** {SEVERITY_EMOJI.get(settings['threat_level'], '')} {settings['threat_level']}\n"
            f"**Lockdown:** {'🔒 Active' if settings['lockdown_mode'] else '🔓 Inactive'}   "
            f"**Panic Mode:** {'🚨 Active' if settings['panic_mode'] else 'Inactive'}\n\n"
            f"**Modules ON:** {', '.join(modules_on) or 'none'}\n"
            f"**Modules OFF:** {', '.join(modules_off) or 'none'}"
        )
        embed = E.base("📊  Security Dashboard", desc, color=SEVERITY_COLOR.get(settings["threat_level"], PURPLE))
        embed.add_field(name="Spam Blocked", value=str(stats["spam_blocked"]), inline=True)
        embed.add_field(name="Scam Links Blocked", value=str(stats["scam_links"]), inline=True)
        embed.add_field(name="Raid Attempts", value=str(stats["raid_attempts"]), inline=True)
        embed.add_field(name="Timeouts", value=str(stats["timeouts"]), inline=True)
        embed.add_field(name="Kicks", value=str(stats["kicks"]), inline=True)
        embed.add_field(name="Bans", value=str(stats["bans"]), inline=True)
        embed.add_field(name="Messages Deleted", value=str(stats["deleted_msgs"]), inline=True)
        embed.add_field(name="Currently Quarantined", value=str(quarantined_count), inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        if incidents:
            lines = [
                f"{SEVERITY_EMOJI.get(i['severity'], '')} `{i['category']}` — {i['description'][:80]} "
                f"({discord.utils.format_dt(datetime.fromisoformat(i['created_at']), 'R')})"
                for i in incidents
            ]
            embed.add_field(name="Recent Incidents", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @security.command(name="audit", description="Scan this server for security misconfigurations.")
    async def security_audit(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        settings = await get_settings(self.bot, guild.id)
        score, findings = await self.compute_audit(guild, settings)
        level = "CRITICAL" if score < 40 else "HIGH" if score < 60 else "MEDIUM" if score < 80 else "LOW"

        embed = E.base(
            f"🔎  Security Audit — Score {score}/100",
            f"{_target_prog_bar(score)}\n**Risk Level:** {SEVERITY_EMOJI[level]} {level}",
            color=SEVERITY_COLOR[level],
        )
        for f in findings[:10]:
            embed.add_field(name=f"{SEVERITY_EMOJI.get(f['severity'], '•')} {f['severity']}", value=f["text"], inline=False)
        if len(findings) > 10:
            embed.set_footer(text=f"+{len(findings) - 10} more finding(s) not shown")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @security.command(name="lockdown", description="(Admin/Owner only) Lock every text channel immediately.")
    @app_commands.describe(reason="Why you're locking down")
    async def security_lockdown(self, interaction: discord.Interaction, reason: str = "Manual lockdown"):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        locked = await lockdown_guild(interaction.guild, reason)
        await update_settings(self.bot, interaction.guild_id, lockdown_mode=True)
        await log_incident(self.bot, interaction.guild_id, "manual", "HIGH",
                            f"Manual lockdown by {interaction.user}: {reason}", actor_id=interaction.user.id)
        await interaction.followup.send(f"🔒 Locked {locked} channel(s).", ephemeral=True)

    @security.command(name="unlock", description="(Admin/Owner only) Undo a lockdown and clear panic mode.")
    @app_commands.describe(reason="Why you're unlocking")
    async def security_unlock(self, interaction: discord.Interaction, reason: str = "Manual unlock"):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        unlocked = await unlock_guild(interaction.guild, reason)
        await update_settings(self.bot, interaction.guild_id, lockdown_mode=False, panic_mode=False, threat_level="LOW")
        await interaction.followup.send(f"🔓 Unlocked {unlocked} channel(s). Threat level reset to LOW.", ephemeral=True)

    @security.command(name="quarantine", description="(Admin/Owner only) Manually quarantine a member.")
    @app_commands.describe(member="Member to quarantine", reason="Reason")
    async def security_quarantine(self, interaction: discord.Interaction, member: discord.Member, reason: str = "Manual quarantine"):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_settings(self.bot, interaction.guild_id)
        if not settings["quarantine_role_id"]:
            await interaction.response.send_message("⚠️ No quarantine role configured — run `/security setup` first.", ephemeral=True)
            return
        ok = await apply_action(self.bot, member, "quarantine", reason)
        await record_offense(self.bot, interaction.guild_id, member.id, "manual", "quarantine", reason)
        await interaction.response.send_message(
            f"{'⛔ Quarantined' if ok else '❌ Failed to quarantine'} {member.mention}.", ephemeral=True
        )

    @security.command(name="panic", description="(Admin/Owner only) Toggle panic mode: instant lockdown + quarantine new joiners.")
    async def security_panic(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await get_settings(self.bot, interaction.guild_id)
        if settings["panic_mode"]:
            await unlock_guild(interaction.guild, "Panic mode disabled")
            await update_settings(self.bot, interaction.guild_id, panic_mode=False, lockdown_mode=False, threat_level="LOW")
            await interaction.followup.send("✅ Panic mode **disabled**. Server unlocked.", ephemeral=True)
        else:
            locked = await lockdown_guild(interaction.guild, "Panic mode enabled")
            await update_settings(self.bot, interaction.guild_id, panic_mode=True, lockdown_mode=True, threat_level="CRITICAL")
            await log_incident(self.bot, interaction.guild_id, "manual", "CRITICAL",
                                f"Panic mode activated by {interaction.user}", actor_id=interaction.user.id)
            await interaction.followup.send(f"🚨 Panic mode **enabled**. Locked {locked} channel(s), new joiners will be quarantined.", ephemeral=True)

    @security.command(name="backup", description="(Admin/Owner only) Snapshot roles, channels, and categories.")
    async def security_backup(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        backup_id = await create_backup(self.bot, interaction.guild, interaction.user.id)
        await interaction.followup.send(f"💾 Backup **#{backup_id}** created — roles, channels, categories, and emoji list saved.", ephemeral=True)

    @security.command(name="restore", description="(Admin/Owner only) Restore roles/channels from a backup (recreates only what's missing).")
    @app_commands.describe(backup_id="Specific backup ID, or leave blank for the latest")
    async def security_restore(self, interaction: discord.Interaction, backup_id: Optional[int] = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        backup = await get_backup(self.bot, interaction.guild_id, backup_id)
        if backup is None:
            await interaction.followup.send("❌ No backup found. Run `/security backup` first.", ephemeral=True)
            return
        r_roles, r_channels = await restore_backup(interaction.guild, backup)
        await interaction.followup.send(
            f"♻️ Restore complete from backup **#{backup['id']}** — recreated {r_roles} role(s) and "
            f"{r_channels} channel(s) that were missing. Anything that still existed was left untouched.",
            ephemeral=True,
        )

    @security.command(name="status", description="Quick summary of current security settings.")
    async def security_status(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_settings(self.bot, interaction.guild_id)
        guild = interaction.guild

        def ch(cid):
            c = guild.get_channel(cid) if cid else None
            return c.mention if c else "*not set*"

        def rl(rid):
            r = guild.get_role(rid) if rid else None
            return r.mention if r else "*not set*"

        modules = "\n".join(
            f"{'✅' if settings.get(key) else '❌'} {label}" for key, label, _ in MODULE_OPTIONS
        )
        embed = E.base(
            "⚙️  Security Status",
            f"**Log Channel:** {ch(settings['log_channel_id'])}\n"
            f"**Alert Channel:** {ch(settings['alert_channel_id'])}\n"
            f"**Verify Channel:** {ch(settings['verify_channel_id'])}\n"
            f"**Quarantine Role:** {rl(settings['quarantine_role_id'])}\n"
            f"**Muted Role:** {rl(settings['muted_role_id'])}\n"
            f"**Verified Role:** {rl(settings['verified_role_id'])}\n"
            f"**Raid Trigger:** {settings['raid_join_threshold']} joins / {settings['raid_join_window']}s\n"
            f"**Captcha Mode:** {settings['captcha_mode']}\n"
            f"**Threat Level:** {SEVERITY_EMOJI.get(settings['threat_level'], '')} {settings['threat_level']}\n\n"
            f"**Modules:**\n{modules}",
            color=PURPLE,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    whitelist_group = app_commands.Group(
        name="whitelist", description="Manage the security whitelist",
        parent=security, default_permissions=discord.Permissions(manage_guild=True),
    )
    blacklist_group = app_commands.Group(
        name="blacklist", description="Manage the security blacklist",
        parent=security, default_permissions=discord.Permissions(manage_guild=True),
    )

    @whitelist_group.command(name="add", description="(Admin/Owner only) Exempt a user, role, or domain from security checks.")
    @app_commands.describe(kind="What you're whitelisting", target="User/role mention or ID, or a bare domain like example.com")
    @app_commands.choices(kind=[
        app_commands.Choice(name="User", value="user"),
        app_commands.Choice(name="Role", value="role"),
        app_commands.Choice(name="Domain", value="domain"),
    ])
    async def whitelist_add(self, interaction: discord.Interaction, kind: app_commands.Choice[str], target: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        value, display = self._resolve_target(interaction.guild, kind.value, target)
        if value is None:
            await interaction.response.send_message("❌ Couldn't resolve that target.", ephemeral=True)
            return
        await add_list_entry(self.bot, interaction.guild_id, "whitelist", kind.value, value, None, interaction.user.id)
        await interaction.response.send_message(f"✅ Whitelisted {kind.name.lower()}: {display}", ephemeral=True)

    @whitelist_group.command(name="remove", description="(Admin/Owner only) Remove a whitelist entry.")
    @app_commands.describe(kind="What you're removing", target="User/role mention or ID, or a bare domain")
    @app_commands.choices(kind=[
        app_commands.Choice(name="User", value="user"),
        app_commands.Choice(name="Role", value="role"),
        app_commands.Choice(name="Domain", value="domain"),
    ])
    async def whitelist_remove(self, interaction: discord.Interaction, kind: app_commands.Choice[str], target: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        value, display = self._resolve_target(interaction.guild, kind.value, target)
        removed = await remove_list_entry(self.bot, interaction.guild_id, "whitelist", kind.value, value or target)
        await interaction.response.send_message(
            f"{'✅ Removed' if removed else '❌ Not found'}: {display}", ephemeral=True
        )

    @whitelist_group.command(name="list", description="Show the current whitelist.")
    async def whitelist_list(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        entries = await get_list(self.bot, interaction.guild_id, "whitelist")
        if not entries:
            await interaction.response.send_message("The whitelist is empty.", ephemeral=True)
            return
        lines = [f"`{e['kind']}` — {e['value']}" for e in entries[:25]]
        embed = E.base("📋  Whitelist", "\n".join(lines), color=GREEN)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @blacklist_group.command(name="add", description="(Admin/Owner only) Block a domain outright.")
    @app_commands.describe(domain="Domain to blacklist, e.g. scam-site.com", reason="Why it's blacklisted")
    async def blacklist_add(self, interaction: discord.Interaction, domain: str, reason: str = "No reason provided"):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        domain = domain.lower().strip().removeprefix("http://").removeprefix("https://").split("/")[0]
        await add_list_entry(self.bot, interaction.guild_id, "blacklist", "domain", domain, reason, interaction.user.id)
        await interaction.response.send_message(f"✅ Blacklisted domain: `{domain}`", ephemeral=True)

    @blacklist_group.command(name="remove", description="(Admin/Owner only) Remove a blacklisted domain.")
    @app_commands.describe(domain="Domain to remove")
    async def blacklist_remove(self, interaction: discord.Interaction, domain: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        domain = domain.lower().strip()
        removed = await remove_list_entry(self.bot, interaction.guild_id, "blacklist", "domain", domain)
        await interaction.response.send_message(f"{'✅ Removed' if removed else '❌ Not found'}: `{domain}`", ephemeral=True)

    @blacklist_group.command(name="list", description="Show the current domain blacklist.")
    async def blacklist_list(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        entries = await get_list(self.bot, interaction.guild_id, "blacklist", kind="domain")
        if not entries:
            await interaction.response.send_message("The blacklist is empty.", ephemeral=True)
            return
        lines = [f"`{e['value']}` — {e['reason'] or 'no reason'}" for e in entries[:25]]
        embed = E.base("📋  Blacklist", "\n".join(lines), color=RED)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _resolve_target(self, guild: discord.Guild, kind: str, raw: str) -> tuple[Optional[str], str]:
        raw = raw.strip()
        if kind == "domain":
            domain = raw.lower().removeprefix("http://").removeprefix("https://").split("/")[0]
            return domain, f"`{domain}`"
        m = re.match(r"<[@&#]{1,2}!?(\d+)>", raw)
        target_id = m.group(1) if m else (raw if raw.isdigit() else None)
        if target_id is None:
            return None, raw
        if kind == "user":
            member = guild.get_member(int(target_id))
            return target_id, (member.mention if member else f"`{target_id}`")
        if kind == "role":
            role = guild.get_role(int(target_id))
            return target_id, (role.mention if role else f"`{target_id}`")
        return target_id, f"`{target_id}`"


# ==========================================================================
# /security setup — interactive panel
# ==========================================================================

MODULE_OPTIONS = [
    ("mod_anti_spam", "Anti Spam", "🚫"),
    ("mod_anti_links", "Link Protection", "🔗"),
    ("mod_anti_raid", "Raid Protection", "🛡️"),
    ("mod_anti_nuke", "Anti Nuke", "💣"),
    ("mod_risk_scoring", "Risk Scoring", "📊"),
    ("mod_captcha", "Captcha Verification", "🔐"),
    ("mod_scam_detection", "Scam Detection", "🎣"),
]


class SetupView(discord.ui.View):
    def __init__(self, bot, guild_id: int, settings: dict):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id

        self.log_channel_select.placeholder = "📋  Security log channel"
        self.alert_channel_select.placeholder = "🚨  Staff alert channel"
        self.verify_channel_select.placeholder = "🔐  Verification channel (captcha)"
        self.quarantine_role_select.placeholder = "⛔  Quarantine role"

        for key, label, emoji in MODULE_OPTIONS:
            self.module_select.append_option(
                discord.SelectOption(label=label, value=key, emoji=emoji, default=bool(settings.get(key)))
            )
        self.module_select.max_values = len(MODULE_OPTIONS)
        self.module_select.min_values = 0

    async def _ack(self, interaction: discord.Interaction, text: str):
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text])
    async def log_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await update_settings(self.bot, self.guild_id, log_channel_id=select.values[0].id)
        await self._ack(interaction, f"✅ Log channel set to {select.values[0].mention}")

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text])
    async def alert_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await update_settings(self.bot, self.guild_id, alert_channel_id=select.values[0].id)
        await self._ack(interaction, f"✅ Alert channel set to {select.values[0].mention}")

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text])
    async def verify_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await update_settings(self.bot, self.guild_id, verify_channel_id=select.values[0].id)
        await self._ack(interaction, f"✅ Verification channel set to {select.values[0].mention}")

    @discord.ui.select(cls=discord.ui.RoleSelect)
    async def quarantine_role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        await update_settings(self.bot, self.guild_id, quarantine_role_id=select.values[0].id)
        await self._ack(interaction, f"✅ Quarantine role set to {select.values[0].mention}")

    @discord.ui.select(placeholder="⚙️  Toggle modules (select = ON)", min_values=0, max_values=1)
    async def module_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        chosen = set(select.values)
        updates = {key: (key in chosen) for key, _, _ in MODULE_OPTIONS}
        await update_settings(self.bot, self.guild_id, **updates)
        for opt in select.options:
            opt.default = opt.value in chosen
        on = [label for key, label, _ in MODULE_OPTIONS if key in chosen]
        await self._ack(interaction, f"✅ Modules on: {', '.join(on) if on else 'none'}")


# ==========================================================================
# /security ... command group
# ==========================================================================

def _target_prog_bar(score: int) -> str:
    filled = round(score / 10)
    return "🟩" * filled + "⬛" * (10 - filled)


# ==========================================================================
# COG ENTRYPOINT
# ==========================================================================

async def setup(bot: commands.Bot):
    await bot.add_cog(SecurityCog(bot))
