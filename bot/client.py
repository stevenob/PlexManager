from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from config import Config

logger = logging.getLogger(__name__)

COG_MODULES = ["bot.cogs.media", "bot.cogs.library", "bot.cogs.notifications", "bot.cogs.upgrades", "bot.cogs.encode", "bot.cogs.subtitles"]


class PlexManagerBot(commands.Bot):
    def __init__(self, notification_channel_id: int) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self._notification_channel_id = notification_channel_id

    @property
    def notification_channel(self) -> discord.TextChannel | None:
        channel = self.get_channel(self._notification_channel_id)
        if channel is None:
            logger.warning(
                "Notification channel %s not found", self._notification_channel_id
            )
        return channel  # type: ignore[return-value]

    async def setup_hook(self) -> None:
        for module in COG_MODULES:
            try:
                await self.load_extension(module)
                logger.info("Loaded cog: %s", module)
            except Exception:
                logger.exception("Failed to load cog %s", module)
                raise

        # Sync to specific guild for instant command availability.
        # Clear any stale global commands to avoid duplicates.
        guild_id = Config.GUILD_ID
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            # Clear global commands to prevent duplicates
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            logger.info("Slash command tree synced to guild %s", guild_id)
        else:
            await self.tree.sync()
            logger.info("Slash command tree synced globally")

    async def on_ready(self) -> None:
        logger.info(
            "Bot connected as %s in %d guild(s)", self.user, len(self.guilds)
        )

    async def send_notification(self, embed: discord.Embed) -> None:
        channel = self.notification_channel
        if channel is None:
            logger.error("Cannot send notification — channel not available")
            return
        try:
            await channel.send(embed=embed)
            logger.debug("Notification sent to #%s", channel.name)
        except discord.HTTPException:
            logger.exception("Failed to send notification to #%s", channel.name)
