"""Tests for media.metadata – TMDb metadata client."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from media.metadata import TMDbClient, IMAGE_BASE_URL
from media.models import Episode, MediaType, Movie


# ---------------------------------------------------------------------------
# Mock aiohttp response helper
# ---------------------------------------------------------------------------

class MockResponse:
    """Lightweight stand-in for an ``aiohttp.ClientResponse``."""

    def __init__(self, json_data, status=200, headers=None):
        self._json = json_data
        self.status = status
        self.headers = headers or {}

    async def json(self):
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def _make_session(*responses):
    """Return a MagicMock session whose ``.get()`` yields *responses* as
    async context managers (MockResponse objects)."""
    session = MagicMock()
    session.closed = False
    if len(responses) == 1:
        session.get.return_value = responses[0]
    else:
        session.get.side_effect = list(responses)
    return session


def _run(coro):
    """Convenience wrapper around ``asyncio.run``."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuildPosterUrl(unittest.TestCase):
    def test_build_poster_url(self):
        url = TMDbClient._build_poster_url("/abc123.jpg")
        self.assertEqual(url, f"{IMAGE_BASE_URL}/abc123.jpg")

    def test_build_poster_url_none(self):
        self.assertIsNone(TMDbClient._build_poster_url(None))
        self.assertIsNone(TMDbClient._build_poster_url(""))


class TestSearchMovie(unittest.TestCase):
    def test_search_movie_success(self):
        async def _body():
            client = TMDbClient(api_key="fake-key")
            client._session = _make_session(
                MockResponse({"results": [{"id": 603, "title": "The Matrix"}]}),
            )
            return await client.search_movie("The Matrix", year=1999)

        result = _run(_body())
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 603)

    def test_search_movie_no_results(self):
        async def _body():
            client = TMDbClient(api_key="fake-key")
            client._session = _make_session(MockResponse({"results": []}))
            return await client.search_movie("NonexistentMovie12345")

        result = _run(_body())
        self.assertIsNone(result)


class TestSearchTV(unittest.TestCase):
    def test_search_tv_success(self):
        async def _body():
            client = TMDbClient(api_key="fake-key")
            client._session = _make_session(
                MockResponse({"results": [{"id": 1396, "name": "Breaking Bad"}]}),
            )
            return await client.search_tv("Breaking Bad")

        result = _run(_body())
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 1396)


class TestEnrichMovie(unittest.TestCase):
    def test_enrich_movie(self):
        async def _body():
            client = TMDbClient(api_key="fake-key")
            client._session = _make_session(
                MockResponse({"results": [{"id": 603, "title": "The Matrix"}]}),
                MockResponse({
                    "id": 603,
                    "title": "The Matrix",
                    "overview": "A computer hacker learns...",
                    "poster_path": "/poster.jpg",
                    "vote_average": 8.7,
                    "genres": [{"id": 28, "name": "Action"}],
                    "release_date": "1999-03-31",
                    "runtime": 136,
                    "credits": {
                        "crew": [{"name": "Lana Wachowski", "job": "Director"}],
                    },
                }),
            )

            movie = Movie(
                path="/movies/The Matrix (1999)/The Matrix.mkv",
                filename="The Matrix.mkv",
                size=700_000_000,
                created_at=datetime.now(tz=timezone.utc),
                modified_at=datetime.now(tz=timezone.utc),
                title="The Matrix",
                year=1999,
            )
            return await client._enrich_movie(movie)

        result = _run(_body())
        self.assertEqual(result.tmdb_id, 603)
        self.assertEqual(result.title, "The Matrix")
        self.assertIn("Action", result.genres)
        self.assertEqual(result.poster_url, f"{IMAGE_BASE_URL}/poster.jpg")
        self.assertEqual(result.runtime, 136)
        self.assertEqual(result.director, "Lana Wachowski")


class TestEnrichEpisode(unittest.TestCase):
    def test_enrich_episode(self):
        async def _body():
            client = TMDbClient(api_key="fake-key")
            client._session = _make_session(
                MockResponse({"results": [{"id": 1396, "name": "Breaking Bad"}]}),
                MockResponse({
                    "id": 1396,
                    "name": "Breaking Bad",
                    "overview": "A chemistry teacher diagnosed...",
                    "poster_path": "/bb_poster.jpg",
                    "vote_average": 9.5,
                    "genres": [{"id": 18, "name": "Drama"}],
                    "first_air_date": "2008-01-20",
                }),
                MockResponse({
                    "name": "...And the Bag's in the River",
                    "overview": "Walt deals with the aftermath...",
                }),
            )

            episode = Episode(
                path="/tv/Breaking Bad/Season 01/S01E03.mkv",
                filename="S01E03.mkv",
                size=400_000_000,
                created_at=datetime.now(tz=timezone.utc),
                modified_at=datetime.now(tz=timezone.utc),
                title="Breaking Bad",
                show_title="Breaking Bad",
                season_number=1,
                episode_number=3,
            )
            return await client._enrich_episode(episode)

        result = _run(_body())
        self.assertEqual(result.tmdb_id, 1396)
        self.assertEqual(result.title, "Breaking Bad")
        self.assertIn("Drama", result.genres)
        self.assertEqual(result.episode_title, "...And the Bag's in the River")


class TestRateLimitRetry(unittest.TestCase):
    @patch("media.metadata.asyncio.sleep", new_callable=AsyncMock)
    def test_rate_limit_retry(self, mock_sleep):
        async def _body():
            client = TMDbClient(api_key="fake-key")
            client._session = _make_session(
                MockResponse({}, status=429, headers={"Retry-After": "1"}),
                MockResponse({"results": [{"id": 603, "title": "The Matrix"}]}),
            )
            return await client.search_movie("The Matrix")

        result = _run(_body())
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 603)
        mock_sleep.assert_awaited()


if __name__ == "__main__":
    unittest.main()
