"""Enrichment: visits a business website and extracts contact info.

Strategy: fetch the homepage with the fast `Fetcher` (plain HTTP/curl_cffi).
If the response is empty or comes back with a blocked-looking status code,
escalate that same URL to a pooled stealth-browser session (real browser).
Also follows any on-page /contact or /about links and merges what's found
there.

The fast path stays uncapped-per-call and cheap (bounded globally by
config.MAX_CONCURRENCY in server.py); the escalation path shares the same
`browser_pool` as discovery.py, since both are the same kind of expensive
stealth-browser work and should draw from one pool, not two.
"""

import asyncio

from scrapling.fetchers import Fetcher

from . import backoff, browser_pool, config
from .extractors import extract_emails, extract_phones, extract_socials, find_contact_page_links
from .sources import storefront

BLOCKED_STATUS_CODES = {401, 403, 407, 429, 444, 500, 502, 503, 504}


def _looks_blocked(response) -> bool:
    if response is None:
        return True
    if response.status in BLOCKED_STATUS_CODES:
        return True
    body = response.body or b""
    return len(body) < 200


async def _fetch(url: str):
    """Fetch a URL with the fast Fetcher, escalating to a pooled stealth session if blocked."""

    async def _fast():
        return await asyncio.to_thread(
            Fetcher.get,
            url,
            timeout=config.REQUEST_TIMEOUT,
            headers={"User-Agent": config.USER_AGENT},
            stealthy_headers=True,
        )

    response = await backoff.fetch_with_backoff(url, _fast, _looks_blocked)

    if _looks_blocked(response):
        async with browser_pool.checkout() as session:

            async def _stealth():
                return await session.fetch(
                    url,
                    network_idle=True,
                    timeout=int(config.REQUEST_TIMEOUT * 1000),
                )

            response = await backoff.fetch_with_backoff(url, _stealth, _looks_blocked)

    return response


async def enrich_website(url: str, follow_contact_pages: bool = True) -> dict:
    """Fetch `url` (and its /contact, /about links) and extract contact info.

    Returns a dict with: website, email, phone, instagram, facebook,
    linkedin, twitter, youtube, tiktok.
    """
    result = {
        "website": url,
        "email": "",
        "phone": "",
        "instagram": "",
        "facebook": "",
        "linkedin": "",
        "twitter": "",
        "youtube": "",
        "tiktok": "",
        "storefront_platforms": [],
    }

    home = await _fetch(url)
    if home is None:
        return result

    storefront_result = storefront.detect((home.body or b"").decode("utf-8", errors="ignore"))
    if storefront_result:
        result.update(storefront_result)

    pages = [home]
    if follow_contact_pages:
        for link in find_contact_page_links(home, url)[:2]:
            sub = await _fetch(link)
            if sub is not None:
                pages.append(sub)

    emails: list[str] = []
    phones: list[str] = []
    socials = {k: "" for k in ("instagram", "facebook", "linkedin", "twitter", "youtube", "tiktok")}

    for page in pages:
        emails.extend(extract_emails(page))
        phones.extend(extract_phones(page))
        page_socials = extract_socials(page)
        for platform, link in page_socials.items():
            if link and not socials[platform]:
                socials[platform] = link

    result["email"] = emails[0] if emails else ""
    result["phone"] = phones[0] if phones else ""
    result.update(socials)
    return result
