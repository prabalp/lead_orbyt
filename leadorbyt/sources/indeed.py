"""Indeed -- NOT integrated.

Indeed's Publisher API (the only public/self-serve API it ever offered) has
been closed to new applicants for most use cases, and there is no other
sanctioned way to query Indeed programmatically; scraping Indeed's site
would violate its Terms of Service. `INDEED_PUBLISHER_ID` is wired through
config in case you already hold grandfathered/partner publisher access --
plug your own request against Indeed's Publisher API here if so. Until
then this always reports itself as unintegrated rather than guessing at an
endpoint or scraping.
"""

from .. import config


def enabled() -> bool:
    return False


async def job_search(company_name: str) -> dict | None:
    return None


def status() -> str:
    if config.INDEED_PUBLISHER_ID:
        return "INDEED_PUBLISHER_ID is set, but this module has no implementation wired to it yet"
    return "not integrated: Indeed has no open self-serve API; scraping would violate its ToS"
