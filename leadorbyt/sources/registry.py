"""Fans a single business out to every configured third-party data source
and merges whatever comes back into one dict.

Each source module below is independently optional: it's only invoked if
its own `enabled()` check (an API key in `config`) passes, so this degrades
gracefully to a no-op call when nothing is configured, and picks up new
sources automatically as keys get added to `.env`. A source raising or
timing out never aborts the others -- each call is individually try/excepted
so one broken source can't drop the rest.

`enrich_extras()` is the single entry point `jobs.py` calls once per
discovered business, alongside (not instead of) `enrich.py`'s own website
scrape -- it needs the business name and search location that only the
discovery step has, whereas `enrich.py` only ever sees a bare URL.

Storefront (Shopify/WooCommerce) detection is intentionally NOT here: it's
a free check against HTML `enrich.py` has already fetched, so it lives in
`enrich_website()` directly rather than costing this module a second fetch.

Indeed and LinkedIn are intentionally NOT wired in here -- see
sources/indeed.py and sources/linkedin.py for why.
"""

import asyncio
import logging

from . import (
    apollo,
    builtwith,
    clearbit,
    cognism,
    crunchbase,
    findymail,
    foursquare,
    google_places,
    hunter,
    job_boards,
    leadmagic,
    lusha,
    news,
    opencorporates,
    osm,
    peopledatalabs,
    prospeo,
    rocketreach,
    sec_edgar,
    snov,
    wappalyzer,
    wiza,
    yelp,
    zoominfo,
)

logger = logging.getLogger("leadorbyt.sources.registry")

# (name, enabled_check(ctx), coroutine-factory(ctx)) triples. `enabled_check`
# is evaluated per call (not at import time) so a key added to `.env`
# mid-process takes effect on the next search, and can also depend on what
# was actually passed in (e.g. OSM only if coordinates were given). Sources
# with no key requirement (OpenCorporates keyless tier, the per-company job
# boards) just check `True`.
_SOURCES = [
    ("opencorporates", lambda c: opencorporates.ENABLED, lambda c: opencorporates.search_company(c["name"])),
    ("sec_edgar", lambda c: sec_edgar.enabled(), lambda c: sec_edgar.search_company(c["name"])),
    ("crunchbase", lambda c: crunchbase.enabled(), lambda c: crunchbase.lookup_by_domain(c["domain"])),
    ("clearbit", lambda c: clearbit.enabled(), lambda c: clearbit.lookup_by_domain(c["domain"])),
    ("builtwith", lambda c: builtwith.enabled(), lambda c: builtwith.lookup_by_domain(c["domain"])),
    ("wappalyzer", lambda c: wappalyzer.enabled(), lambda c: wappalyzer.lookup_by_url(c["website"])),
    ("hunter", lambda c: hunter.enabled(), lambda c: hunter.domain_search(c["domain"])),
    ("apollo", lambda c: apollo.enabled(), lambda c: apollo.enrich_organization(c["domain"])),
    ("snov", lambda c: snov.enabled(), lambda c: snov.domain_search(c["domain"])),
    ("rocketreach", lambda c: rocketreach.enabled(), lambda c: rocketreach.lookup_company(c["domain"])),
    ("peopledatalabs", lambda c: peopledatalabs.enabled(), lambda c: peopledatalabs.enrich_company(c["domain"])),
    ("lusha", lambda c: lusha.enabled(), lambda c: lusha.enrich_company(c["domain"])),
    ("cognism", lambda c: cognism.enabled(), lambda c: cognism.enrich_company(c["domain"])),
    ("zoominfo", lambda c: zoominfo.enabled(), lambda c: zoominfo.enrich_company(c["domain"])),
    ("findymail", lambda c: findymail.enabled(), lambda c: findymail.find_domain_emails(c["domain"])),
    ("leadmagic", lambda c: leadmagic.enabled(), lambda c: leadmagic.enrich_company(c["domain"])),
    ("wiza", lambda c: wiza.enabled(), lambda c: wiza.lookup_company(c["domain"])),
    ("prospeo", lambda c: prospeo.enabled(), lambda c: prospeo.domain_search(c["domain"])),
    ("greenhouse", lambda c: True, lambda c: job_boards.greenhouse_jobs(c["domain"])),
    ("lever", lambda c: True, lambda c: job_boards.lever_jobs(c["domain"])),
    ("ashby", lambda c: True, lambda c: job_boards.ashby_jobs(c["domain"])),
    ("themuse", lambda c: True, lambda c: job_boards.themuse_jobs(c["name"])),
    ("newsapi", lambda c: news.enabled(), lambda c: news.recent_mentions(c["name"])),
    (
        "google_places",
        lambda c: google_places.enabled(),
        lambda c: google_places.search(f"{c['name']} {c['location']}".strip()),
    ),
    ("yelp", lambda c: yelp.enabled(), lambda c: yelp.search(c["name"], c["location"])),
    ("foursquare", lambda c: foursquare.enabled(), lambda c: foursquare.search(c["name"], c["location"])),
    ("osm", lambda c: c["lat"] is not None and c["lon"] is not None, lambda c: osm.lookup_by_coords(c["name"], c["lat"], c["lon"])),
]


async def enrich_extras(
    business_name: str,
    website: str = "",
    domain: str = "",
    location: str = "",
    lat: float | None = None,
    lon: float | None = None,
) -> dict:
    """Query every enabled source for this business and merge the results.

    :param business_name: The business's display name (used by name-keyed sources).
    :param website: Full website URL, if known.
    :param domain: Bare domain (e.g. "acme.com"); derived from `website` if omitted.
    :param location: Free-text location (city/state), used by local-search sources.
    :param lat: Latitude, if known (enables the free OpenStreetMap/Overpass lookup).
    :param lon: Longitude, if known (enables the free OpenStreetMap/Overpass lookup).
    :return: dict with a namespaced key per field the sources returned, plus
        "sources_used" (list of source names that returned data).
    """
    from ..store import normalize_domain

    domain = domain or normalize_domain(website)
    ctx = {"name": business_name, "website": website, "domain": domain, "location": location, "lat": lat, "lon": lon}

    async def _run(name: str, coro):
        try:
            return name, await coro
        except Exception:
            logger.exception(f"Source '{name}' raised unexpectedly")
            return name, None

    tasks = [_run(name, make_coro(ctx)) for name, is_enabled, make_coro in _SOURCES if is_enabled(ctx)]
    results = await asyncio.gather(*tasks)

    merged: dict = {}
    used: list[str] = []
    for name, result in results:
        if result:
            merged.update(result)
            used.append(name)

    merged["sources_used"] = used
    return merged
