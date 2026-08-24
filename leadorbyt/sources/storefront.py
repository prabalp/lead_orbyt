"""Shopify / WooCommerce storefront detection via HTML signatures.

Free and keyless -- runs against the HTML `enrich.py` has already fetched
for a business's homepage, so it costs no extra request. Signatures below
are the same well-known markers `Wappalyzer`'s own open-source fingerprints
use for these two platforms (cdn.shopify.com asset host, Shopify.shop JS
global, `woocommerce` CSS classes, the wp-content/plugins/woocommerce path).
"""

import re

SHOPIFY_PATTERNS = [
    re.compile(r"cdn\.shopify\.com", re.I),
    re.compile(r"Shopify\.shop", re.I),
    re.compile(r"var\s+Shopify\s*=", re.I),
    re.compile(r"shopify-features", re.I),
]

WOOCOMMERCE_PATTERNS = [
    re.compile(r"woocommerce", re.I),
    re.compile(r"wp-content/plugins/woocommerce", re.I),
    re.compile(r"wc-ajax", re.I),
]


def detect(html: str) -> dict | None:
    """Inspect raw page `html` for Shopify/WooCommerce markers. No network call."""
    if not html:
        return None

    is_shopify = any(p.search(html) for p in SHOPIFY_PATTERNS)
    is_woocommerce = any(p.search(html) for p in WOOCOMMERCE_PATTERNS)
    if not is_shopify and not is_woocommerce:
        return None

    platforms = []
    if is_shopify:
        platforms.append("shopify")
    if is_woocommerce:
        platforms.append("woocommerce")
    return {"storefront_platforms": platforms}
