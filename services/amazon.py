"""Amazon search scraper for Blu-ray and DVD price lookups.

Scrapes Amazon search results pages — no API key required.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

AMAZON_SEARCH_URL = "https://www.amazon.com/s"

# Realistic browser headers to avoid blocks
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

# Regex patterns for parsing Amazon search results HTML
_PRICE_WHOLE = re.compile(r'a-price-whole">(\d+)')
_PRICE_FRAC = re.compile(r'a-price-fraction">(\d+)')
_TITLE_RE = re.compile(
    r'a-size-medium[^"]*a-text-normal"[^>]*>(.*?)</span>', re.DOTALL
)
_ASIN_RE = re.compile(r'data-asin="([A-Z0-9]{10})"')
_IMG_RE = re.compile(r'<img[^>]+class="s-image"[^>]+src="([^"]+)"')


@dataclass
class Listing:
    """Represents a single Amazon listing."""
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
class SearchResult:
    """Result of an Amazon search for a movie's Blu-ray or DVD."""
    movie_title: str
    listings: list[Listing] = field(default_factory=list)
    average_price: float = 0.0
    deals: list[Listing] = field(default_factory=list)
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
        self.deals.sort(key=lambda l: l.price)


def _parse_results(html: str) -> list[dict]:
    """Parse Amazon search results HTML into raw listing dicts."""
    results = []

    # Split by data-asin to find individual results
    chunks = re.split(r'data-asin="([A-Z0-9]{10})"', html)

    # chunks alternates: [before, asin1, after1, asin2, after2, ...]
    i = 1
    while i < len(chunks) - 1:
        asin = chunks[i]
        block = chunks[i + 1]
        i += 2

        # Skip sponsored/ad results
        if "AdHolder" in block or "s-sponsored" in block:
            continue

        # Extract title
        title_m = _TITLE_RE.search(block)
        if not title_m:
            continue
        title = title_m.group(1).strip()
        title = re.sub(r"<[^>]+>", "", title)  # Strip any inner HTML tags

        # Extract price
        whole_m = _PRICE_WHOLE.search(block)
        frac_m = _PRICE_FRAC.search(block)
        if not whole_m:
            continue
        whole = whole_m.group(1).replace(",", "")
        frac = frac_m.group(1) if frac_m else "00"
        try:
            price = float(f"{whole}.{frac}")
        except ValueError:
            continue

        # Extract image
        img_m = _IMG_RE.search(block)
        image_url = img_m.group(1) if img_m else None

        results.append({
            "asin": asin,
            "title": title,
            "price": price,
            "image_url": image_url,
        })

    return results


class AmazonSearchClient:
    """Scrapes Amazon search results for Blu-ray and DVD pricing."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._rate_limit = asyncio.Semaphore(2)

    @property
    def is_configured(self) -> bool:
        """Always True — no API key needed."""
        return True

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            jar = aiohttp.CookieJar()
            self._session = aiohttp.ClientSession(
                timeout=timeout, headers=_HEADERS, cookie_jar=jar
            )
            # Prime cookies by visiting Amazon homepage
            try:
                async with self._session.get("https://www.amazon.com/") as resp:
                    await resp.read()
            except Exception:
                pass
        return self._session

    async def _search(self, query: str, category: str = "movies-tv") -> str | None:
        """Fetch Amazon search results HTML using curl (bypasses bot detection)."""
        import urllib.parse
        params = urllib.parse.urlencode({"k": query, "i": category, "ref": "nb_sb_noss"})
        url = f"{AMAZON_SEARCH_URL}?{params}"

        async with self._rate_limit:
            for attempt in range(3):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "curl", "-s", "-L", "--compressed",
                        "-H", f"User-Agent: {_HEADERS['User-Agent']}",
                        "-H", f"Accept: {_HEADERS['Accept']}",
                        "-H", f"Accept-Language: {_HEADERS['Accept-Language']}",
                        url,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)

                    if proc.returncode != 0 or not stdout:
                        logger.warning("curl failed (attempt %d)", attempt + 1)
                        if attempt < 2:
                            await asyncio.sleep(2 ** attempt)
                        continue

                    html = stdout.decode(errors="replace")
                    if "To discuss automated access" in html or len(html) < 5000:
                        logger.warning("Amazon CAPTCHA/block detected (attempt %d)", attempt + 1)
                        if attempt < 2:
                            await asyncio.sleep(2 ** (attempt + 1))
                        continue

                    return html
                except asyncio.TimeoutError:
                    logger.warning("Amazon search timed out (attempt %d)", attempt + 1)
                    if attempt < 2:
                        await asyncio.sleep(2)
                except Exception as exc:
                    logger.warning("Amazon search error: %s", exc)
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
        return None

    async def search_bluray(
        self,
        movie_title: str,
        year: int | None = None,
        max_price: float = 0,
        limit: int = 20,
    ) -> SearchResult:
        """Search Amazon for Blu-ray listings of a movie."""
        result = SearchResult(movie_title=movie_title)

        query = f"{movie_title} Blu-ray"
        if year:
            query = f"{movie_title} {year} Blu-ray"

        html = await self._search(query)
        if html is None:
            result.no_results = True
            return result

        raw = _parse_results(html)
        if not raw:
            result.no_results = True
            return result

        for item in raw[:limit]:
            # Skip free/Kindle results and suspiciously cheap non-physical items
            if item["price"] < 3.0:
                continue
            if max_price > 0 and item["price"] > max_price:
                continue
            # Skip results that don't mention Blu-ray in the title
            title_lower = item["title"].lower()
            if "blu-ray" not in title_lower and "blu ray" not in title_lower and "bluray" not in title_lower:
                continue
            listing = Listing(
                item_id=item["asin"],
                title=item["title"],
                price=item["price"],
                currency="USD",
                condition="New",
                listing_url=f"https://www.amazon.com/dp/{item['asin']}",
                image_url=item.get("image_url"),
                shipping_cost=0.0,  # Amazon typically free shipping
            )
            result.listings.append(listing)

        result.total_found = len(result.listings)
        result.compute_deals()

        logger.info(
            "Amazon Blu-ray search for '%s': %d listings, avg $%.2f, %d deals",
            movie_title, len(result.listings), result.average_price, len(result.deals),
        )
        return result

    async def search_dvd(
        self,
        movie_title: str,
        year: int | None = None,
        limit: int = 20,
    ) -> SearchResult:
        """Search Amazon for DVD listings of a movie (sell value estimate)."""
        result = SearchResult(movie_title=movie_title)

        query = f"{movie_title} DVD"
        if year:
            query = f"{movie_title} {year} DVD"

        html = await self._search(query)
        if html is None:
            result.no_results = True
            return result

        raw = _parse_results(html)
        if not raw:
            result.no_results = True
            return result

        for item in raw[:limit]:
            if item["price"] < 1.0:
                continue
            title_lower = item["title"].lower()
            if "dvd" not in title_lower:
                continue
            listing = Listing(
                item_id=item["asin"],
                title=item["title"],
                price=item["price"],
                currency="USD",
                condition="New",
                listing_url=f"https://www.amazon.com/dp/{item['asin']}",
                image_url=item.get("image_url"),
                shipping_cost=0.0,
            )
            result.listings.append(listing)

        result.total_found = len(result.listings)
        result.compute_deals()

        logger.info(
            "Amazon DVD search for '%s': %d listings, avg $%.2f",
            movie_title, len(result.listings), result.average_price,
        )
        return result

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
