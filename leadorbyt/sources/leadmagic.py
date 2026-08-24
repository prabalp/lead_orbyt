"""LeadMagic company enrichment by domain.

https://docs.leadmagic.io/
"""

from .. import config
from .base import post_json


def enabled() -> bool:
    return bool(config.LEADMAGIC_API_KEY)


async def enrich_company(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    data = await post_json(
        "https://api.leadmagic.io/company-search",
        headers={"X-API-Key": config.LEADMAGIC_API_KEY},
        json_body={"domain": domain},
        source="leadmagic",
    )
    if not data or not data.get("company_name"):
        return None
    return {
        "leadmagic_name": data.get("company_name", ""),
        "leadmagic_industry": data.get("industry", ""),
        "leadmagic_employee_count": data.get("employee_count"),
        "leadmagic_linkedin_url": data.get("linkedin_url", ""),
    }
