"""People Data Labs Company Enrichment API.

https://docs.peopledatalabs.com/docs/company-enrichment-api
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.PEOPLEDATALABS_API_KEY)


async def enrich_company(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    data = await get_json(
        "https://api.peopledatalabs.com/v5/company/enrich",
        headers={"X-Api-Key": config.PEOPLEDATALABS_API_KEY},
        params={"website": domain},
        source="peopledatalabs",
    )
    if not data or data.get("status") != 200:
        return None
    return {
        "pdl_name": data.get("name", ""),
        "pdl_industry": data.get("industry", ""),
        "pdl_employee_count": data.get("employee_count"),
        "pdl_founded": data.get("founded"),
        "pdl_linkedin_url": data.get("linkedin_url", ""),
        "pdl_location": (data.get("location") or {}).get("name", ""),
    }
