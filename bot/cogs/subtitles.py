"""Subtitle manager — extract embedded and download missing subtitles."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from services.subtitles import (
    OpenSubtitlesClient,
    extract_subtitles,
    has_external_srt,
    probe_subtitles,
)

if TYPE_CHECKING:
    from bot.client import PlexManagerBot

logger = logging.getLogger(__name__)

COLOR_SUBS = 0x3498DB  # Blue


class SubtitlesCog(commands.Cog):
    """Subtitle manager — extract and download subtitles."""

    def __init__(self, bot: PlexManagerBot) -> None:
        self.bot = bot
        self._opensubs = OpenSubtitlesClient(
            Config.OPENSUBTITLES_API_KEY,
            Config.OPENSUBTITLES_USERNAME,
            Config.OPENSUBTITLES_PASSWORD,
        )

    async def cog_unload(self) -> None:
        await self._opensubs.close()

    @app_commands.command(
        name="subsextract",
        description="Extract embedded subtitles from all movies to .srt files",
    )
    async def subs_extract(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        movies = await self.bot.db.get_movies_without_subs(limit=1000)
        if not movies:
            await interaction.followup.send("✅ All movies already have subtitles!", ephemeral=True)
            return

        stats = {"extracted": 0, "skipped": 0, "total": len(movies)}

        for movie in movies:
            try:
                existing = has_external_srt(movie.path)
                if existing:
                    await self.bot.db.update_subs_status(movie.path, True)
                    stats["skipped"] += 1
                    continue

                extracted = await extract_subtitles(movie.path)
                if extracted:
                    await self.bot.db.update_subs_status(movie.path, True)
                    stats["extracted"] += 1
                else:
                    stats["skipped"] += 1
            except Exception:
                logger.exception("Subtitle extraction failed for %s", movie.path)

        embed = discord.Embed(
            title="💬 Subtitle Extraction Complete",
            description=f"Processed {stats['total']} movies",
            color=COLOR_SUBS,
        )
        embed.add_field(name="✅ Extracted", value=str(stats["extracted"]), inline=True)
        embed.add_field(name="⏭️ Skipped", value=str(stats["skipped"]), inline=True)
        embed.add_field(name="📊 Total", value=str(stats["total"]), inline=True)
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="subsdownload",
        description="Download missing subtitles from OpenSubtitles",
    )
    async def subs_download(self, interaction: discord.Interaction) -> None:
        if not Config.opensubtitles_configured():
            await interaction.response.send_message(
                "⚠️ OpenSubtitles API not configured. Add `OPENSUBTITLES_API_KEY` to your `.env` file.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # Login for higher limits
        await self._opensubs.login()

        movies = await self.bot.db.get_movies_without_subs(limit=500)
        if not movies:
            await interaction.followup.send("✅ All movies already have subtitles!", ephemeral=True)
            return

        stats = {"downloaded": 0, "not_found": 0, "limit_hit": False, "total": len(movies)}

        for movie in movies:
            # Check if already has subs (may have been extracted earlier)
            existing = has_external_srt(movie.path)
            if existing:
                await self.bot.db.update_subs_status(movie.path, True)
                continue

            try:
                downloaded = await self._opensubs.download_for_movie(
                    movie.path, movie.title or "", movie.year
                )
                if downloaded:
                    await self.bot.db.update_subs_status(movie.path, True)
                    stats["downloaded"] += 1
                else:
                    stats["not_found"] += 1
            except Exception:
                logger.exception("Subtitle download failed for %s", movie.title)

            if self._opensubs._downloads_today >= self._opensubs._max_downloads:
                stats["limit_hit"] = True
                break

        embed = discord.Embed(
            title="💬 Subtitle Download Complete",
            description=f"Searched OpenSubtitles for {stats['total']} movies",
            color=COLOR_SUBS,
        )
        embed.add_field(name="✅ Downloaded", value=str(stats["downloaded"]), inline=True)
        embed.add_field(name="❌ Not Found", value=str(stats["not_found"]), inline=True)
        if stats["limit_hit"]:
            embed.add_field(name="⚠️ Limit", value="Daily limit reached (20/day)", inline=False)
        embed.set_footer(text="English + Spanish · OpenSubtitles.com")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="substatus", description="Show subtitle status for your library")
    async def subs_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        stats = await self.bot.db.get_subs_stats()

        embed = discord.Embed(title="💬 Subtitle Status", color=COLOR_SUBS)
        embed.add_field(name="✅ With Subs", value=str(stats.get("with_subs", 0)), inline=True)
        embed.add_field(name="❌ Without Subs", value=str(stats.get("without_subs", 0)), inline=True)
        embed.add_field(name="📊 Total Movies", value=str(stats.get("total", 0)), inline=True)

        opensubs_status = "✅ Configured" if Config.opensubtitles_configured() else "⚠️ Not configured"
        embed.set_footer(text=f"OpenSubtitles: {opensubs_status}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="subcheck", description="Check subtitle status for a specific movie")
    @app_commands.describe(title="Title of the movie to check")
    async def subs_check(
        self, interaction: discord.Interaction, title: str
    ) -> None:
        await interaction.response.defer()

        results = await self.bot.db.search(title, limit=5)
        movie = None
        for m in results:
            if m.title and m.title.lower() == title.lower():
                movie = m
                break
        if movie is None and results:
            movie = results[0]

        if movie is None:
            await interaction.followup.send(f"No movie found matching '{title}'", ephemeral=True)
            return

        # Check for external .srt files
        existing = has_external_srt(movie.path)
        # Check for embedded subs
        embedded = await probe_subtitles(movie.path)
        text_embedded = [t for t in embedded if t.is_text]
        bitmap_embedded = [t for t in embedded if not t.is_text]

        year_str = f" ({movie.year})" if movie.year else ""
        has_srt = len(existing) > 0
        embed = discord.Embed(
            title=f"{'✅' if has_srt else '❌'} {movie.title}{year_str}",
            description="Has external subtitles" if has_srt else "No external subtitles",
            color=0x2ECC71 if has_srt else 0xE67E22,
        )

        if movie.poster_url:
            embed.set_thumbnail(url=movie.poster_url)

        if existing:
            srt_names = [os.path.basename(s) for s in existing]
            embed.add_field(name="📄 SRT Files", value="\n".join(srt_names), inline=False)

        if text_embedded:
            embed.add_field(
                name="💬 Embedded Text Subs",
                value=", ".join(f"{t.language} ({t.codec})" for t in text_embedded),
                inline=False,
            )

        if bitmap_embedded:
            embed.add_field(
                name="🖼️ Embedded Bitmap Subs",
                value=", ".join(f"{t.language} ({t.codec})" for t in bitmap_embedded) + "\n*(cannot extract to .srt)*",
                inline=False,
            )

        if not existing and not embedded:
            embed.add_field(name="ℹ️", value="No subtitles found (embedded or external)", inline=False)

        embed.set_footer(text=f"File: {movie.filename}")
        await interaction.followup.send(embed=embed)


async def setup(bot: PlexManagerBot) -> None:
    await bot.add_cog(SubtitlesCog(bot))
