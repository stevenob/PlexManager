from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MediaType(Enum):
    MOVIE = "movie"
    TV_SHOW = "tv_show"
    EPISODE = "episode"


SUPPORTED_EXTENSIONS: set[str] = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv"}


@dataclass
class MediaFile:
    path: str
    filename: str
    media_type: MediaType
    size: int
    created_at: datetime
    modified_at: datetime
    tmdb_id: int | None = None
    title: str | None = None
    year: int | None = None
    overview: str | None = None
    poster_url: str | None = None
    rating: float | None = None
    genres: list[str] | None = None
    id: int | None = None
    resolution_width: int | None = None
    resolution_height: int | None = None
    video_codec: str | None = None

    @property
    def resolution_label(self) -> str:
        """Return a resolution label like '1080p', '720p', '480p', or 'Unknown'."""
        if self.resolution_height is None:
            return "Unknown"
        if self.resolution_width is not None and self.resolution_width >= 3840:
            return "4K"
        if self.resolution_height >= 2160:
            return "4K"
        if self.resolution_width is not None and self.resolution_width >= 1920:
            return "1080p"
        if self.resolution_height >= 1080:
            return "1080p"
        if self.resolution_width is not None and self.resolution_width >= 1280:
            return "720p"
        if self.resolution_height >= 720:
            return "720p"
        if self.resolution_height >= 480:
            return "480p"
        return f"{self.resolution_height}p"

    @property
    def codec_label(self) -> str:
        """Return human-readable codec name."""
        if self.video_codec is None:
            return "Unknown"
        codec_map = {
            "hevc": "H.265",
            "h265": "H.265",
            "h264": "H.264",
            "avc": "H.264",
            "mpeg2video": "MPEG-2",
            "mpeg4": "MPEG-4",
            "vp9": "VP9",
            "av1": "AV1",
            "vc1": "VC-1",
            "wmv3": "WMV",
        }
        return codec_map.get(self.video_codec.lower(), self.video_codec.upper())

    @property
    def is_hevc(self) -> bool:
        """Return True if the codec is H.265/HEVC."""
        return self.video_codec is not None and self.video_codec.lower() in ("hevc", "h265")

    @property
    def human_size(self) -> str:
        """Return a human-readable file size (e.g. '1.5 GB')."""
        size = float(self.size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(size) < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"

    @property
    def extension(self) -> str:
        """Return the file extension (e.g. '.mkv')."""
        return os.path.splitext(self.filename)[1].lower()


@dataclass
class Movie(MediaFile):
    runtime: int | None = None
    director: str | None = None
    media_type: MediaType = field(default=MediaType.MOVIE, init=False)


@dataclass
class TVShow:
    title: str
    tmdb_id: int | None = None
    year: int | None = None
    overview: str | None = None
    poster_url: str | None = None
    rating: float | None = None
    genres: list[str] | None = None
    total_seasons: int | None = None
    total_episodes: int | None = None


@dataclass
class Episode(MediaFile):
    show_title: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    episode_title: str | None = None
    media_type: MediaType = field(default=MediaType.EPISODE, init=False)
