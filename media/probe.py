"""Async wrapper around ffprobe for extracting video stream resolution."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class VideoResolution:
    """Holds the width and height of a video stream."""
    width: int
    height: int
    codec: str | None = None

    @property
    def label(self) -> str:
        if self.height >= 2160:
            return "4K"
        if self.height >= 1080:
            return "1080p"
        if self.height >= 720:
            return "720p"
        if self.height >= 480:
            return "480p"
        return f"{self.height}p"

    @property
    def codec_label(self) -> str:
        """Return human-readable codec name."""
        if self.codec is None:
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
        return codec_map.get(self.codec.lower(), self.codec.upper())

    @property
    def is_hevc(self) -> bool:
        """Return True if the codec is H.265/HEVC."""
        return self.codec is not None and self.codec.lower() in ("hevc", "h265")

    def __str__(self) -> str:
        return f"{self.width}x{self.height} ({self.label}, {self.codec_label})"


def ffprobe_available() -> bool:
    """Return True if ffprobe is found on PATH."""
    return shutil.which("ffprobe") is not None


async def probe_resolution(filepath: str, timeout: float = 30.0) -> VideoResolution | None:
    """Probe a video file and return its resolution, or None on failure.

    Uses ffprobe to extract the width and height of the first video stream.
    Returns None if ffprobe is not installed, the file is inaccessible, or
    no video stream is found.
    """
    if not ffprobe_available():
        logger.warning("ffprobe not found on PATH — cannot probe resolution")
        return None

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "v:0",  # first video stream only
        filepath,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("ffprobe timed out for %s", filepath)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None
    except FileNotFoundError:
        logger.warning("ffprobe binary not found")
        return None
    except OSError as exc:
        logger.warning("ffprobe OS error for %s: %s", filepath, exc)
        return None

    if proc.returncode != 0:
        logger.debug(
            "ffprobe returned %d for %s: %s",
            proc.returncode, filepath, stderr.decode(errors="replace").strip(),
        )
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning("ffprobe returned invalid JSON for %s", filepath)
        return None

    streams = data.get("streams", [])
    if not streams:
        logger.debug("No video streams found in %s", filepath)
        return None

    stream = streams[0]
    width = stream.get("width")
    height = stream.get("height")
    codec = stream.get("codec_name")

    if width is None or height is None:
        logger.debug("Video stream missing width/height in %s", filepath)
        return None

    try:
        resolution = VideoResolution(width=int(width), height=int(height), codec=codec)
    except (ValueError, TypeError):
        logger.warning("Invalid resolution values in %s: w=%s h=%s", filepath, width, height)
        return None

    logger.debug("Probed %s: %s", filepath, resolution)
    return resolution


async def probe_many(
    filepaths: list[str],
    concurrency: int = 4,
    timeout: float = 30.0,
) -> dict[str, VideoResolution | None]:
    """Probe multiple files concurrently, returning a dict of path → resolution.

    Uses a semaphore to limit concurrent ffprobe processes.
    """
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, VideoResolution | None] = {}

    async def _probe(path: str) -> None:
        async with sem:
            results[path] = await probe_resolution(path, timeout=timeout)

    tasks = [asyncio.create_task(_probe(p)) for p in filepaths]
    await asyncio.gather(*tasks, return_exceptions=True)

    return results
