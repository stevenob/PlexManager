"""DVD upgrade tracker — identifies low-res (DVD quality) movies in your library."""

from __future__ import annotations

import logging
import math
import urllib.parse
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.client import PlexManagerBot

logger = logging.getLogger(__name__)

COLOR_UPGRADE = 0x9B59B6
RESULTS_PER_PAGE = 10


class PurchasedButton(discord.ui.Button):
    def __init__(self, path: str, title: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.success,
            label="✅ Purchased",
            custom_id=f"purchased:{hash(path) % 100000}",
        )
        self.path = path
        self.title = title

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        await bot.db.set_upgrade_status(path=self.path, status="purchased", title=self.title)
        await interaction.response.send_message(
            f"✅ **{self.title}** marked as purchased!", ephemeral=True
        )
        if self.view:
            for item in self.view.children:
                item.disabled = True
            await interaction.message.edit(view=self.view)


class IgnoreButton(discord.ui.Button):
    def __init__(self, path: str, title: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="⏭️ Ignore",
            custom_id=f"ignore:{hash(path) % 100000}",
        )
        self.path = path
        self.title = title

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        await bot.db.set_upgrade_status(path=self.path, status="ignored", title=self.title)
        await interaction.response.send_message(
            f"⏭️ **{self.title}** ignored.", ephemeral=True
        )
        if self.view:
            for item in self.view.children:
                item.disabled = True
            await interaction.message.edit(view=self.view)


class LowResView(discord.ui.View):
    def __init__(self, path: str, title: str) -> None:
        super().__init__(timeout=None)
        self.add_item(PurchasedButton(path, title))
        self.add_item(IgnoreButton(path, title))


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

        all_movies = await self.bot.db.get_low_res_movies(max_height=480, limit=500, sort="rating")

        # Filter out ignored/purchased
        excluded = set()
        for movie in all_movies:
            status = await self.bot.db.get_upgrade_status(movie.path)
            if status in ("ignored", "purchased"):
                excluded.add(movie.path)
        movies = [m for m in all_movies if m.path not in excluded]

        if not movies:
            await interaction.followup.send(
                "✅ No DVD-quality movies need upgrading!",
                ephemeral=True,
            )
            return

        current_page = max(1, page or 1)
        total_pages = max(1, math.ceil(len(movies) / RESULTS_PER_PAGE))
        current_page = min(current_page, total_pages)
        start = (current_page - 1) * RESULTS_PER_PAGE
        page_items = movies[start : start + RESULTS_PER_PAGE]

        # Send each movie as a separate embed with buttons
        for movie in page_items:
            year_str = f" ({movie.year})" if movie.year else ""
            rating_str = f"⭐ {movie.rating:.1f}" if movie.rating else "No rating"
            q = urllib.parse.quote(f"{movie.title} Blu-ray")
            links = (
                f"[eBay](https://www.ebay.com/sch/i.html?_nkw={q}&_sacat=617) · "
                f"[Amazon](https://www.amazon.com/s?k={q}&i=movies-tv) · "
                f"[Hamilton](https://www.hamiltonbook.com/searchresult?qs={q}) · "
                f"[Gruv](https://www.gruv.com/search?q={q})"
            )
            embed = discord.Embed(
                title=f"📀 {movie.title}{year_str}",
                description=f"{rating_str} · {movie.resolution_label}\n{links}",
                color=COLOR_UPGRADE,
            )
            if movie.poster_url:
                embed.set_thumbnail(url=movie.poster_url)

            view = LowResView(path=movie.path, title=movie.title or movie.filename)
            await interaction.followup.send(embed=embed, view=view)

        # Summary at the end
        await interaction.followup.send(
            f"📋 Page {current_page}/{total_pages} · {len(movies)} DVD movies remaining · {len(excluded)} ignored/purchased"
        )

    @app_commands.command(name="ignore", description="Exclude a movie from upgrade list")
    @app_commands.describe(title="Title of the movie to ignore")
    async def ignore(self, interaction: discord.Interaction, title: str) -> None:
        results = await self.bot.db.search(title, limit=5)
        movie = None
        for m in results:
            if m.title and m.title.lower() == title.lower():
                movie = m
                break
        if movie is None and results:
            movie = results[0]
        if movie is None:
            await interaction.response.send_message(f"No movie found matching '{title}'", ephemeral=True)
            return
        await self.bot.db.set_upgrade_status(path=movie.path, status="ignored", title=movie.title, year=movie.year)
        await interaction.response.send_message(f"⏭️ **{movie.title}** will be ignored in future upgrade lists.")

    @app_commands.command(name="purchased", description="Mark a movie as purchased (Blu-ray bought)")
    @app_commands.describe(title="Title of the movie you purchased")
    async def purchased(self, interaction: discord.Interaction, title: str) -> None:
        results = await self.bot.db.search(title, limit=5)
        movie = None
        for m in results:
            if m.title and m.title.lower() == title.lower():
                movie = m
                break
        if movie is None and results:
            movie = results[0]
        if movie is None:
            await interaction.response.send_message(f"No movie found matching '{title}'", ephemeral=True)
            return
        await self.bot.db.set_upgrade_status(path=movie.path, status="purchased", title=movie.title, year=movie.year)
        await interaction.response.send_message(f"✅ **{movie.title}** marked as purchased!")

    @app_commands.command(name="unmatched", description="Show movies that failed TMDb lookup")
    async def unmatched(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        movies = await self.bot.db.get_unmatched_movies(limit=50)
        if not movies:
            await interaction.followup.send("✅ All movies have TMDb matches!", ephemeral=True)
            return
        embed = discord.Embed(
            title="⚠️ Unmatched Movies",
            description="These movies couldn't be found on TMDb. Check naming and fix if needed.",
            color=discord.Color.orange(),
        )
        for movie in movies[:25]:
            embed.add_field(
                name=movie.title or movie.filename,
                value=f"**File:** `{movie.filename}`\n**Resolution:** {movie.resolution_label} · **Size:** {movie.human_size}",
                inline=False,
            )
        embed.set_footer(text=f"{len(movies)} unmatched movie(s)")
        await interaction.followup.send(embed=embed)


async def setup(bot: PlexManagerBot) -> None:
    await bot.add_cog(UpgradesCog(bot))
