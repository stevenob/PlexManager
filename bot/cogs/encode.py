"""H.265 re-encoding queue — encode non-H.265 movies using HandBrakeCLI."""

from __future__ import annotations

import logging
import math
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from services.handbrake import HandBrakeEncoder, auto_preset, EncodeProgress

if TYPE_CHECKING:
    from bot.client import PlexManagerBot

logger = logging.getLogger(__name__)

COLOR_ENCODE = 0x9B59B6  # Purple
RESULTS_PER_PAGE = 10


def _human_bytes(size: int) -> str:
    if size == 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    s = float(size)
    while s >= 1024 and i < len(units) - 1:
        s /= 1024
        i += 1
    return f"{s:.1f} {units[i]}"


class EncodeButton(discord.ui.Button):
    """Button to queue a movie for H.265 encoding."""

    def __init__(self, path: str, title: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="🔄 Queue for Encode",
            custom_id=f"encode_queue:{path[:80]}",
        )
        self.path = path
        self.title = title

    async def callback(self, interaction: discord.Interaction) -> None:
        from services.handbrake import auto_preset
        bot = interaction.client
        media = await bot.db.get_media(self.path)
        if media is None:
            await interaction.response.send_message("Movie not found.", ephemeral=True)
            return
        preset = auto_preset(media.resolution_width, media.resolution_height)
        added = await bot.db.add_to_encode_queue(
            path=media.path,
            title=media.title,
            year=media.year,
            preset=preset,
            original_codec=media.video_codec,
            original_size=media.size,
        )
        if added:
            await interaction.response.send_message(
                f"🔄 **{self.title}** queued for H.265 encoding!", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"**{self.title}** is already in the encode queue.", ephemeral=True
            )
        # Disable button
        if self.view:
            for item in self.view.children:
                item.disabled = True
            await interaction.message.edit(view=self.view)


class EncodeView(discord.ui.View):
    """View with an encode queue button."""

    def __init__(self, path: str, title: str) -> None:
        super().__init__(timeout=None)
        self.add_item(EncodeButton(path, title))


class EncodeCog(commands.Cog):
    """H.265 re-encoding queue — process movies through HandBrakeCLI."""

    def __init__(self, bot: PlexManagerBot) -> None:
        self.bot = bot
        self._encoder = HandBrakeEncoder(Config.HANDBRAKE_PATH)
        self._encoding = False
        self._current_title: str | None = None
        self._current_progress = EncodeProgress()
        self._worker_loop.start()

    def cog_unload(self) -> None:
        self._worker_loop.cancel()

    @app_commands.command(name="encode", description="Queue a movie for H.265 re-encoding")
    @app_commands.describe(title="Title of the movie to encode")
    async def encode_add(
        self, interaction: discord.Interaction, title: str
    ) -> None:
        if not self._encoder.is_available:
            await interaction.response.send_message(
                "⚠️ HandBrakeCLI not found. Check `HANDBRAKE_PATH` in your config.",
                ephemeral=True,
            )
            return

        results = await self.bot.db.search(title, limit=5)
        movie = None
        for m in results:
            if m.title and m.title.lower() == title.lower():
                movie = m
                break
        if movie is None and results:
            movie = results[0]

        if movie is None:
            await interaction.response.send_message(
                f"No movie found matching '{title}'", ephemeral=True
            )
            return

        if movie.is_hevc:
            await interaction.response.send_message(
                f"✅ **{movie.title}** is already encoded in H.265!", ephemeral=True
            )
            return

        skip_codecs = ("h264", "avc", "av1")
        if movie.video_codec and movie.video_codec.lower() in skip_codecs:
            await interaction.response.send_message(
                f"⏭️ **{movie.title}** is {movie.codec_label} — re-encoding would be lossy. "
                f"Only MPEG-2/VC-1 movies are eligible.",
                ephemeral=True,
            )
            return

        preset = auto_preset(movie.resolution_width, movie.resolution_height)
        added = await self.bot.db.add_to_encode_queue(
            path=movie.path,
            title=movie.title,
            year=movie.year,
            preset=preset,
            original_codec=movie.video_codec,
            original_size=movie.size,
        )

        if not added:
            await interaction.response.send_message(
                f"**{movie.title}** is already in the encode queue.", ephemeral=True
            )
            return

        year_str = f" ({movie.year})" if movie.year else ""
        embed = discord.Embed(
            title=f"🔄 {movie.title}{year_str}",
            description=f"Queued for H.265 re-encoding",
            color=COLOR_ENCODE,
        )
        if movie.poster_url:
            embed.set_thumbnail(url=movie.poster_url)
        embed.add_field(name="🎞️ Current Codec", value=movie.codec_label, inline=True)
        embed.add_field(name="📺 Resolution", value=movie.resolution_label, inline=True)
        embed.add_field(name="💾 Size", value=movie.human_size, inline=True)
        embed.set_footer(text=f"Preset: {preset}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="encodeall", description="Queue all MPEG-2 and VC-1 movies for H.265 encoding")
    async def encode_all(self, interaction: discord.Interaction) -> None:
        if not self._encoder.is_available:
            await interaction.response.send_message(
                "⚠️ HandBrakeCLI not found.", ephemeral=True
            )
            return

        await interaction.response.defer()

        movies = await self.bot.db.get_encodable_movies(limit=500)
        if not movies:
            await interaction.followup.send(
                "✅ No movies need re-encoding!", ephemeral=True
            )
            return

        added = 0
        skipped = 0
        for movie in movies:
            preset = auto_preset(movie.resolution_width, movie.resolution_height)
            ok = await self.bot.db.add_to_encode_queue(
                path=movie.path,
                title=movie.title,
                year=movie.year,
                preset=preset,
                original_codec=movie.video_codec,
                original_size=movie.size,
            )
            if ok:
                added += 1
            else:
                skipped += 1

        embed = discord.Embed(
            title="🔄 Encode Queue Updated",
            description=f"Queued **{added}** movies for H.265 re-encoding",
            color=COLOR_ENCODE,
        )
        embed.add_field(name="✅ Added", value=str(added), inline=True)
        embed.add_field(name="⏭️ Already Queued", value=str(skipped), inline=True)
        embed.add_field(name="📊 Total Non-H.265", value=str(len(movies)), inline=True)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="queue", description="Show the encode queue")
    @app_commands.describe(page="Page number (default 1)")
    async def encode_queue(
        self, interaction: discord.Interaction, page: Optional[int] = None
    ) -> None:
        await interaction.response.defer()

        jobs = await self.bot.db.get_encode_queue(limit=200)
        if not jobs:
            await interaction.followup.send(
                "📭 Encode queue is empty.", ephemeral=True
            )
            return

        current_page = max(1, page or 1)
        total_pages = max(1, math.ceil(len(jobs) / RESULTS_PER_PAGE))
        current_page = min(current_page, total_pages)
        start = (current_page - 1) * RESULTS_PER_PAGE
        page_items = jobs[start : start + RESULTS_PER_PAGE]

        embed = discord.Embed(
            title="🔄 Encode Queue",
            color=COLOR_ENCODE,
        )

        status_icons = {
            "queued": "⏳",
            "encoding": "🔄",
            "done": "✅",
            "failed": "❌",
            "cancelled": "⏭️",
        }

        lines = []
        for job in page_items:
            icon = status_icons.get(job["status"], "❓")
            title = job.get("title") or "Unknown"
            year = f" ({job['year']})" if job.get("year") else ""
            extra = ""
            if job["status"] == "encoding":
                extra = f" — {job.get('progress', 0)}%"
            elif job["status"] == "done" and job.get("original_size") and job.get("encoded_size"):
                savings = job["original_size"] - job["encoded_size"]
                extra = f" — saved {_human_bytes(savings)}"
            elif job["status"] == "failed":
                extra = f" — {job.get('error', '')[:40]}"
            lines.append(f"{icon} **{title}**{year}{extra}")

        embed.description = "\n".join(lines)
        embed.set_footer(
            text=f"Page {current_page}/{total_pages} · {len(jobs)} job(s)"
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="cancel", description="Remove a movie from the encode queue")
    @app_commands.describe(title="Title of the movie to remove")
    async def encode_cancel(
        self, interaction: discord.Interaction, title: str
    ) -> None:
        # Find the job by title
        jobs = await self.bot.db.get_encode_queue(status="queued", limit=500)
        job = None
        for j in jobs:
            if j.get("title", "").lower() == title.lower():
                job = j
                break
        if job is None:
            # Fuzzy match
            for j in jobs:
                if title.lower() in j.get("title", "").lower():
                    job = j
                    break

        if job is None:
            await interaction.response.send_message(
                f"No queued job found matching '{title}'", ephemeral=True
            )
            return

        removed = await self.bot.db.remove_from_encode_queue(job["path"])
        if removed:
            await interaction.response.send_message(
                f"⏭️ **{job['title']}** removed from encode queue."
            )
        else:
            await interaction.response.send_message(
                f"Couldn't remove **{job['title']}** — it may already be encoding.",
                ephemeral=True,
            )

    # ── Background encode worker ─────────────────────────────────────

    @tasks.loop(seconds=30)
    async def _worker_loop(self) -> None:
        """Process the next queued encode job."""
        if self._encoding:
            return
        if not self._encoder.is_available:
            return

        job = await self.bot.db.get_next_encode_job()
        if job is None:
            return

        self._encoding = True
        self._current_title = job.get("title", "Unknown")
        self._current_progress = EncodeProgress()

        path = job["path"]
        title = job.get("title", "Unknown")
        year = job.get("year")
        year_str = f" ({year})" if year else ""

        logger.info("Starting encode: %s", title)
        await self.bot.db.update_encode_status(path, "encoding")

        # Notify start
        channel = self.bot.get_channel(Config.NOTIFICATION_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title=f"🔄 {title}{year_str}",
                description="H.265 re-encoding started",
                color=COLOR_ENCODE,
            )
            embed.add_field(name="📦 Preset", value=job.get("preset", "Auto"), inline=True)
            embed.add_field(name="💾 Original Size", value=_human_bytes(job.get("original_size", 0)), inline=True)
            embed.add_field(name="🎞️ Codec", value=job.get("original_codec", "Unknown").upper(), inline=True)
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

        # Get resolution from DB
        media = await self.bot.db.get_media(path)
        width = media.resolution_width if media else None
        height = media.resolution_height if media else None

        def _on_progress(progress: EncodeProgress) -> None:
            self._current_progress = progress

        try:
            result = await self._encoder.encode_movie(
                input_path=path,
                width=width,
                height=height,
                progress_callback=_on_progress,
            )

            if result.success:
                # Remove from queue on success
                await self.bot.db.remove_encode_done(path)
                # Update the media file's codec in the DB
                await self.bot.db.update_codec(path, "hevc")

                savings = result.original_size - result.encoded_size

                if channel:
                    embed = discord.Embed(
                        title=f"✅ {title}{year_str}",
                        description="H.265 re-encoding complete",
                        color=0x2ECC71,
                    )
                    embed.add_field(name="💾 Original", value=_human_bytes(result.original_size), inline=True)
                    embed.add_field(name="💾 Encoded", value=_human_bytes(result.encoded_size), inline=True)
                    embed.add_field(name="📉 Saved", value=f"{_human_bytes(savings)} ({result.savings_percent:.1f}%)", inline=True)
                    try:
                        await channel.send(embed=embed)
                    except discord.HTTPException:
                        pass

                logger.info("Encode complete: %s (saved %s)", title, _human_bytes(savings))
            else:
                await self.bot.db.update_encode_status(
                    path, "failed", error=result.error
                )
                if channel:
                    embed = discord.Embed(
                        title=f"❌ {title}{year_str}",
                        description=f"Encode failed: {result.error}",
                        color=0xE74C3C,
                    )
                    try:
                        await channel.send(embed=embed)
                    except discord.HTTPException:
                        pass

                logger.error("Encode failed: %s — %s", title, result.error)

        except Exception as exc:
            logger.exception("Encode error: %s", title)
            await self.bot.db.update_encode_status(
                path, "failed", error=str(exc)[:200]
            )
        finally:
            self._encoding = False
            self._current_title = None
            self._current_progress = EncodeProgress()

    @_worker_loop.before_loop
    async def _before_worker(self) -> None:
        await self.bot.wait_until_ready()
        # Reset any stale "encoding" jobs from a previous bot instance
        stale = await self.bot.db.get_encode_queue(status="encoding", limit=50)
        for job in stale:
            logger.warning("Resetting stale encode job: %s", job.get("title"))
            await self.bot.db.update_encode_status(
                job["path"], "queued", progress=0
            )


async def setup(bot: PlexManagerBot) -> None:
    await bot.add_cog(EncodeCog(bot))
