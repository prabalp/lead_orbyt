"""Clearbit Company Enrichment API (company lookup by domain).

https://dashboard.clearbit.com/docs#enrichment-api-company-api
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.CLEARBIT_API_KEY)


async def lookup_by_domain(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    data = await get_json(
        "https://company.clearbit.com/v2/companies/find",
        headers={"Authorization": f"Bearer {config.CLEARBIT_API_KEY}"},
        params={"domain": domain},
        source="clearbit",
    )
    if not data:
        return None

    metrics = data.get("metrics", {})
    return {
        "clearbit_name": data.get("name", ""),
        "clearbit_legal_name": data.get("legalName", ""),
        "clearbit_domain": data.get("domain", ""),
        "clearbit_description": data.get("description", ""),
        "clearbit_category": (data.get("category") or {}).get("industry", ""),
        "clearbit_employee_count": metrics.get("employees"),
        "clearbit_estimated_revenue": metrics.get("estimatedAnnualRevenue"),
        "clearbit_founded_year": data.get("foundedYear"),
        "clearbit_linkedin_handle": (data.get("linkedin") or {}).get("handle", ""),
        "clearbit_twitter_handle": (data.get("twitter") or {}).get("handle", ""),
    }
