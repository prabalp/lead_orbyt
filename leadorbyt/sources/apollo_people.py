"""Apollo.io people search + email reveal, for the person-lead discovery path.

Unlike apollo.py (domain -> company profile), this module finds *named
people* by job title/location and optionally reveals their verified email.

Confirmed against Apollo's live docs, not assumed:
    https://docs.apollo.io/reference/people-api-search
    https://docs.apollo.io/reference/people-enrichment

- Search (`mixed_people/api_search` -- NOT `mixed_people/search`, which
  403s on Basic-tier plans) is free ("doesn't consume credits") and never
  returns actual contact info, only a `has_email` availability flag per
  person -- so ranking/filtering on search results costs nothing.
- Reveal (`people/match` with `reveal_personal_emails=true`) costs exactly
  1 credit, and ONLY if an email is actually found; a miss
  (`match_confidence: none`) costs nothing. Still rationed by a hard
  `max_paid_lookups` cap in people_jobs.py, since "free on a miss" doesn't
  bound the number of *attempts* an agent could otherwise trigger.
- Phone reveal is intentionally not implemented here: `reveal_phone_number`
  delivers results asynchronously to a caller-provided webhook rather than
  in the response, which doesn't fit this server's request/response model.
"""

from .. import config
from .base import post_json

SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"


def enabled() -> bool:
    return bool(config.APOLLO_API_KEY)


async def search_people(
    job_titles: list[str],
    location: str,
    max_results: int,
    seniorities: list[str] | None = None,
    headcount_min: int | None = None,
    headcount_max: int | None = None,
    industries: list[str] | None = None,
    technologies: list[str] | None = None,
    company_domains: list[str] | None = None,
) -> list[dict]:
    """Free people search by job title + company HQ location. Never returns contact info.

    `seniorities` maps to Apollo's `person_seniorities[]`, `headcount_min`/
    `headcount_max` to `organization_num_employees_ranges[]` (Apollo takes a
    "min,max" range string), `industries` to the free-text `q_keywords`
    param -- all verified against Apollo's live API reference. `technologies`
    has no Apollo equivalent (BetterContact-only filter) and is accepted here
    only so `person_search.py` can pass the same kwargs to either provider
    uniformly; it's silently ignored. `company_domains` maps to
    `q_organization_domains_list[]` -- confirmed against Apollo's docs;
    there is no organization NAME filter on this API either, only domain/ID.
    """
    if not enabled() or not job_titles:
        return []

    people: list[dict] = []
    page = 1
    per_page = min(max_results, 100)

    body: dict = {
        "person_titles": job_titles,
        "organization_locations": [location] if location else [],
    }
    if seniorities:
        body["person_seniorities"] = seniorities
    if headcount_min is not None or headcount_max is not None:
        lo = headcount_min if headcount_min is not None else 1
        hi = headcount_max if headcount_max is not None else 1_000_000
        body["organization_num_employees_ranges"] = [f"{lo},{hi}"]
    if industries:
        body["q_keywords"] = " ".join(industries)
    if company_domains:
        body["q_organization_domains_list"] = company_domains

    while len(people) < max_results:
        data = await post_json(
            SEARCH_URL,
            headers={"x-api-key": config.APOLLO_API_KEY},
            json_body={**body, "page": page, "per_page": per_page},
            source="apollo_people.search",
        )
        if not data:
            break

        batch = data.get("people", [])
        if not batch:
            break

        for person in batch:
            org = person.get("organization") or {}
            people.append(
                {
                    "apollo_person_id": person.get("id", ""),
                    "first_name": person.get("first_name", ""),
                    "last_name_obfuscated": person.get("last_name_obfuscated", ""),
                    "title": person.get("title", ""),
                    "company_name": org.get("name", ""),
                    "company_domain": org.get("primary_domain", "") or org.get("website_url", ""),
                    "has_email": bool(person.get("has_email")),
                    "linkedin_url": person.get("linkedin_url", ""),
                }
            )
            if len(people) >= max_results:
                break

        total_entries = (data.get("pagination") or {}).get("total_entries", len(people))
        if len(batch) < per_page or len(people) >= total_entries:
            break
        page += 1

    return people[:max_results]


async def reveal_email(apollo_person_id: str) -> dict | None:
    """Spend one credit to reveal a verified email, or return None on a miss (free)."""
    if not enabled() or not apollo_person_id:
        return None

    data = await post_json(
        MATCH_URL,
        headers={"x-api-key": config.APOLLO_API_KEY},
        json_body={"id": apollo_person_id, "reveal_personal_emails": True},
        source="apollo_people.match",
    )
    person = (data or {}).get("person") or {}
    email = person.get("email", "")
    if not email or person.get("email_status") == "unavailable":
        return None
    return {
        "email": email,
        "last_name": person.get("last_name", ""),
        "match_confidence": data.get("match_confidence", ""),
    }
