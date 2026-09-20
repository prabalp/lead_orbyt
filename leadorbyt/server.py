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
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer

from . import auth, config, jobs, people_jobs, qualify_ml, signal_jobs, signup, social_connect, store, web_jobs
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
        "result_path": path,
        "lead_count": _count_csv_rows(path),
        "enriched": False,
        "next_action": (
            "Show the Google Maps business list. If the user also wants people "
            "already posting about this (LinkedIn, Reddit, X, Facebook), call "
            "find_web_signals next. Do not assume they want both. Then ask "
            "whether they want website enrichment of the Maps list. Do not call "
            "enrich_lead_list unless the user explicitly confirms."
        ),
    }


def _user_csv_path(path: str, user_id: str) -> str:
    candidate = Path(path).expanduser().resolve()
    allowed_root = (config.OUTPUT_DIR / user_id).resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise LeadOrbytError(
            ErrorType.INVALID_INPUT,
            "lead list must be a CSV previously created for the authenticated user",
        ) from exc
    if candidate.suffix.lower() != ".csv" or not candidate.is_file():
        raise LeadOrbytError(ErrorType.NOT_FOUND, "lead-list CSV does not exist")
    return str(candidate)


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
                    "Show the researched Maps list. If the user also wants web/social "
                    "intent posts, call find_web_signals separately. Ask before "
                    "enrich_lead_list."
                ),
            }
        )
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

    Uses a DuckDuckGo HTML `site:` search via scrapling, not Google Maps and
    not BetterContact. Call this instead of `find_leads_maps` when the user wants
    people posting a need (e.g. “looking for a speaker in Austin”). Call
    `find_leads_maps` first if they asked for local businesses; call this first if
    they asked for social/intent posts. If they want both, call the tools
    sequentially. Do not call this automatically after Maps.

    :param query: What people would post, e.g. "speaking engagement", "looking for a CRM".
    :param location: Optional place to include in the search, e.g. "Austin, TX".
    :param max_results: Maximum posts to return (spread across selected sites).
    :param icp: Optional ICP text for offline qualification.
    :param sites: Optional subset of linkedin, reddit, x, facebook. Default is all enabled.
    :return: CSV path, count, and next_action.
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
        "result_path": path,
        "signal_count": _count_csv_rows(path) if path else 0,
        "next_action": (
            "Show these web/social posts. If the user also wants Google Maps "
            "businesses, call find_leads_maps next. If they want named people at "
            "companies, call find_people_leads. Do not chain extra sources unless asked."
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
    """Status of a job from `submit_web_signal_search`."""
    user_id = auth.require_user_id()
    result = web_jobs.status(job_id, user_id)
    if result is None:
        return {
            "status": "unknown",
            "result_path": None,
            "error": "no such job id",
            "error_type": ErrorType.NOT_FOUND.value,
        }
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
    :return: Enriched CSV path, row count, and completion metadata.
    """
    user_id = auth.require_user_id()
    try:
        source_path = _user_csv_path(lead_list_path, user_id)
        result_path = await jobs.enrich_lead_list(source_path, user_id, icp)
    except Exception as exc:
        _raise_as_runtime_error(exc)
    return {
        "status": "enrichment_complete",
        "source_path": source_path,
        "result_path": result_path,
        "lead_count": _count_csv_rows(result_path),
        "enriched": True,
    }


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
) -> dict:
    return {
        "seniorities": seniorities,
        "headcount_min": headcount_min,
        "headcount_max": headcount_max,
        "industries": industries,
        "technologies": technologies,
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
    :param location: Company headquarters location, e.g. "Austin, TX".
    :param icp: Optional free-text ideal-customer description used to rank/qualify
        people before spending reveal credits (see `qualify_ml.py`). No API key
        involved: qualification is a fully offline confidence gate learned from
        verdicts you (the calling agent) supply via `submit_lead_verdicts` --
        preview which leads need one first with `list_unlabeled_leads`. Until
        enough verdicts exist, a lead comes back `qualified=True`/
        `source=agent_pending` (never dropped) and reveals happen in
        search-result order up to `max_paid_lookups`.
    :param max_results: Maximum number of people to discover per search round (free).
    :param max_paid_lookups: Maximum email-reveal attempts; default 0 (no enrichment/spend).
    :param goal_new_leads: If set, keep expanding the search (trying additional
        job-title terms learned from prior qualified/rejected leads for this
        ICP, up to `config.QUERY_EXPANSION_MAX_ROUNDS` rounds) until this many
        *new* (never-before-seen) leads are found, rather than running one
        fixed-size query. Calling this again later with the same job_titles/
        location/icp returns *additional* new leads on top of what a prior
        call already surfaced, since seen leads are remembered.
    :param seniorities: Optional seniority filter, e.g. ["director", "vp", "c_suite"].
    :param headcount_min: Optional minimum employer headcount.
    :param headcount_max: Optional maximum employer headcount.
    :param industries: Optional employer industry/keyword filter.
    :param technologies: Optional employer technology-stack filter (BetterContact only).
    :return: Absolute path to the generated CSV file.
    """
    user_id = auth.require_user_id()
    max_paid_lookups = min(max_paid_lookups, config.MAX_PAID_LOOKUPS_CEILING)
    filters = _person_filters(seniorities, headcount_min, headcount_max, industries, technologies)

    try:
        job_id = await people_jobs.submit(
            user_id, job_titles, location, max_results, max_paid_lookups, icp, goal_new_leads, filters
        )
        result = await people_jobs.wait_for(job_id)
    except Exception as exc:
        _raise_as_runtime_error(exc)
    if result["error"]:
        raise RuntimeError(f"error: {result.get('error_type') or ErrorType.INTERNAL.value}: {result['error']}")
    return result["result_path"]


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
) -> str:
    """Enqueue a person-lead search and return immediately with a job id.

    Use this instead of `find_people_leads` when running many searches a day.
    Poll the result with `get_people_search_status`. See `find_people_leads`
    for the parameter and cost-cap details.

    :return: A job id to pass to `get_people_search_status`.
    """
    user_id = auth.require_user_id()
    max_paid_lookups = min(max_paid_lookups, config.MAX_PAID_LOOKUPS_CEILING)
    filters = _person_filters(seniorities, headcount_min, headcount_max, industries, technologies)
    return await people_jobs.submit(
        user_id, job_titles, location, max_results, max_paid_lookups, icp, goal_new_leads, filters
    )


@server.tool()
async def get_people_search_status(job_id: str) -> dict:
    """Check the status of a person-lead search job submitted via `submit_people_search`.

    :param job_id: The job id returned by `submit_people_search`.
    :return: dict with `status`, `result_path`, `error`, `error_type`, `stage`,
        `people_found`, `paid_lookups_used`.
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
    return result


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
) -> list[dict]:
    """Free preview of which leads still need a qualification verdict for `icp`.

    leadorbyt never calls an AI API of its own -- qualification is a fully
    offline confidence gate learned entirely from verdicts you (the calling
    agent) supply. This runs the same free search `find_people_leads` would,
    then returns only the leads the gate can't yet decide on its own
    (cold-start, or genuinely ambiguous). Judge each one against `icp`
    yourself and report your verdicts via `submit_lead_verdicts`; a later
    `find_people_leads(..., icp=icp)` call will then gate-decide on them.

    :return: list of dicts, each with `dedup_key`, `profile_text`, and every
        field the person search discovered for that lead.
    """
    user_id = auth.require_user_id()
    people = await person_search.search_people(
        job_titles,
        location,
        max_results,
        seniorities=seniorities,
        headcount_min=headcount_min,
        headcount_max=headcount_max,
        industries=industries,
        technologies=technologies,
    )
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
    :return: Absolute path to the generated CSV, or a login_required payload
        asking the user to connect Reddit in the browser.
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
    return result["result_path"]


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
    """Forget a previously connected Reddit or X account for this tenant."""
    user_id = auth.require_user_id()
    name = (provider or "").strip().lower()
    if name not in social_connect.CONNECTABLE:
        raise RuntimeError(f"error: invalid_input: cannot disconnect {provider!r}")
    await asyncio.to_thread(store.delete_connected_account, user_id, name)
    return {"status": "disconnected", "provider": name}


@server.tool()
async def submit_lead_verdicts(icp: str, verdicts: list[dict]) -> dict:
    """Record your own qualification verdicts for leads from `list_unlabeled_leads`.

    Writes each verdict as a training label for `icp`'s confidence gate,
    exactly as if leadorbyt's own LLM had answered it.

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


def create_app():
    """HTTP app: public signup UI plus API-key-gated MCP at /mcp."""
    signup.register(server)
    social_connect.register(server)
    app = server.streamable_http_app()
    app.add_middleware(auth.ApiKeyAuthMiddleware)
    return app


def main() -> None:
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
