"""Joins discovery + enrichment + extra-source results by website URL and exports to CSV."""

import json
from pathlib import Path

from scrapling.spiders.result import ItemList

from .store import normalize_domain as _normalize

CSV_FIELDS = [
    "business_name",
    "category",
    "website",
    "email",
    "phone",
    "address",
    "instagram",
    "facebook",
    "linkedin",
    "twitter",
    "youtube",
    "tiktok",
    "storefront_platforms",
    "extra_sources_used",
    "extra_data_json",
]


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
    """
    merged = ItemList()
    extras_by_url = extras_by_url or {}

    for record in discovery_items:
        website = record.get("website", "")
        key = _normalize(website)
        enrichment = enrichment_by_url.get(key, {})
        extras = extras_by_url.get(key, {})
        sources_used = extras.get("sources_used", [])
        extra_payload = {k: v for k, v in extras.items() if k != "sources_used"}

        row = {
            "business_name": record.get("business_name", ""),
            "category": record.get("category", ""),
            "website": website,
            "email": enrichment.get("email", ""),
            "phone": enrichment.get("phone") or record.get("phone", ""),
            "address": record.get("address", ""),
            "instagram": enrichment.get("instagram", ""),
            "facebook": enrichment.get("facebook", ""),
            "linkedin": enrichment.get("linkedin", ""),
            "twitter": enrichment.get("twitter", ""),
            "youtube": enrichment.get("youtube", ""),
            "tiktok": enrichment.get("tiktok", ""),
            "storefront_platforms": ",".join(enrichment.get("storefront_platforms", [])),
            "extra_sources_used": ",".join(sources_used),
            "extra_data_json": json.dumps(extra_payload, default=str) if extra_payload else "",
        }
        merged.append(row)

    return merged


def export_csv(merged: ItemList, path: str | Path) -> Path:
    path = Path(path)
    merged.to_csv(path, fields=CSV_FIELDS)
    return path
