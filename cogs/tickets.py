from __future__ import annotations
import io
import asyncio
import logging
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

from config import PURPLE, PURPLE_DARK, GREEN, RED, ORANGE, CATEGORY_QUESTIONS
import utils.embeds as E
import utils.transcript as T
from cogs.access import require_admin_or_owner

logger = logging.getLogger('TicketBot.tickets')

# NOTE: Every slash command in this cog is restricted to a server Admin or
# the bot's real Owner via require_admin_or_owner() (shared in cogs/access.py).


# ═══════════════════════════════════════════════════════════════════════════════
#  MODALS
# ═══════════════════════════════════════════════════════════════════════════════

class TicketModal(discord.ui.Modal):
    """Dynamic modal that asks category-specific questions before opening a ticket."""

    def __init__(self, bot, category_name: str):
        super().__init__(title=f'🎫  {category_name}', timeout=300)
        self.bot = bot
        self.category_name = category_name
        self.fields_data: list[tuple[str, discord.ui.TextInput]] = []

        questions = CATEGORY_QUESTIONS.get(category_name, [
            ('Subject',     'What is the subject of your request?', False),
            ('Description', 'Please describe your issue in detail.', False),
        ])

        for label, placeholder, required in questions:
            field = discord.ui.TextInput(
                label=label,
                placeholder=placeholder,
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=1000,
            )
            self.fields_data.append((label, field))
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        answers = {label: field.value for label, field in self.fields_data}
        await create_ticket(self.bot, interaction, self.category_name, answers)


class CloseReasonModal(discord.ui.Modal, title='🔒  Close Ticket'):
    reason = discord.ui.TextInput(
        label='Reason (optional)',
        placeholder='Why are you closing this ticket?',
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        await close_ticket(self.bot, interaction, reason=self.reason.value or '')


class TierResultModal(discord.ui.Modal, title='📤  Post Tier Test Result'):
    """Only used from the 'Post Result' ticket button, and only on Tier
    Tester tickets. Posts to the dedicated result channel configured via
    /tieradminpanel → Set Channels (independent of the ticket log/transcript
    channels)."""

    tier_result = discord.ui.TextInput(
        label='Result / Tier Awarded',
        placeholder='e.g. HT2, or "Passed - promoted to LT1"',
        required=True,
        max_length=100,
    )
    notes = discord.ui.TextInput(
        label='Notes (optional)',
        style=discord.TextStyle.paragraph,
        placeholder='Any extra notes about the test…',
        required=False,
        max_length=500,
    )

    def __init__(self, bot, ticket: dict, gamemode: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.ticket = ticket
        self.gamemode = gamemode

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Deferred import: cogs.tier_test imports create_ticket/TicketControlView
        # from this module, so importing it at module scope here would create
        # a circular import. Safe to import lazily inside the handler.
        from cogs.tier_test import get_tier_settings

        tier_settings = await get_tier_settings(self.bot, interaction.guild_id)
        result_ch_id = tier_settings.get('result_channel_id')
        if not result_ch_id:
            return await interaction.followup.send(
                embed=E.error(
                    'No **Post Result** channel is set yet. An admin can set one via '
                    '`/tieradminpanel` → **Set Channels**.'
                ),
                ephemeral=True
            )

        result_ch = interaction.guild.get_channel(result_ch_id)
        if not result_ch:
            return await interaction.followup.send(
                embed=E.error('The configured Post Result channel no longer exists. Ask an admin to set it again.'),
                ephemeral=True
            )

        owner = interaction.guild.get_member(self.ticket['user_id'])
        owner_mention = owner.mention if owner else f'<@{self.ticket["user_id"]}>'

        embed = E.base(
            '📤  Tier Test Result',
            f'**Player:** {owner_mention}\n'
            f'**Gamemode:** {self.gamemode}\n'
            f'**Category:** {self.ticket["category"]}\n'
            f'**Result:** {self.tier_result.value}',
            color=PURPLE
        )
        if self.notes.value:
            embed.add_field(name='Notes', value=self.notes.value, inline=False)
        embed.add_field(name='Tested By', value=interaction.user.mention, inline=False)
        embed.set_footer(text=f'Ticket #{self.ticket["id"]}')

        try:
            await result_ch.send(embed=embed)
        except discord.HTTPException as e:
            logger.error(f'Failed to post tier result: {e}')
            return await interaction.followup.send(
                embed=E.error('Could not post the result — check my permissions in that channel.'),
                ephemeral=True
            )

        await interaction.followup.send(
            embed=E.success(f'Result posted in {result_ch.mention}.'), ephemeral=True)


class TierResultGamemodeSelectView(discord.ui.View):
    """Shown the moment staff click 'Post Result' on a Tier Tester ticket —
    a dropdown of every gamemode configured for this server (scoped to the
    ticket's edition when it can be read off the ticket category, otherwise
    Java + Bedrock combined). Picking one opens TierResultModal with that
    gamemode already attached, so it always ends up tagged on the posted
    result."""

    def __init__(self, bot, ticket: dict, gamemodes: list[tuple[str, str]]):
        super().__init__(timeout=120)
        self.bot = bot
        self.ticket = ticket

        options = [
            discord.SelectOption(label=name, value=name, emoji=emoji)
            for name, emoji in gamemodes
        ][:25]  # Discord hard cap of 25 options per select

        self.gamemode_select.options = options
        self.gamemode_select.placeholder = 'Select the gamemode this result is for…'

    @discord.ui.select()
    async def gamemode_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        gamemode = select.values[0]
        await interaction.response.send_modal(TierResultModal(self.bot, self.ticket, gamemode))


# ═══════════════════════════════════════════════════════════════════════════════
#  VIEWS
# ═══════════════════════════════════════════════════════════════════════════════

class CategorySelectView(discord.ui.View):
    """Dropdown for selecting a ticket category from the panel."""

    def __init__(self, bot, categories: list[dict]):
        super().__init__(timeout=None)
        self.add_item(CategorySelect(bot, categories))


class CategorySelect(discord.ui.Select):
    def __init__(self, bot, categories: list[dict]):
        self.bot = bot
        options = [
            discord.SelectOption(
                label=c['name'],
                description=c['description'][:100] if c.get('description') else '',
                emoji=c.get('emoji', '🎫'),
                value=c['name'],
            )
            for c in categories if c['enabled']
        ]
        super().__init__(
            placeholder='📂  Select a category to open a ticket…',
            options=options,
            min_values=1,
            max_values=1,
            custom_id='ticket:category_select',
        )

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        db = self.bot.db

        # Blacklist check
        if await db.is_blacklisted(interaction.guild_id, interaction.user.id):
            return await interaction.response.send_message(
                embed=E.error('You are blacklisted from creating tickets.'),
                ephemeral=True
            )

        # Duplicate ticket check
        existing = await db.get_open_ticket(interaction.guild_id, interaction.user.id)
        if existing:
            ch = interaction.guild.get_channel(existing['channel_id'])
            msg = f'You already have an open ticket: {ch.mention}' if ch else 'You already have an open ticket.'
            return await interaction.response.send_message(
                embed=E.error(msg), ephemeral=True
            )

        # Cooldown check
        settings = await db.get_settings(interaction.guild_id)
        cooldown = settings.get('cooldown_seconds', 300) if settings else 300
        remaining = await db.check_cooldown(interaction.guild_id, interaction.user.id, cooldown)
        if remaining > 0:
            return await interaction.response.send_message(
                embed=E.error(f'Please wait **{remaining}s** before opening another ticket.'),
                ephemeral=True
            )

        # Open modal with category-specific questions
        await interaction.response.send_modal(TicketModal(self.bot, category))


class TicketControlView(discord.ui.View):
    """Buttons shown inside every ticket channel."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _get_ticket(self, interaction: discord.Interaction):
        return await self.bot.db.get_ticket_by_channel(interaction.channel_id)

    # ── Claim ──────────────────────────────────────────────────────────────────
    @discord.ui.button(label='Claim', emoji='✅', style=discord.ButtonStyle.success,
                       custom_id='ticket:claim')
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = self.bot.db
        ticket = await self._get_ticket(interaction)
        if not ticket:
            return await interaction.response.send_message(
                embed=E.error('Ticket not found.'), ephemeral=True)

        if ticket['status'] != 'open':
            return await interaction.response.send_message(
                embed=E.error('This ticket is not open.'), ephemeral=True)

        if ticket.get('claimed_by'):
            claimer = interaction.guild.get_member(ticket['claimed_by'])
            name = claimer.mention if claimer else f'<@{ticket["claimed_by"]}>'
            return await interaction.response.send_message(
                embed=E.error(f'Already claimed by {name}.'), ephemeral=True)

        settings = await db.get_settings(interaction.guild_id) or {}
        if not await _has_role(interaction.user, settings.get('claim_role_ids', []),
                               settings.get('support_role_ids', [])):
            return await interaction.response.send_message(
                embed=E.error('You do not have permission to claim tickets.'), ephemeral=True)

        await db.update_ticket(interaction.channel_id, 'claimed_by', interaction.user.id)
        await interaction.response.send_message(embed=E.ticket_claimed(interaction.user))

        owner = interaction.guild.get_member(ticket['user_id'])
        await _send_log(self.bot, interaction.guild, 'claimed', interaction.user,
                        ticket['id'], interaction.channel, owner)
        await db.log_action(interaction.guild_id, ticket['id'], 'claimed', interaction.user.id)

    # ── Close ──────────────────────────────────────────────────────────────────
    @discord.ui.button(label='Close', emoji='🔒', style=discord.ButtonStyle.danger,
                       custom_id='ticket:close')
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = self.bot.db
        ticket = await self._get_ticket(interaction)
        if not ticket:
            return await interaction.response.send_message(
                embed=E.error('Ticket not found.'), ephemeral=True)

        if ticket['status'] == 'closed':
            return await interaction.response.send_message(
                embed=E.error('Ticket is already closed.'), ephemeral=True)

        settings = await db.get_settings(interaction.guild_id) or {}
        is_owner = interaction.user.id == ticket['user_id']
        has_perm  = await _has_role(interaction.user,
                                    settings.get('close_role_ids', []),
                                    settings.get('support_role_ids', []))
        if not (is_owner or has_perm):
            return await interaction.response.send_message(
                embed=E.error('You do not have permission to close this ticket.'), ephemeral=True)

        # Show reason modal (also handles the actual close)
        await interaction.response.send_modal(CloseReasonModal(self.bot))

    # ── Reopen ─────────────────────────────────────────────────────────────────
    @discord.ui.button(label='Reopen', emoji='🔓', style=discord.ButtonStyle.primary,
                       custom_id='ticket:reopen')
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = self.bot.db
        ticket = await self._get_ticket(interaction)
        if not ticket:
            return await interaction.response.send_message(
                embed=E.error('Ticket not found.'), ephemeral=True)

        if ticket['status'] == 'open':
            return await interaction.response.send_message(
                embed=E.error('Ticket is already open.'), ephemeral=True)

        settings = await db.get_settings(interaction.guild_id) or {}
        if not await _has_role(interaction.user,
                               settings.get('reopen_role_ids', []),
                               settings.get('support_role_ids', [])):
            return await interaction.response.send_message(
                embed=E.error('You do not have permission to reopen tickets.'), ephemeral=True)

        await db.update_ticket(interaction.channel_id, 'status', 'open')
        await db.update_ticket(interaction.channel_id, 'closed_at', None)

        owner = interaction.guild.get_member(ticket['user_id'])
        if owner:
            overwrite = interaction.channel.overwrites_for(owner)
            overwrite.send_messages = True
            await interaction.channel.set_permissions(owner, overwrite=overwrite)

        await interaction.response.send_message(embed=E.ticket_reopened(interaction.user))
        await _send_log(self.bot, interaction.guild, 'reopened', interaction.user,
                        ticket['id'], interaction.channel, owner)
        await db.log_action(interaction.guild_id, ticket['id'], 'reopened', interaction.user.id)

    # ── Transcript ─────────────────────────────────────────────────────────────
    @discord.ui.button(label='Transcript', emoji='📄', style=discord.ButtonStyle.secondary,
                       custom_id='ticket:transcript')
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await generate_transcript(self.bot, interaction, ephemeral=True)

    # ── Post Result (Tier Tester tickets only) ───────────────────────────────
    # Shows on every ticket's control panel (this view is shared/persistent
    # across the whole bot), but only does something on tickets created by
    # the Tier Tester system (category starts with "Tier Test"/"Tier Tester").
    # Posts to the dedicated result channel set via /tieradminpanel.
    @discord.ui.button(label='Post Result', emoji='📤', style=discord.ButtonStyle.success,
                       custom_id='ticket:post_result')
    async def post_result(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = self.bot.db
        ticket = await self._get_ticket(interaction)
        if not ticket:
            return await interaction.response.send_message(
                embed=E.error('Ticket not found.'), ephemeral=True)

        if not ticket['category'].startswith(('Tier Test', 'Tier Tester')):
            return await interaction.response.send_message(
                embed=E.error('This button is only usable inside Tier Tester tickets.'), ephemeral=True)

        settings = await db.get_settings(interaction.guild_id) or {}
        if not await _has_role(interaction.user,
                               settings.get('claim_role_ids', []),
                               settings.get('support_role_ids', [])):
            return await interaction.response.send_message(
                embed=E.error('You do not have permission to post a result for this ticket.'), ephemeral=True)

        # Deferred import to avoid the circular import (see TierResultModal.on_submit).
        from cogs.tier_test import get_gamemodes, EDITIONS

        # The ticket category is "Tier Test • <gamemode> • <edition>" (or
        # "Tier Tester App • ..."), so the edition is the last "•" segment.
        # If it doesn't cleanly match, fall back to Java + Bedrock combined
        # so staff always get a full, working list of gamemodes to pick from.
        parts = [p.strip() for p in ticket['category'].split('•')]
        edition = parts[-1] if parts and parts[-1] in EDITIONS else None

        if edition:
            gamemodes = await get_gamemodes(self.bot, interaction.guild_id, edition)
        else:
            java_gm = await get_gamemodes(self.bot, interaction.guild_id, 'Java Edition')
            bedrock_gm = await get_gamemodes(self.bot, interaction.guild_id, 'Bedrock Edition')
            seen = set()
            gamemodes = []
            for name, emoji in java_gm + bedrock_gm:
                if name not in seen:
                    seen.add(name)
                    gamemodes.append((name, emoji))

        # get_gamemodes always seeds a server's list from the built-in
        # defaults on first use, so this is only ever empty if an admin has
        # explicitly cleared every gamemode via /tieradminpanel.
        if not gamemodes:
            return await interaction.response.send_message(
                embed=E.error('No gamemodes are configured yet. An admin can add some via `/tieradminpanel`.'),
                ephemeral=True
            )

        await interaction.response.send_message(
            embed=E.base(
                '🎮  Select Gamemode',
                'Which gamemode was this result for? Pick it below, then fill in the result.',
                color=PURPLE
            ),
            view=TierResultGamemodeSelectView(self.bot, ticket, gamemodes),
            ephemeral=True
        )

    # ── Delete ─────────────────────────────────────────────────────────────────
    @discord.ui.button(label='Delete', emoji='🗑️', style=discord.ButtonStyle.danger,
                       custom_id='ticket:delete')
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = self.bot.db
        ticket = await self._get_ticket(interaction)
        if not ticket:
            return await interaction.response.send_message(
                embed=E.error('Ticket not found.'), ephemeral=True)

        settings = await db.get_settings(interaction.guild_id) or {}
        if not await _has_role(interaction.user,
                               settings.get('admin_role_ids', []),
                               settings.get('support_role_ids', [])):
            return await interaction.response.send_message(
                embed=E.error('Only admins can delete tickets.'), ephemeral=True)

        await interaction.response.send_message(
            embed=E.base('🗑️  Deleting ticket…', 'This channel will be deleted in 5 seconds.',
                         color=RED),
            ephemeral=False
        )

        owner = interaction.guild.get_member(ticket['user_id'])
        await _send_log(self.bot, interaction.guild, 'deleted', interaction.user,
                        ticket['id'], interaction.channel, owner)
        await db.log_action(interaction.guild_id, ticket['id'], 'deleted', interaction.user.id)
        await db.update_ticket(interaction.channel_id, 'status', 'deleted')

        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f'Ticket deleted by {interaction.user}')
        except discord.HTTPException as e:
            logger.error(f'Failed to delete ticket channel: {e}')


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _has_role(member: discord.Member, *role_id_lists: list[int]) -> bool:
    member_role_ids = {r.id for r in member.roles}
    for rid_list in role_id_lists:
        if any(r in member_role_ids for r in rid_list):
            return True
    if member.guild_permissions.administrator:
        return True
    return False


async def _send_log(bot, guild: discord.Guild, action: str, actor: discord.Member,
                    ticket_id: int, channel: discord.abc.GuildChannel,
                    owner: discord.Member | None = None, extra: str = ''):
    settings = await bot.db.get_settings(guild.id)
    if not settings or not settings.get('log_channel_id'):
        return
    log_ch = guild.get_channel(settings['log_channel_id'])
    if not log_ch:
        return
    try:
        embed = E.log_action(action, actor, ticket_id, channel, owner, extra)
        await log_ch.send(embed=embed)
    except discord.HTTPException as e:
        logger.error(f'Failed to send log: {e}')


async def create_ticket(bot, interaction: discord.Interaction,
                        category_name: str, answers: dict,
                        extra_ping_role_ids: list[int] | None = None,
                        category_id: int | None = None):
    db = bot.db
    guild = interaction.guild
    user  = interaction.user

    settings = await db.get_settings(guild.id)
    if not settings:
        await db.setup_guild(guild.id)
        settings = await db.get_settings(guild.id)

    # Find or create the Discord category channel for tickets. `category_id`
    # (e.g. the Tier Tester system's own dedicated category set via
    # /tieradminpanel) takes priority over the support ticket system's
    # default when provided.
    ticket_cat_id = category_id or settings.get('ticket_category_id')
    ticket_cat = guild.get_channel(ticket_cat_id) if ticket_cat_id else None

    channel_name = f'ticket-{user.name[:25].lower().replace(" ", "-")}'

    # Build overwrites
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_channels=True, manage_permissions=True
        ),
    }

    # Grant support roles
    for role_id in (settings.get('support_role_ids') or []):
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

    # Grant any extra roles passed in (e.g. Tier Tester ping role) the same
    # access, so whoever gets pinged can actually see/use the ticket.
    for role_id in (extra_ping_role_ids or []):
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

    try:
        channel = await guild.create_text_channel(
            name=channel_name,
            category=ticket_cat,
            overwrites=overwrites,
            topic=f'Ticket for {user} | Category: {category_name}',
            reason=f'Ticket opened by {user}'
        )
    except discord.Forbidden:
        return await interaction.followup.send(
            embed=E.error('I don\'t have permission to create channels.'), ephemeral=True
        )

    ticket_id = await db.create_ticket(guild.id, user.id, channel.id, category_name, answers)
    await db.set_cooldown(guild.id, user.id)

    welcome_embed = E.ticket_welcome(user, category_name, answers, ticket_id)
    control_view  = TicketControlView(bot)

    welcome_msg = await channel.send(embed=welcome_embed, view=control_view)
    try:
        await welcome_msg.pin()
    except discord.HTTPException:
        pass

    # Ping support roles (+ any extra roles the caller wants pinged, e.g.
    # the Tier Tester system's own configurable ping role — deduped so a
    # role that's already a support role never gets pinged twice)
    all_ping_role_ids = list(dict.fromkeys(
        (settings.get('support_role_ids') or []) + (extra_ping_role_ids or [])
    ))
    pings = ' '.join(f'<@&{r}>' for r in all_ping_role_ids)
    if pings:
        ping_msg = await channel.send(pings)
        await asyncio.sleep(1)
        try:
            await ping_msg.delete()
        except discord.HTTPException:
            pass

    await interaction.followup.send(
        embed=E.success(f'Your ticket has been created: {channel.mention}'),
        ephemeral=True
    )

    await _send_log(bot, guild, 'created', user, ticket_id, channel)
    await db.log_action(guild.id, ticket_id, 'created', user.id,
                        f'Category: {category_name}')


async def close_ticket(bot, interaction: discord.Interaction, reason: str = ''):
    db = bot.db
    ticket = await db.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        return await interaction.followup.send(embed=E.error('Ticket not found.'), ephemeral=True)

    if ticket['status'] == 'closed':
        return await interaction.followup.send(embed=E.error('Already closed.'), ephemeral=True)

    owner = interaction.guild.get_member(ticket['user_id'])
    if owner:
        overwrite = interaction.channel.overwrites_for(owner)
        overwrite.send_messages = False
        try:
            await interaction.channel.set_permissions(owner, overwrite=overwrite)
        except discord.HTTPException:
            pass

    closed_at = datetime.now(timezone.utc).isoformat()
    await db.update_ticket(interaction.channel_id, 'status', 'closed')
    await db.update_ticket(interaction.channel_id, 'closed_at', closed_at)

    await interaction.followup.send(embed=E.ticket_closed(interaction.user, reason))

    # Auto-transcript on close
    await generate_transcript(bot, interaction, ephemeral=False, silent=True)

    await _send_log(bot, interaction.guild, 'closed', interaction.user,
                    ticket['id'], interaction.channel, owner,
                    extra=reason or '')
    await db.log_action(interaction.guild_id, ticket['id'], 'closed', interaction.user.id,
                        reason or '')


async def generate_transcript(bot, interaction: discord.Interaction,
                               ephemeral: bool = True, silent: bool = False):
    db = bot.db
    ticket = await db.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        if not silent:
            await interaction.followup.send(embed=E.error('Ticket not found.'), ephemeral=True)
        return

    settings = await db.get_settings(interaction.guild_id) or {}

    # Collect messages
    messages: list[discord.Message] = []
    async for msg in interaction.channel.history(limit=500, oldest_first=True):
        messages.append(msg)

    owner  = interaction.guild.get_member(ticket['user_id'])
    closer = interaction.guild.get_member(ticket.get('claimed_by') or interaction.user.id)

    html = T.build_html(
        guild=interaction.guild,
        channel=interaction.channel,
        ticket_data=ticket,
        messages=messages,
        opener=owner,
        closer=interaction.user,
    )

    file_bytes = html.encode('utf-8')
    file = discord.File(io.BytesIO(file_bytes),
                        filename=f'transcript-{ticket["id"]}.html')

    trans_embed = E.base(
        '📄  Transcript Generated',
        f'Transcript for ticket **#{ticket["id"]}** has been saved.',
        color=PURPLE
    )
    trans_embed.add_field(name='Category', value=ticket['category'], inline=True)
    trans_embed.add_field(name='Messages', value=str(len(messages)), inline=True)
    if owner:
        trans_embed.add_field(name='Opened By', value=owner.mention, inline=True)

    # Send to transcript channel
    trans_ch_id = settings.get('transcript_channel_id')
    if trans_ch_id:
        trans_ch = interaction.guild.get_channel(trans_ch_id)
        if trans_ch:
            file2 = discord.File(io.BytesIO(file_bytes),
                                 filename=f'transcript-{ticket["id"]}.html')
            try:
                await trans_ch.send(embed=trans_embed, file=file2)
            except discord.HTTPException as e:
                logger.error(f'Could not send transcript to channel: {e}')

    if not silent:
        file3 = discord.File(io.BytesIO(file_bytes),
                             filename=f'transcript-{ticket["id"]}.html')
        await interaction.followup.send(embed=trans_embed, file=file3, ephemeral=ephemeral)

    await _send_log(bot, interaction.guild, 'transcript', interaction.user,
                    ticket['id'], interaction.channel, owner)
    await db.log_action(interaction.guild_id, ticket['id'], 'transcript', interaction.user.id)


# ═══════════════════════════════════════════════════════════════════════════════
#  COG
# ═══════════════════════════════════════════════════════════════════════════════

class TicketsCog(commands.Cog, name='Tickets'):
    def __init__(self, bot):
        self.bot = bot
        # Re-register persistent views on startup
        bot.add_view(TicketControlView(bot))

    # ── /setup ─────────────────────────────────────────────────────────────────
    @app_commands.command(name='setup', description='(Admin/Owner only) Set up the ticket panel in this channel.')
    @app_commands.describe(channel='Channel to send the ticket panel (default: current channel)')
    async def setup(self, interaction: discord.Interaction,
                    channel: discord.TextChannel | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = self.bot.db
        guild = interaction.guild
        target = channel or interaction.channel

        await db.setup_guild(guild.id)
        settings = await db.get_settings(guild.id)
        categories = await db.get_enabled_categories(guild.id)

        if not categories:
            return await interaction.followup.send(
                embed=E.error('No enabled ticket categories found. Use `/adminpanel` to enable some.'),
                ephemeral=True
            )

        panel_embed = E.ticket_panel(guild, categories)
        view = CategorySelectView(self.bot, categories)

        try:
            msg = await target.send(embed=panel_embed, view=view)
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=E.error(f'I cannot send messages in {target.mention}.'), ephemeral=True
            )

        await db.update_setting(guild.id, 'setup_channel_id', target.id)

        await interaction.followup.send(
            embed=E.success(f'Ticket panel has been set up in {target.mention}!'),
            ephemeral=True
        )

    # ── /newticket ─────────────────────────────────────────────────────────────
    @app_commands.command(name='newticket', description='(Admin/Owner only) Open a new support ticket.')
    @app_commands.describe(category='Ticket category')
    @app_commands.choices(category=[
        app_commands.Choice(name='General Support', value='General Support'),
        app_commands.Choice(name='Billing Support', value='Billing Support'),
        app_commands.Choice(name='Report User',     value='Report User'),
        app_commands.Choice(name='Partnership',     value='Partnership'),
    ])
    async def newticket(self, interaction: discord.Interaction, category: str = 'General Support'):
        if not await require_admin_or_owner(self.bot, interaction):
            return

        db = self.bot.db

        if await db.is_blacklisted(interaction.guild_id, interaction.user.id):
            return await interaction.response.send_message(
                embed=E.error('You are blacklisted from creating tickets.'), ephemeral=True)

        existing = await db.get_open_ticket(interaction.guild_id, interaction.user.id)
        if existing:
            ch = interaction.guild.get_channel(existing['channel_id'])
            msg = f'You already have an open ticket: {ch.mention}' if ch else 'You already have an open ticket.'
            return await interaction.response.send_message(embed=E.error(msg), ephemeral=True)

        settings = await db.get_settings(interaction.guild_id)
        cooldown = settings.get('cooldown_seconds', 300) if settings else 300
        remaining = await db.check_cooldown(interaction.guild_id, interaction.user.id, cooldown)
        if remaining > 0:
            return await interaction.response.send_message(
                embed=E.error(f'Please wait **{remaining}s** before opening another ticket.'),
                ephemeral=True)

        await interaction.response.send_modal(TicketModal(self.bot, category))

    # ── /close ─────────────────────────────────────────────────────────────────
    @app_commands.command(name='close', description='(Admin/Owner only) Close the current ticket.')
    async def close(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await interaction.response.send_modal(CloseReasonModal(self.bot))

    # ── /claim ─────────────────────────────────────────────────────────────────
    @app_commands.command(name='claim', description='(Admin/Owner only) Claim the current ticket.')
    async def claim(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        view = TicketControlView(self.bot)
        # Reuse the button callback
        btn = view.claim
        await btn.callback(view, interaction)

    # ── /transcript ────────────────────────────────────────────────────────────
    @app_commands.command(name='transcript', description='(Admin/Owner only) Generate a transcript of the current ticket.')
    async def transcript(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await generate_transcript(self.bot, interaction, ephemeral=True)

    # ── /add ───────────────────────────────────────────────────────────────────
    @app_commands.command(name='add', description='(Admin/Owner only) Add a user to the current ticket.')
    @app_commands.describe(user='User to add')
    async def add(self, interaction: discord.Interaction, user: discord.Member):
        if not await require_admin_or_owner(self.bot, interaction):
            return

        ticket = await self.bot.db.get_ticket_by_channel(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message(
                embed=E.error('This is not a ticket channel.'), ephemeral=True)

        await interaction.channel.set_permissions(user, view_channel=True,
                                                   send_messages=True, read_message_history=True)
        await interaction.response.send_message(
            embed=E.success(f'{user.mention} has been added to this ticket.'))

    # ── /remove ────────────────────────────────────────────────────────────────
    @app_commands.command(name='remove', description='(Admin/Owner only) Remove a user from the current ticket.')
    @app_commands.describe(user='User to remove')
    async def remove(self, interaction: discord.Interaction, user: discord.Member):
        if not await require_admin_or_owner(self.bot, interaction):
            return

        ticket = await self.bot.db.get_ticket_by_channel(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message(
                embed=E.error('This is not a ticket channel.'), ephemeral=True)

        if user.id == ticket['user_id']:
            return await interaction.response.send_message(
                embed=E.error('Cannot remove the ticket owner.'), ephemeral=True)

        await interaction.channel.set_permissions(user, view_channel=False)
        await interaction.response.send_message(
            embed=E.success(f'{user.mention} has been removed from this ticket.'))


async def setup(bot):
    await bot.add_cog(TicketsCog(bot))
