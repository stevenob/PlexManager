from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.client import PlexManagerBot

logger = logging.getLogger(__name__)


class LibraryCog(commands.Cog):
    """Commands for library management and bot status."""

    def __init__(self, bot: PlexManagerBot) -> None:
        self.bot = bot
        self._started_at = datetime.now(timezone.utc)

    @app_commands.command(name="rescan", description="Trigger a manual library rescan")
    async def rescan(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        try:
            result = await self.bot.scanner.scan()
            added = result.get("added", 0)
            removed = result.get("removed", 0)
            total = result.get("total", 0)
            await interaction.followup.send(
                f"✅ Scan complete: **{added}** added, **{removed}** removed, **{total}** total"
            )
        except Exception:
            logger.exception("Library rescan failed")
            await interaction.followup.send(
                "❌ Scan failed — check bot logs for details.", ephemeral=True
            )

    @app_commands.command(name="status", description="Show bot status information")
    async def status(self, interaction: discord.Interaction) -> None:
        uptime = discord.utils.format_dt(self._started_at, style="R")
        guild_count = len(self.bot.guilds)

        media_path = ", ".join(getattr(self.bot, "media_paths", [])) or "N/A"

        embed = discord.Embed(title="🤖 Bot Status", color=discord.Color.teal())
        embed.add_field(name="Uptime", value=f"Started {uptime}", inline=True)
        embed.add_field(name="Guilds", value=str(guild_count), inline=True)
        embed.add_field(name="Media Path", value=f"`{media_path}`", inline=False)

        try:
            stats: dict = await self.bot.db.get_stats()
            total_items = (
                stats.get("total_movies", 0)
                + stats.get("total_episodes", 0)
            )
            embed.add_field(
                name="Database",
                value=(
                    f"**{total_items}** media files "
                    f"({stats.get('total_movies', 0)} movies, "
                    f"{stats.get('total_episodes', 0)} episodes)"
                ),
                inline=False,
            )
        except Exception:
            embed.add_field(name="Database", value="⚠️ Unavailable", inline=False)

        await interaction.response.send_message(embed=embed)


async def setup(bot: PlexManagerBot) -> None:
    await bot.add_cog(LibraryCog(bot))
