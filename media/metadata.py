"""Async TMDb API client for enriching media files with metadata."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from media.models import Episode, MediaFile, MediaType, Movie

logger = logging.getLogger(__name__)

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"


class TMDbClient:
    """Async client for The Movie Database (TMDb) API."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        self._rate_limit = asyncio.Semaphore(40)

    # -- HTTP layer ----------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _request(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict | None:
        """Make a GET request to TMDb, handling rate-limits, 5xx, and errors."""
        url = f"{BASE_URL}{endpoint}"
        query: dict[str, Any] = {"api_key": self._api_key}
        if params:
            query.update(params)

        session = await self._get_session()

        async with self._rate_limit:
            for attempt in range(3):
                try:
                    async with session.get(url, params=query) as resp:
                        if resp.status == 200:
                            return await resp.json()

                        if resp.status == 429:
                            retry_after = int(
                                resp.headers.get("Retry-After", "2")
                            )
                            logger.warning(
                                "TMDb rate-limited; retrying in %ss",
                                retry_after,
                            )
                            await asyncio.sleep(retry_after)
                            continue

                        if resp.status >= 500:
                            backoff = 2 ** attempt
                            logger.warning(
                                "TMDb server error %s for %s (attempt %d); "
                                "retrying in %ss",
                                resp.status, url, attempt + 1, backoff,
                            )
                            await asyncio.sleep(backoff)
                            continue

                        logger.error(
                            "TMDb request failed: %s %s (attempt %d)",
                            resp.status,
                            url,
                            attempt + 1,
                        )
                        return None
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.error("TMDb connection error: %s", exc)
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        return None

        return None

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _build_poster_url(poster_path: str | None) -> str | None:
        if not poster_path:
            return None
        return f"{IMAGE_BASE_URL}{poster_path}"

    # -- Search --------------------------------------------------------------

    async def search_movie(
        self, title: str, year: int | None = None
    ) -> dict | None:
        """Search for a movie by title (and optional year)."""
        params: dict[str, Any] = {"query": title}
        if year is not None:
            params["year"] = year

        data = await self._request("/search/movie", params)
        if not data or not data.get("results"):
            return None
        return data["results"][0]

    async def search_tv(self, title: str) -> dict | None:
        """Search for a TV show by title."""
        data = await self._request("/search/tv", {"query": title})
        if not data or not data.get("results"):
            return None
        return data["results"][0]

    # -- Details -------------------------------------------------------------

    async def get_movie_details(self, tmdb_id: int) -> dict | None:
        """Fetch full movie details including credits."""
        return await self._request(
            f"/movie/{tmdb_id}", {"append_to_response": "credits"}
        )

    async def get_tv_details(self, tmdb_id: int) -> dict | None:
        """Fetch full TV show details."""
        return await self._request(f"/tv/{tmdb_id}")

    async def _get_episode_details(
        self, tmdb_id: int, season: int, episode: int
    ) -> dict | None:
        """Fetch episode-specific details from TMDb."""
        return await self._request(
            f"/tv/{tmdb_id}/season/{season}/episode/{episode}"
        )

    # -- Enrichment ----------------------------------------------------------

    async def enrich_media(self, media: MediaFile) -> MediaFile:
        """Search TMDb and populate *media* with rich metadata in-place."""
        if media.media_type == MediaType.MOVIE:
            return await self._enrich_movie(media)
        if media.media_type in (MediaType.TV_SHOW, MediaType.EPISODE):
            return await self._enrich_episode(media)

        logger.warning("Unknown media type for '%s'", media.filename)
        return media

    async def _enrich_movie(self, media: MediaFile) -> MediaFile:
        search_title = media.title or media.filename
        result = await self.search_movie(search_title, media.year)
        if not result:
            logger.info("No TMDb match for movie '%s'", search_title)
            return media

        details = await self.get_movie_details(result["id"])
        if not details:
            logger.info("Could not fetch details for TMDb id %s", result["id"])
            return media

        media.tmdb_id = details["id"]
        media.title = details.get("title")
        media.overview = details.get("overview")
        media.poster_url = self._build_poster_url(details.get("poster_path"))
        media.rating = details.get("vote_average")
        media.genres = [g["name"] for g in details.get("genres", [])]

        release_date = details.get("release_date") or ""
        if release_date:
            try:
                media.year = int(release_date[:4])
            except (ValueError, IndexError):
                pass

        if isinstance(media, Movie):
            media.runtime = details.get("runtime")
            credits = details.get("credits", {})
            crew = credits.get("crew", [])
            directors = [m["name"] for m in crew if m.get("job") == "Director"]
            media.director = directors[0] if directors else None

        return media

    async def _enrich_episode(self, media: MediaFile) -> MediaFile:
        search_title = (
            media.show_title
            if isinstance(media, Episode) and media.show_title
            else media.title or media.filename
        )

        result = await self.search_tv(search_title)
        if not result:
            logger.info("No TMDb match for show '%s'", search_title)
            return media

        show_details = await self.get_tv_details(result["id"])
        if not show_details:
            logger.info(
                "Could not fetch show details for TMDb id %s", result["id"]
            )
            return media

        media.tmdb_id = show_details["id"]
        media.title = show_details.get("name")
        media.overview = show_details.get("overview")
        media.poster_url = self._build_poster_url(
            show_details.get("poster_path")
        )
        media.rating = show_details.get("vote_average")
        media.genres = [g["name"] for g in show_details.get("genres", [])]

        first_air = show_details.get("first_air_date") or ""
        if first_air:
            try:
                media.year = int(first_air[:4])
            except (ValueError, IndexError):
                pass

        # Attempt episode-level enrichment
        if (
            isinstance(media, Episode)
            and media.season_number is not None
            and media.episode_number is not None
        ):
            ep = await self._get_episode_details(
                show_details["id"], media.season_number, media.episode_number
            )
            if ep:
                media.episode_title = ep.get("name")
                if ep.get("overview"):
                    media.overview = ep["overview"]

        return media

    # -- Lifecycle -----------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
