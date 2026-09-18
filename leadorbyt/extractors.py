"""Regex/heuristic helpers for pulling contact info out of a page.

Operates on a Scrapling `Response`/`Selector`-like object (anything exposing
`.css()`, `.xpath()`, and `.get_all_text()`/`.body`) plus plain HTML strings.
"""

import re
from urllib.parse import unquote, urlparse

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}"
)

JUNK_EMAIL_PATTERNS = (
    "example.com",
    "example.org",
    "yourdomain",
    "domain.com",
    "sentry.io",
    "wixpress.com",
    "godaddy.com",
    "@2x",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    "noreply@",
    "no-reply@",
    "@google.com",
    "gstatic.com",
)

SOCIAL_DOMAINS = {
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.com"),
    "linkedin": ("linkedin.com",),
    "twitter": ("twitter.com", "x.com"),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
}

CONTACT_PAGE_HINTS = ("contact", "about", "about-us", "connect", "get-in-touch")


def _is_junk_email(email: str) -> bool:
    lowered = email.lower()
    return any(pat in lowered for pat in JUNK_EMAIL_PATTERNS)


def extract_emails(response) -> list[str]:
    """Collect deduped, filtered emails from mailto: links and page text."""
    found: list[str] = []

    for href in response.css("a[href^='mailto:']::attr(href)").getall():
        addr = href.split("mailto:", 1)[-1].split("?")[0].strip()
        addr = unquote(addr)
        if addr:
            found.append(addr)

    text = response.get_all_text(ignore_tags=("script", "style"))
    found.extend(EMAIL_RE.findall(text or ""))

    seen: dict[str, None] = {}
    for email in found:
        email = email.strip().strip(".,;:")
        if not email or _is_junk_email(email):
            continue
        seen.setdefault(email.lower(), None)

    return list(seen.keys())


def extract_phones(response) -> list[str]:
    """Collect deduped phone numbers from tel: links and page text."""
    found: list[str] = []

    for href in response.css("a[href^='tel:']::attr(href)").getall():
        number = href.split("tel:", 1)[-1].strip()
        number = unquote(number)
        if number:
            found.append(number)

    text = response.get_all_text(ignore_tags=("script", "style"))
    found.extend(PHONE_RE.findall(text or ""))

    seen: dict[str, None] = {}
    for number in found:
        cleaned = re.sub(r"[^\d+]", "", number)
        if len(re.sub(r"\D", "", cleaned)) < 7:
            continue
        seen.setdefault(cleaned, None)

    return list(seen.keys())


def extract_socials(response) -> dict[str, str]:
    """Return the first matching link per social platform, keyed by platform name."""
    result = {platform: "" for platform in SOCIAL_DOMAINS}

    for href in response.css("a::attr(href)").getall():
        if not href:
            continue
        host = urlparse(href).netloc.lower()
        if not host:
            continue
        for platform, domains in SOCIAL_DOMAINS.items():
            if result[platform]:
                continue
            if any(host == d or host.endswith("." + d) for d in domains):
                result[platform] = href

    return result


def find_contact_page_links(response, base_url: str) -> list[str]:
    """Return absolute URLs on the page that look like contact/about pages."""
    links = []
    for href in response.css("a::attr(href)").getall():
        if not href:
            continue
        lowered = href.lower()
        if any(hint in lowered for hint in CONTACT_PAGE_HINTS):
            links.append(response.urljoin(href))
    seen: dict[str, None] = {}
    for link in links:
        seen.setdefault(link, None)
    return list(seen.keys())
