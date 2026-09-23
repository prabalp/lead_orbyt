"""leadorbyt MCP server: exposes lead-discovery tools over stdio.

NOTE ON THE MCP SDK: the `mcp` package that ships as of this writing (v2.0.0,
pulled in automatically as a dependency of `scrapling[all]`) does NOT have the
`FastMCP` class the user asked for -- that class was renamed to `MCPServer`
and moved to `mcp.server.mcpserver` in this SDK version. Confirmed by
inspecting the installed package directly (`mcp.server.mcpserver.MCPServer`);
this is not a guess. The decorator-based `@server.tool()` API is unchanged.

SCALE NOTE: `find_leads_maps`/`enrich_url` keep their original external contract
(call it, get a result back) for compatibility with the existing Claude
Desktop wiring, but internally now go through a persistent SQLite cache
(store.py) and a bounded job queue + worker pool (jobs.py) backed by a small
pool of reused browser sessions (browser_pool.py), instead of in-memory
per-session dicts and an unbounded browser-per-call model. `submit_search`/
`get_search_status` are added for automation clients doing many searches a
day that don't want to hold an MCP call open for 1-2 minutes per search.
"""

import asyncio
import csv
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import (
    auth,
    config,
    files,
    jobs,
    org_jobs,
    people_jobs,
    qualify_ml,
    signal_jobs,
    signup,
    social_connect,
    store,
    web_jobs,
    web_search_jobs,
)
from .enrich import enrich_website
from .errors import ErrorType, LeadOrbytError
from .merge import _normalize
from .people_merge import dedup_key as person_dedup_key
from .signal_merge import dedup_key as signal_dedup_key
from .sources import enrich_extras, person_search, reddit

logging.basicConfig(level=config.LOG_LEVEL, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("leadorbyt")

server = MCPServer(name="leadorbyt")


def _raise_as_runtime_error(exc: Exception) -> None:
    """Convert any exception into the stable `error: <type>: <message>` contract."""
    if isinstance(exc, LeadOrbytError):
        raise RuntimeError(str(exc)) from exc
    raise RuntimeError(f"error: {ErrorType.INTERNAL.value}: {exc}") from exc


def _count_csv_rows(path: str) -> int:
    with open(path, newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def _discovery_result(path: str) -> dict:
    return {
        "status": "discovery_complete",
        "result_path": files.download_url(path),
        "lead_count": _count_csv_rows(path),
        "enriched": False,
        "next_action": (
            "Call read_result_csv(result_path=...) to retrieve the rows, then show "
            "the Google Maps business list. If the user also wants people already "
            "posting about this (LinkedIn, Reddit, X, Facebook), call "
            "find_web_signals next. Do not assume they want both. Then ask "
            "whether they want website enrichment of the Maps list. Do not call "
            "enrich_lead_list unless the user explicitly confirms. Mention that "
            "this CSV is auto-deleted after a retention window (see "
            "read_result_csv's docstring) and offer to help them download it now "
            "if they want to keep it."
        ),
    }


def _user_csv_path(path: str, user_id: str) -> str:
    return files.resolve_owned_path(path, user_id)


@server.tool()
async def find_leads_maps(niche: str, location: str, max_results: int = 20, icp: str = "") -> dict:
    """Research businesses of a niche/location and export a discovery-only CSV.

    This tool NEVER visits business websites, calls paid enrichment sources,
    or spends enrichment credits. It only searches Google Maps (name, category,
    website, phone, address, plus code, maps URL, coordinates, and any
    email/socials Maps already publishes). It does NOT search LinkedIn/Reddit/X
    /Facebook; that is `find_web_signals`. It does NOT search named people;
    that is `find_people_leads`. If the user wants more than one of those,
    call the matching tools one after another, starting with the source they
    asked for first.
    After presenting the Maps list, the calling assistant MUST ask whether the
    user wants enrichment. It must not call `enrich_lead_list` unless the user
    explicitly agrees.

    Results are
    cached persistently (survives restarts) for config.CACHE_TTL_DAYS, so
    repeating the same niche+location within that window returns instantly
    instead of re-scraping. For high-volume automated use, prefer
    `submit_search` + `get_search_status` so the caller isn't blocked for the
    1-2 minutes a fresh discovery can take.

    :param niche: The kind of business to search for, e.g. "coffee shops", "plumbers".
    :param location: Where to search, e.g. "Austin, TX".
    :param max_results: Maximum number of businesses to discover (default 20).
    :param icp: Optional free-text description of an ideal lead (e.g. "independent
        coffee shops with an active Instagram and no existing loyalty app").
        When set, each lead is scored against it (see `qualify_ml.py`) and the
        CSV gains `qualified`/`qualification_score`/`qualification_reason`
        columns. No API key involved: a lead is qualified by a fully offline
        confidence gate learned from verdicts you (the calling agent) supply
        via `submit_lead_verdicts` -- until enough exist, every lead comes
        back `qualified=True`/`source=agent_pending` rather than dropped.
    :return: Discovery metadata including CSV path, lead count, and required next action.
    """
    user_id = auth.require_user_id()
    niche_key = niche.strip().lower()
    location_key = location.strip().lower()

    cached = await asyncio.to_thread(
        store.get_discovery_search, user_id, niche_key, location_key, max_results
    )
    if cached is not None and not icp:
        logger.info(f"Cache hit for search ({niche_key!r}, {location_key!r}, {max_results})")
        return _discovery_result(cached)

    try:
        job_id = await jobs.submit(user_id, niche, location, max_results, icp)
        result = await jobs.wait_for(job_id)
    except Exception as exc:
        _raise_as_runtime_error(exc)
    if result["error"]:
        raise RuntimeError(f"error: {result.get('error_type') or ErrorType.INTERNAL.value}: {result['error']}")
    return _discovery_result(result["result_path"])


@server.tool()
async def submit_search(niche: str, location: str, max_results: int = 20, icp: str = "") -> str:
    """Enqueue a discovery-only lead research job and return a job id.

    This never performs website or paid enrichment. On completion, present
    the list and ask the user before calling `enrich_lead_list`.

    Use this instead of `find_leads_maps` when running many searches a day --
    it doesn't hold the connection open while discovery runs.
    Poll the result with `get_search_status`.

    :param niche: The kind of business to search for, e.g. "coffee shops", "plumbers".
    :param location: Where to search, e.g. "Austin, TX".
    :param max_results: Maximum number of businesses to discover (default 20).
    :param icp: Optional ICP text; see `find_leads_maps` for details.
    :return: A job id to pass to `get_search_status`.
    """
    user_id = auth.require_user_id()
    niche_key = niche.strip().lower()
    location_key = location.strip().lower()

    cached = await asyncio.to_thread(
        store.get_discovery_search, user_id, niche_key, location_key, max_results
    )
    if cached is not None and not icp:
        logger.info(f"Cache hit for search ({niche_key!r}, {location_key!r}, {max_results})")
        return await jobs.submit_cached(user_id, niche, location, max_results, cached)

    return await jobs.submit(user_id, niche, location, max_results, icp)


@server.tool()
async def get_search_status(job_id: str) -> dict:
    """Check the status of a search job submitted via `submit_search`.

    :param job_id: The job id returned by `submit_search`.
    :return: dict with `status` ("queued"/"running"/"done"/"error"), `result_path`,
        `error`, `error_type`, `stage`, `items_discovered`, `items_enriched`, `items_total`.
        Once `status` is "done", call `read_result_csv(result_path=...)` to
        retrieve the actual rows -- `result_path` alone is just a download URL.
    """
    user_id = auth.require_user_id()
    result = jobs.status(job_id, user_id)
    if result is None:
        return {
            "status": "unknown",
            "result_path": None,
            "error": "no such job id",
            "error_type": ErrorType.NOT_FOUND.value,
        }
    if result.get("status") == "done" and result.get("result_path"):
        result.update(
            {
                "enriched": False,
                "next_action": (
                    "Call read_result_csv(result_path=...) to retrieve the rows, "
                    "then show the researched Maps list. If the user also wants "
                    "web/social intent posts, call find_web_signals separately. "
                    "Ask before enrich_lead_list."
                ),
            }
        )
    if result.get("result_path"):
        result["result_path"] = files.download_url(result["result_path"])
    return result


@server.tool()
async def find_web_signals(
    query: str,
    location: str = "",
    max_results: int = 20,
    icp: str = "",
    sites: list[str] | None = None,
) -> dict:
    """Find public posts already asking for `query` on LinkedIn, Reddit, X, and Facebook.

    Uses a `site:` search (Serper when configured, else a DuckDuckGo HTML
    scrape) via scrapling, not Google Maps and not BetterContact. Call this
    instead of `find_leads_maps` when the user wants people posting a need
    (e.g. "looking for a speaker in Austin"). Call `find_leads_maps` first if
    they asked for local businesses; call this first if they asked for
    social/intent posts. If they want both, call the tools sequentially. Do
    not call this automatically after Maps.

    :param query: What people would post, e.g. "speaking engagement", "looking for a CRM".
    :param location: Optional place to include in the search, e.g. "Austin, TX".
    :param max_results: Maximum posts to return (spread across selected sites).
    :param icp: Optional ICP text for offline qualification.
    :param sites: Optional subset of linkedin, reddit, x, facebook. Default is all enabled.
    :return: CSV download URL, count, and next_action. Call
        read_result_csv(result_path=...) to actually retrieve the rows.
    """
    user_id = auth.require_user_id()
    try:
        job_id = await web_jobs.submit(user_id, query, location, max_results, icp, sites)
        result = await web_jobs.wait_for(job_id)
    except Exception as exc:
        _raise_as_runtime_error(exc)
    if result["error"]:
        raise RuntimeError(f"error: {result.get('error_type') or ErrorType.INTERNAL.value}: {result['error']}")
    path = result["result_path"]
    return {
        "status": "web_signals_complete",
        "result_path": files.download_url(path),
        "signal_count": _count_csv_rows(path) if path else 0,
        "next_action": (
            "Call read_result_csv(result_path=...) to retrieve the rows, then show "
            "these web/social posts. If the user also wants Google Maps businesses, "
            "call find_leads_maps next. If they want named people at companies, "
            "call find_people_leads. Do not chain extra sources unless asked. "
            "Mention that this CSV is auto-deleted after a retention window (see "
            "read_result_csv's docstring) and offer to help them download it now "
            "if they want to keep it."
        ),
    }


@server.tool()
async def submit_web_signal_search(
    query: str,
    location: str = "",
    max_results: int = 20,
    icp: str = "",
    sites: list[str] | None = None,
) -> str:
    """Enqueue a web/social intent search. Poll with `get_web_signal_search_status`."""
    user_id = auth.require_user_id()
    try:
        return await web_jobs.submit(user_id, query, location, max_results, icp, sites)
    except Exception as exc:
        _raise_as_runtime_error(exc)


@server.tool()
async def get_web_signal_search_status(job_id: str) -> dict:
    """Status of a job from `submit_web_signal_search`.

    Once `status` is "done", call `read_result_csv(result_path=...)` to
    retrieve the actual rows -- `result_path` alone is just a download URL.
    """
    user_id = auth.require_user_id()
    result = web_jobs.status(job_id, user_id)
    if result is None:
        return {
            "status": "unknown",
            "result_path": None,
            "error": "no such job id",
            "error_type": ErrorType.NOT_FOUND.value,
        }
    if result.get("result_path"):
        result["result_path"] = files.download_url(result["result_path"])
    return result


@server.tool()
async def web_search(query: str, max_results: int = 10) -> dict:
    """General-purpose web search -- a free-text query in, ranked results out,
    the same way an assistant's own built-in web search works. NOT restricted
    to the LinkedIn/Reddit/X/Facebook domains find_web_signals searches, and
    not tied to any discovery pipeline -- use this for open-ended research
    (an organization's leadership page, a conference site, an industry
    directory, news coverage, anything indexed) instead of reaching for a
    web-search tool outside leadorbyt.

    Backend: Serper (google.serper.dev, real Google results) when
    `SERPER_API_KEY` is configured, else a DuckDuckGo HTML scrape --
    identical two-tier setup to find_web_signals, and the same known
    caveat: the DDG fallback is unreliable against datacenter IPs (see
    web_signals.py's module docstring for why; this is the exact
    dependency that caused an earlier find_web_signals outage).

    :param query: Free-text search query.
    :param max_results: Maximum results to return (capped at 10 by Serper's
        free tier when that's the active backend).
    :return: `status`, `result_path` (download URL for a CSV of
        title/url/snippet rows -- call read_result_csv(result_path=...) to
        retrieve them), and `result_count`.
    """
    user_id = auth.require_user_id()
    try:
        job_id = await web_search_jobs.submit(user_id, query, max_results)
        result = await web_search_jobs.wait_for(job_id)
    except Exception as exc:
        _raise_as_runtime_error(exc)
    if result["error"]:
        raise RuntimeError(f"error: {result.get('error_type') or ErrorType.INTERNAL.value}: {result['error']}")
    path = result["result_path"]
    return {
        "status": "web_search_complete",
        "result_path": files.download_url(path),
        "result_count": _count_csv_rows(path) if path else 0,
    }


@server.tool()
async def submit_web_search(query: str, max_results: int = 10) -> str:
    """Enqueue a general-purpose web search. Poll with `get_web_search_status`.

    Use this instead of `web_search` when running many searches a day.

    :return: A job id to pass to `get_web_search_status`.
    """
    user_id = auth.require_user_id()
    return await web_search_jobs.submit(user_id, query, max_results)


@server.tool()
async def get_web_search_status(job_id: str) -> dict:
    """Check the status of a job from `submit_web_search`.

    :param job_id: The job id returned by `submit_web_search`.
    :return: dict with `status`, `result_path`, `error`, `error_type`, `stage`,
        `results_found`. Once `status` is "done", call
        `read_result_csv(result_path=...)` to retrieve the actual rows.
    """
    user_id = auth.require_user_id()
    result = web_search_jobs.status(job_id, user_id)
    if result is None:
        return {
            "status": "unknown",
            "result_path": None,
            "error": "no such job id",
            "error_type": ErrorType.NOT_FOUND.value,
        }
    if result.get("result_path"):
        result["result_path"] = files.download_url(result["result_path"])
    return result


@server.tool()
async def find_organizations(category: str, location: str = "", max_results: int = 20, icp: str = "") -> dict:
    """Discover organizations of a category -- e.g. "national associations
    serving sales leaders", "regional homebuilder trade associations" --
    not people (find_people_leads) and not local physical businesses
    (find_leads_maps, which uses Google Maps and doesn't work for this: a
    nationwide category search on Maps mostly returns irrelevant or
    non-US results, since Maps is built for local businesses).

    Runs several directory/listing-style web_search queries for the
    category and treats each search result as a candidate organization
    (title -> name, URL's domain -> domain, snippet -> description). This
    is a best-effort pass over search-engine snippets, not a verified
    directory -- see org_search.py's module docstring for exactly what it
    does and doesn't do (it doesn't crawl into directory pages to extract
    what THEY list). Read-only/discovery-only, like find_leads_maps -- no
    paid lookups.

    The natural next step: feed this CSV's `domain` column into
    find_people_leads(company_domains=[...], job_titles=[...]) to get
    named people at exactly these organizations.

    :param category: The kind of organization to find, e.g. "sales
        leadership trade association" or "regional homebuilder association".
    :param location: Optional place to narrow the search, e.g. "Texas" or
        "United States". Leave blank for a nationwide/unrestricted search.
    :param max_results: Maximum organizations to return.
    :param icp: Optional ICP text for offline qualification (see
        qualify_ml.py / get_icp_gate_status) -- same mechanism as
        find_people_leads, same cold-start caveat.
    :return: `status`, `result_path` (download URL -- call
        read_result_csv(result_path=...) to retrieve the rows), and
        `organization_count`.
    """
    user_id = auth.require_user_id()
    try:
        job_id = await org_jobs.submit(user_id, category, location, max_results, icp)
        result = await org_jobs.wait_for(job_id)
    except Exception as exc:
        _raise_as_runtime_error(exc)
    if result["error"]:
        raise RuntimeError(f"error: {result.get('error_type') or ErrorType.INTERNAL.value}: {result['error']}")
    path = result["result_path"]
    return {
        "status": "organization_search_complete",
        "result_path": files.download_url(path),
        "organization_count": _count_csv_rows(path) if path else 0,
        "next_action": (
            "Call read_result_csv(result_path=...) to retrieve the rows, then show "
            "these organizations. If the user wants named people at them, call "
            "find_people_leads(company_domains=[...the domain column...], "
            "job_titles=[...]) next."
        ),
    }


@server.tool()
async def submit_organization_search(category: str, location: str = "", max_results: int = 20, icp: str = "") -> str:
    """Enqueue an organization-category search. Poll with `get_organization_search_status`.

    Use this instead of `find_organizations` when running many searches a day.
    See `find_organizations` for parameter details.

    :return: A job id to pass to `get_organization_search_status`.
    """
    user_id = auth.require_user_id()
    return await org_jobs.submit(user_id, category, location, max_results, icp)


@server.tool()
async def get_organization_search_status(job_id: str) -> dict:
    """Check the status of a job from `submit_organization_search`.

    :param job_id: The job id returned by `submit_organization_search`.
    :return: dict with `status`, `result_path`, `error`, `error_type`, `stage`,
        `organizations_found`. Once `status` is "done", call
        `read_result_csv(result_path=...)` to retrieve the actual rows.
    """
    user_id = auth.require_user_id()
    result = org_jobs.status(job_id, user_id)
    if result is None:
        return {
            "status": "unknown",
            "result_path": None,
            "error": "no such job id",
            "error_type": ErrorType.NOT_FOUND.value,
        }
    if result.get("result_path"):
        result["result_path"] = files.download_url(result["result_path"])
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
    try:
        data = await enrich_website(url)
        await asyncio.to_thread(store.put_enrichment, key, data)
    except Exception as exc:
        _raise_as_runtime_error(exc)
    return data


@server.tool()
async def enrich_lead_list(lead_list_path: str, icp: str = "") -> dict:
    """Enrich a discovery CSV after the user explicitly approves enrichment.

    Do not call this merely because the user asked to "find leads". First call
    `find_leads_maps`, present its researched list, and ask the user. Call this only
    after an explicit request to enrich that list.

    Enrichment visits each listed website to validate/fill missing
    email/phone/socials without overwriting Google Maps values, and queries
    every configured third-party source. Blank source keys are skipped.
    Cached website/extras data is reused. Google Maps discovery is not repeated.

    :param lead_list_path: `result_path` returned by `find_leads_maps`.
    :param icp: Optional ICP text for post-enrichment offline qualification.
    :return: Enriched CSV download URL, row count, and completion metadata.
        Call read_result_csv(result_path=...) to retrieve the enriched rows.
    """
    user_id = auth.require_user_id()
    try:
        source_path = _user_csv_path(lead_list_path, user_id)
        result_path = await jobs.enrich_lead_list(source_path, user_id, icp)
    except Exception as exc:
        _raise_as_runtime_error(exc)
    return {
        "status": "enrichment_complete",
        "source_path": files.download_url(source_path),
        "result_path": files.download_url(result_path),
        "lead_count": _count_csv_rows(result_path),
        "enriched": True,
    }


RESULT_CSV_MAX_LIMIT = 200


@server.tool()
async def read_result_csv(result_path: str, offset: int = 0, limit: int = 100) -> dict:
    """Read rows out of a result CSV so you can actually show them to the user.

    Every search/enrichment tool (find_leads_maps, find_web_signals,
    find_people_leads, find_reddit_signals, enrich_lead_list, and the
    submit_*/get_*_status pairs) returns a `result_path` that is only a
    download URL -- it does NOT put the rows in front of you. Call this with
    that exact `result_path` (copied verbatim from that tool's response) to
    retrieve and page through the actual data.

    Result CSVs are NOT kept forever -- a cron job deletes them after
    LEADORBYT_RESULT_RETENTION_DAYS (default 7 days; see
    leadorbyt/cleanup.py) to keep server storage from growing unbounded. If
    the user wants to keep a result past that window, tell them to download
    it to their own machine now, e.g.:
    `curl -H "Authorization: Bearer <their API key>" "<result_path>" -o leads.csv`
    -- the URL alone (opened in a plain browser) will not work, since the
    download endpoint requires that same Bearer key used for every MCP call.

    :param result_path: The exact `result_path` (or `source_path`) string
        returned by another tool, for this same authenticated tenant.
    :param offset: Zero-based row index to start from. Use this to page
        through a result set larger than one call's `limit` across repeated
        calls (e.g. offset=0, then offset=100, then offset=200, ...).
    :param limit: Max rows to return in this call. Capped server-side at
        200 regardless of what's requested, since this goes into your own
        context window, not a file download.
    :return: dict with `path`, `total_rows` (rows in the whole CSV),
        `offset`, `limit`, `columns`, and `rows` (a list of {column: value}
        dicts covering just this page).
    """
    user_id = auth.require_user_id()
    offset = max(0, offset)
    limit = max(1, min(limit, RESULT_CSV_MAX_LIMIT))
    try:
        path = files.resolve_owned_path(result_path, user_id)
    except Exception as exc:
        _raise_as_runtime_error(exc)

    columns: list[str] = []
    total_rows = 0
    page: list[dict] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        for i, row in enumerate(reader):
            if offset <= i < offset + limit:
                page.append(row)
            total_rows += 1

    return {
        "path": result_path,
        "total_rows": total_rows,
        "offset": offset,
        "limit": limit,
        "columns": columns,
        "rows": page,
    }


@server.tool()
async def list_result_files(prefix: str = "") -> dict:
    """List this tenant's own result CSVs, to recover a `result_path` you lost track of.

    Use this if an earlier tool call's `result_path` fell out of context
    (e.g. deep in a long conversation) and you need to get back to it,
    rather than re-running the search. Follow up with
    `read_result_csv(result_path=...)` to read the rows.

    :param prefix: Optional case-insensitive substring to filter filenames by,
        e.g. "web_signals" or a niche/query you searched for earlier.
    :return: dict with `files`: a list of {name, result_path, size_bytes,
        modified_at}, newest first.
    """
    user_id = auth.require_user_id()
    root = config.OUTPUT_DIR / user_id
    if not root.is_dir():
        return {"files": []}

    needle = prefix.strip().lower()
    entries = []
    for path in root.glob("*.csv"):
        if needle and needle not in path.name.lower():
            continue
        stat = path.stat()
        entries.append(
            {
                "name": path.name,
                "result_path": files.download_url(str(path)),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    entries.sort(key=lambda e: e["modified_at"], reverse=True)
    return {"files": entries}


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
    try:
        return await enrich_extras(business_name=business_name, website=website, location=location)
    except Exception as exc:
        _raise_as_runtime_error(exc)


def _person_filters(
    seniorities: list[str] | None,
    headcount_min: int | None,
    headcount_max: int | None,
    industries: list[str] | None,
    technologies: list[str] | None,
    company_domains: list[str] | None,
) -> dict:
    return {
        "seniorities": seniorities,
        "headcount_min": headcount_min,
        "headcount_max": headcount_max,
        "industries": industries,
        "technologies": technologies,
        "company_domains": company_domains,
    }


@server.tool()
async def find_people_leads(
    job_titles: list[str],
    location: str,
    icp: str = "",
    max_results: int = 20,
    max_paid_lookups: int = 0,
    goal_new_leads: int | None = None,
    seniorities: list[str] | None = None,
    headcount_min: int | None = None,
    headcount_max: int | None = None,
    industries: list[str] | None = None,
    technologies: list[str] | None = None,
    company_domains: list[str] | None = None,
) -> str:
    """Find named decision-makers (people) by job title + company location.

    Returns a PERSON CSV (full_name/title/company/linkedin_url/email) -- use
    this instead of `find_leads_maps` when the ideal lead is a named individual at
    a company rather than a local business (e.g. "IT directors" for a B2B
    SaaS product), which `find_leads_maps`' Google Maps discovery is not built to find.
    For people already posting the need on LinkedIn/Reddit/X/Facebook, use
    `find_web_signals` instead. If the user wants more than one of those,
    call them one after another in the order they asked.

    Provider: BetterContact is used when `BETTERCONTACT_API_KEY` is
    configured (its free search already includes full name, LinkedIn URL,
    and company domain), falling back to Apollo otherwise (whose free tier
    withholds all three behind the paid step). The CSV's `source_provider`
    column records which one actually served each row.

    Cost model: searching is always free with either provider. Revealing an
    email costs one credit, and only on a verified hit -- a miss costs
    nothing, for both providers. The safe default is zero paid lookups. If
    the user asks only for leads, leave it at zero, present the researched
    list, and ask whether they want email enrichment. Set it above zero only
    after the user explicitly asks for email addresses or enrichment.
    `max_paid_lookups` hard-caps how many
    reveal attempts this call will make (clamped server-side to
    `config.MAX_PAID_LOOKUPS_CEILING` regardless of what's requested), so
    cost and latency stay bounded independent of how many people qualify.
    Already-revealed people are served from cache and never re-billed; a
    person already qualified/rejected/resolved for this exact `icp` in a
    prior call is remembered and not re-qualified or re-revealed for free.

    :param job_titles: Target job titles, e.g. ["CISO", "IT Director", "VP Engineering"].
    :param location: Company headquarters location, e.g. "Austin, TX" or "Florida".
        A US state abbreviation is expanded automatically ("TX" ->
        "Texas") and a bare state name gets ", United States" appended
        automatically ("Florida" -> "Florida, United States") -- both are
        required for BetterContact's location filter to match anything; a
        bare city name with no state (e.g. just "Austin") is not
        auto-corrected and may still match nothing.
    :param icp: Optional free-text ideal-customer description used to rank/qualify
        people before spending reveal credits (see `qualify_ml.py`). No API key
        involved: qualification is a fully offline confidence gate learned from
        verdicts you (the calling agent) supply via `submit_lead_verdicts` --
        preview which leads need one first with `list_unlabeled_leads`. Until
        enough verdicts exist, a lead comes back `qualified=True`/
        `source=agent_pending` (never dropped) and reveals happen in
        search-result order up to `max_paid_lookups`.
        RECOMMENDED WORKFLOW for a new ICP, before pulling a large
        max_results: call `get_icp_gate_status(icp)` first. If not
        `gate_ready` (or not `balanced`), call `list_unlabeled_leads(icp=...,
        max_results=30)`, judge each returned profile yourself against the
        ICP (you already have the reasoning for this -- that's the whole
        point of not spending a separate AI API call on it), and
        `submit_lead_verdicts` with your verdicts -- aim for a genuinely
        mixed set of clearly-good and clearly-bad examples, not just enough
        to clear the minimum count. Only then call `find_people_leads`
        with this exact same ICP text and your real target max_results.
        Skipping this just gets you max_results leads all tagged
        `agent_pending` -- not wrong, but not scored either.
    :param max_results: Target total number of people (free -- this is separate
        from max_paid_lookups). A single BetterContact search request caps at
        ~100 leads AND is fully deterministic per filter set (the identical
        request returns the identical ~100 people every time -- confirmed
        live), so asking for more than ~100 automatically runs additional
        search rounds that vary seniority (then headcount, whichever you
        haven't already constrained yourself) into slices you haven't tried
        yet, up to `config.PEOPLE_SEARCH_MAX_ROUNDS` rounds total (default
        10, so ~1000 leads is the practical ceiling in one call -- each round
        is a real API call + poll wait, so a large max_results adds real
        latency, roughly a few seconds per round). If you've already
        constrained BOTH seniorities and headcount yourself, there's nothing
        left to auto-vary and max_results beyond ~100 has no further effect
        -- vary `location` or `job_titles` across separate calls instead.
    :param max_paid_lookups: Maximum email-reveal attempts; default 0 (no enrichment/spend).
    :param goal_new_leads: If set, keep expanding the search (trying additional
        job-title terms learned from prior qualified/rejected leads for this
        ICP, up to `config.QUERY_EXPANSION_MAX_ROUNDS` rounds) until this many
        *new* (never-before-seen) leads are found, rather than running one
        fixed-size query. Calling this again later with the same job_titles/
        location/icp returns *additional* new leads on top of what a prior
        call already surfaced, since seen leads are remembered.
    :param seniorities: Optional seniority filter, e.g. ["director", "vp", "c_suite"].
    :param headcount_min: Optional minimum employer headcount. A real filter
        (confirmed against live BetterContact data). Note the matched
        headcount is for the specific company/LinkedIn page BetterContact
        has on file for that lead, which can be a subsidiary or regional
        office smaller than the parent brand's total headcount -- a company
        recognized as "Fortune 500" by name can still legitimately match a
        low headcount_max.
    :param headcount_max: Optional maximum employer headcount. See headcount_min.
    :param industries: Optional employer industry filter (BetterContact only).
        A real exact-match filter, but BetterContact's own documented
        taxonomy (https://doc.bettercontact.rocks/api-reference/taxonomies#industries)
        does not reliably match the free-text values in its actual lead
        data (confirmed live: a real lead's industry came back as "Freight
        and package transportation", which is not in that taxonomy at all)
        -- most values, including ones taken straight from BetterContact's
        own docs, are likely to match zero leads. Omit this filter unless
        you've confirmed a specific value returns results for your account.
    :param technologies: Optional employer technology-stack filter (BetterContact
        only). A real exact-match filter against
        https://doc.bettercontact.rocks/api-reference/taxonomies#technologies;
        values are lowercased automatically before sending.
    :param company_domains: Optional list of employer website domains
        (e.g. ["fedex.com", "acme.org"]) to restrict the search to people at
        exactly those organizations -- confirmed real and OR-matched
        correctly across multiple domains on both providers. There is no
        company/organization NAME filter on either provider's API (only
        domain, or Apollo's internal org ID) -- a raw company name like
        "Acme Inc" cannot be matched this way. To go from "organizations of
        category X" to this list, use find_organizations or web_search
        first to discover the relevant orgs and their domains, then pass
        those domains here. Combines with job_titles/seniorities/headcount
        as an additional AND constraint, not a replacement for them.
    :return: Download URL for the generated CSV file. Call
        read_result_csv(result_path=...) with it to retrieve the rows.
    """
    user_id = auth.require_user_id()
    max_paid_lookups = min(max_paid_lookups, config.MAX_PAID_LOOKUPS_CEILING)
    filters = _person_filters(seniorities, headcount_min, headcount_max, industries, technologies, company_domains)

    try:
        job_id = await people_jobs.submit(
            user_id, job_titles, location, max_results, max_paid_lookups, icp, goal_new_leads, filters
        )
        result = await people_jobs.wait_for(job_id)
    except Exception as exc:
        _raise_as_runtime_error(exc)
    if result["error"]:
        raise RuntimeError(f"error: {result.get('error_type') or ErrorType.INTERNAL.value}: {result['error']}")
    return files.download_url(result["result_path"])


@server.tool()
async def submit_people_search(
    job_titles: list[str],
    location: str,
    icp: str = "",
    max_results: int = 20,
    max_paid_lookups: int = 0,
    goal_new_leads: int | None = None,
    seniorities: list[str] | None = None,
    headcount_min: int | None = None,
    headcount_max: int | None = None,
    industries: list[str] | None = None,
    technologies: list[str] | None = None,
    company_domains: list[str] | None = None,
) -> str:
    """Enqueue a person-lead search and return immediately with a job id.

    Use this instead of `find_people_leads` when running many searches a day.
    Poll the result with `get_people_search_status`. See `find_people_leads`
    for the parameter and cost-cap details.

    :return: A job id to pass to `get_people_search_status`.
    """
    user_id = auth.require_user_id()
    max_paid_lookups = min(max_paid_lookups, config.MAX_PAID_LOOKUPS_CEILING)
    filters = _person_filters(seniorities, headcount_min, headcount_max, industries, technologies, company_domains)
    return await people_jobs.submit(
        user_id, job_titles, location, max_results, max_paid_lookups, icp, goal_new_leads, filters
    )


@server.tool()
async def get_people_search_status(job_id: str) -> dict:
    """Check the status of a person-lead search job submitted via `submit_people_search`.

    :param job_id: The job id returned by `submit_people_search`.
    :return: dict with `status`, `result_path`, `error`, `error_type`, `stage`,
        `people_found`, `paid_lookups_used`. Once `status` is "done", call
        `read_result_csv(result_path=...)` to retrieve the actual rows.
    """
    user_id = auth.require_user_id()
    result = people_jobs.status(job_id, user_id)
    if result is None:
        return {
            "status": "unknown",
            "result_path": None,
            "error": "no such job id",
            "error_type": ErrorType.NOT_FOUND.value,
        }
    if result.get("result_path"):
        result["result_path"] = files.download_url(result["result_path"])
    return result


@server.tool()
async def get_icp_gate_status(icp: str) -> dict:
    """Check whether the offline qualification gate for this exact ICP text
    has enough agent-supplied verdicts to actually score leads yet -- call
    this BEFORE running a large find_people_leads(icp=..., max_results=...)
    pull, so you don't burn it on results that all come back
    `qualified=True, source="agent_pending"` (a flat placeholder, not a
    real score -- see qualify_ml.py).

    :param icp: The exact ICP text you intend to use. Matched by exact text
        hash (`store.icp_hash`) -- rewording it even slightly starts a
        fresh, unlabeled gate with none of this ICP's prior verdicts.
    :return: dict with `labels_recorded`, `positive_labels`,
        `negative_labels`, `min_labels_required` (config.QUALIFY_MIN_LABELS),
        `gate_ready` (labels_recorded >= min_labels_required), and
        `balanced` (at least one positive AND one negative label). A
        lopsided all-one-class sample can technically clear
        `min_labels_required` while still being a bad gate -- with only
        positive examples (or only negative), the model has no contrast to
        learn a real boundary from (the only counterweight is one synthetic
        anchor: the ICP text's own embedding, labeled positive -- see
        qualify_ml.py's `_fit_gate`). Don't treat `gate_ready` alone as
        "good to go" -- check `balanced` too, and prefer well past the bare
        minimum (~20-30 verdicts spanning clearly-good and clearly-bad
        examples) over exactly `min_labels_required`, for a boundary that
        actually generalizes to the next 1000 leads rather than overfitting
        a handful of points.
    """
    user_id = auth.require_user_id()
    icp_hash_value = store.icp_hash(icp)
    labels = await asyncio.to_thread(store.get_qualification_labels, user_id, icp_hash_value)
    positive = sum(1 for _, label in labels if label == 1.0)
    negative = len(labels) - positive
    return {
        "labels_recorded": len(labels),
        "positive_labels": positive,
        "negative_labels": negative,
        "min_labels_required": config.QUALIFY_MIN_LABELS,
        "gate_ready": len(labels) >= config.QUALIFY_MIN_LABELS,
        "balanced": positive > 0 and negative > 0,
    }


@server.tool()
async def list_unlabeled_leads(
    job_titles: list[str],
    location: str,
    icp: str,
    max_results: int = 20,
    seniorities: list[str] | None = None,
    headcount_min: int | None = None,
    headcount_max: int | None = None,
    industries: list[str] | None = None,
    technologies: list[str] | None = None,
    company_domains: list[str] | None = None,
) -> list[dict]:
    """Free preview of which leads still need a qualification verdict for `icp`.

    leadorbyt never calls an AI API of its own -- qualification is a fully
    offline confidence gate learned entirely from verdicts you (the calling
    agent) supply. This runs the same free search `find_people_leads` would,
    then returns only the leads the gate can't yet decide on its own
    (cold-start, or genuinely ambiguous). Judge each one against `icp`
    yourself and report your verdicts via `submit_lead_verdicts`; a later
    `find_people_leads(..., icp=icp)` call will then gate-decide on them.

    For a brand-new ICP (check with `get_icp_gate_status` first), call this
    with `max_results` around 30 -- enough to plausibly get both clearly-fit
    and clearly-unfit examples to judge, not just the bare minimum the gate
    needs to turn on. A cold gate returns everything here (nothing to
    decide on yet), so the first call's judging work is unavoidable; it's
    what makes every later search on this ICP actually scored instead of
    all `agent_pending`.

    See `find_people_leads` for `location`/`headcount_min`/`headcount_max`/
    `industries`/`technologies` parameter details (location normalization,
    and the caveat on `industries`' taxonomy not matching live data).

    :return: list of dicts, each with `dedup_key`, `profile_text`, and every
        field the person search discovered for that lead.
    """
    user_id = auth.require_user_id()
    try:
        people = await person_search.search_people(
            job_titles,
            location,
            max_results,
            seniorities=seniorities,
            headcount_min=headcount_min,
            headcount_max=headcount_max,
            industries=industries,
            technologies=technologies,
            company_domains=company_domains,
        )
    except Exception as exc:
        _raise_as_runtime_error(exc)
    for person in people:
        if not person.get("full_name"):
            person["full_name"] = f"{person.get('first_name', '')} {person.get('last_name_obfuscated', '')}".strip()

    pending = await qualify_ml.pending_profiles(people, icp, user_id)
    for person in pending:
        person["dedup_key"] = person_dedup_key(person)
    return pending


@server.tool()
async def find_reddit_signals(
    query: str,
    subreddits: list[str] | None = None,
    icp: str = "",
    sort: str = "new",
    time_filter: str = "week",
    max_results: int = 20,
) -> str | dict:
    """Find people currently expressing interest/pain matching `query` on Reddit.

    Returns a SIGNAL CSV (reddit_username/subreddit/post_title/post_body/
    permalink) -- use this instead of `find_leads_maps`/`find_people_leads` when
    you want to catch people *actively talking about a need* right now
    (e.g. complaining about a problem, asking for recommendations), rather
    than looking up businesses or named people by category/title. leadorbyt
    does not message these people -- this tool only searches and qualifies;
    reaching out is your own separate step.

    COMPLIANCE NOTE: Reddit's API terms name "lead generation" as commercial
    use requiring Reddit's paid/contracted API access; this tool runs on the
    free tier, which is outside that licensed scope for this purpose. See
    `sources/reddit.py`'s module docstring. Configuring `REDDIT_CLIENT_ID`/
    `REDDIT_CLIENT_SECRET` is an explicit choice the operator makes with
    that understanding.

    Cost model: entirely free -- there is no paid reveal step (a Reddit
    username and post permalink are already everything this tool returns;
    there is no contact-resolution API to call).

    :param query: Keywords/phrasing to search for, e.g. "looking for a CRM"
        or "frustrated with excel for invoicing".
    :param subreddits: Optional list of subreddits to restrict the search to,
        e.g. ["smallbusiness", "sales"]. Unrestricted (global search) if omitted.
    :param icp: Optional free-text ideal-customer description used to qualify
        matches (see `qualify_ml.py`). No API key involved: qualification is
        a fully offline confidence gate learned from verdicts you (the
        calling agent) supply via `submit_lead_verdicts` -- preview which
        signals need one first with `list_unlabeled_reddit_signals`. Until
        enough verdicts exist, a signal comes back `qualified=True`/
        `source=agent_pending` rather than dropped.
    :param sort: Reddit search sort order ("new", "relevance", "top", "comments").
    :param time_filter: Reddit search time window ("hour", "day", "week", "month", "year", "all").
    :param max_results: Maximum number of matching posts to return.
    :return: Download URL for the generated CSV, or a login_required payload
        asking the user to connect Reddit in the browser. Call
        read_result_csv(result_path=...) with it to retrieve the rows.
    """
    user_id = auth.require_user_id()
    user_token = await asyncio.to_thread(social_connect.valid_access_token, user_id, "reddit")
    if not reddit.enabled() and not user_token:
        try:
            return social_connect.start_connect(user_id, "reddit")
        except LeadOrbytError as exc:
            _raise_as_runtime_error(exc)
    try:
        job_id = await signal_jobs.submit(user_id, query, subreddits, max_results, icp, sort, time_filter)
        result = await signal_jobs.wait_for(job_id)
    except Exception as exc:
        _raise_as_runtime_error(exc)
    if result["error"]:
        raise RuntimeError(f"error: {result.get('error_type') or ErrorType.INTERNAL.value}: {result['error']}")
    return files.download_url(result["result_path"])


@server.tool()
async def submit_reddit_signal_search(
    query: str,
    subreddits: list[str] | None = None,
    icp: str = "",
    sort: str = "new",
    time_filter: str = "week",
    max_results: int = 20,
) -> str:
    """Enqueue a Reddit signal search and return immediately with a job id.

    Use this instead of `find_reddit_signals` when running many searches a
    day. Poll the result with `get_reddit_signal_search_status`. See
    `find_reddit_signals` for parameter and compliance details.

    :return: A job id to pass to `get_reddit_signal_search_status`.
    """
    user_id = auth.require_user_id()
    return await signal_jobs.submit(user_id, query, subreddits, max_results, icp, sort, time_filter)


@server.tool()
async def get_reddit_signal_search_status(job_id: str) -> dict:
    """Check the status of a Reddit signal search job submitted via `submit_reddit_signal_search`.

    :param job_id: The job id returned by `submit_reddit_signal_search`.
    :return: dict with `status`, `result_path`, `error`, `error_type`, `stage`, `signals_found`.
        Once `status` is "done", call `read_result_csv(result_path=...)` to
        retrieve the actual rows.
    """
    user_id = auth.require_user_id()
    result = signal_jobs.status(job_id, user_id)
    if result is None:
        return {
            "status": "unknown",
            "result_path": None,
            "error": "no such job id",
            "error_type": ErrorType.NOT_FOUND.value,
        }
    if result.get("result_path"):
        result["result_path"] = files.download_url(result["result_path"])
    return result


@server.tool()
async def list_unlabeled_reddit_signals(
    query: str,
    icp: str,
    subreddits: list[str] | None = None,
    sort: str = "new",
    time_filter: str = "week",
    max_results: int = 20,
) -> list[dict]:
    """Free preview of which Reddit signals still need a qualification verdict for `icp`.

    Same agent-qualify hand-off as `list_unlabeled_leads`, for Reddit
    signals: runs the same free search `find_reddit_signals` would, then
    returns only the ones the gate can't yet decide on its own. Judge each
    one and report verdicts via `submit_lead_verdicts`; a later
    `find_reddit_signals(..., icp=icp)` call will then gate-decide on them.

    :return: list of dicts, each with `dedup_key`, `profile_text`, and every
        field the Reddit search discovered for that signal.
    """
    user_id = auth.require_user_id()
    user_token = await asyncio.to_thread(social_connect.valid_access_token, user_id, "reddit")
    signals = await reddit.search_signals(
        query, subreddits, sort, time_filter, max_results, access_token=user_token
    )
    pending = await qualify_ml.pending_profiles(signals, icp, user_id)
    for signal in pending:
        signal["dedup_key"] = signal_dedup_key(signal)
    return pending


@server.tool()
async def list_social_connections() -> dict:
    """Show which social networks this tenant can officially log into.

    Reddit and X use OAuth in the browser. LinkedIn and Facebook cannot be
    connected: they have no public lead-search API, and collecting a password
    or browser session to scrape them would violate their terms. Public
    `site:` hits from find_web_signals still include those networks without login.
    """
    user_id = auth.require_user_id()
    return social_connect.connection_summary(user_id)


@server.tool()
async def connect_social_account(provider: str) -> dict:
    """Start an official OAuth login for `reddit` or `x`.

    Returns `login_url`. Ask the user to open it in a browser and sign in
    there. Never ask them to paste a password into chat. LinkedIn and
    Facebook return `status=unsupported` with the reason.

    After they finish, retry the search. `find_web_signals` then uses the X
    recent search API when X is connected; `find_reddit_signals` uses the
    user's Reddit token when connected.
    """
    user_id = auth.require_user_id()
    try:
        return social_connect.start_connect(user_id, provider)
    except LeadOrbytError as exc:
        _raise_as_runtime_error(exc)


@server.tool()
async def disconnect_social_account(provider: str) -> dict:
    """Forget a previously connected Reddit or X account for this tenant.

    Also revokes the token with the provider (best-effort -- disconnection
    still succeeds locally even if the provider's revoke call fails).
    """
    user_id = auth.require_user_id()
    name = (provider or "").strip().lower()
    if name not in social_connect.CONNECTABLE:
        raise RuntimeError(f"error: invalid_input: cannot disconnect {provider!r}")
    await asyncio.to_thread(social_connect.disconnect, user_id, name)
    return {"status": "disconnected", "provider": name}


@server.tool()
async def submit_lead_verdicts(icp: str, verdicts: list[dict]) -> dict:
    """Record your own qualification verdicts for leads from `list_unlabeled_leads`.

    Writes each verdict as a training label for `icp`'s confidence gate,
    exactly as if leadorbyt's own LLM had answered it.

    Submit BOTH qualified=True and qualified=False verdicts, not just the
    good ones -- an all-one-class batch technically satisfies
    config.QUALIFY_MIN_LABELS but gives the gate almost nothing to draw a
    real boundary from (check `get_icp_gate_status`'s `balanced` field
    after submitting). Rejected examples are exactly as valuable as
    accepted ones here.

    :param icp: The same ICP text used in `list_unlabeled_leads`.
    :param verdicts: list of `{"profile_text": str, "qualified": bool, "reason": str,
        "dedup_key": str}` -- echo the fields `list_unlabeled_leads` gave you for each lead.
    :return: {"labels_added": <count>}
    """
    user_id = auth.require_user_id()
    icp_hash = store.icp_hash(icp)
    for verdict in verdicts:
        embedding = qualify_ml.embed(verdict["profile_text"])
        label = 1.0 if verdict["qualified"] else 0.0
        await asyncio.to_thread(
            store.add_qualification_label,
            user_id,
            icp_hash,
            verdict.get("dedup_key", ""),
            qualify_ml.embedding_to_bytes(embedding),
            label,
        )
    return {"labels_added": len(verdicts)}


def _log_social_login_config() -> None:
    """Warn at startup about missing env vars that block social login, rather
    than letting the first `connect_social_account` call fail with a vague
    error. Only var names are logged -- never a value."""
    if not config.TOKEN_ENCRYPTION_KEY:
        logger.warning(
            "LEADORBYT_TOKEN_ENCRYPTION_KEY is not set -- social login is disabled "
            "(list_social_connections will report encryption_ready=false)"
        )
    missing_reddit = [
        name
        for name, value in (
            ("REDDIT_CLIENT_ID", config.REDDIT_CLIENT_ID),
            ("REDDIT_CLIENT_SECRET", config.REDDIT_CLIENT_SECRET),
            ("REDDIT_USER_AGENT", config.REDDIT_USER_AGENT),
        )
        if not value
    ]
    if missing_reddit:
        logger.warning(
            "Reddit OAuth login is disabled -- missing env var(s): %s",
            ", ".join(missing_reddit),
        )
    # X_OAUTH_CLIENT_SECRET is optional (PKCE public client) -- see social_connect.provider_configured.
    if not config.X_OAUTH_CLIENT_ID:
        logger.warning("X OAuth login is disabled -- missing env var: X_OAUTH_CLIENT_ID")


def create_app():
    """HTTP app: public signup UI plus API-key-gated MCP at /mcp."""
    _log_social_login_config()
    signup.register(server)
    social_connect.register(server)
    files.register(server)
    # The SDK's default DNS-rebinding protection only allows Host: 127.0.0.1/localhost,
    # which rejects every request once this runs behind a reverse proxy on a real domain
    # (Caddy forwards the original Host header, e.g. lead.orbyt.in) -- so it must be told
    # the public host explicitly, taken from LEADORBYT_PUBLIC_URL.
    public_host = urlparse(config.PUBLIC_URL).netloc
    app = server.streamable_http_app(
        host=public_host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public_host],
            allowed_origins=[config.PUBLIC_URL],
        ),
    )
    app.add_middleware(auth.ApiKeyAuthMiddleware)
    return app


def main() -> None:
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
