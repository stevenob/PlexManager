"""Recursive media scanner that indexes video files into the database."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

from media.models import Episode, MediaFile, MediaType, Movie, SUPPORTED_EXTENSIONS
from storage.database import MediaDatabase
from media.metadata import TMDbClient

logger = logging.getLogger(__name__)

_TEMP_SUFFIXES = frozenset({".part", ".tmp", ".downloading"})

# S01E03, s01e03, S1E3
_SE_PATTERN = re.compile(r"[Ss](\d{1,2})\s*[Ee](\d{1,3})")
# 1x03
_X_PATTERN = re.compile(r"(\d{1,2})[Xx](\d{1,3})")
# Year in parentheses, brackets, or dot-separated
_YEAR_PAREN = re.compile(r"\((\d{4})\)")
_YEAR_BRACKET = re.compile(r"\[(\d{4})\]")
_YEAR_DOT = re.compile(r"\.(\d{4})\.")
# "Season 01" directory
_SEASON_DIR = re.compile(r"[Ss]eason\s*(\d{1,2})", re.IGNORECASE)
# "Episode 03" in filename
_EPISODE_TOKEN = re.compile(r"[Ee]pisode\s*(\d{1,3})")


class MediaScanner:
    """Recursively scans a NAS mount and indexes media into the database."""

    def __init__(
        self, media_paths: list, db: MediaDatabase, tmdb: TMDbClient
    ) -> None:
        self.media_paths = media_paths if isinstance(media_paths, list) else [media_paths]
        self.db = db
        self.tmdb = tmdb
        self.supported_extensions = SUPPORTED_EXTENSIONS

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(self) -> dict:
        """Run a full scan of all media paths and return stats."""
        logger.info("Starting media scan of %s", self.media_paths)

        # Discover files on disk
        disk_paths: set[str] = set()
        count = 0
        def _walk_error(err: OSError) -> None:
            logger.warning("Cannot access path during scan: %s", err)

        for media_path in self.media_paths:
            for dirpath, dirnames, filenames in os.walk(media_path, onerror=_walk_error):
                # Skip hidden directories in-place so os.walk won't recurse
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fname in filenames:
                    if fname.startswith("."):
                        continue
                    if not self._is_video_file(fname):
                        continue
                    full = os.path.join(dirpath, fname)
                    disk_paths.add(full)
                    count += 1
                    if count % 100 == 0:
                        logger.info("Scanning... found %d files", count)

        logger.info("Scan discovered %d video files on disk", len(disk_paths))

        # Compare against indexed paths
        indexed_paths = await self.db.get_all_paths()

        # Guard: if scan found nothing but DB has records, the media paths
        # are likely inaccessible (unmounted NAS, permissions). Skip removals
        # to avoid wiping the entire database.
        if not disk_paths and indexed_paths:
            logger.warning(
                "Scan found 0 files but database has %d records — "
                "media paths may be inaccessible. Skipping removals.",
                len(indexed_paths),
            )
            return {
                "added": 0,
                "removed": 0,
                "unchanged": 0,
                "total": len(indexed_paths),
                "skipped": True,
            }

        new_paths = disk_paths - indexed_paths
        deleted_paths = indexed_paths - disk_paths
        unchanged_paths = disk_paths & indexed_paths

        added = 0
        for path in new_paths:
            try:
                media = self._classify_file(path)
                # Enrich with TMDb if metadata not already cached
                if not await self.db.has_tmdb_metadata(path):
                    try:
                        media = await self.tmdb.enrich_media(media)
                    except Exception:
                        logger.exception("TMDb enrichment failed for %s", path)
                await self.db.add_media(media)
                added += 1
            except PermissionError:
                logger.warning("Permission denied: %s", path)
            except Exception:
                logger.exception("Failed to process new file: %s", path)

        removed = 0
        for path in deleted_paths:
            try:
                await self.db.remove_media(path)
                removed += 1
            except Exception:
                logger.exception("Failed to remove deleted file: %s", path)

        stats = {
            "added": added,
            "removed": removed,
            "unchanged": len(unchanged_paths),
            "total": added + len(unchanged_paths),
        }
        logger.info("Scan complete: %s", stats)
        return stats

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify_file(self, filepath: str) -> MediaFile | Episode | Movie:
        """Parse *filepath* to build the appropriate media dataclass."""
        stat = os.stat(filepath)
        filename = os.path.basename(filepath)
        size = stat.st_size
        birth_ts = getattr(stat, "st_birthtime", None) or stat.st_ctime
        created_at = datetime.fromtimestamp(birth_ts, tz=timezone.utc)
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

        parts = filepath.replace("\\", "/").split("/")
        parts_lower = [p.lower() for p in parts]

        # Determine media type from directory structure
        if "tv shows" in parts_lower or "tv" in parts_lower:
            show_title, season, episode, episode_title = self._parse_episode_info(filepath)
            return Episode(
                path=filepath,
                filename=filename,
                size=size,
                created_at=created_at,
                modified_at=modified_at,
                title=show_title,
                show_title=show_title,
                season_number=season,
                episode_number=episode,
                episode_title=episode_title,
            )

        if "movies" in parts_lower:
            title, year = self._parse_movie_info(filepath)
            return Movie(
                path=filepath,
                filename=filename,
                size=size,
                created_at=created_at,
                modified_at=modified_at,
                title=title,
                year=year,
            )

        # Fallback — unknown media type
        stem = os.path.splitext(filename)[0]
        title = stem.replace(".", " ").replace("_", " ").strip()
        return MediaFile(
            path=filepath,
            filename=filename,
            media_type=MediaType.MOVIE,
            size=size,
            created_at=created_at,
            modified_at=modified_at,
            title=title,
        )

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_movie_info(self, filepath: str) -> tuple[str, int | None]:
        """Extract movie title and year from *filepath*.

        Handles ``Title (2023)``, ``Title.2023``, and ``Title [2023]``.
        """
        parts = filepath.replace("\\", "/").split("/")
        parts_lower = [p.lower() for p in parts]

        # Find the directory segment right after "Movies"
        movie_dir: str | None = None
        for idx, segment in enumerate(parts_lower):
            if segment == "movies" and idx + 1 < len(parts):
                movie_dir = parts[idx + 1]
                break

        source = movie_dir or os.path.splitext(os.path.basename(filepath))[0]

        year: int | None = None
        for pattern in (_YEAR_PAREN, _YEAR_BRACKET, _YEAR_DOT):
            m = pattern.search(source)
            if m:
                year = int(m.group(1))
                break

        # Strip the year and surrounding punctuation from the title
        title = source
        if year is not None:
            title = re.sub(r"[\(\[\.]?\s*" + str(year) + r"\s*[\)\]\.]?", "", title)
        title = title.replace(".", " ").replace("_", " ").strip(" -")

        return title, year

    def _parse_episode_info(
        self, filepath: str
    ) -> tuple[str, int | None, int | None, str | None]:
        """Extract show_title, season, episode number, and episode title.

        Supports ``S01E03``, ``1x03``, and ``Season 01 / Episode 03``.
        """
        parts = filepath.replace("\\", "/").split("/")
        parts_lower = [p.lower() for p in parts]
        filename = os.path.basename(filepath)
        stem = os.path.splitext(filename)[0]

        # --- Show title: the segment immediately after the TV directory ---
        show_title: str | None = None
        tv_idx: int | None = None
        for idx, segment in enumerate(parts_lower):
            if segment in ("tv shows", "tv"):
                tv_idx = idx
                break
        if tv_idx is not None and tv_idx + 1 < len(parts):
            show_title = parts[tv_idx + 1]

        # --- Season from directory structure ---
        season: int | None = None
        for segment in parts:
            m = _SEASON_DIR.search(segment)
            if m:
                season = int(m.group(1))
                break

        # --- Season + episode from filename ---
        episode: int | None = None
        episode_title: str | None = None

        se_match = _SE_PATTERN.search(stem)
        if se_match:
            season = int(se_match.group(1))
            episode = int(se_match.group(2))
            # Everything after "S01E03" is a candidate episode title
            remainder = stem[se_match.end():]
            remainder = remainder.lstrip(" -").strip()
            if remainder:
                episode_title = remainder.replace(".", " ").replace("_", " ").strip()
        else:
            x_match = _X_PATTERN.search(stem)
            if x_match:
                season = int(x_match.group(1))
                episode = int(x_match.group(2))
                remainder = stem[x_match.end():]
                remainder = remainder.lstrip(" -").strip()
                if remainder:
                    episode_title = remainder.replace(".", " ").replace("_", " ").strip()
            else:
                ep_match = _EPISODE_TOKEN.search(stem)
                if ep_match:
                    episode = int(ep_match.group(1))
                    remainder = stem[ep_match.end():]
                    remainder = remainder.lstrip(" -").strip()
                    if remainder:
                        episode_title = remainder.replace(".", " ").replace("_", " ").strip()

        return show_title, season, episode, episode_title

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _is_video_file(self, filename: str) -> bool:
        """Return ``True`` if *filename* has a supported video extension
        and is not a temporary download artefact."""
        ext = os.path.splitext(filename)[1].lower()
        if ext in _TEMP_SUFFIXES:
            return False
        return ext in self.supported_extensions
