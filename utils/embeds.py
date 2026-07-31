from __future__ import annotations
from datetime import datetime, timezone
import discord
from config import (PURPLE, PURPLE_DARK, PURPLE_LIGHT,
                    GREEN, RED, ORANGE, BLUE)

FOOTER = 'Tier Testing, Support Ticket System'


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def base(title: str, description: str = '', color: int = PURPLE) -> discord.Embed:
    e = discord.Embed(title=title, description=description,
                      color=color, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=FOOTER)
    return e


# ─────────────────────── Ticket Panel ───────────────────────────────────────

def ticket_panel(guild: discord.Guild, categories: list[dict]) -> discord.Embed:
    cat_text = '\n'.join(
        f'{c["emoji"]} **{c["name"]}** — {c["description"]}'
        for c in categories if c['enabled']
    )
    e = discord.Embed(
        title='🎫 Support Center',
        description=(
            f'Welcome to **{guild.name}** Support!\n\n'
            'Select a category below that matches your issue.\n'
            'Our team will get back to you as soon as possible.\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━\n'
            f'{cat_text}\n'
            '━━━━━━━━━━━━━━━━━━━━━━\n\n'
            '> ⚠️ One ticket per user · Please be patient'
        ),
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
        e.set_author(name=f'{guild.name} Support', icon_url=guild.icon.url)
    else:
        e.set_author(name=f'{guild.name} Support')
    e.set_footer(text=f'{guild.name} • Premium Support System')
    return e


# ─────────────────────── Ticket Welcome ─────────────────────────────────────

def ticket_welcome(user: discord.Member, category: str,
                   answers: dict, ticket_id: int) -> discord.Embed:
    e = discord.Embed(
        title=f'🎫  Ticket #{ticket_id}  •  {category}',
        description=(
            f'Hey {user.mention}, thank you for reaching out!\n\n'
            'Our support team has been notified and will assist you shortly.\n'
            'Please provide any additional details while you wait.\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━'
        ),
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    e.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    e.add_field(name='📂  Category', value=category, inline=True)
    e.add_field(name='👤  Opened By', value=user.mention, inline=True)
    e.add_field(name='🕐  Created', value=f'<t:{_now_ts()}:F>', inline=False)

    if answers:
        for question, answer in answers.items():
            if answer and answer.strip():
                e.add_field(name=f'❓  {question}', value=answer[:1024], inline=False)

    e.set_footer(text='Premium Ticket System  •  Use the buttons below to manage this ticket')
    return e


# ─────────────────────── Status Embeds ──────────────────────────────────────

def ticket_claimed(staff: discord.Member) -> discord.Embed:
    e = base('✅  Ticket Claimed', color=GREEN)
    e.description = f'This ticket has been claimed by {staff.mention}.\nThey will assist you shortly.'
    e.set_author(name=staff.display_name, icon_url=staff.display_avatar.url)
    return e


def ticket_closed(closer: discord.Member, reason: str = '') -> discord.Embed:
    desc = f'This ticket has been closed by {closer.mention}.'
    if reason:
        desc += f'\n\n**Reason:** {reason}'
    e = base('🔒  Ticket Closed', desc, color=RED)
    return e


def ticket_reopened(opener: discord.Member) -> discord.Embed:
    e = base('🔓  Ticket Reopened', color=GREEN)
    e.description = f'This ticket has been reopened by {opener.mention}.'
    return e


def close_confirm() -> discord.Embed:
    return base(
        '⚠️  Confirm Close',
        'Are you sure you want to close this ticket?\nThis action will lock the channel.',
        color=ORANGE
    )


# ─────────────────────── Log Embed ──────────────────────────────────────────

_LOG_META: dict[str, tuple[str, str, int]] = {
    'created':    ('🎫', 'Ticket Created',             GREEN),
    'claimed':    ('✅', 'Ticket Claimed',              BLUE),
    'closed':     ('🔒', 'Ticket Closed',              RED),
    'reopened':   ('🔓', 'Ticket Reopened',            ORANGE),
    'transcript': ('📄', 'Transcript Generated',       PURPLE),
    'deleted':    ('🗑️',  'Ticket Deleted',             RED),
}


def log_action(
    action: str,
    actor: discord.Member,
    ticket_id: int,
    channel: discord.abc.GuildChannel,
    owner: discord.Member | None = None,
    extra: str = ''
) -> discord.Embed:
    emoji, title, color = _LOG_META.get(action, ('📋', action.title(), PURPLE))
    e = discord.Embed(title=f'{emoji}  {title}', color=color,
                      timestamp=datetime.now(timezone.utc))
    e.add_field(name='Ticket', value=f'{channel.mention}  (#{ticket_id})', inline=True)
    e.add_field(name='Action By', value=actor.mention, inline=True)
    if owner:
        e.add_field(name='Ticket Owner', value=owner.mention, inline=True)
    if extra:
        e.add_field(name='Details', value=extra[:1024], inline=False)
    e.set_footer(text=FOOTER)
    return e


# ─────────────────────── Admin Panel ────────────────────────────────────────

def admin_panel(guild: discord.Guild, settings: dict, categories: list[dict]) -> discord.Embed:
    e = discord.Embed(
        title='⚙️  Admin Control Panel',
        description=(
            f'Managing **{guild.name}**\n'
            'Use the buttons below to configure each section.\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━'
        ),
        color=PURPLE_DARK,
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)

    log_ch   = f'<#{settings["log_channel_id"]}>'   if settings.get('log_channel_id')        else '❌ Not Set'
    trans_ch = f'<#{settings["transcript_channel_id"]}>' if settings.get('transcript_channel_id') else '❌ Not Set'
    e.add_field(name='📋  Log Channel',        value=log_ch,   inline=True)
    e.add_field(name='📄  Transcript Channel', value=trans_ch, inline=True)
    e.add_field(name='\u200b', value='\u200b', inline=True)

    def _roles(ids: list[int]) -> str:
        return ', '.join(f'<@&{r}>' for r in ids) if ids else '❌ None'

    e.add_field(name='👥  Support Roles',  value=_roles(settings.get('support_role_ids', []))[:1024],    inline=False)
    e.add_field(name='🎯  Claim Roles',    value=_roles(settings.get('claim_role_ids', []))[:1024],      inline=True)
    e.add_field(name='🔒  Close Roles',    value=_roles(settings.get('close_role_ids', []))[:1024],      inline=True)
    e.add_field(name='🔓  Reopen Roles',   value=_roles(settings.get('reopen_role_ids', []))[:1024],     inline=True)
    e.add_field(name='📄  Transcript Roles', value=_roles(settings.get('transcript_role_ids', []))[:1024], inline=True)
    e.add_field(name='🛡️  Admin Roles',    value=_roles(settings.get('admin_role_ids', []))[:1024],      inline=True)
    e.add_field(name='\u200b', value='\u200b', inline=True)

    cat_lines = [
        f'{"✅" if c["enabled"] else "❌"}  {c["emoji"]}  {c["name"]}'
        for c in categories
    ]
    e.add_field(name='📂  Ticket Categories', value='\n'.join(cat_lines) or 'None', inline=False)
    e.set_footer(text='Premium Ticket System  •  Admin Panel')
    return e


def error(message: str) -> discord.Embed:
    return base('❌  Error', message, color=RED)


def success(message: str) -> discord.Embed:
    return base('✅  Success', message, color=GREEN)
