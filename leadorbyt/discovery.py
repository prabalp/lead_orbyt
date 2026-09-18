"""Discovery: finds candidate businesses for a niche + location on Google Maps.

Two-stage crawl:
    1. Render the search results feed (JS SPA, needs a real browser) and
       collect each result's "place" link.
    2. Fetch each place detail page and extract every contact-like field
       Maps publishes: name, category, website, phone, address, plus code,
       maps URL, coordinates, and any email/socials present in links or text.

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
import re
from urllib.parse import quote

from scrapling.spiders import Response

from . import backoff, browser_pool, config, robots
from .errors import ErrorType, LeadOrbytError
from .extractors import extract_emails, extract_phones, extract_socials

logger = logging.getLogger("leadorbyt.discovery")

FEED_SELECTOR = 'div[role="feed"]'
RESULT_LINK_SELECTOR = "a.hfpxzc"

# Google Maps place URLs embed the pin's coordinates as "@<lat>,<lon>,<zoom>z"
# in the path -- cheap to pull out of the URL we already have, no extra request.
_COORDS_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")


def _extract_coords(place_url: str) -> tuple[float | None, float | None]:
    match = _COORDS_RE.search(place_url)
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


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


def _label_value(response: Response, selector: str) -> str:
    """Google often stores the visible value after a 'Label:' prefix in aria-label."""
    raw = (response.css(selector).get("") or "").strip()
    if not raw:
        return ""
    return raw.split(":", 1)[-1].strip() if ":" in raw else raw


def _parse_place_page(response: Response, place_url: str = "", discovered_by_query: str = "") -> dict | None:
    """Pull every contact-like field Google Maps exposes on a place page.

    Structured `data-item-id` attributes (website/phone/address/plus code) are
    preferred because they are more stable than obfuscated CSS classes. Emails
    and socials are rare on Maps, but when they are present in mailto/tel/social
    links or page text they are kept rather than discarded. Website scraping
    later fills only blanks.
    """
    name = response.css("h1::text").get("").strip()
    if not name:
        return None

    website = response.css('a[data-item-id="authority"]::attr(href)').get("")
    phone = _label_value(response, 'button[data-item-id^="phone:tel:"]::attr(aria-label)')
    address = _label_value(response, 'button[data-item-id="address"]::attr(aria-label)')
    plus_code = _label_value(response, 'button[data-item-id="oloc"]::attr(aria-label)') or _label_value(
        response, 'button[data-item-id="plus_code"]::attr(aria-label)'
    )
    category = response.css('button[jsaction*="category"]::text').get("").strip()
    maps_url = place_url or response.url
    lat, lon = _extract_coords(maps_url)

    emails = extract_emails(response)
    phones = extract_phones(response)
    socials = extract_socials(response)
    if not phone and phones:
        phone = phones[0]
    email = emails[0] if emails else ""

    return {
        "business_name": name,
        "category": category,
        "website": website,
        "email": email,
        "phone": phone,
        "address": address,
        "plus_code": plus_code,
        "maps_url": maps_url,
        "lat": lat,
        "lon": lon,
        "instagram": socials.get("instagram", ""),
        "facebook": socials.get("facebook", ""),
        "linkedin": socials.get("linkedin", ""),
        "twitter": socials.get("twitter", ""),
        "youtube": socials.get("youtube", ""),
        "tiktok": socials.get("tiktok", ""),
        "discovered_by_query": discovered_by_query,
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
    discovered_by_query = f"{niche} | {location}"

    if not await robots.can_fetch(search_url):
        raise LeadOrbytError(ErrorType.ROBOTS_DISALLOWED, f"robots.txt disallows {search_url}")

    async with browser_pool.checkout() as session:

        async def _fetch_search():
            return await session.fetch(
                search_url,
                page_action=lambda page: _scroll_feed(page, max_results),
                timeout=int(config.REQUEST_TIMEOUT * 1000),
            )

        # A failure here propagates as a typed LeadOrbytError (BLOCKED or
        # TRANSPORT_ERROR) rather than degrading to an empty list -- the
        # whole search failed to even render, which is not the same thing as
        # "this niche+location genuinely has no businesses."
        feed_response = await backoff.fetch_with_backoff(search_url, _fetch_search, _looks_blocked)

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

            # Per-place failures are expected at scale (one flaky listing
            # shouldn't fail the whole search) -- skip and keep going, unlike
            # the feed-level fetch above.
            try:
                place_response = await backoff.fetch_with_backoff(place_url, _fetch_place, _looks_blocked)
            except LeadOrbytError as exc:
                logger.warning(f"Could not fetch place page {place_url}: {exc}")
                continue

            item = _parse_place_page(place_response, place_url, discovered_by_query)
            if item is None:
                logger.warning(f"Could not parse place page: {place_url}")
                continue
            items.append(item)

        return items
