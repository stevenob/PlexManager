"""Async SQLite database layer for media indexing and event tracking."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from media.models import Episode, MediaFile, MediaType, Movie

logger = logging.getLogger(__name__)

_CREATE_MEDIA_FILES = """
CREATE TABLE IF NOT EXISTS media_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT UNIQUE NOT NULL,
    filename        TEXT NOT NULL,
    media_type      TEXT NOT NULL,
    size            INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    modified_at     TEXT NOT NULL,
    tmdb_id         INTEGER,
    title           TEXT,
    year            INTEGER,
    overview        TEXT,
    poster_url      TEXT,
    rating          REAL,
    genres          TEXT,
    show_title      TEXT,
    season_number   INTEGER,
    episode_number  INTEGER,
    episode_title   TEXT,
    runtime         INTEGER,
    director        TEXT,
    resolution_width  INTEGER,
    resolution_height INTEGER,
    video_codec       TEXT
)
"""

_CREATE_WATCH_HISTORY = """
CREATE TABLE IF NOT EXISTS watch_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    title           TEXT,
    media_type      TEXT
)
"""

_CREATE_UPGRADE_STATUS = """
CREATE TABLE IF NOT EXISTS upgrade_status (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT UNIQUE NOT NULL,
    tmdb_id         INTEGER,
    title           TEXT,
    year            INTEGER,
    status          TEXT NOT NULL DEFAULT 'tracking',
    updated_at      TEXT NOT NULL,
    purchase_url    TEXT,
    CONSTRAINT valid_status CHECK (status IN ('tracking', 'ignored', 'purchased', 'no_bluray'))
)
"""

_CREATE_UPGRADE_DEALS = """
CREATE TABLE IF NOT EXISTS upgrade_deals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT NOT NULL,
    title           TEXT,
    ebay_item_id    TEXT UNIQUE NOT NULL,
    ebay_url        TEXT NOT NULL,
    price           REAL NOT NULL,
    avg_price       REAL,
    shipping_cost   REAL NOT NULL DEFAULT 0,
    condition       TEXT,
    found_at        TEXT NOT NULL,
    notified        INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_ENCODE_QUEUE = """
CREATE TABLE IF NOT EXISTS encode_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT UNIQUE NOT NULL,
    title           TEXT,
    year            INTEGER,
    status          TEXT NOT NULL DEFAULT 'queued',
    preset          TEXT,
    original_codec  TEXT,
    original_size   INTEGER,
    encoded_size    INTEGER,
    progress        INTEGER DEFAULT 0,
    queued_at       TEXT NOT NULL,
    started_at      TEXT,
    completed_at    TEXT,
    error           TEXT,
    CONSTRAINT valid_encode_status CHECK (status IN ('queued', 'encoding', 'done', 'failed', 'cancelled'))
)
"""


def _row_to_media(row: aiosqlite.Row) -> MediaFile:
    """Convert a sqlite3.Row to the appropriate MediaFile/Movie/Episode dataclass."""
    media_type = MediaType(row["media_type"])
    genres = json.loads(row["genres"]) if row["genres"] else None
    created_at = datetime.fromisoformat(row["created_at"])
    modified_at = datetime.fromisoformat(row["modified_at"])

    common = dict(
        id=row["id"],
        path=row["path"],
        filename=row["filename"],
        media_type=media_type,
        size=row["size"],
        created_at=created_at,
        modified_at=modified_at,
        tmdb_id=row["tmdb_id"],
        title=row["title"],
        year=row["year"],
        overview=row["overview"],
        poster_url=row["poster_url"],
        rating=row["rating"],
        genres=genres,
        resolution_width=row["resolution_width"],
        resolution_height=row["resolution_height"],
        video_codec=row["video_codec"],
    )

    if media_type == MediaType.MOVIE:
        subclass_common = {k: v for k, v in common.items() if k != "media_type"}
        return Movie(
            **subclass_common,
            runtime=row["runtime"],
            director=row["director"],
        )

    if media_type == MediaType.EPISODE:
        subclass_common = {k: v for k, v in common.items() if k != "media_type"}
        return Episode(
            **subclass_common,
            show_title=row["show_title"],
            season_number=row["season_number"],
            episode_number=row["episode_number"],
            episode_title=row["episode_title"],
        )

    return MediaFile(**common)


class MediaDatabase:
    """Async wrapper around an SQLite database for media indexing."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the database connection and create tables if they don't exist."""
        logger.info("Connecting to database at %s", self.db_path)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(_CREATE_MEDIA_FILES)
        await self._db.execute(_CREATE_WATCH_HISTORY)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_type_title ON media_files (media_type, title)"
        )
        # Migration: add resolution columns if missing
        for col, col_type in [("resolution_width", "INTEGER"), ("resolution_height", "INTEGER")]:
            try:
                await self._db.execute(f"ALTER TABLE media_files ADD COLUMN {col} {col_type}")
            except Exception:
                pass  # Column already exists
        try:
            await self._db.execute("ALTER TABLE media_files ADD COLUMN video_codec TEXT")
        except Exception:
            pass  # Column already exists

        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_created_at ON media_files (created_at)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_show_title ON media_files (show_title)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_watch_history_id ON watch_history (id DESC)"
        )
        await self._db.execute(_CREATE_UPGRADE_STATUS)
        await self._db.execute(_CREATE_UPGRADE_DEALS)
        # Migration: add shipping_cost column if missing
        try:
            await self._db.execute("ALTER TABLE upgrade_deals ADD COLUMN shipping_cost REAL NOT NULL DEFAULT 0")
        except Exception:
            pass  # Column already exists
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_upgrade_status_status ON upgrade_status (status)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_upgrade_deals_path ON upgrade_deals (path)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_upgrade_deals_notified ON upgrade_deals (notified)"
        )
        await self._db.execute(_CREATE_ENCODE_QUEUE)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_encode_queue_status ON encode_queue (status)"
        )
        await self._db.commit()
        logger.info("Database ready")

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None
            logger.info("Database connection closed")

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database is not connected — call connect() first")
        return self._db

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def add_media(self, media: MediaFile) -> int:
        """Insert or update a media file record. Returns the row ID."""
        genres_json = json.dumps(media.genres) if media.genres is not None else None
        created_iso = media.created_at.isoformat()
        modified_iso = media.modified_at.isoformat()

        show_title = getattr(media, "show_title", None)
        season_number = getattr(media, "season_number", None)
        episode_number = getattr(media, "episode_number", None)
        episode_title = getattr(media, "episode_title", None)
        runtime = getattr(media, "runtime", None)
        director = getattr(media, "director", None)
        resolution_width = getattr(media, "resolution_width", None)
        resolution_height = getattr(media, "resolution_height", None)
        video_codec = getattr(media, "video_codec", None)

        async with self._conn.execute(
            """
            INSERT INTO media_files (
                path, filename, media_type, size, created_at, modified_at,
                tmdb_id, title, year, overview, poster_url, rating, genres,
                show_title, season_number, episode_number, episode_title,
                runtime, director, resolution_width, resolution_height,
                video_codec
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?
            )
            ON CONFLICT(path) DO UPDATE SET
                filename      = excluded.filename,
                media_type    = excluded.media_type,
                size          = excluded.size,
                created_at    = excluded.created_at,
                modified_at   = excluded.modified_at,
                tmdb_id       = excluded.tmdb_id,
                title         = excluded.title,
                year          = excluded.year,
                overview      = excluded.overview,
                poster_url    = excluded.poster_url,
                rating        = excluded.rating,
                genres        = excluded.genres,
                show_title    = excluded.show_title,
                season_number = excluded.season_number,
                episode_number= excluded.episode_number,
                episode_title = excluded.episode_title,
                runtime       = excluded.runtime,
                director      = excluded.director,
                resolution_width  = excluded.resolution_width,
                resolution_height = excluded.resolution_height,
                video_codec       = excluded.video_codec
            """,
            (
                media.path,
                media.filename,
                media.media_type.value,
                media.size,
                created_iso,
                modified_iso,
                media.tmdb_id,
                media.title,
                media.year,
                media.overview,
                media.poster_url,
                media.rating,
                genres_json,
                show_title,
                season_number,
                episode_number,
                episode_title,
                runtime,
                director,
                resolution_width,
                resolution_height,
                video_codec,
            ),
        ) as cursor:
            row_id = cursor.lastrowid

        # Log an "added" event in watch_history
        await self._conn.execute(
            """
            INSERT INTO watch_history (path, event_type, timestamp, title, media_type)
            VALUES (?, 'added', ?, ?, ?)
            """,
            (
                media.path,
                datetime.now(timezone.utc).isoformat(),
                media.title,
                media.media_type.value,
            ),
        )
        await self._conn.commit()
        logger.debug("Added/updated media: %s (row %s)", media.path, row_id)
        return row_id  # type: ignore[return-value]

    async def remove_media(self, path: str) -> MediaFile | None:
        """Delete a media file by path and log a deletion event.

        Returns the deleted record so callers can use it for notifications,
        or ``None`` if no record matched.
        """
        media = await self.get_media(path)
        if media is None:
            logger.warning("remove_media called for unknown path: %s", path)
            return None

        await self._conn.execute("DELETE FROM media_files WHERE path = ?", (path,))

        await self._conn.execute(
            """
            INSERT INTO watch_history (path, event_type, timestamp, title, media_type)
            VALUES (?, 'deleted', ?, ?, ?)
            """,
            (
                path,
                datetime.now(timezone.utc).isoformat(),
                media.title,
                media.media_type.value,
            ),
        )
        await self._conn.commit()
        logger.info("Removed media: %s", path)
        return media

    async def get_media(self, path: str) -> MediaFile | None:
        """Retrieve a single media file by its path."""
        async with self._conn.execute(
            "SELECT * FROM media_files WHERE path = ?", (path,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_media(row)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        media_type: MediaType | None = None,
        limit: int = 25,
    ) -> list[MediaFile]:
        """Search media by title using LIKE, optionally filtered by type."""
        pattern = f"%{query}%"
        if media_type is not None:
            sql = (
                "SELECT * FROM media_files "
                "WHERE title LIKE ? AND media_type = ? "
                "ORDER BY title LIMIT ?"
            )
            params: tuple = (pattern, media_type.value, limit)  # type: ignore[assignment]
        else:
            sql = (
                "SELECT * FROM media_files "
                "WHERE title LIKE ? "
                "ORDER BY title LIMIT ?"
            )
            params = (pattern, limit)

        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()

        logger.debug("search(%r, type=%s) returned %d results", query, media_type, len(rows))
        return [_row_to_media(r) for r in rows]

    async def search_shows(self, query: str, limit: int = 25) -> list[dict]:
        """Search TV shows grouped by show title, returning summary info."""
        pattern = f"%{query}%"
        sql = (
            "SELECT show_title, "
            "COUNT(DISTINCT season_number) AS seasons, "
            "COUNT(*) AS episodes, "
            "SUM(size) AS total_size, "
            "MAX(poster_url) AS poster_url, "
            "MAX(overview) AS overview, "
            "MAX(rating) AS rating, "
            "MAX(genres) AS genres, "
            "MAX(year) AS year "
            "FROM media_files "
            "WHERE media_type = 'episode' AND show_title LIKE ? "
            "GROUP BY show_title "
            "ORDER BY show_title LIMIT ?"
        )
        async with self._conn.execute(sql, (pattern, limit)) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_movies(
        self, query: str | None = None, limit: int = 25
    ) -> list[MediaFile]:
        """List all movies, optionally filtered by title."""
        if query:
            sql = (
                "SELECT * FROM media_files "
                "WHERE media_type = 'movie' AND title LIKE ? "
                "ORDER BY title LIMIT ?"
            )
            params: tuple = (f"%{query}%", limit)
        else:
            sql = (
                "SELECT * FROM media_files "
                "WHERE media_type = 'movie' "
                "ORDER BY title LIMIT ?"
            )
            params = (limit,)
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_media(r) for r in rows]

    async def list_shows(
        self, query: str | None = None, limit: int = 25
    ) -> list[dict]:
        """List all TV shows grouped by show title, optionally filtered."""
        if query:
            sql = (
                "SELECT show_title, "
                "COUNT(DISTINCT season_number) AS seasons, "
                "COUNT(*) AS episodes, "
                "SUM(size) AS total_size, "
                "MAX(poster_url) AS poster_url, "
                "MAX(rating) AS rating, "
                "MAX(year) AS year "
                "FROM media_files "
                "WHERE media_type = 'episode' AND show_title LIKE ? "
                "GROUP BY show_title "
                "ORDER BY show_title LIMIT ?"
            )
            params = (f"%{query}%", limit)
        else:
            sql = (
                "SELECT show_title, "
                "COUNT(DISTINCT season_number) AS seasons, "
                "COUNT(*) AS episodes, "
                "SUM(size) AS total_size, "
                "MAX(poster_url) AS poster_url, "
                "MAX(rating) AS rating, "
                "MAX(year) AS year "
                "FROM media_files "
                "WHERE media_type = 'episode' "
                "GROUP BY show_title "
                "ORDER BY show_title LIMIT ?"
            )
            params = (limit,)
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_random(
        self, media_type: str | None = None
    ) -> MediaFile | None:
        """Return a random media file, optionally filtered by type."""
        if media_type == "tv_show":
            sql = "SELECT * FROM media_files WHERE media_type = 'episode' ORDER BY RANDOM() LIMIT 1"
            params: tuple = ()
        elif media_type:
            sql = "SELECT * FROM media_files WHERE media_type = ? ORDER BY RANDOM() LIMIT 1"
            params = (media_type,)
        else:
            sql = "SELECT * FROM media_files ORDER BY RANDOM() LIMIT 1"
            params = ()
        async with self._conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return _row_to_media(row) if row else None

    async def get_genres(self, media_type: str | None = None) -> list[str]:
        """Return a sorted list of distinct genres in the library."""
        if media_type == "tv_show":
            sql = "SELECT DISTINCT genres FROM media_files WHERE genres IS NOT NULL AND media_type = 'episode'"
            params: tuple = ()
        elif media_type:
            sql = "SELECT DISTINCT genres FROM media_files WHERE genres IS NOT NULL AND media_type = ?"
            params = (media_type,)
        else:
            sql = "SELECT DISTINCT genres FROM media_files WHERE genres IS NOT NULL"
            params = ()
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        genre_set: set[str] = set()
        for row in rows:
            for g in json.loads(row["genres"]):
                genre_set.add(g)
        return sorted(genre_set)

    async def search_by_genre(
        self, genre: str, media_type: str | None = None, limit: int = 25
    ) -> list[MediaFile]:
        """Return media files matching a genre."""
        pattern = f'%"{genre}"%'
        if media_type == "tv_show":
            sql = (
                "SELECT * FROM media_files "
                "WHERE genres LIKE ? AND media_type = 'episode' "
                "ORDER BY title LIMIT ?"
            )
            params: tuple = (pattern, limit)
        elif media_type:
            sql = (
                "SELECT * FROM media_files "
                "WHERE genres LIKE ? AND media_type = ? "
                "ORDER BY title LIMIT ?"
            )
            params = (pattern, media_type, limit)
        else:
            sql = (
                "SELECT * FROM media_files "
                "WHERE genres LIKE ? "
                "ORDER BY title LIMIT ?"
            )
            params = (pattern, limit)
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_media(r) for r in rows]

    async def find_duplicates(self, limit: int = 25) -> list[dict]:
        """Find media with duplicate titles (potential duplicate files)."""
        sql = (
            "SELECT title, media_type, COUNT(*) AS count, "
            "SUM(size) AS total_size, "
            "GROUP_CONCAT(path, '||') AS paths "
            "FROM media_files "
            "WHERE media_type = 'movie' "
            "GROUP BY title, media_type "
            "HAVING COUNT(*) > 1 "
            "ORDER BY total_size DESC LIMIT ?"
        )
        async with self._conn.execute(sql, (limit,)) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_recent(self, limit: int = 10) -> list[MediaFile]:
        """Return the most recently added media files."""
        async with self._conn.execute(
            "SELECT * FROM media_files ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_media(r) for r in rows]

    async def get_stats(self) -> dict:
        """Return aggregate statistics about the indexed library."""
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        sql = """
            SELECT
                COALESCE(SUM(CASE WHEN media_type = ? THEN 1 ELSE 0 END), 0) AS total_movies,
                COALESCE(SUM(CASE WHEN media_type = ? THEN 1 ELSE 0 END), 0) AS total_episodes,
                COALESCE(COUNT(DISTINCT CASE WHEN media_type = ? AND show_title IS NOT NULL
                    THEN show_title END), 0) AS total_shows,
                COALESCE(SUM(size), 0) AS total_size,
                COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS recent_count
            FROM media_files
        """
        async with self._conn.execute(
            sql,
            (MediaType.MOVIE.value, MediaType.EPISODE.value, MediaType.EPISODE.value, week_ago),
        ) as cur:
            row = await cur.fetchone()

        stats = {
            "total_movies": row[0],
            "total_episodes": row[1],
            "total_shows": row[2],
            "total_size": row[3],
            "recent_count": row[4],
        }
        logger.debug("Library stats: %s", stats)
        return stats

    async def get_all_paths(self) -> set[str]:
        """Return every indexed file path (useful for scan diffing)."""
        async with self._conn.execute("SELECT path FROM media_files") as cursor:
            rows = await cursor.fetchall()
        return {row["path"] for row in rows}

    async def get_watch_history(self, limit: int = 10) -> list[dict]:
        """Return recent watch history events."""
        async with self._conn.execute(
            "SELECT path, event_type, timestamp, title, media_type "
            "FROM watch_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_low_res_movies(self, max_height: int = 480, limit: int = 100, sort: str = "rating") -> list[MediaFile]:
        """Return movies with resolution below max_height pixels.

        Uses both height and width to correctly classify widescreen content
        (e.g. 1920x800 is 1080p despite the low height).
        sort: 'rating' (highest rated first) or 'title' (alphabetical).
        """
        order = "rating DESC NULLS LAST" if sort == "rating" else "title"
        sql = (
            "SELECT * FROM media_files "
            "WHERE media_type = 'movie' "
            "AND resolution_height IS NOT NULL "
            "AND resolution_height > 0 "
            "AND resolution_height <= ? "
            "AND (resolution_width IS NULL OR resolution_width < 1920) "
            f"GROUP BY title "
            f"ORDER BY {order} LIMIT ?"
        )
        async with self._conn.execute(sql, (max_height, limit)) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_media(r) for r in rows]

    async def get_movies_without_resolution(self, limit: int = 500) -> list[MediaFile]:
        """Return movies that haven't been probed for resolution yet."""
        sql = (
            "SELECT * FROM media_files "
            "WHERE media_type = 'movie' "
            "AND resolution_height IS NULL "
            "ORDER BY title LIMIT ?"
        )
        async with self._conn.execute(sql, (limit,)) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_media(r) for r in rows]

    async def update_resolution(self, path: str, width: int, height: int) -> None:
        """Update the resolution for a media file."""
        await self._conn.execute(
            "UPDATE media_files SET resolution_width = ?, resolution_height = ? WHERE path = ?",
            (width, height, path),
        )
        await self._conn.commit()

    async def get_unmatched_movies(self, limit: int = 100) -> list[MediaFile]:
        """Return movies that have no TMDb match (tmdb_id is NULL)."""
        sql = (
            "SELECT * FROM media_files "
            "WHERE media_type = 'movie' "
            "AND tmdb_id IS NULL "
            "ORDER BY title LIMIT ?"
        )
        async with self._conn.execute(sql, (limit,)) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_media(r) for r in rows]

    async def has_tmdb_metadata(self, path: str) -> bool:
        """Check whether the file at *path* already has TMDb metadata cached."""
        async with self._conn.execute(
            "SELECT tmdb_id FROM media_files WHERE path = ?", (path,)
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None and row["tmdb_id"] is not None

    async def get_non_hevc_movies(self, limit: int = 200) -> list[MediaFile]:
        """Return movies not encoded in H.265/HEVC."""
        sql = (
            "SELECT * FROM media_files "
            "WHERE media_type = 'movie' "
            "AND video_codec IS NOT NULL "
            "AND video_codec NOT IN ('hevc', 'h265') "
            "ORDER BY title LIMIT ?"
        )
        async with self._conn.execute(sql, (limit,)) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_media(r) for r in rows]

    async def get_encodable_movies(self, limit: int = 500) -> list[MediaFile]:
        """Return movies that should be re-encoded to H.265.

        Only targets legacy codecs (MPEG-2, VC-1, etc.) — skips H.264 and
        AV1 since transcoding those would be lossy or counterproductive.
        """
        sql = (
            "SELECT * FROM media_files "
            "WHERE media_type = 'movie' "
            "AND video_codec IS NOT NULL "
            "AND video_codec IN ('mpeg2video', 'mpeg1video', 'vc1', 'wmv3', 'mpeg4', 'msmpeg4v3', 'msmpeg4v2') "
            "ORDER BY title LIMIT ?"
        )
        async with self._conn.execute(sql, (limit,)) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_media(r) for r in rows]

    async def get_movies_without_codec(self, limit: int = 500) -> list[MediaFile]:
        """Return movies that haven't been probed for codec yet."""
        sql = (
            "SELECT * FROM media_files "
            "WHERE media_type = 'movie' "
            "AND video_codec IS NULL "
            "ORDER BY title LIMIT ?"
        )
        async with self._conn.execute(sql, (limit,)) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_media(r) for r in rows]

    async def update_codec(self, path: str, codec: str) -> None:
        """Update the video codec for a media file."""
        await self._conn.execute(
            "UPDATE media_files SET video_codec = ? WHERE path = ?",
            (codec, path),
        )
        await self._conn.commit()

    async def get_codec_stats(self) -> dict:
        """Return counts of movies by codec type."""
        sql = """
            SELECT
                COALESCE(video_codec, 'unscanned') as codec,
                COUNT(*) as count
            FROM media_files
            WHERE media_type = 'movie'
            GROUP BY video_codec
            ORDER BY count DESC
        """
        async with self._conn.execute(sql) as cursor:
            rows = await cursor.fetchall()
        return {row["codec"]: row["count"] for row in rows}

    # ------------------------------------------------------------------
    # Upgrade tracking
    # ------------------------------------------------------------------

    async def get_upgrade_status(self, path: str) -> str | None:
        """Return the upgrade status for a movie path, or None if not tracked."""
        async with self._conn.execute(
            "SELECT status FROM upgrade_status WHERE path = ?", (path,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["status"] if row else None

    async def set_upgrade_status(
        self,
        path: str,
        status: str,
        tmdb_id: int | None = None,
        title: str | None = None,
        year: int | None = None,
        purchase_url: str | None = None,
    ) -> None:
        """Insert or update the upgrade status for a movie."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO upgrade_status (path, tmdb_id, title, year, status, updated_at, purchase_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at,
                tmdb_id = COALESCE(excluded.tmdb_id, upgrade_status.tmdb_id),
                title = COALESCE(excluded.title, upgrade_status.title),
                year = COALESCE(excluded.year, upgrade_status.year),
                purchase_url = COALESCE(excluded.purchase_url, upgrade_status.purchase_url)
            """,
            (path, tmdb_id, title, year, status, now, purchase_url),
        )
        await self._conn.commit()

    async def get_movies_by_upgrade_status(
        self, status: str, limit: int = 100
    ) -> list[dict]:
        """Return upgrade_status rows filtered by status."""
        async with self._conn.execute(
            "SELECT * FROM upgrade_status WHERE status = ? ORDER BY title LIMIT ?",
            (status, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_upgrade_summary(self) -> dict:
        """Return counts by upgrade status."""
        sql = """
            SELECT status, COUNT(*) as count
            FROM upgrade_status
            GROUP BY status
        """
        async with self._conn.execute(sql) as cursor:
            rows = await cursor.fetchall()
        summary = {row["status"]: row["count"] for row in rows}
        return summary

    async def add_upgrade_deal(
        self,
        path: str,
        title: str | None,
        item_id: str,
        listing_url: str,
        price: float,
        avg_price: float,
        condition: str | None = None,
        shipping_cost: float = 0.0,
    ) -> bool:
        """Store a deal. Returns True if newly inserted, False if duplicate."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._conn.execute(
                """
                INSERT INTO upgrade_deals (path, title, ebay_item_id, ebay_url, price, avg_price, shipping_cost, condition, found_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (path, title, item_id, listing_url, price, avg_price, shipping_cost, condition, now),
            )
            await self._conn.commit()
            return True
        except Exception:
            # Duplicate item_id
            return False

    async def get_unnotified_deals(self, limit: int = 50) -> list[dict]:
        """Return deals that haven't been sent to Discord yet."""
        async with self._conn.execute(
            "SELECT * FROM upgrade_deals WHERE notified = 0 ORDER BY price ASC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_deals_notified(self, deal_ids: list[int]) -> None:
        """Mark deals as notified."""
        if not deal_ids:
            return
        placeholders = ",".join("?" * len(deal_ids))
        await self._conn.execute(
            f"UPDATE upgrade_deals SET notified = 1 WHERE id IN ({placeholders})",
            deal_ids,
        )
        await self._conn.commit()

    async def get_stale_no_bluray(self, days: int = 30, limit: int = 50) -> list[dict]:
        """Return no_bluray entries older than `days` for re-checking."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        async with self._conn.execute(
            "SELECT * FROM upgrade_status WHERE status = 'no_bluray' AND updated_at < ? LIMIT ?",
            (cutoff, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_recent_deals(self, limit: int = 25) -> list[dict]:
        """Return the most recent deals, for display purposes."""
        async with self._conn.execute(
            """
            SELECT d.*, s.status as upgrade_status
            FROM upgrade_deals d
            LEFT JOIN upgrade_status s ON d.path = s.path
            ORDER BY d.found_at DESC LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_best_deals(self, limit: int = 25) -> list[dict]:
        """Return the cheapest deal per movie, sorted by price."""
        async with self._conn.execute(
            """
            SELECT d.*, s.status as upgrade_status
            FROM upgrade_deals d
            LEFT JOIN upgrade_status s ON d.path = s.path
            INNER JOIN (
                SELECT path, MIN(price) as min_price
                FROM upgrade_deals
                GROUP BY path
            ) best ON d.path = best.path AND d.price = best.min_price
            GROUP BY d.path
            ORDER BY d.price ASC LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Encode queue
    # ------------------------------------------------------------------

    async def add_to_encode_queue(
        self,
        path: str,
        title: str | None = None,
        year: int | None = None,
        preset: str | None = None,
        original_codec: str | None = None,
        original_size: int | None = None,
    ) -> bool:
        """Add a movie to the encode queue. Returns True if added, False if duplicate."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._conn.execute(
                """
                INSERT INTO encode_queue (path, title, year, preset, original_codec, original_size, queued_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (path, title, year, preset, original_codec, original_size, now),
            )
            await self._conn.commit()
            return True
        except Exception:
            return False

    async def get_encode_queue(self, status: str | None = None, limit: int = 50) -> list[dict]:
        """Return encode queue entries, optionally filtered by status."""
        if status:
            sql = "SELECT * FROM encode_queue WHERE status = ? ORDER BY queued_at ASC LIMIT ?"
            params = (status, limit)
        else:
            sql = "SELECT * FROM encode_queue ORDER BY queued_at ASC LIMIT ?"
            params = (limit,)
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_next_encode_job(self) -> dict | None:
        """Return the next queued encode job, or None."""
        async with self._conn.execute(
            "SELECT * FROM encode_queue WHERE status = 'queued' ORDER BY queued_at ASC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_encode_status(
        self,
        path: str,
        status: str,
        progress: int | None = None,
        encoded_size: int | None = None,
        error: str | None = None,
    ) -> None:
        """Update the status of an encode queue entry."""
        now = datetime.now(timezone.utc).isoformat()
        updates = ["status = ?"]
        params: list = [status]

        if status == "encoding":
            updates.append("started_at = ?")
            params.append(now)
        if status in ("done", "failed"):
            updates.append("completed_at = ?")
            params.append(now)
        if progress is not None:
            updates.append("progress = ?")
            params.append(progress)
        if encoded_size is not None:
            updates.append("encoded_size = ?")
            params.append(encoded_size)
        if error is not None:
            updates.append("error = ?")
            params.append(error)

        params.append(path)
        sql = f"UPDATE encode_queue SET {', '.join(updates)} WHERE path = ?"
        await self._conn.execute(sql, params)
        await self._conn.commit()

    async def remove_from_encode_queue(self, path: str) -> bool:
        """Remove an entry from the encode queue. Returns True if removed."""
        async with self._conn.execute(
            "DELETE FROM encode_queue WHERE path = ? AND status IN ('queued', 'cancelled')",
            (path,),
        ) as cursor:
            removed = cursor.rowcount > 0
        await self._conn.commit()
        return removed

    async def remove_encode_done(self, path: str) -> None:
        """Remove a completed encode job from the queue."""
        await self._conn.execute(
            "DELETE FROM encode_queue WHERE path = ?", (path,)
        )
        await self._conn.commit()

    async def get_encode_stats(self) -> dict:
        """Return encode queue statistics."""
        sql = """
            SELECT
                COALESCE(SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END), 0) AS queued,
                COALESCE(SUM(CASE WHEN status = 'encoding' THEN 1 ELSE 0 END), 0) AS encoding,
                COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) AS done,
                COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed,
                COALESCE(SUM(CASE WHEN status = 'done' THEN original_size ELSE 0 END), 0) AS total_original,
                COALESCE(SUM(CASE WHEN status = 'done' THEN encoded_size ELSE 0 END), 0) AS total_encoded
            FROM encode_queue
        """
        async with self._conn.execute(sql) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else {}
