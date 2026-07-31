from __future__ import annotations
import logging
import discord
from discord import app_commands
from discord.ext import commands

from config import PURPLE, PURPLE_DARK, GREEN, RED, ORANGE
import utils.embeds as E
from cogs.access import require_admin_or_owner

logger = logging.getLogger('TicketBot.admin')


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
#
#  Every command in this cog is restricted to a server Admin or the bot's
#  real Owner via require_admin_or_owner() (shared in cogs/access.py).
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
#  MODALS
# ═══════════════════════════════════════════════════════════════════════════════

class BlacklistModal(discord.ui.Modal, title='🚫  Blacklist / Unblacklist User'):
    user_id = discord.ui.TextInput(
        label='User ID',
        placeholder='Enter the Discord user ID',
        required=True,
        max_length=25,
    )
    reason = discord.ui.TextInput(
        label='Reason (blacklist only)',
        placeholder='Optional reason for blacklisting',
        required=False,
        max_length=300,
    )
    action = discord.ui.TextInput(
        label='Action',
        placeholder='Type "add" to blacklist, "remove" to unblacklist',
        required=True,
        max_length=10,
    )

    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = self.bot.db

        try:
            uid = int(self.user_id.value.strip())
        except ValueError:
            return await interaction.followup.send(embed=E.error('Invalid user ID.'), ephemeral=True)

        act = self.action.value.strip().lower()
        if act == 'add':
            await db.blacklist_user(interaction.guild_id, uid, self.reason.value or '')
            await interaction.followup.send(
                embed=E.success(f'User `{uid}` has been blacklisted.'), ephemeral=True)
        elif act == 'remove':
            await db.unblacklist_user(interaction.guild_id, uid)
            await interaction.followup.send(
                embed=E.success(f'User `{uid}` has been removed from the blacklist.'), ephemeral=True)
        else:
            await interaction.followup.send(
                embed=E.error('Action must be "add" or "remove".'), ephemeral=True)


class CooldownModal(discord.ui.Modal, title='⏱️  Set Cooldown'):
    seconds = discord.ui.TextInput(
        label='Cooldown (seconds)',
        placeholder='e.g. 300 (5 minutes)',
        required=True,
        max_length=6,
    )

    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            val = int(self.seconds.value.strip())
            if val < 0:
                raise ValueError
        except ValueError:
            return await interaction.followup.send(embed=E.error('Enter a valid positive number.'), ephemeral=True)
        await self.bot.db.update_setting(interaction.guild_id, 'cooldown_seconds', val)
        await interaction.followup.send(
            embed=E.success(f'Cooldown set to **{val}** seconds.'), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  VIEWS
# ═══════════════════════════════════════════════════════════════════════════════

class AdminPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def _guard(self, interaction: discord.Interaction) -> bool:
        return await require_admin_or_owner(self.bot, interaction)

    # ── Set Log Channel ────────────────────────────────────────────────────────
    @discord.ui.button(label='Log Channel', emoji='📋', style=discord.ButtonStyle.secondary, row=0)
    async def set_log(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_message(
            embed=E.base('📋  Set Log Channel',
                         'Mention the channel you want to use as the log channel.\n'
                         'Example: `#ticket-logs`\n\nType your response below:',
                         color=PURPLE),
            ephemeral=True
        )

        def check(m):
            return (m.author.id == interaction.user.id and
                    m.channel.id == interaction.channel_id and
                    m.channel_mentions)

        try:
            msg = await self.bot.wait_for('message', check=check, timeout=60)
            ch = msg.channel_mentions[0]
            await self.bot.db.update_setting(interaction.guild_id, 'log_channel_id', ch.id)
            await interaction.followup.send(
                embed=E.success(f'Log channel set to {ch.mention}'), ephemeral=True)
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
        except Exception:
            await interaction.followup.send(embed=E.error('Timed out or no channel provided.'), ephemeral=True)

    # ── Set Transcript Channel ─────────────────────────────────────────────────
    @discord.ui.button(label='Transcript Channel', emoji='📄', style=discord.ButtonStyle.secondary, row=0)
    async def set_transcript(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_message(
            embed=E.base('📄  Set Transcript Channel',
                         'Mention the channel to receive transcripts.\nExample: `#transcripts`',
                         color=PURPLE),
            ephemeral=True
        )

        def check(m):
            return (m.author.id == interaction.user.id and
                    m.channel.id == interaction.channel_id and
                    m.channel_mentions)

        try:
            msg = await self.bot.wait_for('message', check=check, timeout=60)
            ch = msg.channel_mentions[0]
            await self.bot.db.update_setting(interaction.guild_id, 'transcript_channel_id', ch.id)
            await interaction.followup.send(
                embed=E.success(f'Transcript channel set to {ch.mention}'), ephemeral=True)
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
        except Exception:
            await interaction.followup.send(embed=E.error('Timed out or no channel provided.'), ephemeral=True)

    # ── Set Ticket Category ────────────────────────────────────────────────────
    @discord.ui.button(label='Ticket Category', emoji='📂', style=discord.ButtonStyle.secondary, row=0)
    async def set_category(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_message(
            embed=E.base('📂  Set Ticket Category',
                         'Mention or type the ID of the **Discord Category** (not a channel) '
                         'where ticket channels should be created.',
                         color=PURPLE),
            ephemeral=True
        )

        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel_id

        try:
            msg = await self.bot.wait_for('message', check=check, timeout=60)
            raw = msg.content.strip()
            try:
                cat_id = int(raw)
            except ValueError:
                return await interaction.followup.send(
                    embed=E.error('Please provide a valid Category ID.'), ephemeral=True)

            cat = interaction.guild.get_channel(cat_id)
            if not isinstance(cat, discord.CategoryChannel):
                return await interaction.followup.send(
                    embed=E.error('That is not a valid Discord category channel.'), ephemeral=True)

            await self.bot.db.update_setting(interaction.guild_id, 'ticket_category_id', cat_id)
            await interaction.followup.send(
                embed=E.success(f'Tickets will be created under **{cat.name}**.'), ephemeral=True)
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
        except Exception:
            await interaction.followup.send(embed=E.error('Timed out.'), ephemeral=True)

    # ── Manage Roles ───────────────────────────────────────────────────────────
    @discord.ui.button(label='Manage Roles', emoji='👥', style=discord.ButtonStyle.primary, row=1)
    async def manage_roles(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_message(
            embed=E.base('👥  Manage Roles', 'Choose which role type to configure:', color=PURPLE),
            view=RoleManageView(self.bot),
            ephemeral=True
        )

    # ── Toggle Categories ──────────────────────────────────────────────────────
    @discord.ui.button(label='Toggle Categories', emoji='📂', style=discord.ButtonStyle.primary, row=1)
    async def toggle_categories(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await self._guard(interaction):
            return
        categories = await self.bot.db.get_categories(interaction.guild_id)
        if not categories:
            return await interaction.response.send_message(
                embed=E.error('No categories found.'), ephemeral=True)
        await interaction.response.send_message(
            embed=E.base('📂  Toggle Categories', 'Select categories to enable/disable:', color=PURPLE),
            view=CategoryToggleView(self.bot, categories),
            ephemeral=True
        )

    # ── Blacklist ──────────────────────────────────────────────────────────────
    @discord.ui.button(label='Blacklist', emoji='🚫', style=discord.ButtonStyle.danger, row=1)
    async def blacklist(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(BlacklistModal(self.bot))

    # ── View Blacklist ─────────────────────────────────────────────────────────
    @discord.ui.button(label='View Blacklist', emoji='📋', style=discord.ButtonStyle.secondary, row=2)
    async def view_blacklist(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await self._guard(interaction):
            return
        blist = await self.bot.db.get_blacklist(interaction.guild_id)
        if not blist:
            return await interaction.response.send_message(
                embed=E.base('🚫  Blacklist', 'No users are currently blacklisted.', color=PURPLE),
                ephemeral=True
            )
        lines = [
            f'`{b["user_id"]}` — {b["reason"] or "No reason"}'
            for b in blist
        ]
        e = E.base('🚫  Blacklisted Users', '\n'.join(lines[:20]), color=RED)
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ── Cooldown ───────────────────────────────────────────────────────────────
    @discord.ui.button(label='Set Cooldown', emoji='⏱️', style=discord.ButtonStyle.secondary, row=2)
    async def set_cooldown(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(CooldownModal(self.bot))

    # ── View Settings ──────────────────────────────────────────────────────────
    @discord.ui.button(label='View Settings', emoji='⚙️', style=discord.ButtonStyle.secondary, row=2)
    async def view_settings(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await self._guard(interaction):
            return
        settings = await self.bot.db.get_settings(interaction.guild_id)
        if not settings:
            await self.bot.db.setup_guild(interaction.guild_id)
            settings = await self.bot.db.get_settings(interaction.guild_id)
        categories = await self.bot.db.get_categories(interaction.guild_id)
        embed = E.admin_panel(interaction.guild, settings, categories)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Reset Settings ─────────────────────────────────────────────────────────
    @discord.ui.button(label='Reset Settings', emoji='🔄', style=discord.ButtonStyle.danger, row=2)
    async def reset_settings(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await interaction.response.send_message(
            embed=E.base('🔄  Confirm Reset',
                         '⚠️ This will reset **all** bot settings for this server.\n'
                         'All configured roles, channels, and categories will be cleared.\n\n'
                         'Click **Confirm** to proceed.',
                         color=ORANGE),
            view=ResetConfirmView(self.bot),
            ephemeral=True
        )


class RoleManageView(discord.ui.View):
    ROLE_TYPES = [
        ('support_role_ids',    '👥 Support Roles',    'Can view and respond to tickets'),
        ('claim_role_ids',      '🎯 Claim Roles',      'Can claim tickets'),
        ('close_role_ids',      '🔒 Close Roles',      'Can close tickets'),
        ('reopen_role_ids',     '🔓 Reopen Roles',     'Can reopen closed tickets'),
        ('transcript_role_ids', '📄 Transcript Roles', 'Can generate transcripts'),
        ('admin_role_ids',      '🛡️ Admin Roles',      'Full admin control'),
    ]

    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot
        options = [
            discord.SelectOption(label=label, description=desc, value=key)
            for key, label, desc in self.ROLE_TYPES
        ]
        select = discord.ui.Select(
            placeholder='Select role type to manage…',
            options=options,
            custom_id='admin:role_type_select'
        )
        select.callback = self._role_type_selected
        self.add_item(select)
        self._selected_type = None

    async def _role_type_selected(self, interaction: discord.Interaction):
        self._selected_type = interaction.data['values'][0]
        label = next(l for k, l, _ in self.ROLE_TYPES if k == self._selected_type)

        settings = await self.bot.db.get_settings(interaction.guild_id) or {}
        current = settings.get(self._selected_type, [])
        current_str = ', '.join(f'<@&{r}>' for r in current) if current else 'None'

        # Build role select
        roles = [r for r in interaction.guild.roles if not r.is_default()][:25]
        role_options = [
            discord.SelectOption(label=r.name[:100], value=str(r.id),
                                 default=r.id in current)
            for r in roles
        ]

        role_select = discord.ui.Select(
            placeholder=f'Select roles for {label}…',
            options=role_options,
            min_values=0,
            max_values=min(len(role_options), 10),
            custom_id='admin:role_select'
        )

        async def role_callback(inter: discord.Interaction):
            new_ids = [int(v) for v in inter.data['values']]
            await self.bot.db.update_setting(inter.guild_id, self._selected_type, new_ids)
            role_mentions = ', '.join(f'<@&{r}>' for r in new_ids) if new_ids else 'None'
            await inter.response.send_message(
                embed=E.success(f'**{label}** updated to: {role_mentions}'),
                ephemeral=True
            )

        role_select.callback = role_callback
        view = discord.ui.View(timeout=120)
        view.add_item(role_select)

        await interaction.response.send_message(
            embed=E.base(f'👥  {label}',
                         f'Current: {current_str}\n\nSelect new roles:',
                         color=PURPLE),
            view=view,
            ephemeral=True
        )


class CategoryToggleView(discord.ui.View):
    def __init__(self, bot, categories: list[dict]):
        super().__init__(timeout=120)
        self.bot = bot
        options = [
            discord.SelectOption(
                label=c['name'],
                emoji='✅' if c['enabled'] else '❌',
                description=f'Currently {"enabled" if c["enabled"] else "disabled"}',
                value=c['name'],
            )
            for c in categories
        ]
        select = discord.ui.Select(
            placeholder='Select categories to toggle…',
            options=options,
            min_values=1,
            max_values=len(options),
            custom_id='admin:cat_toggle'
        )
        select.callback = self._toggled
        self.add_item(select)
        self._categories = categories

    async def _toggled(self, interaction: discord.Interaction):
        selected = set(interaction.data['values'])
        db = self.bot.db
        lines = []
        for cat in self._categories:
            name = cat['name']
            was_enabled = bool(cat['enabled'])
            should_enable = name in selected
            if was_enabled != should_enable:
                await db.toggle_category(interaction.guild_id, name, should_enable)
                status = '✅ Enabled' if should_enable else '❌ Disabled'
                lines.append(f'{status}: **{name}**')

        msg = '\n'.join(lines) if lines else 'No changes made.'
        await interaction.response.send_message(embed=E.success(msg), ephemeral=True)


class ResetConfirmView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=60)
        self.bot = bot

    @discord.ui.button(label='Confirm Reset', style=discord.ButtonStyle.danger, emoji='🔄')
    async def confirm(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await self.bot.db.reset_settings(interaction.guild_id)
        await interaction.response.send_message(
            embed=E.success('All settings have been reset to defaults.'), ephemeral=True)
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary, emoji='✖️')
    async def cancel(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base('✖️  Cancelled', 'Reset was cancelled.', color=0x95A5A6),
            ephemeral=True)
        self.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  COG
# ═══════════════════════════════════════════════════════════════════════════════

class AdminCog(commands.Cog, name='Admin'):
    def __init__(self, bot):
        self.bot = bot

    # ── /adminpanel ────────────────────────────────────────────────────────────
    @app_commands.command(name='adminpanel', description='Open the bot admin control panel.')
    async def adminpanel(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return

        await self.bot.db.setup_guild(interaction.guild_id)
        settings = await self.bot.db.get_settings(interaction.guild_id)
        categories = await self.bot.db.get_categories(interaction.guild_id)

        embed = E.admin_panel(interaction.guild, settings, categories)
        view = AdminPanelView(self.bot)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /blacklist ─────────────────────────────────────────────────────────────
    app_commands.command(name='blacklist', description='Blacklist a user from creating tickets.')
    @app_commands.describe(user='User to blacklist', reason='Reason for blacklisting')
    async def blacklist(self, interaction: discord.Interaction,
                        user: discord.Member, reason: str = ''):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await self.bot.db.blacklist_user(interaction.guild_id, user.id, reason)
        await interaction.response.send_message(
            embed=E.success(f'{user.mention} has been blacklisted.\nReason: {reason or "None"}'),
            ephemeral=True)

    # ── /unblacklist ───────────────────────────────────────────────────────────
    @app_commands.command(name='unblacklist', description='Remove a user from the blacklist.')
    @app_commands.describe(user='User to unblacklist')
    async def unblacklist(self, interaction: discord.Interaction, user: discord.Member):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await self.bot.db.unblacklist_user(interaction.guild_id, user.id)
        await interaction.response.send_message(
            embed=E.success(f'{user.mention} has been removed from the blacklist.'),
            ephemeral=True)

    # ── /setrole ───────────────────────────────────────────────────────────────
    @app_commands.command(name='setrole', description='Configure a role permission type.')
    @app_commands.describe(
        role_type='Type of permission',
        role='Role to assign',
    )
    @app_commands.choices(role_type=[
        app_commands.Choice(name='Support',    value='support_role_ids'),
        app_commands.Choice(name='Claim',      value='claim_role_ids'),
        app_commands.Choice(name='Close',      value='close_role_ids'),
        app_commands.Choice(name='Reopen',     value='reopen_role_ids'),
        app_commands.Choice(name='Transcript', value='transcript_role_ids'),
        app_commands.Choice(name='Admin',      value='admin_role_ids'),
    ])
    async def setrole(self, interaction: discord.Interaction,
                      role_type: str, role: discord.Role):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await self.bot.db.get_settings(interaction.guild_id) or {}
        await self.bot.db.setup_guild(interaction.guild_id)
        current = settings.get(role_type, [])
        if role.id not in current:
            current.append(role.id)
        await self.bot.db.update_setting(interaction.guild_id, role_type, current)
        label = role_type.replace('_role_ids', '').replace('_', ' ').title()
        await interaction.response.send_message(
            embed=E.success(f'{role.mention} added to **{label}** roles.'), ephemeral=True)

    # ── /removerole ────────────────────────────────────────────────────────────
    @app_commands.command(name='removerole', description='Remove a role from a permission type.')
    @app_commands.describe(role_type='Type of permission', role='Role to remove')
    @app_commands.choices(role_type=[
        app_commands.Choice(name='Support',    value='support_role_ids'),
        app_commands.Choice(name='Claim',      value='claim_role_ids'),
        app_commands.Choice(name='Close',      value='close_role_ids'),
        app_commands.Choice(name='Reopen',     value='reopen_role_ids'),
        app_commands.Choice(name='Transcript', value='transcript_role_ids'),
        app_commands.Choice(name='Admin',      value='admin_role_ids'),
    ])
    async def removerole(self, interaction: discord.Interaction,
                         role_type: str, role: discord.Role):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await self.bot.db.get_settings(interaction.guild_id) or {}
        current = [r for r in settings.get(role_type, []) if r != role.id]
        await self.bot.db.update_setting(interaction.guild_id, role_type, current)
        label = role_type.replace('_role_ids', '').replace('_', ' ').title()
        await interaction.response.send_message(
            embed=E.success(f'{role.mention} removed from **{label}** roles.'), ephemeral=True)

    # ── /settings ─────────────────────────────────────────────────────────────
    @app_commands.command(name='settings', description='View the current bot settings.')
    async def settings(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await self.bot.db.setup_guild(interaction.guild_id)
        settings = await self.bot.db.get_settings(interaction.guild_id)
        categories = await self.bot.db.get_categories(interaction.guild_id)
        embed = E.admin_panel(interaction.guild, settings, categories)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /botping ───────────────────────────────────────────────────────────────
    @app_commands.command(name='botping', description='(Admin/Owner only) Check the bot latency.')
    async def botping(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        latency = round(self.bot.latency * 1000)
        color = GREEN if latency < 100 else ORANGE if latency < 200 else RED
        e = E.base('🏓  Pong!', f'Latency: **{latency}ms**', color=color)
        await interaction.response.send_message(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
