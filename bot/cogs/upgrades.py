"""DVD upgrade tracker — identifies low-res (DVD quality) movies in your library."""

from __future__ import annotations

import logging
import math
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.client import PlexManagerBot

logger = logging.getLogger(__name__)

COLOR_UPGRADE = 0x9B59B6
RESULTS_PER_PAGE = 10


class UpgradesCog(commands.Cog):
    """DVD upgrade tracker — find movies that could be upgraded to Blu-ray."""

    def __init__(self, bot: PlexManagerBot) -> None:
        self.bot = bot

    @app_commands.command(name="lowres", description="Show DVD-quality movies ranked by rating")
    @app_commands.describe(page="Page number (default 1)")
    async def lowres(
        self, interaction: discord.Interaction, page: Optional[int] = None
    ) -> None:
        await interaction.response.defer()

        movies = await self.bot.db.get_low_res_movies(max_height=480, limit=500, sort="rating")

        if not movies:
            await interaction.followup.send(
                "✅ No DVD-quality movies found! All your movies are 720p or higher.",
                ephemeral=True,
            )
            return

        current_page = max(1, page or 1)
        total_pages = max(1, math.ceil(len(movies) / RESULTS_PER_PAGE))
        current_page = min(current_page, total_pages)
        start = (current_page - 1) * RESULTS_PER_PAGE
        page_items = movies[start : start + RESULTS_PER_PAGE]

        embed = discord.Embed(
            title="📀 DVD-Quality Movies",
            description="Ranked by TMDb rating — best upgrades first",
            color=COLOR_UPGRADE,
        )

        for movie in page_items:
            year_str = f" ({movie.year})" if movie.year else ""
            rating_str = f"⭐ {movie.rating:.1f}" if movie.rating else "No rating"
            embed.add_field(
                name=f"📀 {movie.title}{year_str}",
                value=(
                    f"**{rating_str}** · "
                    f"{movie.resolution_label} · "
                    f"{movie.codec_label} · "
                    f"{movie.human_size}"
                ),
                inline=False,
            )

        if page_items and page_items[0].poster_url:
            embed.set_thumbnail(url=page_items[0].poster_url)

        embed.set_footer(
            text=f"Page {current_page}/{total_pages} · {len(movies)} DVD-quality movie(s)"
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="unmatched", description="Show movies that failed TMDb lookup")
    async def unmatched(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        movies = await self.bot.db.get_unmatched_movies(limit=50)

        if not movies:
            await interaction.followup.send(
                "✅ All movies have TMDb matches!", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="⚠️ Unmatched Movies",
            description="These movies couldn't be found on TMDb. Check naming and fix if needed.",
            color=discord.Color.orange(),
        )

        for movie in movies[:25]:
            embed.add_field(
                name=movie.title or movie.filename,
                value=(
                    f"**File:** `{movie.filename}`\n"
                    f"**Resolution:** {movie.resolution_label} · "
                    f"**Size:** {movie.human_size}"
                ),
                inline=False,
            )

        embed.set_footer(text=f"{len(movies)} unmatched movie(s)")
        await interaction.followup.send(embed=embed)


async def setup(bot: PlexManagerBot) -> None:
    await bot.add_cog(UpgradesCog(bot))
