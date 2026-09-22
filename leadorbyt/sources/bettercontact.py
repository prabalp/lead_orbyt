"""BetterContact Lead Finder: person search + email enrichment.

The priority backend for the person-lead discovery path (see
sources/person_search.py, people_jobs.py) -- unlike Apollo, BetterContact's
free search always returns full name, LinkedIn URL, and company domain, not
just an obfuscated name and availability flags. Only `enrich_email_address`/
`enrich_phone_number` cost credits, and (per the pattern verified for every
other paid provider in this codebase) only when a match is actually found.

Confirmed against BetterContact's live API reference (not assumed):
    https://doc.bettercontact.rocks/api-reference/endpoint/lead_finder_post
    https://doc.bettercontact.rocks/api-reference/endpoint/lead_finder_get

Base URL and the `X-API-Key` header are as documented on those two endpoint
pages (BetterContact's top-level "introduction" doc page 404s, so this
wasn't cross-checked against a single canonical page -- if requests start
failing with 401s, verify these two constants against your account's own
API dashboard first).

Async job model, unlike Apollo's single-request search: `POST /lead_finder/async`
submits a search and returns a `request_id`; `GET /lead_finder/async/{request_id}`
is polled until `status` is `"terminated"` or `"on_hold"` (never branch on
HTTP status alone -- a 202 with `status: "processing"` is still in flight).

FILTER BEHAVIOR, confirmed live against the API (not from docs alone --
BetterContact's own docs turned out to be incomplete/inaccurate on two of
these):

- `lead_location`: free text per BetterContact's docs, but empirically
  stricter than that. State/province ABBREVIATIONS never match ("San
  Francisco, CA" / "Miami, FL" both return zero leads); the full name does
  ("San Francisco, California"). A bare single-word state name alone also
  matches nothing ("Florida" -> 0 leads) -- it needs a second component,
  either a city ("Miami, Florida") or a country ("Florida, United States").
  A bare country name alone works fine ("United States" -> 100 leads).
  `_normalize_location` expands US state abbreviations and backfills
  ", United States" onto a bare state name so all of the location shapes a
  caller is likely to pass ("Austin, TX", "Florida", "California") resolve
  to a shape BetterContact actually matches.
- `company_headcount_min`/`company_headcount_max`: genuinely filter (a
  headcount_max=10 search returns only "Self employed" (0-1 employees);
  headcount_min=100000 returns only 10001+ companies) -- confirmed NOT
  broken. A `headcount_max=1000` search returning a company branded
  "Exxonmobil" is not a bug: that specific matched entity has BetterContact
  `company_employees_range_start/end` of 501-1000 (evidently a distinct
  LinkedIn company page/subsidiary from the parent corporation's real
  headcount) -- a BetterContact data-quality characteristic, not a filter
  bug on either side.
- `company_industry`: a real, applied exact-match filter (an unmatchable
  garbage value reliably returns 0 leads, proving it isn't silently
  ignored) -- BUT the documented taxonomy
  (https://doc.bettercontact.rocks/api-reference/taxonomies#industries,
  e.g. "Computer Software", "Marketing & Advertising") does not match the
  actual free-text values BetterContact's own data uses (e.g. a real
  VP-Marketing lead's `company_industry` came back as "Freight and package
  transportation", which appears nowhere in their documented taxonomy).
  This means values from BetterContact's own docs have a good chance of
  matching zero real records. This is a BetterContact taxonomy/data
  mismatch we cannot reliably work around client-side (there is no
  discoverable list of the *actual* values in use) -- see `search_people`'s
  docstring and `industries` in server.py's tool docstrings, which say so.
- `company_technologies`: also a real, applied exact-match filter (same
  garbage-value test). BetterContact's docs give lowercase examples
  ("hubspot", "salesforce"); values are lowercased here defensively, but
  capitalization was not observed to matter live.
"""

import asyncio

from .. import config
from ..errors import ErrorType, LeadOrbytError
from .base import get_json, post_json

BASE_URL = "https://app.bettercontact.rocks/api/v2"

# City/state (or bare state) shapes people actually type. Abbreviations are
# expanded; a bare state name gets ", United States" appended -- see the
# module docstring's FILTER BEHAVIOR note for why both are required.
_US_STATE_ABBREVIATIONS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}
_US_STATE_NAMES = frozenset(_US_STATE_ABBREVIATIONS.values())


def _normalize_location(location: str) -> str:
    location = location.strip()
    if not location:
        return location
    parts = [_US_STATE_ABBREVIATIONS.get(p.strip().upper(), p.strip()) for p in location.split(",")]
    if len(parts) == 1 and parts[0] in _US_STATE_NAMES:
        parts.append("United States")
    return ", ".join(parts)


def enabled() -> bool:
    return bool(config.BETTERCONTACT_API_KEY)


def _headers() -> dict:
    return {"X-API-Key": config.BETTERCONTACT_API_KEY}


async def _submit(filters: dict, enrich_email: bool = False) -> str:
    """Returns a request_id. Raises LeadOrbytError -- never returns a falsy
    sentinel -- so a genuine request failure can never be mistaken for a
    legitimate zero-result search by a caller."""
    body: dict = {"filters": filters}
    if enrich_email:
        body["enrich_email_address"] = True
    data = await post_json(
        f"{BASE_URL}/lead_finder/async", headers=_headers(), json_body=body, source="bettercontact.submit"
    )
    if not data:
        raise LeadOrbytError(
            ErrorType.TRANSPORT_ERROR, "BetterContact lead_finder request failed (no response)", retryable=True
        )
    if not data.get("success"):
        raise LeadOrbytError(
            ErrorType.INTERNAL, f"BetterContact lead_finder request rejected: {data.get('message') or data}"
        )
    request_id = data.get("request_id")
    if not request_id:
        raise LeadOrbytError(ErrorType.INTERNAL, f"BetterContact lead_finder response missing request_id: {data}")
    return request_id


async def _poll(request_id: str) -> dict:
    """Raises LeadOrbytError on a transport failure or a timed-out poll --
    both are distinct from `{"leads": []}`, a legitimate zero-match search."""
    for _ in range(config.BETTERCONTACT_MAX_POLL_ATTEMPTS):
        data = await get_json(
            f"{BASE_URL}/lead_finder/async/{request_id}", headers=_headers(), source="bettercontact.poll"
        )
        if not data:
            raise LeadOrbytError(
                ErrorType.TRANSPORT_ERROR, "BetterContact lead_finder poll failed (no response)", retryable=True
            )
        if data.get("status") in ("terminated", "on_hold"):
            return data
        await asyncio.sleep(config.BETTERCONTACT_POLL_INTERVAL_SECONDS)
    raise LeadOrbytError(
        ErrorType.TRANSPORT_ERROR,
        f"BetterContact lead_finder request {request_id} did not finish within "
        f"{config.BETTERCONTACT_MAX_POLL_ATTEMPTS} polls -- it may still be processing",
        retryable=True,
    )


def _normalize_lead(lead: dict) -> dict:
    return {
        "bc_lead_id": lead.get("id", ""),
        "full_name": lead.get("contact_full_name", ""),
        "first_name": lead.get("contact_first_name", ""),
        "last_name_obfuscated": lead.get("contact_last_name", ""),  # BetterContact gives the real last name
        "title": lead.get("contact_job_title", ""),
        "company_name": lead.get("company_name", ""),
        "company_domain": lead.get("company_domain", ""),
        "linkedin_url": lead.get("contact_linkedin_profile_url", ""),
        "has_email": bool(lead.get("contact_email_address")),
    }


async def search_people(
    job_titles: list[str],
    location: str,
    max_results: int,
    seniorities: list[str] | None = None,
    headcount_min: int | None = None,
    headcount_max: int | None = None,
    industries: list[str] | None = None,
    technologies: list[str] | None = None,
) -> list[dict]:
    """Free search -- always includes full name, LinkedIn URL, and company domain.

    `seniorities` maps to `lead_seniority`, `headcount_min`/`headcount_max`
    to `company_headcount_min`/`company_headcount_max` (confirmed real
    filters), `industries` to `company_industry` (a real filter, but see the
    module docstring's FILTER BEHAVIOR note -- BetterContact's documented
    taxonomy does not reliably match its own live data, so most values are
    likely to match zero leads even though the filter itself works),
    `technologies` to `company_technologies` (lowercased here; also a real
    filter). `location` is passed through `_normalize_location` first --
    also see the module docstring.
    """
    if not enabled() or not job_titles:
        return []

    filters: dict = {"lead_job_title": {"include": job_titles}}
    if location:
        filters["lead_location"] = {"include": [_normalize_location(location)]}
    if seniorities:
        filters["lead_seniority"] = {"include": seniorities}
    if headcount_min is not None:
        filters["company_headcount_min"] = headcount_min
    if headcount_max is not None:
        filters["company_headcount_max"] = headcount_max
    if industries:
        filters["company_industry"] = {"include": industries}
    if technologies:
        filters["company_technologies"] = {"include": [t.strip().lower() for t in technologies]}

    request_id = await _submit(filters)
    result = await _poll(request_id)
    leads = result.get("leads", [])[:max_results]
    return [_normalize_lead(lead) for lead in leads]


async def reveal_emails(linkedin_urls: list[str]) -> dict[str, dict]:
    """Batch-reveal emails for up to len(linkedin_urls) people in ONE paid request.

    Scoped by the `lead_linkedin_url` exact-match filter, so this only ever
    enriches the specific people passed in -- never a broader match. Returns
    a dict keyed by linkedin_url; a person missing from the result (or with
    a blank email) was a miss and cost nothing.
    """
    if not enabled() or not linkedin_urls:
        return {}

    request_id = await _submit({"lead_linkedin_url": {"include": linkedin_urls}}, enrich_email=True)
    result = await _poll(request_id)

    out: dict[str, dict] = {}
    for lead in result.get("leads", []):
        linkedin_url = lead.get("contact_linkedin_profile_url", "")
        if not linkedin_url:
            continue
        out[linkedin_url] = {
            "email": lead.get("contact_email_address", ""),
            "full_name": lead.get("contact_full_name", ""),
        }
    return out
