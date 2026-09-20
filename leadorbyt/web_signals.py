"""Web/social intent discovery via a public HTML search engine + scrapling.

When someone is buying a speaking engagement, looking for a CRM, hiring a
plumber, etc., people already post that on LinkedIn, Reddit, X, and Facebook.
A `site:` search finds those posts. This module is that pass: it does NOT
replace Google Maps business discovery, and it does not log into any social
network.

Why DuckDuckGo HTML, not Google Search:
    This project already refuses URLs that robots.txt disallows (see
    robots.py / discovery.py). Google's `/search` is disallowed for crawlers;
    `html.duckduckgo.com/html/` is the public HTML SERP we can fetch with the
    same scrapling Fetcher used for website enrichment, escalating to the
    stealth pool only when the fast path looks blocked.

Selectors for the DDG HTML SERP were confirmed against the public markup
(`div.result`, `a.result__a`, `.result__snippet`) and will break if DDG
changes it -- same class of risk as Maps selectors in discovery.py.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from scrapling.spiders import Response

from . import backoff, browser_pool, config, robots
from .errors import ErrorType, LeadOrbytError
from .extractors import EMAIL_RE, _is_junk_email

logger = logging.getLogger("leadorbyt.web_signals")

DDG_HTML = "https://html.duckduckgo.com/html/"

SITE_FILTERS = {
    "linkedin": "site:linkedin.com",
    "reddit": "site:reddit.com",
    "x": "(site:x.com OR site:twitter.com)",
    "facebook": "site:facebook.com",
}

HOST_SOURCE = (
    ("linkedin.com", "linkedin"),
    ("reddit.com", "reddit"),
    ("x.com", "x"),
    ("twitter.com", "x"),
    ("facebook.com", "facebook"),
    ("fb.com", "facebook"),
)

WEB_SIGNAL_CSV_FIELDS = [
    "source",
    "author",
    "community",
    "post_title",
    "post_body",
    "url",
    "email",
    "discovered_by_query",
    "is_new_lead",
    "qualified",
    "qualification_score",
    "qualification_reason",
]


def _looks_blocked(response: Response | None) -> bool:
    if response is None:
        return True
    if response.status >= 400:
        return True
    body = response.body or b""
    return len(body) < 200


def unwrap_result_url(href: str) -> str:
    """Follow DuckDuckGo's `uddg=` redirect wrapper to the real destination."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    wrapped = parse_qs(parsed.query).get("uddg")
    if wrapped:
        return unquote(wrapped[0])
    return href


def source_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for suffix, source in HOST_SOURCE:
        if host == suffix or host.endswith("." + suffix):
            return source
    return "web"


def author_from_url(url: str, source: str) -> str:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if not parts:
        return ""
    if source == "linkedin":
        if "in" in parts:
            return parts[parts.index("in") + 1] if parts.index("in") + 1 < len(parts) else ""
        if "posts" in parts:
            slug = parts[parts.index("posts") + 1] if parts.index("posts") + 1 < len(parts) else ""
            return slug.split("_")[0]
    if source == "reddit":
        if parts[0] in ("user", "u") and len(parts) > 1:
            return parts[1]
        if parts[0] == "r" and "comments" in parts:
            return ""
    if source == "x" and parts[0] not in ("i", "search", "intent", "hashtag"):
        return parts[0]
    if source == "facebook" and parts[0] not in ("watch", "share", "groups", "events"):
        return parts[0]
    return ""


def community_from_url(url: str, source: str) -> str:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if source == "reddit" and parts[:1] == ["r"] and len(parts) > 1:
        return f"r/{parts[1]}"
    return source


def emails_from_text(text: str) -> str:
    seen: dict[str, None] = {}
    for match in EMAIL_RE.findall(text or ""):
        email = match.strip().strip(".,;:")
        if email and not _is_junk_email(email):
            seen.setdefault(email.lower(), None)
    return next(iter(seen), "")


def build_query(niche: str, location: str, site_filter: str) -> str:
    parts = [niche.strip()]
    if location.strip():
        parts.append(location.strip())
    parts.append(site_filter)
    return " ".join(parts)


def parse_ddg_html(response: Response, discovered_by_query: str) -> list[dict]:
    """Turn a DuckDuckGo HTML SERP into signal rows. Tested against FakeResponse."""
    items: list[dict] = []
    seen_urls: set[str] = set()
    for node in response.css("div.result"):
        title = (node.css("a.result__a").get() or "").strip()
        href = node.css("a.result__a::attr(href)").get() or ""
        snippet = (node.css(".result__snippet").get() or "").strip()
        url = unwrap_result_url(href)
        if not url or url in seen_urls:
            continue
        host = urlparse(url).netloc.lower()
        if "duckduckgo.com" in host or "bing.com" in host or host.endswith("google.com"):
            continue
        seen_urls.add(url)
        source = source_from_url(url)
        items.append(
            {
                "source": source,
                "author": author_from_url(url, source),
                "community": community_from_url(url, source),
                "post_title": title,
                "post_body": snippet[:500],
                "url": url,
                "email": emails_from_text(f"{title} {snippet}"),
                "discovered_by_query": discovered_by_query,
            }
        )
    return items


def dedup_key(signal: dict) -> str:
    return f"web:{signal.get('url', '')}"


async def _fetch_serp(query: str) -> Response | None:
    search_url = f"{DDG_HTML}?q={quote_plus(query)}"
    if not await robots.can_fetch(search_url):
        raise LeadOrbytError(ErrorType.ROBOTS_DISALLOWED, f"robots.txt disallows {search_url}")

    from scrapling.fetchers import Fetcher

    def _fast():
        return Fetcher.get(
            search_url,
            timeout=config.REQUEST_TIMEOUT,
            headers={"User-Agent": config.USER_AGENT},
        )

    try:
        response = await asyncio.to_thread(_fast)
        if not _looks_blocked(response):
            return response
    except Exception:
        logger.warning("Fast DDG fetch failed for %r; escalating to stealth", query)

    async def _stealth():
        async with browser_pool.checkout() as session:
            return await session.fetch(search_url, timeout=int(config.REQUEST_TIMEOUT * 1000))

    try:
        return await backoff.fetch_with_backoff(search_url, _stealth, _looks_blocked)
    except LeadOrbytError as exc:
        logger.warning("Web SERP fetch failed for %r: %s", query, exc)
        return None


async def discover(
    niche: str,
    location: str,
    max_results: int = 20,
    user_id: str | None = None,
    sites: list[str] | None = None,
) -> list[dict]:
    """Search configured social sites for posts matching niche + location."""
    if not config.WEB_SIGNALS_ENABLED:
        return []
    wanted = {s.strip().lower() for s in (sites or config.WEB_SIGNAL_SITES) if s and str(s).strip()}
    site_list = [s for s in ("linkedin", "reddit", "x", "facebook") if s in wanted]
    if not site_list:
        return []

    per_site = max(2, (max_results + len(site_list) - 1) // len(site_list))
    merged: list[dict] = []
    seen: set[str] = set()

    if user_id and "x" in site_list:
        from . import social_connect
        from .sources import x_search

        x_token = await asyncio.to_thread(social_connect.valid_access_token, user_id, "x")
        if x_token:
            query = " ".join(part for part in (niche.strip(), location.strip()) if part)
            for item in await x_search.search_recent(query, x_token, per_site):
                if item["url"] not in seen:
                    seen.add(item["url"])
                    merged.append(item)

    for site in site_list:
        query = build_query(niche, location, SITE_FILTERS[site])
        logger.info("Web signal search %s: %r", site, query)
        try:
            response = await _fetch_serp(query)
        except LeadOrbytError as exc:
            logger.warning("Skipping %s web search: %s", site, exc)
            continue
        if response is None:
            continue
        taken = 0
        for item in parse_ddg_html(response, query):
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            merged.append(item)
            taken += 1
            if taken >= per_site or len(merged) >= max_results:
                break
        if len(merged) >= max_results:
            break

    return merged[:max_results]
