"""Real-time file system watcher using watchdog.

Monitors a NAS mount path recursively, detecting file additions, deletions,
and moves, then updates the database and sends Discord notifications.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

import discord

from media.models import (
    Episode,
    MediaFile,
    MediaType,
    Movie,
    SUPPORTED_EXTENSIONS,
)
from media.scanner import MediaScanner
from storage.database import MediaDatabase
from media.metadata import TMDbClient

if TYPE_CHECKING:
    from bot.client import PlexManagerBot

logger = logging.getLogger(__name__)

_TEMP_SUFFIXES = frozenset({".part", ".tmp", ".downloading"})

# Delay (in seconds) before processing a newly-created file, giving the OS
# time to finish writing / copying it.
_NEW_FILE_SETTLE_SECONDS = 2.0


# ------------------------------------------------------------------
# Helper predicates
# ------------------------------------------------------------------

def _is_video_file(filename: str) -> bool:
    """Return ``True`` if *filename* has a supported video extension."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in SUPPORTED_EXTENSIONS


def _is_temp_file(filename: str) -> bool:
    """Return ``True`` if *filename* looks like a partial / temp download."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in _TEMP_SUFFIXES


# ------------------------------------------------------------------
# MediaEventHandler — watchdog callbacks (sync, runs on observer thread)
# ------------------------------------------------------------------

class MediaEventHandler(FileSystemEventHandler):
    """Translates watchdog file-system events into async database / bot actions."""

    def __init__(
        self,
        db: MediaDatabase,
        tmdb: TMDbClient,
        bot: PlexManagerBot,
        media_path: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        super().__init__()
        self.db = db
        self.tmdb = tmdb
        self.bot = bot
        self.media_path = media_path
        self.loop = loop

    # -- filtering -------------------------------------------------------

    @staticmethod
    def _should_ignore(path: str) -> bool:
        """Return ``True`` if the event should be silently skipped."""
        basename = os.path.basename(path)

        if basename.startswith("."):
            return True
        if _is_temp_file(basename):
            return True
        if not _is_video_file(basename):
            return True

        return False

    # -- watchdog overrides ----------------------------------------------

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src = str(event.src_path)
        if self._should_ignore(src):
            return

        logger.info("File created detected: %s — waiting %.1fs for write to settle",
                     src, _NEW_FILE_SETTLE_SECONDS)

        timer = threading.Timer(
            _NEW_FILE_SETTLE_SECONDS,
            self._schedule_new_file,
            args=(src,),
        )
        timer.daemon = True
        timer.start()

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src = str(event.src_path)
        if self._should_ignore(src):
            return

        logger.info("File deletion detected: %s", src)
        asyncio.run_coroutine_threadsafe(self._handle_deleted_file(src), self.loop)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return

        src = str(event.src_path)
        dest = str(event.dest_path)

        # Treat as delete of the old path …
        if not self._should_ignore(src):
            logger.info("File move detected (source removed): %s", src)
            asyncio.run_coroutine_threadsafe(self._handle_deleted_file(src), self.loop)

        # … and create of the new path.
        if not self._should_ignore(dest):
            logger.info("File move detected (destination created): %s — "
                         "waiting %.1fs for write to settle",
                         dest, _NEW_FILE_SETTLE_SECONDS)
            timer = threading.Timer(
                _NEW_FILE_SETTLE_SECONDS,
                self._schedule_new_file,
                args=(dest,),
            )
            timer.daemon = True
            timer.start()

    # -- bridging sync → async -------------------------------------------

    def _schedule_new_file(self, filepath: str) -> None:
        """Called from a ``threading.Timer``; schedules the async handler."""
        asyncio.run_coroutine_threadsafe(self._handle_new_file(filepath), self.loop)

    # -- async handlers (run on the bot's event loop) --------------------

    async def _handle_new_file(self, filepath: str) -> None:
        """Classify, enrich, persist, and notify for a newly-added file."""
        try:
            logger.info("Processing new file: %s", filepath)

            # Re-use MediaScanner's classification logic
            scanner = MediaScanner(self.media_path, self.db, self.tmdb)
            media = scanner._classify_file(filepath)

            # Enrich with TMDb metadata if not already cached
            if not await self.db.has_tmdb_metadata(filepath):
                try:
                    media = await self.tmdb.enrich_media(media)
                except Exception:
                    logger.exception("TMDb enrichment failed for %s", filepath)

            await self.db.add_media(media)

            # Build Discord notification
            display_title = media.title or media.filename
            embed = discord.Embed(
                title="📥 New Media Added",
                color=0x2ECC71,
            )
            embed.add_field(name="Title", value=display_title, inline=True)
            embed.add_field(name="Type", value=media.media_type.value, inline=True)
            embed.add_field(name="Size", value=media.human_size, inline=True)
            embed.add_field(name="Path", value=filepath, inline=False)

            if media.poster_url:
                embed.set_thumbnail(url=media.poster_url)

            await self.bot.send_notification(embed)
            logger.info("New media indexed and notified: %s", display_title)

        except Exception:
            logger.exception("Error handling new file: %s", filepath)

    async def _handle_deleted_file(self, filepath: str) -> None:
        """Remove from database and notify if the record existed."""
        try:
            logger.info("Processing deleted file: %s", filepath)
            record = await self.db.remove_media(filepath)

            if record is None:
                logger.debug("Deleted file was not in database: %s", filepath)
                return

            display_title = record.title or record.filename
            embed = discord.Embed(
                title="🗑️ Media Removed",
                color=0xE74C3C,
            )
            embed.add_field(name="Title", value=display_title, inline=True)
            embed.add_field(name="Type", value=record.media_type.value, inline=True)
            embed.add_field(name="Path", value=filepath, inline=False)

            await self.bot.send_notification(embed)
            logger.info("Media removed and notified: %s", display_title)

        except Exception:
            logger.exception("Error handling deleted file: %s", filepath)


# ------------------------------------------------------------------
# MediaMonitor — high-level start / stop interface
# ------------------------------------------------------------------

class MediaMonitor:
    """Manages a ``watchdog`` Observer that monitors media paths for changes."""

    def __init__(
        self,
        media_paths: list,
        db: MediaDatabase,
        tmdb: TMDbClient,
        bot: PlexManagerBot,
    ) -> None:
        self.media_paths = media_paths if isinstance(media_paths, list) else [media_paths]
        self.db = db
        self.tmdb = tmdb
        self.bot = bot
        self._observer = Observer()

    def start(self) -> None:
        """Schedule the event handler and start the observer thread."""
        loop = asyncio.get_event_loop()
        for media_path in self.media_paths:
            handler = MediaEventHandler(
                db=self.db,
                tmdb=self.tmdb,
                bot=self.bot,
                media_path=media_path,
                loop=loop,
            )
            self._observer.schedule(handler, media_path, recursive=True)
            logger.info("MediaMonitor started — watching %s", media_path)
        self._observer.start()

    def stop(self) -> None:
        """Stop the observer thread and wait for it to finish."""
        self._observer.stop()
        self._observer.join()
        logger.info("MediaMonitor stopped")
