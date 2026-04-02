"""Tests for watcher.monitor – file system watcher event handling."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from watcher.monitor import MediaEventHandler, _is_video_file, _is_temp_file


def _make_handler() -> MediaEventHandler:
    """Build a ``MediaEventHandler`` with fully mocked dependencies."""
    db = AsyncMock()
    tmdb = AsyncMock()
    bot = AsyncMock()
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    return MediaEventHandler(
        db=db,
        tmdb=tmdb,
        bot=bot,
        media_path="/fake/media",
        loop=loop,
    )


def _make_event(src_path: str, is_directory: bool = False) -> MagicMock:
    """Create a mock ``FileSystemEvent``."""
    event = MagicMock()
    event.src_path = src_path
    event.is_directory = is_directory
    return event


class TestHelperPredicates(unittest.TestCase):
    def test_is_video_file(self):
        self.assertTrue(_is_video_file("movie.mkv"))
        self.assertTrue(_is_video_file("clip.mp4"))
        self.assertTrue(_is_video_file("film.avi"))
        self.assertFalse(_is_video_file("readme.txt"))
        self.assertFalse(_is_video_file("photo.jpg"))

    def test_is_temp_file(self):
        self.assertTrue(_is_temp_file("download.part"))
        self.assertTrue(_is_temp_file("transfer.tmp"))
        self.assertTrue(_is_temp_file("file.downloading"))
        self.assertFalse(_is_temp_file("movie.mkv"))
        self.assertFalse(_is_temp_file("notes.txt"))


class TestOnCreated(unittest.TestCase):
    def test_on_created_ignores_directories(self):
        handler = _make_handler()
        event = _make_event("/fake/media/Movies", is_directory=True)
        handler.on_created(event)
        handler.loop.call_soon_threadsafe.assert_not_called()

    def test_on_created_ignores_non_video(self):
        handler = _make_handler()
        event = _make_event("/fake/media/Movies/notes.txt")
        handler.on_created(event)
        handler.loop.call_soon_threadsafe.assert_not_called()

    def test_on_created_ignores_temp_files(self):
        handler = _make_handler()
        event = _make_event("/fake/media/Movies/download.part")
        handler.on_created(event)
        handler.loop.call_soon_threadsafe.assert_not_called()

    def test_on_created_ignores_hidden_files(self):
        handler = _make_handler()
        event = _make_event("/fake/media/Movies/.hidden.mkv")
        handler.on_created(event)
        handler.loop.call_soon_threadsafe.assert_not_called()


class TestOnDeleted(unittest.TestCase):
    @patch("watcher.monitor.asyncio.run_coroutine_threadsafe")
    def test_on_deleted_processes_video(self, mock_rcts):
        handler = _make_handler()
        event = _make_event("/fake/media/Movies/The Matrix (1999)/The Matrix.mkv")
        handler.on_deleted(event)
        mock_rcts.assert_called_once()
        # First arg is the coroutine, second is the loop
        args = mock_rcts.call_args
        self.assertIs(args[0][1], handler.loop)


if __name__ == "__main__":
    unittest.main()
