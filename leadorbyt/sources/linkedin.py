"""LinkedIn -- NOT integrated.

LinkedIn has no public self-serve API for company or people search/lookup;
what exists (Marketing/Talent/Sales Navigator APIs) requires a restricted
partner agreement with LinkedIn, and scraping the site would violate its
Terms of Service. `LINKEDIN_API_KEY` is wired through config in case you
already hold such a partner agreement -- plug your own request against
your granted LinkedIn API here if so. Until then this always reports
itself as unintegrated rather than guessing at an endpoint or scraping.
"""

from .. import config


def enabled() -> bool:
    return False


async def company_lookup(domain: str) -> dict | None:
    return None


def status() -> str:
    if config.LINKEDIN_API_KEY:
        return "LINKEDIN_API_KEY is set, but this module has no implementation wired to it yet"
    return "not integrated: LinkedIn has no public self-serve API; scraping would violate its ToS"
