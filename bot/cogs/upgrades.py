"""Blu-ray upgrade finder — scans for low-res movies and searches eBay for deals."""

from __future__ import annotations

import logging
import math
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from media.probe import ffprobe_available
from services.ebay import EbayClient
from services.upgrade_tracker import UpgradeTracker

if TYPE_CHECKING:
    from bot.client import PlexManagerBot

logger = logging.getLogger(__name__)

COLOR_UPGRADE = 0x9B59B6  # Purple for upgrade-related
COLOR_DEAL = 0xF1C40F     # Gold for deals
COLOR_INFO = 0x3498DB      # Blue for info

RESULTS_PER_PAGE = 10


# ── UI Components ─────────────────────────────────────────────────────────


class PurchasedButton(discord.ui.Button):
    """Button to mark a movie as purchased from a deal notification."""

    def __init__(self, path: str, ebay_url: str, title: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.success,
            label="✅ Purchased",
            custom_id=f"upgrade_purchased:{path[:80]}",
        )
        self.path = path
        self.ebay_url = ebay_url
        self.title = title

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: PlexManagerBot = interaction.client  # type: ignore
        await bot.db.set_upgrade_status(
            path=self.path,
            status="purchased",
            purchase_url=self.ebay_url,
        )
        await interaction.response.send_message(
            f"✅ **{self.title}** marked as purchased!", ephemeral=True
        )
        # Disable the button on the original message
        if self.view:
            for item in self.view.children:
                item.disabled = True  # type: ignore
            await interaction.message.edit(view=self.view)


class DealView(discord.ui.View):
    """View with eBay link and Purchased button for deal notifications."""

    def __init__(self, path: str, ebay_url: str, title: str) -> None:
        super().__init__(timeout=None)  # Persistent view
        self.add_item(discord.ui.Button(
            style=discord.ButtonStyle.link, label="View on eBay", url=ebay_url
        ))
        self.add_item(PurchasedButton(path, ebay_url, title))


# ── Cog ────────────────────────────────────────────────────────────────────


class UpgradesCog(commands.Cog):
    """Blu-ray upgrade finder — find deals on upgrades for low-res movies."""

    def __init__(self, bot: PlexManagerBot) -> None:
        self.bot = bot
        self._ebay = EbayClient(Config.EBAY_APP_ID, Config.EBAY_CERT_ID)
        self._tracker = UpgradeTracker(
            db=bot.db,
            ebay=self._ebay,
            tmdb=bot.tmdb,
            max_price=Config.MAX_EBAY_PRICE,
        )
        self._scan_loop.start()

    @property
    def upgrade_channel(self) -> discord.TextChannel | None:
        """Return the dedicated upgrade channel, falling back to the main notification channel."""
        if Config.UPGRADE_CHANNEL_ID:
            ch = self.bot.get_channel(Config.UPGRADE_CHANNEL_ID)
            if ch is not None:
                return ch  # type: ignore[return-value]
        return self.bot.notification_channel

    async def cog_unload(self) -> None:
        self._scan_loop.cancel()
        await self._ebay.close()

    # ── Upgrade command group ─────────────────────────────────────────

    upgrade_group = app_commands.Group(
        name="upgrades", description="Blu-ray upgrade finder for low-res movies"
    )

    @upgrade_group.command(name="list", description="Show all low-resolution movies in your library")
    @app_commands.describe(page="Page number (default 1)")
    async def upgrade_list(
        self, interaction: discord.Interaction, page: Optional[int] = None
    ) -> None:
        await interaction.response.defer()

        movies = await self.bot.db.get_low_res_movies(max_height=480, limit=200)

        if not movies:
            await interaction.followup.send(
                "✅ No low-resolution movies found! All your movies are 1080p or higher.",
                ephemeral=True,
            )
            return

        current_page = max(1, page or 1)
        total_pages = max(1, math.ceil(len(movies) / RESULTS_PER_PAGE))
        current_page = min(current_page, total_pages)
        start = (current_page - 1) * RESULTS_PER_PAGE
        page_items = movies[start : start + RESULTS_PER_PAGE]

        embed = discord.Embed(
            title="📀 Low-Resolution Movies",
            description="Movies available below 1080p — potential Blu-ray upgrades",
            color=COLOR_UPGRADE,
        )

        for movie in page_items:
            year_str = f" ({movie.year})" if movie.year else ""
            status = await self.bot.db.get_upgrade_status(movie.path)
            status_emoji = {
                "tracking": "🔍",
                "ignored": "⏭️",
                "purchased": "✅",
                "no_bluray": "❌",
            }.get(status or "tracking", "🔍")

            embed.add_field(
                name=f"{movie.title}{year_str}",
                value=(
                    f"**Resolution:** {movie.resolution_label} · "
                    f"**Size:** {movie.human_size} · "
                    f"**Status:** {status_emoji} {status or 'tracking'}"
                ),
                inline=False,
            )

        if movies and movies[0].poster_url:
            embed.set_thumbnail(url=movies[0].poster_url)

        embed.set_footer(
            text=f"Page {current_page}/{total_pages} · {len(movies)} low-res movie(s)"
        )
        await interaction.followup.send(embed=embed)

    @upgrade_group.command(
        name="unmatched",
        description="Show low-res movies that failed TMDb lookup",
    )
    async def upgrade_unmatched(self, interaction: discord.Interaction) -> None:
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

        for movie in movies[:25]:  # Discord embed field limit
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

    @upgrade_group.command(
        name="deals", description="Show current Blu-ray deals from eBay"
    )
    async def upgrade_deals(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        deals = await self.bot.db.get_recent_deals(limit=25)

        if not deals:
            msg = "No deals found yet."
            if not Config.ebay_configured():
                msg += "\n\n⚠️ eBay API is not configured. Add `EBAY_APP_ID` and `EBAY_CERT_ID` to your `.env` file."
            await interaction.followup.send(msg, ephemeral=True)
            return

        embed = discord.Embed(
            title="💰 Blu-ray Deals",
            description="Below-average-price Blu-ray listings on eBay",
            color=COLOR_DEAL,
        )

        lines = []
        for deal in deals:
            status_icon = "✅ " if deal.get("upgrade_status") == "purchased" else ""
            savings = deal.get("avg_price", 0) - deal.get("price", 0)
            shipping = deal.get("shipping_cost", 0)
            shipping_str = f"${shipping:.2f} ship" if shipping > 0 else "Free ship"
            lines.append(
                f"{status_icon}**{deal.get('title', 'Unknown')}** — "
                f"${deal['price']:.2f} · {shipping_str} · "
                f"Save ${savings:.2f} · "
                f"[eBay]({deal.get('ebay_url', '#')})"
            )

        embed.add_field(name="Deals", value="\n".join(lines[:15]), inline=False)
        embed.set_footer(text=f"{len(deals)} deal(s)")
        await interaction.followup.send(embed=embed)

    @upgrade_group.command(
        name="check", description="Check eBay Blu-ray prices for a specific movie"
    )
    @app_commands.describe(title="Title of the movie to search for")
    async def upgrade_check(
        self, interaction: discord.Interaction, title: str
    ) -> None:
        if not Config.ebay_configured():
            await interaction.response.send_message(
                "⚠️ eBay API is not configured. Add `EBAY_APP_ID` and `EBAY_CERT_ID` to your `.env` file.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # Look up the movie in the DB for year/poster info
        results = await self.bot.db.search(title, limit=5)
        movie = None
        for m in results:
            if m.title and m.title.lower() == title.lower():
                movie = m
                break
        if movie is None and results:
            movie = results[0]

        search_title = movie.title if movie else title
        search_year = movie.year if movie else None

        ebay_result = await self._ebay.search_bluray(
            movie_title=search_title,
            year=search_year,
            max_price=Config.MAX_EBAY_PRICE,
        )

        if ebay_result.no_results:
            await interaction.followup.send(
                f"No Blu-ray listings found for **{search_title}** on eBay.",
                ephemeral=True,
            )
            return

        year_str = f" ({movie.year})" if movie and movie.year else ""
        embed = discord.Embed(
            title=f"🔍 {search_title}{year_str}",
            description=f"Blu-ray prices — **${ebay_result.average_price:.2f} average** across {ebay_result.total_found} listings",
            color=COLOR_DEAL,
        )

        if movie and movie.poster_url:
            embed.set_thumbnail(url=movie.poster_url)

        # Top row: price stats
        cheapest = min(ebay_result.listings, key=lambda l: l.price) if ebay_result.listings else None
        highest = max(ebay_result.listings, key=lambda l: l.price) if ebay_result.listings else None
        embed.add_field(name="💲 Avg Price", value=f"${ebay_result.average_price:.2f}", inline=True)
        embed.add_field(name="📉 Lowest", value=f"${cheapest.price:.2f}" if cheapest else "N/A", inline=True)
        embed.add_field(name="📈 Highest", value=f"${highest.price:.2f}" if highest else "N/A", inline=True)

        # Listings as a compact block
        lines = []
        for listing in ebay_result.listings[:8]:
            deal_icon = "💰" if listing.price < ebay_result.average_price else "　"
            shipping_str = f"${listing.shipping_cost:.2f} ship" if listing.shipping_cost > 0 else "Free ship"
            lines.append(
                f"{deal_icon} **${listing.price:.2f}** · {shipping_str} · {listing.condition} — [View]({listing.listing_url})"
            )
        embed.add_field(name="Listings", value="\n".join(lines), inline=False)

        res_label = movie.resolution_label if movie and movie.resolution_height else "Unknown"
        embed.set_footer(text=f"Current: {res_label} · 💰 = below average")
        await interaction.followup.send(embed=embed)

    @upgrade_group.command(
        name="sellcheck", description="Check what your DVD could sell for on eBay"
    )
    @app_commands.describe(title="Title of the movie to price check")
    async def upgrade_sellcheck(
        self, interaction: discord.Interaction, title: str
    ) -> None:
        if not Config.ebay_configured():
            await interaction.response.send_message(
                "⚠️ eBay API is not configured. Add `EBAY_APP_ID` and `EBAY_CERT_ID` to your `.env` file.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # Look up the movie in the DB for year/poster info
        results = await self.bot.db.search(title, limit=5)
        movie = None
        for m in results:
            if m.title and m.title.lower() == title.lower():
                movie = m
                break
        if movie is None and results:
            movie = results[0]

        search_title = movie.title if movie else title
        search_year = movie.year if movie else None

        dvd_result = await self._ebay.search_dvd(
            movie_title=search_title,
            year=search_year,
        )

        if dvd_result.no_results:
            await interaction.followup.send(
                f"No DVD listings found for **{search_title}** on eBay.",
                ephemeral=True,
            )
            return

        year_str = f" ({search_year})" if search_year else ""
        embed = discord.Embed(
            title=f"💿 {search_title}{year_str}",
            description=f"DVD sell value — **${dvd_result.average_price:.2f} average** across {dvd_result.total_found} listings",
            color=0xE67E22,
        )

        if movie and movie.poster_url:
            embed.set_thumbnail(url=movie.poster_url)

        cheapest = min(dvd_result.listings, key=lambda l: l.price) if dvd_result.listings else None
        highest = max(dvd_result.listings, key=lambda l: l.price) if dvd_result.listings else None
        embed.add_field(name="💲 Avg Price", value=f"${dvd_result.average_price:.2f}", inline=True)
        embed.add_field(name="📈 Highest", value=f"${highest.price:.2f}" if highest else "N/A", inline=True)
        embed.add_field(name="📉 Lowest", value=f"${cheapest.price:.2f}" if cheapest else "N/A", inline=True)

        res_label = movie.resolution_label if movie and movie.resolution_height else "Unknown"
        embed.set_footer(text=f"Current: {res_label} · {dvd_result.total_found} DVD listings on eBay")
        await interaction.followup.send(embed=embed)

    @upgrade_group.command(
        name="scan", description="Trigger a Blu-ray deal search on eBay"
    )
    async def upgrade_scan(self, interaction: discord.Interaction) -> None:
        if not Config.ebay_configured():
            await interaction.response.send_message(
                "⚠️ eBay API is not configured. Add `EBAY_APP_ID` and `EBAY_CERT_ID` to your `.env` file.",
                ephemeral=True,
            )
            return

        if not ffprobe_available():
            await interaction.response.send_message(
                "⚠️ `ffprobe` is not installed. Install ffmpeg to enable resolution detection.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # First, probe any unscanned movies
        probe_stats = await self._tracker.probe_unscanned_movies(limit=50)

        # Then run the eBay upgrade scan
        result = await self._tracker.scan()

        embed = discord.Embed(
            title="🔍 Upgrade Scan Complete",
            color=COLOR_UPGRADE,
        )
        embed.add_field(
            name="Resolution Probe",
            value=(
                f"**Probed:** {probe_stats['probed']} · "
                f"**Low-res found:** {probe_stats['low_res']} · "
                f"**Failed:** {probe_stats['failed']}"
            ),
            inline=False,
        )
        embed.add_field(
            name="eBay Search",
            value=(
                f"**Movies checked:** {result.movies_scanned} · "
                f"**New deals:** {result.new_deals_found} · "
                f"**No Blu-ray:** {result.no_bluray_marked} · "
                f"**Skipped:** {result.skipped} · "
                f"**Errors:** {result.errors}"
            ),
            inline=False,
        )

        await interaction.followup.send(embed=embed)

        # Send individual deal notifications
        await self._notify_new_deals()

    @upgrade_group.command(
        name="ignore", description="Exclude a movie from upgrade scans"
    )
    @app_commands.describe(title="Title of the movie to ignore")
    async def upgrade_ignore(
        self, interaction: discord.Interaction, title: str
    ) -> None:
        movies = await self.bot.db.search(title, limit=5)
        movie = None
        for m in movies:
            if m.title and m.title.lower() == title.lower():
                movie = m
                break
        if movie is None and movies:
            movie = movies[0]

        if movie is None:
            await interaction.response.send_message(
                f"No movie found matching '{title}'", ephemeral=True
            )
            return

        await self.bot.db.set_upgrade_status(
            path=movie.path,
            status="ignored",
            tmdb_id=movie.tmdb_id,
            title=movie.title,
            year=movie.year,
        )
        await interaction.response.send_message(
            f"⏭️ **{movie.title}** will be ignored in future upgrade scans."
        )

    @upgrade_group.command(
        name="purchased", description="Mark a movie as purchased (Blu-ray bought)"
    )
    @app_commands.describe(title="Title of the movie you purchased")
    async def upgrade_purchased(
        self, interaction: discord.Interaction, title: str
    ) -> None:
        movies = await self.bot.db.search(title, limit=5)
        movie = None
        for m in movies:
            if m.title and m.title.lower() == title.lower():
                movie = m
                break
        if movie is None and movies:
            movie = movies[0]

        if movie is None:
            await interaction.response.send_message(
                f"No movie found matching '{title}'", ephemeral=True
            )
            return

        await self.bot.db.set_upgrade_status(
            path=movie.path,
            status="purchased",
            tmdb_id=movie.tmdb_id,
            title=movie.title,
            year=movie.year,
        )
        await interaction.response.send_message(
            f"✅ **{movie.title}** marked as purchased! It will be removed from upgrade scans."
        )

    @upgrade_group.command(
        name="status", description="Show upgrade scan summary"
    )
    async def upgrade_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        summary = await self.bot.db.get_upgrade_summary()
        low_res = await self.bot.db.get_low_res_movies(max_height=480, limit=1000)
        unmatched = await self.bot.db.get_unmatched_movies(limit=1000)
        unscanned = await self.bot.db.get_movies_without_resolution(limit=1000)

        embed = discord.Embed(
            title="📊 Upgrade Scanner Status",
            color=COLOR_INFO,
        )
        embed.add_field(name="📀 Low-Res Movies", value=str(len(low_res)), inline=True)
        embed.add_field(
            name="🔍 Tracking",
            value=str(summary.get("tracking", 0)),
            inline=True,
        )
        embed.add_field(
            name="💰 Deals Found",
            value=str(len(await self.bot.db.get_recent_deals(limit=1000))),
            inline=True,
        )
        embed.add_field(
            name="✅ Purchased",
            value=str(summary.get("purchased", 0)),
            inline=True,
        )
        embed.add_field(
            name="❌ No Blu-ray",
            value=str(summary.get("no_bluray", 0)),
            inline=True,
        )
        embed.add_field(
            name="⏭️ Ignored",
            value=str(summary.get("ignored", 0)),
            inline=True,
        )
        embed.add_field(
            name="⚠️ Unmatched (no TMDb)",
            value=str(len(unmatched)),
            inline=True,
        )
        embed.add_field(
            name="❓ Unscanned (no resolution)",
            value=str(len(unscanned)),
            inline=True,
        )

        ebay_status = "✅ Configured" if Config.ebay_configured() else "⚠️ Not configured"
        ffprobe_status = "✅ Available" if ffprobe_available() else "⚠️ Not installed"
        embed.add_field(
            name="🔧 Services",
            value=f"eBay API: {ebay_status}\nffprobe: {ffprobe_status}",
            inline=False,
        )

        await interaction.followup.send(embed=embed)

    @upgrade_group.command(
        name="rescan_resolution",
        description="Probe resolution for movies that haven't been scanned yet",
    )
    async def rescan_resolution(self, interaction: discord.Interaction) -> None:
        if not ffprobe_available():
            await interaction.response.send_message(
                "⚠️ `ffprobe` is not installed. Install ffmpeg to enable resolution detection.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        stats = await self._tracker.probe_unscanned_movies(limit=1000)

        embed = discord.Embed(
            title="🔍 Resolution Scan Complete",
            color=COLOR_INFO,
        )
        embed.add_field(name="Total", value=str(stats["total"]), inline=True)
        embed.add_field(name="Probed", value=str(stats["probed"]), inline=True)
        embed.add_field(name="Low-Res Found", value=str(stats["low_res"]), inline=True)
        embed.add_field(name="Failed", value=str(stats["failed"]), inline=True)

        await interaction.followup.send(embed=embed)

    # ── Background scan loop ─────────────────────────────────────────

    @tasks.loop(hours=1)
    async def _scan_loop(self) -> None:
        """Periodic upgrade scan — runs at the configured interval."""
        # Only run every UPGRADE_SCAN_INTERVAL_HOURS
        if not hasattr(self, "_scan_counter"):
            self._scan_counter = 0
        self._scan_counter += 1
        if self._scan_counter < Config.UPGRADE_SCAN_INTERVAL_HOURS:
            return
        self._scan_counter = 0

        if not Config.ebay_configured():
            return

        logger.info("Starting scheduled upgrade scan")

        # Probe unscanned movies first
        await self._tracker.probe_unscanned_movies(limit=50)

        # Re-check stale no_bluray entries monthly
        await self._tracker.recheck_no_bluray(max_age_days=30)

        # Run the main scan
        result = await self._tracker.scan()

        if result.new_deals_found > 0:
            await self._notify_new_deals()

        logger.info("Scheduled upgrade scan complete: %d new deals", result.new_deals_found)

    @_scan_loop.before_loop
    async def _before_scan(self) -> None:
        await self.bot.wait_until_ready()

    # ── Deal notification helper ─────────────────────────────────────

    async def _notify_new_deals(self) -> None:
        """Send Discord notifications for new unnotified deals."""
        deals = await self.bot.db.get_unnotified_deals(limit=20)
        if not deals:
            return

        deal_ids = []
        for deal in deals:
            title = deal.get("title", "Unknown")
            savings = deal.get("avg_price", 0) - deal.get("price", 0)
            shipping = deal.get("shipping_cost", 0)
            shipping_str = f"${shipping:.2f}" if shipping > 0 else "Free"

            embed = discord.Embed(
                title=f"💰 {title}",
                description=f"Blu-ray deal found — **${savings:.2f} below average**",
                color=COLOR_DEAL,
            )

            # Try to get poster from DB
            media = await self.bot.db.get_media(deal.get("path", ""))
            if media and media.poster_url:
                embed.set_thumbnail(url=media.poster_url)

            embed.add_field(name="💲 Price", value=f"${deal['price']:.2f}", inline=True)
            embed.add_field(name="🚚 Shipping", value=shipping_str, inline=True)
            embed.add_field(name="📦 Condition", value=deal.get("condition", "N/A"), inline=True)

            res_label = media.resolution_label if media else "Unknown"
            embed.set_footer(text=f"Current: {res_label} · Avg Blu-ray: ${deal.get('avg_price', 0):.2f}")

            view = DealView(
                path=deal.get("path", ""),
                ebay_url=deal.get("ebay_url", "#"),
                title=title,
            )

            channel = self.upgrade_channel
            if channel:
                try:
                    await channel.send(embed=embed, view=view)
                except discord.HTTPException:
                    logger.exception("Failed to send deal notification")

            deal_ids.append(deal["id"])

        if deal_ids:
            await self.bot.db.mark_deals_notified(deal_ids)
            logger.info("Sent %d deal notifications", len(deal_ids))


async def setup(bot: PlexManagerBot) -> None:
    await bot.add_cog(UpgradesCog(bot))
