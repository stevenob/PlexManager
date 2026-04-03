import asyncio
import logging
import logging.handlers
import signal
import sys

from config import Config
from bot.client import PlexManagerBot
from storage.database import MediaDatabase
from media.metadata import TMDbClient
from media.scanner import MediaScanner
from watcher.monitor import MediaMonitor

logger = logging.getLogger("plexmanager")


def setup_logging() -> None:
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    level = getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)

    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(log_format))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        "plexmanager.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    file_handler.setFormatter(logging.Formatter(log_format))
    root.addHandler(file_handler)


async def main() -> None:
    # --- Validate configuration ---
    errors = Config.validate()
    if errors:
        for error in errors:
            print(f"Configuration error: {error}", file=sys.stderr)
        sys.exit(1)

    # --- Set up logging ---
    setup_logging()
    logger.info("Configuration validated successfully")

    # --- Initialise core components ---
    db = MediaDatabase(Config.DB_PATH)
    tmdb = TMDbClient(Config.TMDB_API_KEY)
    bot = PlexManagerBot(Config.NOTIFICATION_CHANNEL_ID)
    scanner = MediaScanner(Config.MEDIA_PATHS, db, tmdb)
    monitor = MediaMonitor(Config.MEDIA_PATHS, db, tmdb, bot)

    # Expose components on the bot so cogs can access them via self.bot.<attr>
    bot.db = db
    bot.scanner = scanner
    bot.tmdb = tmdb
    bot.monitor = monitor
    bot.media_paths = Config.MEDIA_PATHS

    # --- Extend the bot's setup_hook ---
    # The original hook loads cogs and syncs the command tree.
    # We chain our startup tasks (DB connect, initial scan, watcher) after it.
    _original_setup_hook = bot.setup_hook

    async def setup_hook() -> None:
        await _original_setup_hook()

        await db.connect()
        logger.info("Database connected (%s)", Config.DB_PATH)

        stats = await scanner.scan()
        logger.info(
            "Initial media scan complete — added=%d, removed=%d, unchanged=%d, total=%d",
            stats["added"],
            stats["removed"],
            stats["unchanged"],
            stats["total"],
        )

        monitor.start()
        logger.info("File-system watcher started on %s", Config.MEDIA_PATHS)

    bot.setup_hook = setup_hook

    # --- Graceful shutdown plumbing ---
    _shutting_down = False

    def _request_shutdown() -> None:
        nonlocal _shutting_down
        if _shutting_down:
            logger.warning("Forced exit (second signal received)")
            sys.exit(1)
        _shutting_down = True
        logger.info("Shutdown signal received — closing bot…")
        asyncio.get_running_loop().create_task(bot.close())

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_shutdown)
    else:
        signal.signal(signal.SIGINT, lambda *_: _request_shutdown())
        signal.signal(signal.SIGTERM, lambda *_: _request_shutdown())

    # --- Start the bot (blocks until bot.close() is called) ---
    try:
        async with bot:
            logger.info("Starting Discord bot…")
            await bot.start(Config.DISCORD_TOKEN)
    finally:
        logger.info("Cleaning up resources…")
        monitor.stop()
        logger.info("File-system watcher stopped")
        await tmdb.close()
        logger.info("TMDb client closed")
        await db.close()
        logger.info("Database connection closed")
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
