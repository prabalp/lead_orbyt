"""Public per-company job board APIs: Greenhouse, Lever, Ashby, The Muse.

Greenhouse/Lever/Ashby publish each customer's open roles on a free, keyless
JSON endpoint keyed by that company's own board token/slug (there is no
global API key for "all Greenhouse customers"). Since we usually only know a
business's name/domain, `slug_guess()` derives a best-effort token from the
domain and these functions simply return `None` on a 404/empty board rather
than treating a guess-miss as an error.

The Muse has an actual company-wide search API (optionally keyed, higher
rate limit with THEMUSE_API_KEY) and doesn't need a slug guess.
"""

import re

from .. import config
from .base import get_json


def slug_guess(domain: str) -> str:
    """Best-effort company board slug from a domain, e.g. 'acme-corp.io' -> 'acmecorp'."""
    root = domain.split(".")[0] if domain else ""
    return re.sub(r"[^a-z0-9]", "", root.lower())


async def greenhouse_jobs(domain: str) -> dict | None:
    slug = slug_guess(domain)
    if not slug:
        return None
    data = await get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", source="greenhouse")
    jobs = (data or {}).get("jobs") or []
    if not jobs:
        return None
    return {
        "greenhouse_open_roles": len(jobs),
        "greenhouse_sample_titles": [j.get("title") for j in jobs[:5]],
    }


async def lever_jobs(domain: str) -> dict | None:
    slug = slug_guess(domain)
    if not slug:
        return None
    data = await get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, source="lever")
    if not data:
        return None
    return {
        "lever_open_roles": len(data),
        "lever_sample_titles": [j.get("text") for j in data[:5]],
    }


async def ashby_jobs(domain: str) -> dict | None:
    slug = slug_guess(domain)
    if not slug:
        return None
    data = await get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        params={"includeCompensation": "false"},
        source="ashby",
    )
    jobs = (data or {}).get("jobs") or []
    if not jobs:
        return None
    return {
        "ashby_open_roles": len(jobs),
        "ashby_sample_titles": [j.get("title") for j in jobs[:5]],
    }


async def themuse_jobs(company_name: str) -> dict | None:
    if not company_name:
        return None
    params = {"company": company_name, "page": 0}
    if config.THEMUSE_API_KEY:
        params["api_key"] = config.THEMUSE_API_KEY
    data = await get_json("https://www.themuse.com/api/public/jobs", params=params, source="themuse")
    results = (data or {}).get("results") or []
    if not results:
        return None
    return {
        "themuse_open_roles": (data or {}).get("total", len(results)),
        "themuse_sample_titles": [r.get("name") for r in results[:5]],
    }
