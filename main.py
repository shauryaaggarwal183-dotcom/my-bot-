import os
import sys
import logging
import traceback
import discord
from discord.ext import commands
from database import Database

# =========================
# LOGGING SETUP
# =========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger("TicketBot")


# =========================
# BOT CLASS
# =========================
class TicketBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None
        )

        self.db = Database()
        self.tree.on_error = self.on_app_command_error

    # =========================
    # SETUP HOOK
    # =========================
    async def setup_hook(self):
        logger.info("Starting bot setup...")

        # DB INIT (SAFE)
        try:
            await self.db.init()
            logger.info("Database initialized.")
        except Exception:
            logger.error("DB INIT FAILED")
            logger.error(traceback.format_exc())

        # COGS
        cogs = [
            "cogs.tickets",
            "cogs.admin",
            "cogs.tier_test",
            "cogs.access",
            "cogs.ai_qa",
            "cogs.comp",
        ]

        for cog in cogs:
            try:
                await self.load_extension(cog)
                logger.info(f"Loaded cog: {cog}")
            except Exception:
                logger.error(f"FAILED TO LOAD COG: {cog}")
                logger.error(traceback.format_exc())

        # SYNC COMMANDS
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} slash commands.")
        except Exception:
            logger.error("Slash command sync failed")
            logger.error(traceback.format_exc())

    # =========================
    # READY EVENT
    # =========================
    async def on_ready(self):
        logger.info(f"Logged in as {self.user} ({self.user.id})")
        logger.info(f"Guilds: {len(self.guilds)}")

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="🎫 Tier Testing "
            ),
            status=discord.Status.online
        )

    # =========================
    # GUILD JOIN
    # =========================
    async def on_guild_join(self, guild: discord.Guild):
        try:
            await self.db.setup_guild(guild.id)
            logger.info(f"Joined guild: {guild.name}")
        except Exception:
            logger.error(traceback.format_exc())

    # =========================
    # ERROR HANDLER
    # =========================
    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception):
        logger.error("Slash Command Error:")
        logger.error("".join(traceback.format_exception(type(error), error, error.__traceback__)))

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Something went wrong running that command. Check bot logs.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "❌ Something went wrong running that command. Check bot logs.",
                    ephemeral=True
                )
        except:
            pass

    # =========================
    # SHUTDOWN
    # =========================
    async def close(self):
        await super().close()


# =========================
# MAIN
# =========================
def main():
    token = os.getenv("DISCORD_TOKEN")

    if not token:
        logger.critical("DISCORD_TOKEN missing!")
        sys.exit(1)

    bot = TicketBot()

    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.critical("Invalid token!")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Bot shutting down...")


if __name__ == "__main__":
    main()
