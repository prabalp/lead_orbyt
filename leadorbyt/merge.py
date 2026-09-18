"""Joins discovery + enrichment + extra-source results by website URL and exports to CSV."""

import json
import logging
from pathlib import Path

from scrapling.spiders.result import ItemList

from .store import normalize_domain as _normalize

logger = logging.getLogger("leadorbyt.merge")

CSV_FIELDS = [
    "business_name",
    "category",
    "website",
    "email",
    "phone",
    "address",
    "plus_code",
    "maps_url",
    "lat",
    "lon",
    "instagram",
    "facebook",
    "linkedin",
    "twitter",
    "youtube",
    "tiktok",
    "storefront_platforms",
    "extra_sources_used",
    "extra_data_json",
    "discovered_by_query",
    "is_new_lead",
    "qualified",
    "qualification_score",
    "qualification_reason",
]


def dedup_key(record: dict) -> str:
    """Stable identity for a discovered business: normalized domain when it has
    a website, else normalized name+address -- avoids every no-website business
    colliding on the same empty-string key.
    """
    website = record.get("website", "")
    domain = _normalize(website)
    if domain:
        return domain
    name = (record.get("business_name") or "").strip().lower()
    address = (record.get("address") or "").strip().lower()
    return f"name:{name}|{address}"


def _first_nonempty(*values) -> str:
    """Keep the first already-known value; enrichment only fills blanks."""
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def merge_records(
    discovery_items: list[dict],
    enrichment_by_url: dict[str, dict],
    extras_by_url: dict[str, dict] | None = None,
) -> ItemList:
    """Join discovery records with their enrichment + extra-source records on normalized website domain.

    `extras_by_url` (from `sources.registry.enrich_extras`, keyed the same
    way as `enrichment_by_url`) is optional so existing callers/tests that
    only run discovery + website enrichment keep working unchanged; when
    omitted, the three new columns are just empty.

    Google Maps discovery is the source of truth for any field it already
    populated. Website enrichment and extras only fill blanks (and extras
    still land in extra_data_json). Deduplicates on `dedup_key()`.
    """
    merged = ItemList()
    extras_by_url = extras_by_url or {}
    seen: set[str] = set()

    for record in discovery_items:
        key_for_dedup = dedup_key(record)
        if key_for_dedup in seen:
            logger.debug(f"Skipping duplicate business: {record.get('business_name')!r}")
            continue
        seen.add(key_for_dedup)

        website = record.get("website", "")
        key = _normalize(website)
        enrichment = enrichment_by_url.get(key, {})
        extras = extras_by_url.get(key, {})
        sources_used = extras.get("sources_used", [])
        extra_payload = {k: v for k, v in extras.items() if k != "sources_used"}
        platforms = enrichment.get("storefront_platforms") or []
        if isinstance(platforms, str):
            platform_value = platforms
        else:
            platform_value = ",".join(platforms)

        row = {
            "business_name": record.get("business_name", ""),
            "category": record.get("category", ""),
            "website": website,
            "email": _first_nonempty(record.get("email"), enrichment.get("email")),
            "phone": _first_nonempty(record.get("phone"), enrichment.get("phone")),
            "address": record.get("address", ""),
            "plus_code": record.get("plus_code", ""),
            "maps_url": record.get("maps_url", ""),
            "lat": record.get("lat", ""),
            "lon": record.get("lon", ""),
            "instagram": _first_nonempty(record.get("instagram"), enrichment.get("instagram")),
            "facebook": _first_nonempty(record.get("facebook"), enrichment.get("facebook")),
            "linkedin": _first_nonempty(record.get("linkedin"), enrichment.get("linkedin")),
            "twitter": _first_nonempty(record.get("twitter"), enrichment.get("twitter")),
            "youtube": _first_nonempty(record.get("youtube"), enrichment.get("youtube")),
            "tiktok": _first_nonempty(record.get("tiktok"), enrichment.get("tiktok")),
            "storefront_platforms": platform_value,
            "extra_sources_used": ",".join(sources_used),
            "extra_data_json": json.dumps(extra_payload, default=str) if extra_payload else "",
            "discovered_by_query": record.get("discovered_by_query", ""),
            "is_new_lead": "",
            "qualified": "",
            "qualification_score": "",
            "qualification_reason": "",
        }
        merged.append(row)

    return merged


def export_csv(merged: ItemList, path: str | Path, fields: list[str] = CSV_FIELDS) -> Path:
    path = Path(path)
    merged.to_csv(path, fields=fields)
    return path
