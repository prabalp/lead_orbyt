"""Apollo.io organization enrichment by domain.

https://docs.apollo.io/reference/organization-enrichment
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.APOLLO_API_KEY)


async def enrich_organization(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    data = await get_json(
        "https://api.apollo.io/api/v1/organizations/enrich",
        headers={"x-api-key": config.APOLLO_API_KEY},
        params={"domain": domain},
        source="apollo",
    )
    org = (data or {}).get("organization")
    if not org:
        return None
    return {
        "apollo_name": org.get("name", ""),
        "apollo_industry": org.get("industry", ""),
        "apollo_employee_count": org.get("estimated_num_employees"),
        "apollo_linkedin_url": org.get("linkedin_url", ""),
        "apollo_phone": org.get("phone", ""),
        "apollo_founded_year": org.get("founded_year"),
    }
