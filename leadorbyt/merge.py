"""Joins discovery + enrichment results by website URL and exports to CSV."""

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
]


def merge_records(discovery_items: list[dict], enrichment_by_url: dict[str, dict]) -> ItemList:
    """Join discovery records with their enrichment record on normalized website domain."""
    merged = ItemList()

    for record in discovery_items:
        website = record.get("website", "")
        enrichment = enrichment_by_url.get(_normalize(website), {})

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
        }
        merged.append(row)

    return merged


def export_csv(merged: ItemList, path: str | Path) -> Path:
    path = Path(path)
    merged.to_csv(path, fields=CSV_FIELDS)
    return path
