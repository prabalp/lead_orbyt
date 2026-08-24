"""leadorbyt MCP server: exposes lead-discovery tools over stdio.

NOTE ON THE MCP SDK: the `mcp` package that ships as of this writing (v2.0.0,
pulled in automatically as a dependency of `scrapling[all]`) does NOT have the
`FastMCP` class the user asked for -- that class was renamed to `MCPServer`
and moved to `mcp.server.mcpserver` in this SDK version. Confirmed by
inspecting the installed package directly (`mcp.server.mcpserver.MCPServer`);
this is not a guess. The decorator-based `@server.tool()` API is unchanged.

SCALE NOTE: `find_leads`/`enrich_url` keep their original external contract
(call it, get a result back) for compatibility with the existing Claude
Desktop wiring, but internally now go through a persistent SQLite cache
(store.py) and a bounded job queue + worker pool (jobs.py) backed by a small
pool of reused browser sessions (browser_pool.py), instead of in-memory
per-session dicts and an unbounded browser-per-call model. `submit_search`/
`get_search_status` are added for automation clients doing many searches a
day that don't want to hold an MCP call open for 1-2 minutes per search.
"""

import asyncio
import logging

import uvicorn
from mcp.server.mcpserver import MCPServer

from . import auth, config, jobs, store
from .enrich import enrich_website
from .merge import _normalize
from .sources import enrich_extras

logging.basicConfig(level=config.LOG_LEVEL, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("leadorbyt")

server = MCPServer(name="leadorbyt")


@server.tool()
async def find_leads(niche: str, location: str, max_results: int = 20) -> str:
    """Find businesses of a given type/niche in a given location and export contact info to CSV.

    Runs discovery (Google Maps search) -> enrichment (visits each business's
    website for email/phone/socials) -> merge -> CSV export. Results are
    cached persistently (survives restarts) for config.CACHE_TTL_DAYS, so
    repeating the same niche+location within that window returns instantly
    instead of re-scraping. For high-volume automated use, prefer
    `submit_search` + `get_search_status` so the caller isn't blocked for the
    1-2 minutes a fresh search can take.

    :param niche: The kind of business to search for, e.g. "coffee shops", "plumbers".
    :param location: Where to search, e.g. "Austin, TX".
    :param max_results: Maximum number of businesses to discover (default 20).
    :return: Absolute path to the generated CSV file.
    """
    user_id = auth.require_user_id()
    niche_key = niche.strip().lower()
    location_key = location.strip().lower()

    cached = await asyncio.to_thread(store.get_search, user_id, niche_key, location_key, max_results)
    if cached is not None:
        logger.info(f"Cache hit for search ({niche_key!r}, {location_key!r}, {max_results})")
        return cached

    job_id = await jobs.submit(user_id, niche, location, max_results)
    result = await jobs.wait_for(job_id)
    if result["error"]:
        raise RuntimeError(result["error"])
    return result["result_path"]


@server.tool()
async def submit_search(niche: str, location: str, max_results: int = 20) -> str:
    """Enqueue a lead-discovery search and return immediately with a job id.

    Use this instead of `find_leads` when running many searches a day --
    it doesn't hold the connection open while discovery/enrichment run.
    Poll the result with `get_search_status`.

    :param niche: The kind of business to search for, e.g. "coffee shops", "plumbers".
    :param location: Where to search, e.g. "Austin, TX".
    :param max_results: Maximum number of businesses to discover (default 20).
    :return: A job id to pass to `get_search_status`.
    """
    user_id = auth.require_user_id()
    niche_key = niche.strip().lower()
    location_key = location.strip().lower()

    cached = await asyncio.to_thread(store.get_search, user_id, niche_key, location_key, max_results)
    if cached is not None:
        logger.info(f"Cache hit for search ({niche_key!r}, {location_key!r}, {max_results})")
        return await jobs.submit_cached(user_id, niche, location, max_results, cached)

    return await jobs.submit(user_id, niche, location, max_results)


@server.tool()
async def get_search_status(job_id: str) -> dict:
    """Check the status of a search job submitted via `submit_search`.

    :param job_id: The job id returned by `submit_search`.
    :return: dict with `status` ("queued"/"running"/"done"/"error"), `result_path`, `error`.
    """
    user_id = auth.require_user_id()
    result = jobs.status(job_id, user_id)
    if result is None:
        return {"status": "unknown", "result_path": None, "error": "no such job id"}
    return result


@server.tool()
async def enrich_url(url: str) -> dict:
    """Run enrichment on a single business website URL.

    Useful for testing and for one-off lookups without running a full
    discovery search. Returns email/phone/socials directly. Cached
    persistently by domain for config.CACHE_TTL_DAYS.

    :param url: The business website URL to enrich.
    :return: dict with website, email, phone, instagram, facebook, linkedin, twitter, youtube, tiktok.
    """
    key = _normalize(url)
    cached = await asyncio.to_thread(store.get_enrichment, key)
    if cached is not None:
        logger.info(f"Cache hit for enrichment of {url}")
        return cached

    logger.info(f"Enriching single URL {url}")
    data = await enrich_website(url)
    await asyncio.to_thread(store.put_enrichment, key, data)
    return data


@server.tool()
async def enrich_company_extras(business_name: str, website: str = "", location: str = "") -> dict:
    """Pull additional data on one business from every configured third-party source.

    Queries whichever of OpenStreetMap, Google Places, Yelp, Foursquare, SEC
    EDGAR, OpenCorporates, Crunchbase, Clearbit, BuiltWith, Wappalyzer,
    Greenhouse/Lever/Ashby/The Muse job boards, NewsAPI, Hunter, Apollo,
    Snov, RocketReach, People Data Labs, Lusha, Cognism, ZoomInfo,
    Findymail, LeadMagic, Wiza, and Prospeo have an API key configured (see
    `.env.example`) -- sources without a configured key are silently
    skipped, never called. Not cached; each call re-fetches live.

    :param business_name: The business's display name.
    :param website: Its website URL, if known (enables domain-keyed sources).
    :param location: Free-text location (city/state), for local-search sources.
    :return: dict with a namespaced key per field found, plus `sources_used`.
    """
    logger.info(f"Fetching extra data sources for {business_name!r}")
    return await enrich_extras(business_name=business_name, website=website, location=location)


def main() -> None:
    app = server.streamable_http_app()
    app.add_middleware(auth.ApiKeyAuthMiddleware)
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
