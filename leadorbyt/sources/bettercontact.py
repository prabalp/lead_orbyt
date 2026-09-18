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
"""

import asyncio

from .. import config
from .base import get_json, post_json

BASE_URL = "https://app.bettercontact.rocks/api/v2"


def enabled() -> bool:
    return bool(config.BETTERCONTACT_API_KEY)


def _headers() -> dict:
    return {"X-API-Key": config.BETTERCONTACT_API_KEY}


async def _submit(filters: dict, enrich_email: bool = False) -> str | None:
    body: dict = {"filters": filters}
    if enrich_email:
        body["enrich_email_address"] = True
    data = await post_json(
        f"{BASE_URL}/lead_finder/async", headers=_headers(), json_body=body, source="bettercontact.submit"
    )
    if not data or not data.get("success"):
        return None
    return data.get("request_id")


async def _poll(request_id: str) -> dict | None:
    for _ in range(config.BETTERCONTACT_MAX_POLL_ATTEMPTS):
        data = await get_json(
            f"{BASE_URL}/lead_finder/async/{request_id}", headers=_headers(), source="bettercontact.poll"
        )
        if not data:
            return None
        if data.get("status") in ("terminated", "on_hold"):
            return data
        await asyncio.sleep(config.BETTERCONTACT_POLL_INTERVAL_SECONDS)
    return None


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
    to `company_headcount_min`/`company_headcount_max`, `industries` to
    `company_industry`, `technologies` to `company_technologies` -- all
    verified against BetterContact's live API reference.
    """
    if not enabled() or not job_titles:
        return []

    filters: dict = {"lead_job_title": {"include": job_titles}}
    if location:
        filters["lead_location"] = {"include": [location]}
    if seniorities:
        filters["lead_seniority"] = {"include": seniorities}
    if headcount_min is not None:
        filters["company_headcount_min"] = headcount_min
    if headcount_max is not None:
        filters["company_headcount_max"] = headcount_max
    if industries:
        filters["company_industry"] = {"include": industries}
    if technologies:
        filters["company_technologies"] = {"include": technologies}

    request_id = await _submit(filters)
    if not request_id:
        return []
    result = await _poll(request_id)
    if not result:
        return []
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
    if not request_id:
        return {}
    result = await _poll(request_id)
    if not result:
        return {}

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
