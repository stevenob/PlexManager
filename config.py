import os
import shutil
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


def _safe_int(value: Optional[str], default: int) -> int:
    """Parse an integer from an env var, returning *default* on failure."""
    if not value:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _safe_float(value: Optional[str], default: float) -> float:
    """Parse a float from an env var, returning *default* on failure."""
    if not value:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


class Config:
    DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    NOTIFICATION_CHANNEL_ID: int = _safe_int(os.getenv("NOTIFICATION_CHANNEL_ID"), 0)
    GUILD_ID: int = _safe_int(os.getenv("GUILD_ID"), 0)
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "")
    MEDIA_PATHS: list = [
        p.strip() for p in os.getenv("MEDIA_PATHS", os.getenv("MEDIA_PATH", "/Volumes/Media")).split(",") if p.strip()
    ]
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    SCAN_INTERVAL_MINUTES: int = _safe_int(os.getenv("SCAN_INTERVAL_MINUTES"), 60)
    DB_PATH: str = os.getenv("DB_PATH", "plexmanager.db")

    # eBay API (optional — enables Blu-ray deal searching)
    EBAY_APP_ID: str = os.getenv("EBAY_APP_ID", "")
    EBAY_CERT_ID: str = os.getenv("EBAY_CERT_ID", "")
    UPGRADE_SCAN_INTERVAL_HOURS: int = _safe_int(os.getenv("UPGRADE_SCAN_INTERVAL_HOURS"), 24)
    MAX_EBAY_PRICE: float = _safe_float(os.getenv("MAX_EBAY_PRICE"), 0)
    UPGRADE_CHANNEL_ID: int = _safe_int(os.getenv("UPGRADE_CHANNEL_ID"), 0)

    SUPPORTED_EXTENSIONS: set = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv"}

    @classmethod
    def validate(cls) -> list[str]:
        errors = []
        if not cls.DISCORD_TOKEN:
            errors.append("DISCORD_TOKEN is required")
        if not cls.TMDB_API_KEY:
            errors.append("TMDB_API_KEY is required")
        if not cls.NOTIFICATION_CHANNEL_ID:
            errors.append("NOTIFICATION_CHANNEL_ID is required (must be a valid integer)")
        if not cls.MEDIA_PATHS:
            errors.append("MEDIA_PATHS is required")
        if cls.SCAN_INTERVAL_MINUTES < 1:
            errors.append("SCAN_INTERVAL_MINUTES must be at least 1")
        return errors

    @classmethod
    def ebay_configured(cls) -> bool:
        """Return True if both eBay API credentials are set."""
        return bool(cls.EBAY_APP_ID and cls.EBAY_CERT_ID)

    @classmethod
    def warn_missing_tools(cls) -> list[str]:
        """Return warnings for optional external tools not found in PATH."""
        warnings: list[str] = []
        if not shutil.which("ffprobe"):
            warnings.append("ffprobe not found in PATH — media analysis features will be limited")
        return warnings
