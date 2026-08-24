"""Cognism company enrichment API.

Cognism's Enrich API is partner-issued (endpoint/auth details are handed to
you at contract signing, not published generically) -- COGNISM_API_KEY is
sent as a bearer token against their documented enrichment endpoint. If your
account uses a different base URL, override it in this file.
"""

from .. import config
from .base import get_json

API_URL = "https://app.cognism.com/api/enrich/v1/company"


def enabled() -> bool:
    return bool(config.COGNISM_API_KEY)


async def enrich_company(domain: str) -> dict | None:
    if not enabled() or not domain:
        return None

    data = await get_json(
        API_URL,
        headers={"Authorization": f"Bearer {config.COGNISM_API_KEY}"},
        params={"domain": domain},
        source="cognism",
    )
    company = (data or {}).get("company") or data
    if not company or not company.get("name"):
        return None
    return {
        "cognism_name": company.get("name", ""),
        "cognism_industry": company.get("industry", ""),
        "cognism_employee_count": company.get("employeeCount"),
    }
