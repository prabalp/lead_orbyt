"""OpenCorporates company search.

Works keyless at very low volume (shared public rate limit); set
OPENCORPORATES_API_KEY for a registered app's higher limit. Since it's
usable without a key, this module is only "disabled" in the sense of never
being called if you don't want it -- see registry.py.

https://api.opencorporates.com/documentation/API-Reference
"""

from .. import config
from .base import get_json

ENABLED = True  # keyless tier available; API key (if set) just raises the limit


async def search_company(company_name: str, jurisdiction: str | None = None) -> dict | None:
    params = {"q": company_name}
    if jurisdiction:
        params["jurisdiction_code"] = jurisdiction
    if config.OPENCORPORATES_API_KEY:
        params["api_token"] = config.OPENCORPORATES_API_KEY

    data = await get_json(
        "https://api.opencorporates.com/v0.4/companies/search",
        params=params,
        source="opencorporates",
    )
    results = ((data or {}).get("results") or {}).get("companies") or []
    if not results:
        return None

    company = results[0].get("company", {})
    return {
        "opencorporates_number": company.get("company_number", ""),
        "opencorporates_name": company.get("name", ""),
        "opencorporates_jurisdiction": company.get("jurisdiction_code", ""),
        "opencorporates_status": company.get("current_status", ""),
        "opencorporates_incorporation_date": company.get("incorporation_date", ""),
        "opencorporates_url": company.get("opencorporates_url", ""),
    }
