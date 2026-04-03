from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from media.models import MediaFile, MediaType

if TYPE_CHECKING:
    from bot.client import PlexManagerBot

logger = logging.getLogger(__name__)

# Colors matching watcher/monitor.py conventions
COLOR_ADDED = 0x2ECC71
COLOR_REMOVED = 0xE74C3C
COLOR_BATCH = 0x3498DB


# ── Embed helpers ──────────────────────────────────────────────────────────


def build_added_embed(media: MediaFile) -> discord.Embed:
    """Rich embed for a single added media file."""
    display_title = media.title or media.filename
    embed = discord.Embed(title="📥 New Media Added", color=COLOR_ADDED)
    embed.add_field(name="Title", value=display_title, inline=True)
    embed.add_field(name="Type", value=media.media_type.value, inline=True)
    embed.add_field(name="Size", value=media.human_size, inline=True)
    if media.poster_url:
        embed.set_thumbnail(url=media.poster_url)
    return embed


def build_removed_embed(media: MediaFile) -> discord.Embed:
    """Rich embed for a single removed media file."""
    display_title = media.title or media.filename
    embed = discord.Embed(title="🗑️ Media Removed", color=COLOR_REMOVED)
    embed.add_field(name="Title", value=display_title, inline=True)
    embed.add_field(name="Type", value=media.media_type.value, inline=True)
    return embed


def build_batch_embed(items: list[tuple[MediaFile, str]]) -> discord.Embed:
    """Combined embed listing multiple file changes."""
    embed = discord.Embed(title="📦 Batch Media Update", color=COLOR_BATCH)

    lines: list[str] = []
    for media, event_type in items:
        emoji = "📥" if event_type == "added" else "🗑️"
        display_title = media.title or media.filename
        lines.append(f"{emoji} **{display_title}** ({media.media_type.value})")

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"{len(items)} file(s) changed")
    return embed


# ── Cog ────────────────────────────────────────────────────────────────────


class NotificationsCog(commands.Cog):
    """Batched media-change notifications and history lookup."""

    def __init__(self, bot: PlexManagerBot) -> None:
        self.bot = bot
        self._buffer: list[tuple[MediaFile, str]] = []
        self._flush_notifications.start()

    def cog_unload(self) -> None:
        self._flush_notifications.cancel()

    # ── Public API ─────────────────────────────────────────────────────

    def queue_notification(self, media: MediaFile, event_type: str) -> None:
        """Buffer a notification instead of sending it immediately.

        *event_type* must be ``"added"`` or ``"deleted"``.
        """
        self._buffer.append((media, event_type))

    # ── Background flush loop ─────────────────────────────────────────

    @tasks.loop(seconds=10)
    async def _flush_notifications(self) -> None:
        if not self._buffer:
            return

        items = list(self._buffer)
        self._buffer.clear()

        if len(items) == 1:
            media, event_type = items[0]
            if event_type == "added":
                embed = build_added_embed(media)
            else:
                embed = build_removed_embed(media)
        elif len(items) <= 10:
            embed = build_batch_embed(items)
        else:
            counts: dict[str, int] = defaultdict(int)
            for _, event_type in items:
                counts[event_type] += 1
            parts: list[str] = []
            if counts["added"]:
                parts.append(f"**{counts['added']}** file(s) added")
            if counts["deleted"]:
                parts.append(f"**{counts['deleted']}** file(s) removed")
            embed = discord.Embed(
                title="📦 Bulk Media Update",
                description=", ".join(parts),
                color=COLOR_BATCH,
            )
            embed.set_footer(text=f"{len(items)} total changes")

        await self.bot.send_notification(embed)

    @_flush_notifications.before_loop
    async def _before_flush(self) -> None:
        await self.bot.wait_until_ready()

    # ── Slash command ─────────────────────────────────────────────────

    @app_commands.command(
        name="notifications",
        description="Show recent file-change history",
    )
    @app_commands.describe(count="Number of events to display (default 10, max 50)")
    async def notifications(
        self,
        interaction: discord.Interaction,
        count: Optional[int] = None,
    ) -> None:
        limit = min(max(count or 10, 1), 50)

        await interaction.response.defer()

        async with self.bot.db._conn.execute(
            "SELECT path, event_type, timestamp, title, media_type "
            "FROM watch_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send("No file-change history found.")
            return

        lines: list[str] = []
        for row in rows:
            emoji = "📥" if row["event_type"] == "added" else "🗑️"
            display = row["title"] or os.path.basename(row["path"])
            ts = datetime.fromisoformat(row["timestamp"]).strftime("%Y-%m-%d %H:%M")
            lines.append(f"{emoji} **{display}** — {ts}")

        embed = discord.Embed(
            title="📋 Recent File Changes",
            description="\n".join(lines),
            color=COLOR_BATCH,
        )
        embed.set_footer(text=f"Showing last {len(rows)} event(s)")
        await interaction.followup.send(embed=embed)


async def setup(bot: PlexManagerBot) -> None:
    await bot.add_cog(NotificationsCog(bot))
