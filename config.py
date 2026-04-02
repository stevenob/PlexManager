import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    NOTIFICATION_CHANNEL_ID: int = int(os.getenv("NOTIFICATION_CHANNEL_ID", "0"))
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "")
    MEDIA_PATHS: list = [
        p.strip() for p in os.getenv("MEDIA_PATHS", os.getenv("MEDIA_PATH", "/Volumes/Media")).split(",") if p.strip()
    ]
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "60"))
    DB_PATH: str = os.getenv("DB_PATH", "plexmanager.db")

    SUPPORTED_EXTENSIONS: set = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv"}

    @classmethod
    def validate(cls) -> list[str]:
        errors = []
        if not cls.DISCORD_TOKEN:
            errors.append("DISCORD_TOKEN is required")
        if not cls.TMDB_API_KEY:
            errors.append("TMDB_API_KEY is required")
        if not cls.NOTIFICATION_CHANNEL_ID:
            errors.append("NOTIFICATION_CHANNEL_ID is required")
        if not cls.MEDIA_PATHS:
            errors.append("MEDIA_PATHS is required")
        for path in cls.MEDIA_PATHS:
            if not os.path.isdir(path):
                errors.append(f"MEDIA_PATHS entry '{path}' is not a valid directory")
        return errors
