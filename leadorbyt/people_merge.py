"""Export path for the person-lead discovery pipeline (see people_jobs.py).

Mirrors merge.py's role but for the person schema (name/title/company/email
rather than business/website/address) -- kept as a separate module rather
than overloading merge.py's business-shaped CSV_FIELDS/dedup key.
"""

from pathlib import Path

from scrapling.spiders.result import ItemList

from .merge import export_csv
from .store import normalize_domain as _normalize

PEOPLE_CSV_FIELDS = [
    "full_name",
    "title",
    "company_name",
    "company_domain",
    "linkedin_url",
    "email",
    "qualified",
    "qualification_score",
    "qualification_reason",
    "source_provider",
    "discovered_by_query",
    "is_new_lead",
]


def dedup_key(person: dict) -> str:
    """Stable identity for a discovered person: whichever provider id is
    present (Apollo's own id, or BetterContact's lead id), else the LinkedIn
    URL (free from BetterContact, absent from Apollo's free tier), else
    normalized full name + company -- avoids collisions between two people
    who happen to share a name at different companies.
    """
    apollo_id = person.get("apollo_person_id", "")
    if apollo_id:
        return f"apollo:{apollo_id}"
    bc_id = person.get("bc_lead_id", "")
    if bc_id:
        return f"bettercontact:{bc_id}"
    linkedin_url = person.get("linkedin_url", "")
    if linkedin_url:
        return f"linkedin:{linkedin_url}"
    name = (person.get("full_name") or "").strip().lower()
    company = (person.get("company_name") or "").strip().lower()
    return f"name:{name}|{company}"


def company_domain_key(person: dict) -> str:
    """Normalized company domain, for cache/dedup keys that key on the employer."""
    return _normalize(person.get("company_domain", ""))


def export_people_csv(rows: ItemList, path: str | Path) -> Path:
    return export_csv(rows, path, fields=PEOPLE_CSV_FIELDS)
