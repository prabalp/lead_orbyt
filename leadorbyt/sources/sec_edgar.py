"""SEC EDGAR full-text search -- free, keyless, but requires an identifying
User-Agent per SEC's fair-access policy (https://www.sec.gov/os/webmaster-faq#developers).

Only useful for publicly-traded/reporting companies; most small local
businesses simply won't appear, which is a normal `None` result, not a failure.
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.SEC_EDGAR_USER_AGENT)


async def search_company(company_name: str) -> dict | None:
    """Full-text-search EDGAR filings for `company_name` and return the top hit."""
    if not enabled():
        return None

    data = await get_json(
        "https://efts.sec.gov/LATEST/search-index",
        headers={"User-Agent": config.SEC_EDGAR_USER_AGENT},
        params={"q": company_name, "forms": "10-K,10-Q,8-K"},
        source="sec_edgar",
    )
    hits = (data or {}).get("hits", {}).get("hits", [])
    if not hits:
        return None

    top = hits[0].get("_source", {})
    return {
        "sec_cik": top.get("ciks", [None])[0],
        "sec_entity_name": (top.get("display_names") or [""])[0],
        "sec_form_type": top.get("forms", [None])[0] if top.get("forms") else top.get("form"),
        "sec_filing_date": top.get("file_date"),
    }
