import pytest

from leadorbyt import config
from leadorbyt.sources import apollo_people, bettercontact, person_search


@pytest.fixture(autouse=True)
def clear_keys(monkeypatch):
    monkeypatch.setattr(config, "BETTERCONTACT_API_KEY", "")
    monkeypatch.setattr(config, "APOLLO_API_KEY", "")


def test_active_provider_none_when_nothing_configured():
    assert person_search.active_provider_name() is None


def test_active_provider_apollo_when_only_apollo_configured(monkeypatch):
    monkeypatch.setattr(config, "APOLLO_API_KEY", "key")
    assert person_search.active_provider_name() == "apollo"


def test_active_provider_bettercontact_when_only_bettercontact_configured(monkeypatch):
    monkeypatch.setattr(config, "BETTERCONTACT_API_KEY", "key")
    assert person_search.active_provider_name() == "bettercontact"


def test_bettercontact_takes_priority_when_both_configured(monkeypatch):
    monkeypatch.setattr(config, "APOLLO_API_KEY", "key")
    monkeypatch.setattr(config, "BETTERCONTACT_API_KEY", "key")
    assert person_search.active_provider_name() == "bettercontact"


@pytest.mark.asyncio
async def test_search_people_returns_empty_when_no_provider_configured():
    assert await person_search.search_people(["CISO"], "Austin, TX", 10) == []


@pytest.mark.asyncio
async def test_search_people_tags_source_provider_bettercontact(monkeypatch):
    monkeypatch.setattr(config, "BETTERCONTACT_API_KEY", "key")

    async def fake_search(job_titles, location, max_results):
        return [{"full_name": "Jane Smith"}]

    monkeypatch.setattr(bettercontact, "search_people", fake_search)

    people = await person_search.search_people(["CISO"], "Austin, TX", 10)
    assert people[0]["source_provider"] == "bettercontact"


@pytest.mark.asyncio
async def test_search_people_tags_source_provider_apollo(monkeypatch):
    monkeypatch.setattr(config, "APOLLO_API_KEY", "key")

    async def fake_search(job_titles, location, max_results):
        return [{"first_name": "Jane"}]

    monkeypatch.setattr(apollo_people, "search_people", fake_search)

    people = await person_search.search_people(["CISO"], "Austin, TX", 10)
    assert people[0]["source_provider"] == "apollo"


@pytest.mark.asyncio
async def test_search_people_prefers_bettercontact_over_apollo(monkeypatch):
    monkeypatch.setattr(config, "APOLLO_API_KEY", "key")
    monkeypatch.setattr(config, "BETTERCONTACT_API_KEY", "key")

    async def fake_bc_search(job_titles, location, max_results):
        return [{"full_name": "From BetterContact"}]

    async def fake_apollo_search(job_titles, location, max_results):
        raise AssertionError("Apollo should not be called when BetterContact is configured")

    monkeypatch.setattr(bettercontact, "search_people", fake_bc_search)
    monkeypatch.setattr(apollo_people, "search_people", fake_apollo_search)

    people = await person_search.search_people(["CISO"], "Austin, TX", 10)
    assert people[0]["full_name"] == "From BetterContact"
