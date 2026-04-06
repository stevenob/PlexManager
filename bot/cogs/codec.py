"""H.265 codec checker — identifies movies not encoded in HEVC."""

from __future__ import annotations

import logging
import math
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from media.probe import ffprobe_available, probe_resolution

if TYPE_CHECKING:
    from bot.client import PlexManagerBot

logger = logging.getLogger(__name__)

COLOR_CODEC = 0x1ABC9C  # Teal
RESULTS_PER_PAGE = 10


class CodecCog(commands.Cog):
    """H.265 codec checker — find movies that could be re-encoded."""

    def __init__(self, bot: PlexManagerBot) -> None:
        self.bot = bot

    codec_group = app_commands.Group(
        name="codec", description="H.265 codec checker for movies"
    )

    @codec_group.command(name="list", description="Show movies not encoded in H.265")
    @app_commands.describe(page="Page number (default 1)")
    async def codec_list(
        self, interaction: discord.Interaction, page: Optional[int] = None
    ) -> None:
        await interaction.response.defer()

        movies = await self.bot.db.get_non_hevc_movies(limit=500)

        if not movies:
            await interaction.followup.send(
                "✅ All scanned movies are encoded in H.265!", ephemeral=True
            )
            return

        current_page = max(1, page or 1)
        total_pages = max(1, math.ceil(len(movies) / RESULTS_PER_PAGE))
        current_page = min(current_page, total_pages)
        start = (current_page - 1) * RESULTS_PER_PAGE
        page_items = movies[start : start + RESULTS_PER_PAGE]

        embed = discord.Embed(
            title="🎬 Non-H.265 Movies",
            description=f"Movies that could be re-encoded to H.265/HEVC",
            color=COLOR_CODEC,
        )

        for movie in page_items:
            year_str = f" ({movie.year})" if movie.year else ""
            embed.add_field(
                name=f"{movie.title}{year_str}",
                value=(
                    f"**Codec:** {movie.codec_label} · "
                    f"**Resolution:** {movie.resolution_label} · "
                    f"**Size:** {movie.human_size}"
                ),
                inline=False,
            )

        if page_items and page_items[0].poster_url:
            embed.set_thumbnail(url=page_items[0].poster_url)

        embed.set_footer(
            text=f"Page {current_page}/{total_pages} · {len(movies)} non-H.265 movie(s)"
        )
        await interaction.followup.send(embed=embed)

    @codec_group.command(name="status", description="Show codec breakdown for your library")
    async def codec_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        stats = await self.bot.db.get_codec_stats()
        unscanned = await self.bot.db.get_movies_without_codec(limit=10000)

        # Group codecs into readable categories
        hevc_count = stats.get("hevc", 0) + stats.get("h265", 0)
        h264_count = stats.get("h264", 0)
        other_codecs = {k: v for k, v in stats.items() if k not in ("hevc", "h265", "h264", "unscanned", None)}
        other_count = sum(other_codecs.values())
        unscanned_count = stats.get("unscanned", 0) + stats.get(None, 0)
        total = hevc_count + h264_count + other_count + unscanned_count

        embed = discord.Embed(
            title="📊 Codec Status",
            description=f"Codec breakdown for {total} movies",
            color=COLOR_CODEC,
        )
        embed.add_field(name="✅ H.265/HEVC", value=str(hevc_count), inline=True)
        embed.add_field(name="⚠️ H.264", value=str(h264_count), inline=True)
        embed.add_field(name="⚠️ Other", value=str(other_count), inline=True)

        if other_codecs:
            other_detail = ", ".join(f"{k}: {v}" for k, v in sorted(other_codecs.items(), key=lambda x: -x[1]))
            embed.add_field(name="Other Codecs", value=other_detail, inline=False)

        ffprobe_status = "✅ Available" if ffprobe_available() else "⚠️ Not installed"
        embed.set_footer(text=f"{unscanned_count} unscanned · ffprobe: {ffprobe_status}")
        await interaction.followup.send(embed=embed)

    @codec_group.command(name="check", description="Check a specific movie's codec")
    @app_commands.describe(title="Title of the movie to check")
    async def codec_check(
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
            await interaction.followup.send(
                f"No movie found matching '{title}'", ephemeral=True
            )
            return

        # If codec not yet probed, probe it now
        if movie.video_codec is None and ffprobe_available():
            resolution = await probe_resolution(movie.path)
            if resolution and resolution.codec:
                await self.bot.db.update_codec(movie.path, resolution.codec)
                movie.video_codec = resolution.codec
                if resolution.width and resolution.height:
                    await self.bot.db.update_resolution(movie.path, resolution.width, resolution.height)
                    movie.resolution_width = resolution.width
                    movie.resolution_height = resolution.height

        year_str = f" ({movie.year})" if movie.year else ""
        status_icon = "✅" if movie.is_hevc else "⚠️"
        embed = discord.Embed(
            title=f"{status_icon} {movie.title}{year_str}",
            description=f"{'H.265/HEVC — optimal' if movie.is_hevc else 'Not H.265 — could be re-encoded'}",
            color=0x2ECC71 if movie.is_hevc else 0xE67E22,
        )

        if movie.poster_url:
            embed.set_thumbnail(url=movie.poster_url)

        embed.add_field(name="🎞️ Codec", value=movie.codec_label, inline=True)
        embed.add_field(name="📺 Resolution", value=movie.resolution_label, inline=True)
        embed.add_field(name="💾 Size", value=movie.human_size, inline=True)
        embed.set_footer(text=f"File: {movie.filename}")
        await interaction.followup.send(embed=embed)

    @codec_group.command(
        name="rescan", description="Probe codec for movies not yet scanned"
    )
    async def codec_rescan(self, interaction: discord.Interaction) -> None:
        if not ffprobe_available():
            await interaction.response.send_message(
                "⚠️ `ffprobe` is not installed. Install ffmpeg to enable codec detection.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        movies = await self.bot.db.get_movies_without_codec(limit=1000)

        if not movies:
            await interaction.followup.send(
                "✅ All movies have been scanned for codec!", ephemeral=True
            )
            return

        stats = {"probed": 0, "hevc": 0, "non_hevc": 0, "failed": 0, "total": len(movies)}

        for movie in movies:
            try:
                resolution = await probe_resolution(movie.path)
                if resolution and resolution.codec:
                    await self.bot.db.update_codec(movie.path, resolution.codec)
                    if resolution.width and resolution.height:
                        await self.bot.db.update_resolution(movie.path, resolution.width, resolution.height)
                    stats["probed"] += 1
                    if resolution.is_hevc:
                        stats["hevc"] += 1
                    else:
                        stats["non_hevc"] += 1
                else:
                    stats["failed"] += 1
            except Exception:
                logger.exception("Codec probe failed for '%s'", movie.path)
                stats["failed"] += 1

        embed = discord.Embed(
            title="🎞️ Codec Scan Complete",
            description=f"Scanned {stats['probed']} of {stats['total']} movies",
            color=COLOR_CODEC,
        )
        embed.add_field(name="✅ H.265", value=str(stats["hevc"]), inline=True)
        embed.add_field(name="⚠️ Non-H.265", value=str(stats["non_hevc"]), inline=True)
        embed.add_field(name="❌ Failed", value=str(stats["failed"]), inline=True)

        await interaction.followup.send(embed=embed)

        # Notify about non-H.265 count in upgrade channel
        if stats["non_hevc"] > 0:
            channel_id = Config.UPGRADE_CHANNEL_ID or Config.NOTIFICATION_CHANNEL_ID
            channel = self.bot.get_channel(channel_id)
            if channel:
                notify = discord.Embed(
                    title="🎞️ Codec Scan Results",
                    description=f"Found **{stats['non_hevc']}** movies not encoded in H.265",
                    color=0xE67E22,
                )
                notify.set_footer(text="Use /codec list to view them")
                try:
                    await channel.send(embed=notify)
                except discord.HTTPException:
                    logger.exception("Failed to send codec notification")


async def setup(bot: PlexManagerBot) -> None:
    await bot.add_cog(CodecCog(bot))
