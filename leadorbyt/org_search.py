"""Organization-level discovery: find organizations of a category (e.g.
"national associations serving sales leaders", "regional homebuilder trade
associations") -- not people (find_people_leads) and not local physical
businesses (find_leads_maps, Google Maps).

Google Maps discovery doesn't work for this: Maps is built for local/
physical businesses, and a nationwide category search on it mostly returns
irrelevant or non-US results. This runs several directory/listing-style
`web_search` queries for the category instead (web_search.py -- the same
Serper/DDG backend find_web_signals uses) and treats each individual search
result as a candidate organization: title -> name, the URL's own domain ->
domain, snippet -> description.

This is a best-effort pass over search-engine result snippets, NOT a
verified directory, and it does not crawl into directory/listicle pages to
extract the individual organizations THEY list -- it works best when the
category term itself surfaces individual organizations' own homepages in
the search results (which it usually does for a specific-enough category).
Generic aggregator/social/job-board domains are excluded from results (never
the organization itself), even though their wording is useful IN a query
(e.g. a query mentioning Wikipedia can surface the orgs a Wikipedia page
names, without the Wikipedia page itself ending up as a result).

Output (`domain`) feeds directly into
`find_people_leads(company_domains=[...])`.
"""

from __future__ import annotations

from urllib.parse import urlparse

from . import web_search

ORG_CSV_FIELDS = [
    "org_name",
    "domain",
    "description",
    "url",
    "discovered_by_query",
    "qualified",
    "qualification_score",
    "qualification_reason",
]

# Never the organization itself -- aggregators, social platforms, job
# boards, and search engines that legitimately show up in these queries.
_EXCLUDED_DOMAINS = frozenset(
    {
        "wikipedia.org", "linkedin.com", "facebook.com", "twitter.com", "x.com",
        "youtube.com", "reddit.com", "indeed.com", "glassdoor.com", "google.com",
        "bing.com", "yelp.com", "crunchbase.com", "zoominfo.com", "instagram.com",
        "pinterest.com", "quora.com", "amazon.com", "apple.com", "ziprecruiter.com",
    }
)


def _query_variants(category: str, location: str = "") -> list[str]:
    loc_suffix = f" {location}" if location.strip() else ""
    base = f"{category.strip()}{loc_suffix}"
    return [
        base,
        f"list of {base}",
        f"{base} directory",
        f"member associations {base}",
        f"{base} association website",
    ]


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _is_excluded(domain: str) -> bool:
    return any(domain == d or domain.endswith(f".{d}") for d in _EXCLUDED_DOMAINS)


def dedup_key(org: dict) -> str:
    domain = org.get("domain", "")
    return f"org:{domain}" if domain else f"org:{org.get('org_name', '').lower()}"


def profile_text(org: dict) -> str:
    """Text blob for qualify_ml embedding / agent review -- same role as
    qualify_ml.profile_text plays for people/businesses/signals."""
    return " | ".join(p for p in (org.get("org_name", ""), org.get("description", "")) if p)


async def discover_organizations(category: str, location: str = "", max_results: int = 20) -> list[dict]:
    """Run the query variants, extract candidate organizations, dedup by domain."""
    merged: list[dict] = []
    seen_domains: set[str] = set()
    for query in _query_variants(category, location):
        if len(merged) >= max_results:
            break
        results = await web_search.search_web(query, max_results=10)
        for result in results:
            domain = _domain_of(result["url"])
            if not domain or domain in seen_domains or _is_excluded(domain):
                continue
            seen_domains.add(domain)
            merged.append(
                {
                    "org_name": result["title"] or domain,
                    "domain": domain,
                    "description": result["snippet"],
                    "url": result["url"],
                    "discovered_by_query": query,
                }
            )
            if len(merged) >= max_results:
                break
    return merged
