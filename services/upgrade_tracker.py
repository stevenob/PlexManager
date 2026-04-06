"""Orchestrator for the Blu-ray upgrade scan workflow."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from media.metadata import TMDbClient
from media.probe import probe_resolution
from services.ebay import EbayClient, EbaySearchResult
from storage.database import MediaDatabase

logger = logging.getLogger(__name__)


@dataclass
class UpgradeScanResult:
    """Summary of an upgrade scan run."""
    movies_scanned: int = 0
    new_deals_found: int = 0
    no_bluray_marked: int = 0
    errors: int = 0
    skipped: int = 0
    details: list[EbaySearchResult] = field(default_factory=list)


class UpgradeTracker:
    """Orchestrates the low-res movie → TMDb check → eBay search pipeline."""

    def __init__(
        self,
        db: MediaDatabase,
        ebay: EbayClient,
        tmdb: TMDbClient,
        max_price: float = 0,
    ) -> None:
        self.db = db
        self.ebay = ebay
        self.tmdb = tmdb
        self.max_price = max_price

    async def scan(self, limit: int = 100) -> UpgradeScanResult:
        """Run a full upgrade scan.

        1. Query low-res movies from the database
        2. Skip movies already ignored/purchased/no_bluray
        3. Check TMDb for Blu-ray availability
        4. Search eBay for deals on eligible movies
        5. Persist new deals
        """
        result = UpgradeScanResult()

        # Get low-res movies
        low_res = await self.db.get_low_res_movies(max_height=480, limit=limit)
        if not low_res:
            logger.info("No low-resolution movies found — nothing to scan")
            return result

        logger.info("Found %d low-resolution movies to check for upgrades", len(low_res))

        for movie in low_res:
            result.movies_scanned += 1

            # Check current upgrade status
            status = await self.db.get_upgrade_status(movie.path)
            if status in ("ignored", "purchased"):
                result.skipped += 1
                continue

            if status == "no_bluray":
                # Will be re-checked separately via recheck_no_bluray()
                result.skipped += 1
                continue

            # Ensure movie is in upgrade_status tracking
            if status is None:
                await self.db.set_upgrade_status(
                    path=movie.path,
                    status="tracking",
                    tmdb_id=movie.tmdb_id,
                    title=movie.title,
                    year=movie.year,
                )

            # Check TMDb for Blu-ray release (if we have a tmdb_id)
            if movie.tmdb_id:
                try:
                    has_bluray = await self.tmdb.check_bluray_release(movie.tmdb_id)
                    if has_bluray is False:
                        logger.info(
                            "No Blu-ray release found for '%s' — marking as no_bluray",
                            movie.title,
                        )
                        await self.db.set_upgrade_status(
                            path=movie.path, status="no_bluray",
                            tmdb_id=movie.tmdb_id, title=movie.title, year=movie.year,
                        )
                        result.no_bluray_marked += 1
                        continue
                except Exception:
                    logger.exception("TMDb Blu-ray check failed for '%s'", movie.title)

            # Search eBay
            if not self.ebay.is_configured:
                result.skipped += 1
                continue

            try:
                ebay_result = await self.ebay.search_bluray(
                    movie_title=movie.title or movie.filename,
                    year=movie.year,
                    max_price=self.max_price,
                )
                result.details.append(ebay_result)

                if ebay_result.no_results:
                    # No eBay listings at all — mark as no_bluray
                    logger.info(
                        "No eBay Blu-ray listings for '%s' — marking as no_bluray",
                        movie.title,
                    )
                    await self.db.set_upgrade_status(
                        path=movie.path, status="no_bluray",
                        tmdb_id=movie.tmdb_id, title=movie.title, year=movie.year,
                    )
                    result.no_bluray_marked += 1
                    continue

                # Store any deals found
                for deal in ebay_result.deals:
                    added = await self.db.add_upgrade_deal(
                        path=movie.path,
                        title=movie.title,
                        ebay_item_id=deal.item_id,
                        ebay_url=deal.listing_url,
                        price=deal.price,
                        avg_price=ebay_result.average_price,
                        condition=deal.condition,
                        shipping_cost=deal.shipping_cost,
                    )
                    if added:
                        result.new_deals_found += 1

            except Exception:
                logger.exception("eBay search failed for '%s'", movie.title)
                result.errors += 1

            # Small delay between eBay searches to be respectful
            await asyncio.sleep(0.5)

        logger.info(
            "Upgrade scan complete: scanned=%d, deals=%d, no_bluray=%d, skipped=%d, errors=%d",
            result.movies_scanned, result.new_deals_found,
            result.no_bluray_marked, result.skipped, result.errors,
        )
        return result

    async def recheck_no_bluray(self, max_age_days: int = 30) -> int:
        """Re-check movies previously marked as no_bluray.

        Returns the number of movies reclassified back to 'tracking'.
        """
        stale = await self.db.get_stale_no_bluray(days=max_age_days)
        if not stale:
            return 0

        reclassified = 0
        for entry in stale:
            tmdb_id = entry.get("tmdb_id")
            if not tmdb_id:
                # No TMDb ID — just refresh the timestamp
                await self.db.set_upgrade_status(
                    path=entry["path"], status="no_bluray",
                    title=entry.get("title"), year=entry.get("year"),
                )
                continue

            try:
                has_bluray = await self.tmdb.check_bluray_release(tmdb_id)
                if has_bluray is True:
                    logger.info(
                        "Blu-ray now available for '%s' — reclassifying to tracking",
                        entry.get("title"),
                    )
                    await self.db.set_upgrade_status(
                        path=entry["path"], status="tracking",
                        tmdb_id=tmdb_id, title=entry.get("title"),
                        year=entry.get("year"),
                    )
                    reclassified += 1
                else:
                    # Still no Blu-ray — refresh the timestamp
                    await self.db.set_upgrade_status(
                        path=entry["path"], status="no_bluray",
                        tmdb_id=tmdb_id, title=entry.get("title"),
                        year=entry.get("year"),
                    )
            except Exception:
                logger.exception("Re-check failed for '%s'", entry.get("title"))

        logger.info("Re-checked %d no_bluray entries, reclassified %d", len(stale), reclassified)
        return reclassified

    async def probe_unscanned_movies(self, limit: int = 100) -> dict:
        """Probe resolution for movies that haven't been scanned yet.

        Returns stats: {'probed': N, 'failed': N, 'low_res': N}.
        """
        movies = await self.db.get_movies_without_resolution(limit=limit)
        stats = {"probed": 0, "failed": 0, "low_res": 0, "total": len(movies)}

        for movie in movies:
            try:
                resolution = await probe_resolution(movie.path)
                if resolution:
                    await self.db.update_resolution(
                        movie.path, resolution.width, resolution.height
                    )
                    if resolution.codec:
                        await self.db.update_codec(movie.path, resolution.codec)
                    stats["probed"] += 1
                    if resolution.height <= 480 and resolution.width < 1920:
                        stats["low_res"] += 1
                else:
                    stats["failed"] += 1
            except Exception:
                logger.exception("Resolution probe failed for '%s'", movie.path)
                stats["failed"] += 1

        logger.info("Resolution probe complete: %s", stats)
        return stats
