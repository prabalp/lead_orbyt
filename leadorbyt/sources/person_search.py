"""Provider-agnostic dispatch for person-lead discovery.

BetterContact is the priority backend: its free search always returns full
name, LinkedIn URL, and company domain, unlike Apollo's, which withholds all
three behind the paid match/enrich call (see bettercontact.py's module
docstring). Falls back to Apollo when BetterContact isn't configured. Each
searched person is tagged with `source_provider` so the reveal step (and the
exported CSV, for cost auditing) knows which backend it came from -- a
person from one provider is always revealed through that same provider.
"""

from . import apollo_people, bettercontact


def active_provider_name() -> str | None:
    if bettercontact.enabled():
        return "bettercontact"
    if apollo_people.enabled():
        return "apollo"
    return None


async def search_people(job_titles: list[str], location: str, max_results: int, **filters) -> list[dict]:
    """`**filters` (seniorities/headcount_min/headcount_max/industries/technologies)
    passes through uniformly to whichever provider is active; each provider
    module maps the ones it supports to its own real param name and ignores
    the rest (see apollo_people.py/bettercontact.py for exactly which).
    """
    provider = active_provider_name()
    if provider == "bettercontact":
        people = await bettercontact.search_people(job_titles, location, max_results, **filters)
    elif provider == "apollo":
        people = await apollo_people.search_people(job_titles, location, max_results, **filters)
    else:
        return []
    for person in people:
        person["source_provider"] = provider
    return people
