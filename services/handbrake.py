"""Async HandBrakeCLI wrapper for H.265 re-encoding."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

# Progress parsing patterns (matching AutoRipper)
_PROGRESS_RE = re.compile(r"(\d+\.\d+)\s*%")
_ETA_RE = re.compile(r"ETA\s+(\S+)")
_FPS_RE = re.compile(r"(\d+\.\d+)\s*fps")

# Track scanning patterns
_AUDIO_TRACK_RE = re.compile(r"^\s+\+\s+(\d+),\s+(.+)$")
_SUB_TRACK_RE = re.compile(r"^\s+\+\s+(\d+),\s+(.+)$")


@dataclass
class AudioTrack:
    index: int
    language: str
    codec: str
    description: str


@dataclass
class SubtitleTrack:
    index: int
    language: str
    sub_type: str


@dataclass
class EncodeProgress:
    percent: int = 0
    eta: str = ""
    fps: str = ""
    text: str = ""


@dataclass
class EncodeResult:
    success: bool
    output_path: str = ""
    original_size: int = 0
    encoded_size: int = 0
    error: str = ""

    @property
    def savings_bytes(self) -> int:
        return self.original_size - self.encoded_size

    @property
    def savings_percent(self) -> float:
        if self.original_size == 0:
            return 0.0
        return (self.savings_bytes / self.original_size) * 100


def handbrake_available(path: str = "/opt/homebrew/bin/HandBrakeCLI") -> bool:
    """Return True if HandBrakeCLI is found at the given path."""
    return os.path.isfile(path) and os.access(path, os.X_OK)


def auto_preset(width: int | None, height: int | None) -> str:
    """Pick the best HandBrake preset based on resolution.

    Exactly mirrors AutoRipper's HandBrakeService.autoPreset logic.
    """
    # Check width first for widescreen content
    if width is not None and width >= 3840:
        return "H.265 Apple VideoToolbox 2160p 4K"
    if width is not None and width >= 1920:
        return "H.265 Apple VideoToolbox 1080p"

    if height is not None:
        if height >= 2160:
            return "H.265 Apple VideoToolbox 2160p 4K"
        if height >= 1080:
            return "H.265 Apple VideoToolbox 1080p"
        if height >= 720:
            return "H.265 MKV 720p30"
        if height >= 576:
            return "H.265 MKV 576p25"

    return "H.265 MKV 480p30"


class HandBrakeEncoder:
    """Async wrapper around HandBrakeCLI for H.265 encoding."""

    def __init__(self, handbrake_path: str = "/opt/homebrew/bin/HandBrakeCLI") -> None:
        self._path = handbrake_path

    @property
    def is_available(self) -> bool:
        return handbrake_available(self._path)

    # -- Track scanning -------------------------------------------------------

    async def scan_tracks(
        self, input_path: str
    ) -> tuple[list[AudioTrack], list[SubtitleTrack]]:
        """Scan a file for audio and subtitle tracks.

        Returns (audio_tracks, subtitle_tracks).
        """
        proc = await asyncio.create_subprocess_exec(
            self._path, "--scan", "--input", input_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        # HandBrake outputs scan info to stderr
        output = (stdout.decode(errors="replace") + "\n" +
                  stderr.decode(errors="replace"))

        audio_tracks: list[AudioTrack] = []
        subtitle_tracks: list[SubtitleTrack] = []
        section: str | None = None

        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("+ audio tracks:"):
                section = "audio"
                continue
            if stripped.startswith("+ subtitle tracks:"):
                section = "subtitles"
                continue
            # End of tracks section
            if stripped.startswith("+") and section and "tracks" not in stripped:
                if not _AUDIO_TRACK_RE.match(line):
                    section = None

            if section == "audio":
                m = _AUDIO_TRACK_RE.match(line)
                if m:
                    idx = int(m.group(1))
                    desc = m.group(2).strip()
                    lang = desc.split("(")[0].strip() if "(" in desc else "Unknown"
                    codec_m = re.search(r"\((\w+)\)", desc)
                    codec = codec_m.group(1) if codec_m else "Unknown"
                    audio_tracks.append(AudioTrack(
                        index=idx, language=lang, codec=codec, description=desc
                    ))

            if section == "subtitles":
                m = _SUB_TRACK_RE.match(line)
                if m:
                    idx = int(m.group(1))
                    desc = m.group(2).strip()
                    lang = desc.split("(")[0].strip() if "(" in desc else "Unknown"
                    type_m = re.search(r"\((\w+)\)", desc)
                    sub_type = type_m.group(1) if type_m else "Unknown"
                    subtitle_tracks.append(SubtitleTrack(
                        index=idx, language=lang, sub_type=sub_type
                    ))

        logger.info(
            "Scanned %s: %d audio, %d subtitle tracks",
            input_path, len(audio_tracks), len(subtitle_tracks),
        )
        return audio_tracks, subtitle_tracks

    # -- Encoding --------------------------------------------------------------

    async def encode(
        self,
        input_path: str,
        output_path: str,
        preset: str,
        audio_tracks: list[int] | None = None,
        subtitle_tracks: list[int] | None = None,
        progress_callback: Callable[[EncodeProgress], None] | None = None,
    ) -> EncodeResult:
        """Encode a file with HandBrakeCLI.

        Args:
            input_path: Source video file.
            output_path: Destination path for encoded file.
            preset: HandBrake preset name.
            audio_tracks: List of audio track indices to include (all if None).
            subtitle_tracks: List of subtitle track indices (all if None).
            progress_callback: Called with progress updates.

        Returns:
            EncodeResult with success status and file sizes.
        """
        if not self.is_available:
            return EncodeResult(
                success=False, error=f"HandBrakeCLI not found at {self._path}"
            )

        original_size = os.path.getsize(input_path) if os.path.exists(input_path) else 0

        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        cmd = [
            self._path,
            "-i", input_path,
            "-o", output_path,
            "--preset", preset,
        ]

        # Include all audio tracks
        if audio_tracks:
            cmd += ["--audio", ",".join(str(i) for i in audio_tracks)]

        # Include all subtitle tracks, never burn in
        if subtitle_tracks:
            cmd += ["--subtitle", ",".join(str(i) for i in subtitle_tracks)]
            cmd += ["--subtitle-burned=none"]

        logger.info("Starting encode: %s → %s (preset: %s)", input_path, output_path, preset)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Read stderr for progress (HandBrake outputs progress to stderr)
        async def _read_progress() -> None:
            buf = b""
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                buf += chunk
                # HandBrake uses \r for progress lines, \n for log lines
                while b"\r" in buf or b"\n" in buf:
                    # Split on whichever comes first
                    r_idx = buf.find(b"\r")
                    n_idx = buf.find(b"\n")
                    if r_idx == -1:
                        idx = n_idx
                    elif n_idx == -1:
                        idx = r_idx
                    else:
                        idx = min(r_idx, n_idx)
                    line = buf[:idx].decode(errors="replace").strip()
                    buf = buf[idx + 1:]
                    if not line:
                        continue

                    m = _PROGRESS_RE.search(line)
                    if m and progress_callback:
                        pct = min(int(float(m.group(1))), 100)
                        eta_m = _ETA_RE.search(line)
                        fps_m = _FPS_RE.search(line)
                        progress = EncodeProgress(
                            percent=pct,
                            eta=eta_m.group(1) if eta_m else "",
                            fps=fps_m.group(1) if fps_m else "",
                            text=f"Encoding: {pct}%"
                                  + (f" — ETA {eta_m.group(1)}" if eta_m else "")
                                  + (f" ({fps_m.group(1)} fps)" if fps_m else ""),
                        )
                        progress_callback(progress)

        # Read stdout (usually empty for encode)
        async def _read_stdout() -> None:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break

        await asyncio.gather(_read_progress(), _read_stdout())
        await proc.wait()

        if proc.returncode != 0:
            return EncodeResult(
                success=False,
                original_size=original_size,
                error=f"HandBrakeCLI exited with code {proc.returncode}",
            )

        if not os.path.exists(output_path):
            return EncodeResult(
                success=False,
                original_size=original_size,
                error="Encoding completed but output file not found",
            )

        encoded_size = os.path.getsize(output_path)

        if progress_callback:
            progress_callback(EncodeProgress(
                percent=100, text="Encoding complete"
            ))

        logger.info(
            "Encode complete: %s → %s (%.1f MB → %.1f MB, %.1f%% savings)",
            input_path, output_path,
            original_size / (1024 * 1024),
            encoded_size / (1024 * 1024),
            ((original_size - encoded_size) / original_size * 100) if original_size else 0,
        )

        return EncodeResult(
            success=True,
            output_path=output_path,
            original_size=original_size,
            encoded_size=encoded_size,
        )

    async def encode_movie(
        self,
        input_path: str,
        width: int | None = None,
        height: int | None = None,
        progress_callback: Callable[[EncodeProgress], None] | None = None,
    ) -> EncodeResult:
        """High-level encode: scan tracks, pick preset, encode to temp, swap on success.

        Args:
            input_path: Path to the movie file.
            width: Video width (for preset selection).
            height: Video height (for preset selection).
            progress_callback: Called with progress updates.

        Returns:
            EncodeResult with success status and file sizes.
        """
        # Pick preset
        preset = auto_preset(width, height)
        logger.info("Auto-selected preset '%s' for %dx%d", preset, width or 0, height or 0)

        # Scan tracks
        audio, subs = await self.scan_tracks(input_path)
        audio_indices = [t.index for t in audio] if audio else None
        sub_indices = [t.index for t in subs] if subs else None

        # Build temp output path
        base, ext = os.path.splitext(input_path)
        temp_path = f"{base}_h265.mkv"

        # Encode
        result = await self.encode(
            input_path=input_path,
            output_path=temp_path,
            preset=preset,
            audio_tracks=audio_indices,
            subtitle_tracks=sub_indices,
            progress_callback=progress_callback,
        )

        if not result.success:
            # Clean up failed temp file
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return result

        # Swap: remove original, rename temp
        try:
            os.remove(input_path)
            # Rename to original path but with .mkv extension
            final_path = base + ".mkv"
            os.rename(temp_path, final_path)
            result.output_path = final_path
            logger.info("Swapped: %s → %s", temp_path, final_path)
        except OSError as exc:
            logger.error("Failed to swap files: %s", exc)
            result.error = f"Encode succeeded but file swap failed: {exc}"
            result.success = False

        return result
