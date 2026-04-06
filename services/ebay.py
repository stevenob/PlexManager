"""Async eBay Browse API client for searching Blu-ray deals."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

EBAY_AUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

# eBay category IDs for DVDs & Blu-ray
CATEGORY_DVD_BLURAY = "617"  # DVDs & Blu-ray Discs


@dataclass
class EbayListing:
    """Represents a single eBay listing."""
    item_id: str
    title: str
    price: float
    currency: str
    condition: str
    listing_url: str
    image_url: str | None = None
    seller: str | None = None
    shipping_cost: float = 0.0


@dataclass
class EbaySearchResult:
    """Result of an eBay search for a movie's Blu-ray."""
    movie_title: str
    listings: list[EbayListing] = field(default_factory=list)
    average_price: float = 0.0
    deals: list[EbayListing] = field(default_factory=list)
    total_found: int = 0
    no_results: bool = False

    def compute_deals(self) -> None:
        """Identify listings priced below the average."""
        if not self.listings:
            self.no_results = True
            return

        prices = [l.price for l in self.listings]
        self.average_price = sum(prices) / len(prices)
        self.deals = [l for l in self.listings if l.price < self.average_price]
        # Sort deals by price ascending (best deal first)
        self.deals.sort(key=lambda l: l.price)


class EbayClient:
    """Async client for the eBay Browse API using OAuth client credentials."""

    def __init__(self, app_id: str, cert_id: str) -> None:
        self._app_id = app_id
        self._cert_id = cert_id
        self._session: aiohttp.ClientSession | None = None
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._rate_limit = asyncio.Semaphore(10)

    @property
    def is_configured(self) -> bool:
        """Return True if eBay credentials are provided."""
        return bool(self._app_id) and bool(self._cert_id)

    # -- HTTP layer ----------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _ensure_token(self) -> str:
        """Obtain or refresh the OAuth access token."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        credentials = base64.b64encode(
            f"{self._app_id}:{self._cert_id}".encode()
        ).decode()

        session = await self._get_session()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        }
        data = {
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        }

        try:
            async with session.post(EBAY_AUTH_URL, headers=headers, data=data) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("eBay OAuth failed (%d): %s", resp.status, body)
                    raise RuntimeError(f"eBay OAuth failed: {resp.status}")

                result = await resp.json()
                self._access_token = result["access_token"]
                self._token_expires_at = time.time() + result.get("expires_in", 7200)
                logger.debug("eBay OAuth token obtained, expires in %ds", result.get("expires_in", 0))
                return self._access_token
        except aiohttp.ClientError as exc:
            logger.error("eBay OAuth connection error: %s", exc)
            raise

    async def _api_request(
        self, url: str, params: dict[str, Any] | None = None
    ) -> dict | None:
        """Make an authenticated GET request to the eBay API."""
        if not self.is_configured:
            logger.warning("eBay API not configured — skipping request")
            return None

        token = await self._ensure_token()
        session = await self._get_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        }

        async with self._rate_limit:
            for attempt in range(3):
                try:
                    async with session.get(url, headers=headers, params=params) as resp:
                        if resp.status == 200:
                            return await resp.json()

                        if resp.status == 429:
                            retry_after = int(resp.headers.get("Retry-After", "5"))
                            logger.warning("eBay rate-limited; retrying in %ds", retry_after)
                            await asyncio.sleep(retry_after)
                            continue

                        if resp.status >= 500:
                            backoff = 2 ** attempt
                            logger.warning(
                                "eBay server error %d (attempt %d); retrying in %ds",
                                resp.status, attempt + 1, backoff,
                            )
                            await asyncio.sleep(backoff)
                            continue

                        body = await resp.text()
                        logger.error(
                            "eBay API error %d: %s (attempt %d)",
                            resp.status, body[:200], attempt + 1,
                        )
                        return None
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.error("eBay connection error: %s", exc)
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        return None
        return None

    # -- Search --------------------------------------------------------------

    async def search_bluray(
        self,
        movie_title: str,
        year: int | None = None,
        max_price: float = 0,
        limit: int = 20,
    ) -> EbaySearchResult:
        """Search eBay for Blu-ray listings of a movie.

        Args:
            movie_title: The movie title to search for.
            year: Optional release year to narrow results.
            max_price: Maximum price filter (0 = no cap).
            limit: Max number of results to fetch.

        Returns:
            EbaySearchResult with listings, average price, and flagged deals.
        """
        result = EbaySearchResult(movie_title=movie_title)

        if not self.is_configured:
            logger.info("eBay not configured — returning empty result for '%s'", movie_title)
            result.no_results = True
            return result

        # Build search query
        query = f'"{movie_title}" blu-ray'
        if year:
            query = f'"{movie_title}" ({year}) blu-ray'

        params: dict[str, Any] = {
            "q": query,
            "category_ids": CATEGORY_DVD_BLURAY,
            "limit": str(limit),
            "sort": "price",
            "filter": "conditionIds:{1000|1500|2000|2500|3000},buyingOptions:{FIXED_PRICE}",  # Buy It Now only
        }

        if max_price > 0:
            params["filter"] += f",price:[..{max_price}],priceCurrency:USD"

        data = await self._api_request(EBAY_BROWSE_URL, params)
        if data is None:
            result.no_results = True
            return result

        items = data.get("itemSummaries", [])
        result.total_found = data.get("total", 0)

        if not items:
            result.no_results = True
            return result

        for item in items:
            price_info = item.get("price", {})
            try:
                price = float(price_info.get("value", "0"))
            except (ValueError, TypeError):
                continue

            image = item.get("image", {})
            shipping_info = item.get("shippingOptions", [{}])
            shipping_cost = 0.0
            if shipping_info:
                ship_cost = shipping_info[0].get("shippingCost", {})
                try:
                    shipping_cost = float(ship_cost.get("value", "0"))
                except (ValueError, TypeError):
                    shipping_cost = 0.0
            listing = EbayListing(
                item_id=item.get("itemId", ""),
                title=item.get("title", ""),
                price=price,
                currency=price_info.get("currency", "USD"),
                condition=item.get("condition", "Unknown"),
                listing_url=item.get("itemWebUrl", ""),
                image_url=image.get("imageUrl"),
                seller=item.get("seller", {}).get("username"),
                shipping_cost=shipping_cost,
            )
            result.listings.append(listing)

        result.compute_deals()
        logger.info(
            "eBay search for '%s': %d listings, avg $%.2f, %d deals",
            movie_title, len(result.listings), result.average_price, len(result.deals),
        )
        return result

    async def search_dvd(
        self,
        movie_title: str,
        year: int | None = None,
        limit: int = 20,
    ) -> EbaySearchResult:
        """Search eBay for DVD listings of a movie (to estimate sell value).

        Args:
            movie_title: The movie title to search for.
            year: Optional release year to narrow results.
            limit: Max number of results to fetch.

        Returns:
            EbaySearchResult with listings and average price.
        """
        result = EbaySearchResult(movie_title=movie_title)

        if not self.is_configured:
            logger.info("eBay not configured — returning empty result for '%s'", movie_title)
            result.no_results = True
            return result

        query = f'"{movie_title}" DVD'
        if year:
            query = f'"{movie_title}" ({year}) DVD'

        params: dict[str, Any] = {
            "q": query,
            "category_ids": CATEGORY_DVD_BLURAY,
            "limit": str(limit),
            "sort": "price",
            "filter": "conditionIds:{1000|1500|2000|2500|3000},buyingOptions:{FIXED_PRICE}",
        }

        data = await self._api_request(EBAY_BROWSE_URL, params)
        if data is None:
            result.no_results = True
            return result

        items = data.get("itemSummaries", [])
        result.total_found = data.get("total", 0)

        if not items:
            result.no_results = True
            return result

        for item in items:
            price_info = item.get("price", {})
            try:
                price = float(price_info.get("value", "0"))
            except (ValueError, TypeError):
                continue

            image = item.get("image", {})
            shipping_info = item.get("shippingOptions", [{}])
            shipping_cost = 0.0
            if shipping_info:
                ship_cost = shipping_info[0].get("shippingCost", {})
                try:
                    shipping_cost = float(ship_cost.get("value", "0"))
                except (ValueError, TypeError):
                    shipping_cost = 0.0

            listing = EbayListing(
                item_id=item.get("itemId", ""),
                title=item.get("title", ""),
                price=price,
                currency=price_info.get("currency", "USD"),
                condition=item.get("condition", "Unknown"),
                listing_url=item.get("itemWebUrl", ""),
                image_url=image.get("imageUrl"),
                seller=item.get("seller", {}).get("username"),
                shipping_cost=shipping_cost,
            )
            result.listings.append(listing)

        result.compute_deals()
        logger.info(
            "eBay DVD search for '%s': %d listings, avg $%.2f",
            movie_title, len(result.listings), result.average_price,
        )
        return result

    # -- Lifecycle -----------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
