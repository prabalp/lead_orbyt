"""Discovery: finds candidate businesses for a niche + location on Google Maps.

Two-stage crawl:
    1. Render the search results feed (JS SPA, needs a real browser) and
       collect each result's "place" link.
    2. Fetch each place detail page and extract name/category/website/phone/
       address from Google's `data-item-id` attributes, which are far more
       stable than the feed's obfuscated CSS classes.

Runs directly against a pooled `AsyncStealthySession` (see browser_pool.py)
instead of Scrapling's `CrawlSpider`. `CrawlSpider`'s session lifecycle tears
the browser down at the end of every `.start()` call (confirmed against
reference/scrapling), which would mean relaunching Chromium for every single
search -- fine at low volume, not at thousands/day. Calling `session.fetch()`
directly on a long-lived pooled session only opens/closes a page per call,
so the same warm browser serves every search. All the DOM-selector knowledge
below (fragile, empirically confirmed, not from any Scrapling or Google
documentation) is unchanged from the original CrawlSpider version -- only the
transport changed.

GUESSED / UNCONFIRMED PARTS (flagged per user request):
    Google Maps' DOM is undocumented, obfuscated, and changes without notice.
    Everything below was confirmed empirically by rendering a live search and
    place page during development (see the selectors: `div[role="feed"]`,
    `a.hfpxzc`, `data-item-id="address"/"authority"/"phone:tel:*"`), NOT
    derived from any documentation. If Google changes their markup,
    `_parse_place_page()` and `FEED_SELECTOR`/`RESULT_LINK_SELECTOR` are the
    places to fix.

Why Google Maps over Yelp:
    A live check during development showed Yelp returns HTTP 403 with a
    DataDome CAPTCHA challenge on the very first plain request (no scraping
    possible without heavy anti-bot evasion), while Google Maps returns a
    real 200 response and, importantly, its own robots.txt explicitly allows
    `/maps/search/` and `/maps/place/` for generic user agents.
"""

import asyncio
import logging
from urllib.parse import quote

from scrapling.spiders import Response

from . import backoff, browser_pool, config, robots

logger = logging.getLogger("leadorbyt.discovery")

FEED_SELECTOR = 'div[role="feed"]'
RESULT_LINK_SELECTOR = "a.hfpxzc"


def _looks_blocked(response: Response | None) -> bool:
    if response is None:
        return True
    if response.status >= 400:
        return True
    body = response.body or b""
    return len(body) < 200


async def _scroll_feed(page, max_results: int, max_scrolls: int = 20) -> None:
    """page_action: scroll the results feed panel to lazily load more listings."""
    try:
        await page.wait_for_selector(FEED_SELECTOR, timeout=10_000)
    except Exception:
        return

    previous_count = -1
    for _ in range(max_scrolls):
        count = await page.eval_on_selector_all(f"{RESULT_LINK_SELECTOR}", "els => els.length")
        if count >= max_results or count == previous_count:
            break
        previous_count = count
        await page.eval_on_selector(FEED_SELECTOR, "el => el.scrollBy(0, el.scrollHeight)")
        await asyncio.sleep(1.5)


def _parse_place_page(response: Response) -> dict | None:
    """Extract business details from a rendered Google Maps place page."""
    name = response.css("h1::text").get("").strip()
    if not name:
        return None

    website = response.css('a[data-item-id="authority"]::attr(href)').get("")

    phone = ""
    phone_label = response.css('button[data-item-id^="phone:tel:"]::attr(aria-label)').get("")
    if phone_label:
        phone = phone_label.split(":", 1)[-1].strip()

    address = ""
    address_label = response.css('button[data-item-id="address"]::attr(aria-label)').get("")
    if address_label:
        address = address_label.split(":", 1)[-1].strip()

    category = response.css('button[jsaction*="category"]::text').get("").strip()

    return {
        "business_name": name,
        "category": category,
        "website": website,
        "phone": phone,
        "address": address,
    }


async def discover(niche: str, location: str, max_results: int = 20) -> list[dict]:
    """Find up to `max_results` businesses of `niche` in `location` on Google Maps.

    Checks out a pooled browser session for the duration of the search
    (feed render + each place page), respecting the shared per-domain
    backoff state so concurrent searches don't hammer Google Maps past what
    it tolerates from a single, unrotated IP.
    """
    query = quote(f"{niche} {location}")
    search_url = f"https://www.google.com/maps/search/{query}"

    if not await robots.can_fetch(search_url):
        logger.warning(f"robots.txt disallows {search_url}")
        return []

    async with browser_pool.checkout() as session:

        async def _fetch_search():
            return await session.fetch(
                search_url,
                page_action=lambda page: _scroll_feed(page, max_results),
                timeout=int(config.REQUEST_TIMEOUT * 1000),
            )

        feed_response = await backoff.fetch_with_backoff(search_url, _fetch_search, _looks_blocked)
        if feed_response is None:
            logger.warning(f"Could not render search feed for '{niche}' in '{location}'")
            return []

        links = feed_response.css(f"{RESULT_LINK_SELECTOR}::attr(href)").getall()[:max_results]
        links = [feed_response.urljoin(href) for href in links]
        logger.info(f"Found {len(links)} place links for '{niche}' in '{location}'")

        items: list[dict] = []
        for place_url in links:
            if not await robots.can_fetch(place_url):
                logger.warning(f"robots.txt disallows {place_url}")
                continue

            async def _fetch_place(place_url=place_url):
                return await session.fetch(place_url, timeout=int(config.REQUEST_TIMEOUT * 1000))

            place_response = await backoff.fetch_with_backoff(place_url, _fetch_place, _looks_blocked)
            if place_response is None:
                logger.warning(f"Could not fetch place page: {place_url}")
                continue

            item = _parse_place_page(place_response)
            if item is None:
                logger.warning(f"Could not parse place page: {place_url}")
                continue
            items.append(item)

        return items
