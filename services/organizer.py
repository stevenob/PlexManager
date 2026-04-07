"""Movie organizer — identify and rename movies using TMDb metadata."""

from __future__ import annotations

import logging
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Standard naming pattern: Title (Year)
_STANDARD_PATTERN = re.compile(r"^(.+?)\s*\((\d{4})\)$")

# Characters illegal in filenames
_ILLEGAL_CHARS = re.compile(r'[/\\:*?"<>|]')


# Known edition tags to detect and preserve
_EDITION_TAGS = [
    "Director's Cut", "Directors Cut",
    "Extended Cut", "Extended Edition", "Extended",
    "Unrated", "Unrated Edition",
    "Special Edition",
    "Theatrical Cut", "Theatrical",
    "Cinematic Cut",
    "Ultimate Edition", "Ultimate Cut",
    "Final Cut",
    "Remastered",
    "Anniversary Edition",
    "Criterion Edition",
    "Collector's Edition",
    "Deluxe Edition",
]
_EDITION_RE = re.compile(
    r"[\[\(\-\s]*(" + "|".join(re.escape(t) for t in _EDITION_TAGS) + r")[\]\)\s]*",
    re.IGNORECASE,
)


@dataclass
class RenameProposal:
    """A proposed rename for a movie file."""
    current_path: str
    current_filename: str
    proposed_path: str
    proposed_filename: str
    tmdb_title: str
    tmdb_year: int | None
    tmdb_id: int | None
    tmdb_rating: float | None
    tmdb_poster: str | None
    confidence: str  # "high", "medium", "low"
    edition: str | None = None  # e.g. "Director's Cut"
    already_correct: bool = False


def clean_filename(name: str) -> str:
    """Remove illegal filename characters and normalize."""
    cleaned = _ILLEGAL_CHARS.sub("", name)
    cleaned = cleaned.replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "Untitled"


def build_movie_path(
    base_dir: str, title: str, year: int | None, ext: str = ".mkv",
    edition: str | None = None, resolution: str | None = None,
) -> str:
    """Build Plex-standard movie path.

    Single:      base/Title (Year)/Title (Year).ext
    Edition:     base/Title (Year)/Title (Year) - edition-Director's Cut.ext
    Resolution:  base/Title (Year)/Title (Year) - edition-Director's Cut - 1080p.ext

    Multiple editions/versions all go in the SAME Title (Year)/ folder.
    """
    clean_title = clean_filename(title)
    if year:
        folder_name = f"{clean_title} ({year})"
    else:
        folder_name = clean_title

    # Filename includes edition and resolution tags, folder does not
    filename_base = folder_name
    if edition:
        filename_base += f" - edition-{edition}"
    if resolution:
        filename_base += f" - {resolution}"
    filename = filename_base + ext

    return os.path.join(base_dir, folder_name, filename)


def is_well_named(filepath: str) -> bool:
    """Check if a movie file follows the Title (Year)/Title (Year).ext pattern."""
    parts = filepath.replace("\\", "/").split("/")
    if len(parts) < 2:
        return False

    folder = parts[-2]
    filename_stem = os.path.splitext(parts[-1])[0]

    # Check if folder matches Title (Year) pattern
    folder_match = _STANDARD_PATTERN.match(folder)
    if not folder_match:
        return False

    # Check if filename starts with the folder name (allowing extra tags like "1080p AAC")
    if not filename_stem.startswith(folder):
        # Also allow the filename to just be the title (year) with extra codec info
        folder_base = folder_match.group(0)
        if not filename_stem.startswith(folder_base):
            return False

    return True


async def propose_rename(
    filepath: str, tmdb_client, media_paths: list[str]
) -> RenameProposal | None:
    """Analyze a movie file and propose a rename based on TMDb lookup.

    Returns a RenameProposal, or None if the file can't be identified.
    """
    filename = os.path.basename(filepath)
    filename_stem = os.path.splitext(filename)[0]
    ext = os.path.splitext(filename)[1]

    # Parse current title and year from filename/folder
    parts = filepath.replace("\\", "/").split("/")
    parts_lower = [p.lower() for p in parts]

    # Find the movie folder (segment after "Movies")
    current_title = filename_stem
    current_year = None
    for idx, segment in enumerate(parts_lower):
        if segment == "movies" and idx + 1 < len(parts):
            movie_dir = parts[idx + 1]
            m = _STANDARD_PATTERN.match(movie_dir)
            if m:
                current_title = m.group(1).strip()
                current_year = int(m.group(2))
            else:
                current_title = movie_dir
            break

    # Clean up title for search
    search_title = current_title
    # Detect edition tag before stripping
    edition = None
    edition_m = _EDITION_RE.search(search_title)
    if edition_m:
        edition = edition_m.group(1).strip()
    # Also check filename for edition
    if not edition:
        edition_m = _EDITION_RE.search(filename_stem)
        if edition_m:
            edition = edition_m.group(1).strip()
    # Remove common tags from title for TMDb search
    for tag in ["480p", "720p", "1080p", "2160p", "4K", "AAC", "AC3", "DTS",
                "BluRay", "BRRip", "DVDRip", "WEB-DL", "x264", "x265", "HEVC"] + _EDITION_TAGS:
        search_title = re.sub(re.escape(tag), "", search_title, flags=re.IGNORECASE)
    search_title = re.sub(r"\s+", " ", search_title).strip(" -.()")
    search_title = unicodedata.normalize("NFC", search_title)

    # Search TMDb
    result = await tmdb_client.search_movie(search_title, current_year)
    if not result:
        # Try without year
        result = await tmdb_client.search_movie(search_title)

    if not result:
        return None

    tmdb_title = result.get("title", search_title)
    release_date = result.get("release_date", "")
    tmdb_year = int(release_date[:4]) if release_date and len(release_date) >= 4 else current_year
    tmdb_id = result.get("id")
    tmdb_rating = result.get("vote_average")
    poster_path = result.get("poster_path")
    tmdb_poster = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

    # Determine which media path this file is under
    base_dir = None
    for mp in media_paths:
        if filepath.startswith(mp):
            base_dir = mp
            break
    if base_dir is None:
        base_dir = os.path.dirname(os.path.dirname(filepath))

    # Detect resolution from filename for multi-version tagging
    resolution = None
    res_m = re.search(r"(2160p|4K|1080p|720p|576p|480p)", filename_stem, re.IGNORECASE)
    if res_m:
        resolution = res_m.group(1)

    # Build proposed path
    proposed_path = build_movie_path(base_dir, tmdb_title, tmdb_year, ext, edition=edition, resolution=resolution)
    proposed_filename = os.path.basename(proposed_path)

    # Check if target folder already has a file — avoid overwriting
    proposed_dir = os.path.dirname(proposed_path)
    if os.path.isdir(proposed_dir) and os.path.normpath(filepath) != os.path.normpath(proposed_path):
        if os.path.exists(proposed_path):
            # Exact filename collision — add resolution or counter to differentiate
            if not resolution:
                resolution = "v2"
            proposed_path = build_movie_path(base_dir, tmdb_title, tmdb_year, ext, edition=edition, resolution=resolution)
            proposed_filename = os.path.basename(proposed_path)

    # Check if already correct
    already_correct = os.path.normpath(filepath) == os.path.normpath(proposed_path)

    # Determine confidence
    title_lower = search_title.lower().strip()
    tmdb_lower = tmdb_title.lower().strip()
    if title_lower == tmdb_lower or title_lower in tmdb_lower or tmdb_lower in title_lower:
        confidence = "high"
    elif current_year and tmdb_year and current_year == tmdb_year:
        confidence = "medium"
    else:
        confidence = "low"

    return RenameProposal(
        current_path=filepath,
        current_filename=filename,
        proposed_path=proposed_path,
        proposed_filename=proposed_filename,
        tmdb_title=tmdb_title,
        tmdb_year=tmdb_year,
        tmdb_id=tmdb_id,
        tmdb_rating=tmdb_rating,
        tmdb_poster=tmdb_poster,
        confidence=confidence,
        edition=edition,
        already_correct=already_correct,
    )


def execute_rename(proposal: RenameProposal) -> bool:
    """Execute a rename proposal — move file and associated files (srt, nfo, artwork).

    Returns True if successful.
    """
    try:
        src = proposal.current_path
        dst = proposal.proposed_path

        if os.path.normpath(src) == os.path.normpath(dst):
            return True  # Already correct

        # Create destination directory
        dst_dir = os.path.dirname(dst)
        os.makedirs(dst_dir, exist_ok=True)

        # Move the main video file
        shutil.move(src, dst)
        logger.info("Renamed: %s → %s", src, dst)

        # Move associated files (srt, nfo, jpg, png) from the same directory
        src_dir = os.path.dirname(src)
        src_stem = os.path.splitext(os.path.basename(src))[0]
        dst_stem = os.path.splitext(os.path.basename(dst))[0]

        if os.path.isdir(src_dir):
            for f in os.listdir(src_dir):
                if f.startswith(src_stem) and not f.endswith(os.path.splitext(src)[1]):
                    # This is an associated file (e.g., movie.en.srt, movie.nfo)
                    suffix = f[len(src_stem):]
                    new_name = dst_stem + suffix
                    src_assoc = os.path.join(src_dir, f)
                    dst_assoc = os.path.join(dst_dir, new_name)
                    shutil.move(src_assoc, dst_assoc)
                    logger.info("Moved associated: %s → %s", f, new_name)

            # Also move non-stem files (poster.jpg, fanart.jpg, etc.)
            for f in os.listdir(src_dir):
                ext = os.path.splitext(f)[1].lower()
                if ext in (".jpg", ".png", ".nfo") and not f.startswith(src_stem):
                    shutil.move(os.path.join(src_dir, f), os.path.join(dst_dir, f))

            # Remove old directory if empty
            try:
                os.rmdir(src_dir)
                logger.info("Removed empty directory: %s", src_dir)
            except OSError:
                pass  # Directory not empty

        return True
    except Exception as exc:
        logger.exception("Rename failed: %s → %s: %s", proposal.current_path, proposal.proposed_path, exc)
        return False
