from __future__ import annotations

import logging

from media.probe import ffprobe_available, probe_resolution
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.client import PlexManagerBot

logger = logging.getLogger(__name__)


def _human_bytes(size: int) -> str:
    if size == 0:
        return "0 B"
    s = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if s < 1024:
            return f"{s:.1f} {unit}"
        s /= 1024
    return f"{s:.1f} PB"


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

            # Probe resolution + codec for unscanned movies
            if ffprobe_available():
                unscanned = await self.bot.db.get_movies_without_codec(limit=1000)
                if unscanned:
                    probed = 0
                    for movie in unscanned:
                        try:
                            resolution = await probe_resolution(movie.path)
                            if resolution:
                                if resolution.width and resolution.height:
                                    await self.bot.db.update_resolution(movie.path, resolution.width, resolution.height)
                                if resolution.codec:
                                    await self.bot.db.update_codec(movie.path, resolution.codec)
                                probed += 1
                        except Exception:
                            pass
                    if probed > 0:
                        await interaction.followup.send(
                            f"🎞️ Also probed codec/resolution for **{probed}** movies."
                        )

            # Enrich movies missing TMDb metadata
            unmatched = await self.bot.db.get_unmatched_movies(limit=100)
            if unmatched:
                enriched = 0
                for movie in unmatched:
                    try:
                        media = await self.bot.tmdb.enrich_media(movie)
                        if media.tmdb_id:
                            await self.bot.db.add_media(media)
                            enriched += 1
                    except Exception:
                        pass
                if enriched > 0:
                    await interaction.followup.send(
                        f"🎬 Also enriched TMDb data for **{enriched}** movies."
                    )
        except Exception:
            logger.exception("Library rescan failed")
            await interaction.followup.send(
                "❌ Scan failed — check bot logs for details.", ephemeral=True
            )

    @app_commands.command(name="status", description="Bot status and library dashboard")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        uptime = discord.utils.format_dt(self._started_at, style="R")

        # Library stats
        stats = await self.bot.db.get_stats()
        total_movies = stats.get("total_movies", 0)
        total_episodes = stats.get("total_episodes", 0)
        total_shows = stats.get("total_shows", 0)
        total_size = stats.get("total_size", 0)

        embed = discord.Embed(title="📊 PlexManager Dashboard", color=discord.Color.teal())
        embed.add_field(
            name="📚 Library",
            value=(
                f"**{total_movies}** movies · **{total_shows}** shows · **{total_episodes}** episodes\n"
                f"**{_human_bytes(total_size)}** total · **{stats.get('recent_count', 0)}** added this week"
            ),
            inline=False,
        )

        # Codec breakdown
        try:
            codec_stats = await self.bot.db.get_codec_stats()
            hevc = codec_stats.get("hevc", 0) + codec_stats.get("h265", 0)
            h264 = codec_stats.get("h264", 0)
            other = sum(v for k, v in codec_stats.items() if k not in ("hevc", "h265", "h264", "unscanned", None))
            unscanned = codec_stats.get("unscanned", 0) + codec_stats.get(None, 0)
            embed.add_field(
                name="🎞️ Codecs",
                value=f"✅ H.265: **{hevc}** · H.264: **{h264}** · Other: **{other}** · Unscanned: **{unscanned}**",
                inline=False,
            )
        except Exception:
            pass

        # Encode progress
        try:
            enc = await self.bot.db.get_encode_stats()
            if enc.get("queued", 0) or enc.get("encoding", 0) or enc.get("done", 0):
                enc_parts = []
                if enc.get("encoding", 0): enc_parts.append(f"🔄 Encoding: **{enc['encoding']}**")
                if enc.get("queued", 0): enc_parts.append(f"⏳ Queued: **{enc['queued']}**")
                if enc.get("done", 0): enc_parts.append(f"✅ Done: **{enc['done']}**")
                if enc.get("failed", 0): enc_parts.append(f"❌ Failed: **{enc['failed']}**")
                total_orig = enc.get("total_original", 0)
                total_enc = enc.get("total_encoded", 0)
                if total_orig > 0:
                    savings = total_orig - total_enc
                    enc_parts.append(f"💾 Saved: **{_human_bytes(savings)}**")
                embed.add_field(name="🔄 Encoding", value=" · ".join(enc_parts), inline=False)
        except Exception:
            pass

        # Upgrade summary
        try:
            upgrade = await self.bot.db.get_upgrade_summary()
            if any(upgrade.values()):
                up_parts = []
                if upgrade.get("tracking", 0): up_parts.append(f"🔍 Tracking: **{upgrade['tracking']}**")
                if upgrade.get("purchased", 0): up_parts.append(f"✅ Purchased: **{upgrade['purchased']}**")
                if upgrade.get("no_bluray", 0): up_parts.append(f"❌ No Blu-ray: **{upgrade['no_bluray']}**")
                if upgrade.get("ignored", 0): up_parts.append(f"⏭️ Ignored: **{upgrade['ignored']}**")
                embed.add_field(name="📀 Upgrades", value=" · ".join(up_parts), inline=False)
        except Exception:
            pass

        # Services
        from media.probe import ffprobe_available
        from services.handbrake import handbrake_available
        from config import Config
        services = []
        services.append(f"ffprobe: {'✅' if ffprobe_available() else '⚠️'}")
        services.append(f"HandBrake: {'✅' if handbrake_available(Config.HANDBRAKE_PATH) else '⚠️'}")
        services.append(f"OpenSubs: {'✅' if Config.opensubtitles_configured() else '⚠️'}")

        media_path = ", ".join(getattr(self.bot, "media_paths", [])) or "N/A"
        embed.set_footer(text=f"Started {uptime} · {' · '.join(services)} · {media_path}")

        await interaction.followup.send(embed=embed)


async def setup(bot: PlexManagerBot) -> None:
    await bot.add_cog(LibraryCog(bot))
