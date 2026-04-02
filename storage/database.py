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
    director        TEXT
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

        async with self._conn.execute(
            """
            INSERT INTO media_files (
                path, filename, media_type, size, created_at, modified_at,
                tmdb_id, title, year, overview, poster_url, rating, genres,
                show_title, season_number, episode_number, episode_title,
                runtime, director
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?
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
                director      = excluded.director
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
        db = self._conn

        async with db.execute(
            "SELECT COUNT(*) FROM media_files WHERE media_type = ?",
            (MediaType.MOVIE.value,),
        ) as cur:
            total_movies = (await cur.fetchone())[0]  # type: ignore[index]

        async with db.execute(
            "SELECT COUNT(*) FROM media_files WHERE media_type = ?",
            (MediaType.EPISODE.value,),
        ) as cur:
            total_episodes = (await cur.fetchone())[0]  # type: ignore[index]

        async with db.execute(
            "SELECT COUNT(DISTINCT show_title) FROM media_files WHERE media_type = ? AND show_title IS NOT NULL",
            (MediaType.EPISODE.value,),
        ) as cur:
            total_shows = (await cur.fetchone())[0]  # type: ignore[index]

        async with db.execute("SELECT COALESCE(SUM(size), 0) FROM media_files") as cur:
            total_size = (await cur.fetchone())[0]  # type: ignore[index]

        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        async with db.execute(
            "SELECT COUNT(*) FROM media_files WHERE created_at >= ?", (week_ago,)
        ) as cur:
            recent_count = (await cur.fetchone())[0]  # type: ignore[index]

        stats = {
            "total_movies": total_movies,
            "total_episodes": total_episodes,
            "total_shows": total_shows,
            "total_size": total_size,
            "recent_count": recent_count,
        }
        logger.debug("Library stats: %s", stats)
        return stats

    async def get_all_paths(self) -> set[str]:
        """Return every indexed file path (useful for scan diffing)."""
        async with self._conn.execute("SELECT path FROM media_files") as cursor:
            rows = await cursor.fetchall()
        return {row["path"] for row in rows}

    async def has_tmdb_metadata(self, path: str) -> bool:
        """Check whether the file at *path* already has TMDb metadata cached."""
        async with self._conn.execute(
            "SELECT tmdb_id FROM media_files WHERE path = ?", (path,)
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None and row["tmdb_id"] is not None
