"""General-purpose web search: free-text query in, {title, url, snippet} out.

Not scoped to any pipeline or site restriction -- contrast `web_signals.py`,
which restricts to `site:linkedin.com`/`reddit.com`/`x.com`/`facebook.com`
searches for intent signals, and shapes results into a different (source/
author/community/...) schema. This module is the plain, unrestricted
primitive underneath: `find_organizations` runs several directory-style
queries through it and does its own lightweight org-name/domain extraction
on top; a calling agent can also call `search_web`'s tool wrapper directly
for open-ended research (org leadership pages, conference sites, industry
directories) instead of reaching for a web-search tool outside leadorbyt.

Same two-tier backend as `web_signals.py` (Serper primary, DuckDuckGo HTML
scrape fallback), and the DDG fetch/backoff/URL-unwrap machinery is reused
directly from there rather than re-implemented -- see that module's
docstring for why Serper is preferred (DDG's HTML SERP is unreliable
against datacenter IPs) and confirmed live during the find_web_signals
outage this fixed. The result *shaping* is intentionally separate: this
module returns bare title/url/snippet with no site filter, extraction, or
dedup-by-source logic layered on.
"""

from __future__ import annotations

import logging

from . import config
from .sources.base import post_json
from .web_signals import _fetch_serp, unwrap_result_url

logger = logging.getLogger("leadorbyt.web_search")

SERPER_SEARCH_URL = "https://google.serper.dev/search"

WEB_SEARCH_CSV_FIELDS = ["title", "url", "snippet", "discovered_by_query"]


def _parse_serper_results(data: dict, discovered_by_query: str) -> list[dict]:
    items: list[dict] = []
    seen_urls: set[str] = set()
    for result in data.get("organic") or []:
        url = result.get("link") or ""
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(
            {
                "title": result.get("title") or "",
                "url": url,
                "snippet": (result.get("snippet") or "")[:500],
                "discovered_by_query": discovered_by_query,
            }
        )
    return items


def _parse_ddg_results(response, discovered_by_query: str) -> list[dict]:
    items: list[dict] = []
    seen_urls: set[str] = set()
    for node in response.css("div.result"):
        title = (node.css("a.result__a").get() or "").strip()
        href = node.css("a.result__a::attr(href)").get() or ""
        snippet = (node.css(".result__snippet").get() or "").strip()
        url = unwrap_result_url(href)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        items.append({"title": title, "url": url, "snippet": snippet[:500], "discovered_by_query": discovered_by_query})
    return items


async def _fetch_serper(query: str, num: int) -> list[dict] | None:
    data = await post_json(
        SERPER_SEARCH_URL,
        headers={"Content-Type": "application/json", "X-API-KEY": config.SERPER_API_KEY},
        # Serper's free tier rejects num > 10 outright -- see web_signals.py's
        # _fetch_serper for the same cap, confirmed live against this account.
        json_body={"q": query, "num": min(max(num, 1), 10)},
        source="web_search.serper",
    )
    if not isinstance(data, dict):
        return None
    return _parse_serper_results(data, query)


async def search_web(query: str, max_results: int = 10) -> list[dict]:
    """Run one free-text web search. Serper when configured, else a
    DuckDuckGo HTML scrape (best-effort -- see web_signals.py's module
    docstring on why that fallback isn't fully reliable).

    :return: list of {title, url, snippet, discovered_by_query}, deduped by
        URL, capped at max_results.
    """
    if not query.strip():
        return []

    items: list[dict] | None = None
    if config.SERPER_API_KEY:
        items = await _fetch_serper(query, max_results)
        if items is None:
            logger.warning("Serper search failed for %r; falling back to DDG scrape", query)

    if items is None:
        response = await _fetch_serp(query)
        items = _parse_ddg_results(response, query) if response is not None else []

    return items[:max_results]
