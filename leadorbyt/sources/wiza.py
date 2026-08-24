"""Wiza -- LinkedIn-based prospecting API (company/domain lookups).

https://wiza.co/api
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.WIZA_API_KEY)


async def lookup_company(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    data = await get_json(
        "https://wiza.co/api/individual_reveals/company",
        headers={"Authorization": f"Bearer {config.WIZA_API_KEY}"},
        params={"domain": domain},
        source="wiza",
    )
    company = (data or {}).get("data")
    if not company or not company.get("name"):
        return None
    return {
        "wiza_name": company.get("name", ""),
        "wiza_industry": company.get("industry", ""),
        "wiza_employee_count": company.get("size"),
    }
