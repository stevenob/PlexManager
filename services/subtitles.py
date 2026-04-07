"""Subtitle extraction (ffmpeg) and download (OpenSubtitles) service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger(__name__)

# Language codes for subtitle extraction and download
SUBTITLE_LANGUAGES = {"eng": "en", "spa": "es", "English": "en", "Spanish": "es"}
TARGET_LANGS = ["en", "es"]


@dataclass
class SubtitleTrackInfo:
    """Info about an embedded subtitle track."""
    index: int
    language: str  # ISO 639-2 (eng, spa) or descriptive
    codec: str     # subrip, ass, ssa, hdmv_pgs_subtitle, dvd_subtitle
    is_text: bool  # True if text-based (extractable to .srt)


@dataclass
class SubtitleResult:
    """Result of subtitle operations for a movie."""
    path: str
    title: str
    extracted: list[str] = field(default_factory=list)  # paths of extracted .srt files
    downloaded: list[str] = field(default_factory=list)  # paths of downloaded .srt files
    skipped: str = ""  # reason if skipped
    error: str = ""


# ── Extraction (ffmpeg) ──────────────────────────────────────────────────


async def probe_subtitles(filepath: str) -> list[SubtitleTrackInfo]:
    """Detect embedded subtitle tracks using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "s",
        filepath,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return []

    if proc.returncode != 0:
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []

    tracks = []
    for stream in data.get("streams", []):
        codec = stream.get("codec_name", "unknown")
        lang = (
            stream.get("tags", {}).get("language", "")
            or stream.get("tags", {}).get("title", "")
            or "und"
        )
        is_text = codec.lower() in ("subrip", "srt", "ass", "ssa", "webvtt", "mov_text")
        tracks.append(SubtitleTrackInfo(
            index=stream.get("index", 0),
            language=lang,
            codec=codec,
            is_text=is_text,
        ))
    return tracks


def _lang_code(lang: str) -> str:
    """Convert language tag to 2-letter code."""
    lang_lower = lang.lower().strip()
    mapping = {
        "eng": "en", "en": "en", "english": "en",
        "spa": "es", "es": "es", "spanish": "es",
        "fre": "fr", "fra": "fr", "french": "fr",
        "ger": "de", "deu": "de", "german": "de",
        "por": "pt", "portuguese": "pt",
        "und": "unknown",
    }
    return mapping.get(lang_lower, lang_lower)


def _srt_path(movie_path: str, lang_code: str) -> str:
    """Build the .srt path for a movie file and language."""
    base = os.path.splitext(movie_path)[0]
    return f"{base}.{lang_code}.srt"


def has_external_srt(movie_path: str) -> list[str]:
    """Return list of existing .srt files for a movie."""
    base = os.path.splitext(movie_path)[0]
    directory = os.path.dirname(movie_path)
    movie_stem = os.path.splitext(os.path.basename(movie_path))[0]

    srt_files = []
    if os.path.isdir(directory):
        for f in os.listdir(directory):
            if f.endswith(".srt") and f.startswith(movie_stem):
                srt_files.append(os.path.join(directory, f))
    return srt_files


async def extract_subtitles(movie_path: str) -> list[str]:
    """Extract text-based subtitle tracks from a movie file to .srt files.

    Returns list of paths of extracted .srt files.
    """
    tracks = await probe_subtitles(movie_path)
    text_tracks = [t for t in tracks if t.is_text]

    if not text_tracks:
        return []

    extracted = []
    for track in text_tracks:
        lang = _lang_code(track.language)
        if lang not in TARGET_LANGS:
            continue

        out_path = _srt_path(movie_path, lang)
        if os.path.exists(out_path):
            continue  # Already exists

        cmd = [
            "ffmpeg", "-v", "quiet", "-y",
            "-i", movie_path,
            "-map", f"0:{track.index}",
            "-c:s", "srt",
            out_path,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode == 0 and os.path.exists(out_path):
                # Verify file isn't empty
                if os.path.getsize(out_path) > 10:
                    extracted.append(out_path)
                    logger.info("Extracted %s subtitle: %s", lang, out_path)
                else:
                    os.remove(out_path)
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("Failed to extract subtitle from %s: %s", movie_path, exc)

    return extracted


# ── OpenSubtitles Download ───────────────────────────────────────────────


class OpenSubtitlesClient:
    """Async client for the OpenSubtitles.com REST API."""

    BASE_URL = "https://api.opensubtitles.com/api/v1"

    def __init__(self, api_key: str, username: str = "", password: str = "") -> None:
        self._api_key = api_key
        self._username = username
        self._password = password
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._downloads_today: int = 0
        self._max_downloads: int = 20

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "Api-Key": self._api_key,
                "User-Agent": "PlexManager v1.0",
                "Content-Type": "application/json",
            }
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers=headers,
            )
        return self._session

    async def login(self) -> bool:
        """Authenticate to get a JWT token for higher download limits."""
        if not self._username or not self._password:
            return False

        session = await self._get_session()
        try:
            async with session.post(
                f"{self.BASE_URL}/login",
                json={"username": self._username, "password": self._password},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._token = data.get("token")
                    logger.info("OpenSubtitles login successful")
                    return True
                logger.warning("OpenSubtitles login failed: %d", resp.status)
                return False
        except Exception as exc:
            logger.warning("OpenSubtitles login error: %s", exc)
            return False

    async def search(
        self, title: str, year: int | None = None, languages: str = "en,es"
    ) -> list[dict]:
        """Search for subtitles by movie title."""
        session = await self._get_session()
        params = {
            "query": title,
            "languages": languages,
            "type": "movie",
        }
        if year:
            params["year"] = str(year)

        try:
            async with session.get(
                f"{self.BASE_URL}/subtitles", params=params
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", [])
                logger.warning("OpenSubtitles search failed: %d", resp.status)
                return []
        except Exception as exc:
            logger.warning("OpenSubtitles search error: %s", exc)
            return []

    async def download(self, file_id: int, output_path: str) -> bool:
        """Download a subtitle file by file_id."""
        if self._downloads_today >= self._max_downloads:
            logger.warning("OpenSubtitles daily download limit reached (%d)", self._max_downloads)
            return False

        session = await self._get_session()
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            async with session.post(
                f"{self.BASE_URL}/download",
                json={"file_id": file_id},
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    logger.warning("OpenSubtitles download request failed: %d", resp.status)
                    return False
                data = await resp.json()
                download_url = data.get("link")
                if not download_url:
                    return False

            # Download the actual file
            async with session.get(download_url) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    with open(output_path, "wb") as f:
                        f.write(content)
                    self._downloads_today += 1
                    logger.info("Downloaded subtitle: %s (%d/%d today)",
                              output_path, self._downloads_today, self._max_downloads)
                    return True
                return False
        except Exception as exc:
            logger.warning("OpenSubtitles download error: %s", exc)
            return False

    async def download_for_movie(
        self, movie_path: str, title: str, year: int | None = None
    ) -> list[str]:
        """Search and download missing subtitles for a movie.

        Returns list of downloaded .srt file paths.
        """
        downloaded = []
        existing = has_external_srt(movie_path)
        existing_langs = set()
        for srt in existing:
            # Extract lang code from filename (Movie.en.srt -> en)
            parts = os.path.splitext(srt)[0].rsplit(".", 1)
            if len(parts) == 2 and len(parts[1]) == 2:
                existing_langs.add(parts[1])

        # Determine which languages we still need
        needed_langs = [l for l in TARGET_LANGS if l not in existing_langs]
        if not needed_langs:
            return []

        results = await self.search(title, year, languages=",".join(needed_langs))
        if not results:
            return []

        # Group by language, pick best (most downloads) for each
        by_lang: dict[str, dict] = {}
        for sub in results:
            attrs = sub.get("attributes", {})
            lang = attrs.get("language", "")
            if lang not in needed_langs:
                continue
            files = attrs.get("files", [])
            if not files:
                continue
            download_count = attrs.get("download_count", 0)
            if lang not in by_lang or download_count > by_lang[lang].get("_count", 0):
                by_lang[lang] = {
                    "file_id": files[0].get("file_id"),
                    "lang": lang,
                    "_count": download_count,
                }

        for lang, info in by_lang.items():
            out_path = _srt_path(movie_path, lang)
            if await self.download(info["file_id"], out_path):
                downloaded.append(out_path)

        return downloaded

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
