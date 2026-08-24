"""Lusha Company Enrichment API.

https://www.lusha.com/docs/#operation/companies
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.LUSHA_API_KEY)


async def enrich_company(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    data = await get_json(
        "https://api.lusha.com/v2/company",
        headers={"api_key": config.LUSHA_API_KEY},
        params={"domain": domain},
        source="lusha",
    )
    if not data or not data.get("name"):
        return None
    return {
        "lusha_name": data.get("name", ""),
        "lusha_industry": data.get("mainIndustry", ""),
        "lusha_employee_count": data.get("employeeCount"),
        "lusha_social_links": data.get("socialLinks", {}),
    }
