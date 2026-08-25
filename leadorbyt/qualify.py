"""Cheap pre-filter gating which discovered businesses are worth the paid
third-party fan-out in sources/registry.py.

lead_orbyt has no ICP/campaign model to learn a real qualifier from (unlike
OpenOutreach's per-campaign GP + active learning), so this is deliberately a
flat, rules-based gate over fields the discovery step already returns --
cheap enough to run on every item, with no extra fetch or LLM call. It exists
to stop `jobs.py` from paying ~20 providers for businesses that were never
going to be useful (no website, or an explicitly excluded category), not to
rank or score the survivors.
"""

import logging

from . import config

logger = logging.getLogger("leadorbyt.qualify")


def should_enrich_extras(item: dict) -> bool:
    """Return False if `item` isn't worth the paid third-party fan-out."""
    name = item.get("business_name", "") or "<unnamed>"

    if config.REQUIRE_WEBSITE_FOR_EXTRAS and not item.get("website"):
        logger.info(f"Skipping extras for {name!r}: no website")
        return False

    category = (item.get("category") or "").lower()
    if category and any(excluded in category for excluded in config.EXCLUDE_CATEGORIES):
        logger.info(f"Skipping extras for {name!r}: excluded category '{category}'")
        return False

    return True
