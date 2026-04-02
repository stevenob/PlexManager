"""Tests for media.scanner – filename parsing and classification logic."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from media.models import Episode, MediaType, Movie, SUPPORTED_EXTENSIONS
from media.scanner import MediaScanner


def _make_scanner() -> MediaScanner:
    db = AsyncMock()
    tmdb = AsyncMock()
    return MediaScanner("/fake/media", db, tmdb)


class TestIsVideoFile(unittest.TestCase):
    def setUp(self):
        self.scanner = _make_scanner()

    def test_is_video_file(self):
        for ext in (".mkv", ".mp4", ".avi"):
            self.assertTrue(self.scanner._is_video_file(f"movie{ext}"), ext)

        for ext in (".txt", ".jpg", ".nfo"):
            self.assertFalse(self.scanner._is_video_file(f"file{ext}"), ext)

    def test_skip_temp_files(self):
        for ext in (".part", ".tmp"):
            self.assertFalse(self.scanner._is_video_file(f"download{ext}"), ext)


class TestParseMovieInfo(unittest.TestCase):
    def setUp(self):
        self.scanner = _make_scanner()

    def test_parse_movie_info_parentheses(self):
        path = "/fake/media/Movies/The Matrix (1999)/The Matrix (1999).mkv"
        title, year = self.scanner._parse_movie_info(path)
        self.assertEqual(title, "The Matrix")
        self.assertEqual(year, 1999)

    def test_parse_movie_info_dots(self):
        # _YEAR_DOT needs dots on both sides of year (".1999.")
        path = "/fake/media/Movies/The.Matrix.1999.1080p/movie.mkv"
        title, year = self.scanner._parse_movie_info(path)
        self.assertEqual(year, 1999)
        self.assertTrue(title.startswith("The Matrix"))

    def test_parse_movie_info_brackets(self):
        path = "/fake/media/Movies/The Matrix [1999]/The Matrix [1999].mkv"
        title, year = self.scanner._parse_movie_info(path)
        self.assertEqual(title, "The Matrix")
        self.assertEqual(year, 1999)

    def test_parse_movie_info_no_year(self):
        path = "/fake/media/Movies/The Matrix/The Matrix.mkv"
        title, year = self.scanner._parse_movie_info(path)
        self.assertEqual(title, "The Matrix")
        self.assertIsNone(year)


class TestParseEpisodeInfo(unittest.TestCase):
    def setUp(self):
        self.scanner = _make_scanner()

    def test_parse_episode_info_standard(self):
        path = "/fake/media/TV Shows/Breaking Bad/Season 01/Breaking.Bad.S01E03.mkv"
        show, season, episode, ep_title = self.scanner._parse_episode_info(path)
        self.assertEqual(show, "Breaking Bad")
        self.assertEqual(season, 1)
        self.assertEqual(episode, 3)

    def test_parse_episode_info_lowercase(self):
        path = "/fake/media/TV Shows/Breaking Bad/Season 01/Breaking.Bad.s01e03.mkv"
        show, season, episode, ep_title = self.scanner._parse_episode_info(path)
        self.assertEqual(show, "Breaking Bad")
        self.assertEqual(season, 1)
        self.assertEqual(episode, 3)

    def test_parse_episode_info_x_format(self):
        path = "/fake/media/TV Shows/Breaking Bad/Season 01/Breaking.Bad.1x03.mkv"
        show, season, episode, ep_title = self.scanner._parse_episode_info(path)
        self.assertEqual(show, "Breaking Bad")
        self.assertEqual(season, 1)
        self.assertEqual(episode, 3)


class TestClassifyFile(unittest.TestCase):
    def setUp(self):
        self.scanner = _make_scanner()

    @patch("os.stat")
    def test_classify_movie(self, mock_stat):
        mock_stat.return_value = MagicMock(
            st_size=700_000_000,
            st_birthtime=0.0,
            st_mtime=0.0,
        )
        path = "/fake/media/Movies/The Matrix (1999)/The Matrix (1999).mkv"
        result = self.scanner._classify_file(path)
        self.assertIsInstance(result, Movie)
        self.assertEqual(result.media_type, MediaType.MOVIE)
        self.assertEqual(result.title, "The Matrix")
        self.assertEqual(result.year, 1999)

    @patch("os.stat")
    def test_classify_episode(self, mock_stat):
        mock_stat.return_value = MagicMock(
            st_size=400_000_000,
            st_birthtime=0.0,
            st_mtime=0.0,
        )
        path = "/fake/media/TV Shows/Breaking Bad/Season 01/Breaking.Bad.S01E03.mkv"
        result = self.scanner._classify_file(path)
        self.assertIsInstance(result, Episode)
        self.assertEqual(result.media_type, MediaType.EPISODE)
        self.assertEqual(result.show_title, "Breaking Bad")
        self.assertEqual(result.season_number, 1)
        self.assertEqual(result.episode_number, 3)


if __name__ == "__main__":
    unittest.main()
