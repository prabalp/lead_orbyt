import pytest

from leadorbyt import config
from leadorbyt.sources import apollo_people, bettercontact


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setattr(config, "APOLLO_API_KEY", "fake-key")
    monkeypatch.setattr(config, "BETTERCONTACT_API_KEY", "fake-key")
    monkeypatch.setattr(config, "BETTERCONTACT_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "BETTERCONTACT_MAX_POLL_ATTEMPTS", 3)


@pytest.mark.asyncio
async def test_apollo_maps_seniorities_and_headcount(monkeypatch):
    captured = {}

    async def fake_post_json(url, **kwargs):
        captured.update(kwargs["json_body"])
        return {"people": [], "pagination": {"total_entries": 0}}

    monkeypatch.setattr(apollo_people, "post_json", fake_post_json)
    await apollo_people.search_people(
        ["CISO"], "Austin, TX", 10, seniorities=["director", "vp"], headcount_min=50, headcount_max=500
    )

    assert captured["person_seniorities"] == ["director", "vp"]
    assert captured["organization_num_employees_ranges"] == ["50,500"]


@pytest.mark.asyncio
async def test_apollo_maps_industries_to_keywords(monkeypatch):
    captured = {}

    async def fake_post_json(url, **kwargs):
        captured.update(kwargs["json_body"])
        return {"people": [], "pagination": {"total_entries": 0}}

    monkeypatch.setattr(apollo_people, "post_json", fake_post_json)
    await apollo_people.search_people(["CISO"], "Austin, TX", 10, industries=["fintech", "healthcare"])

    assert captured["q_keywords"] == "fintech healthcare"


@pytest.mark.asyncio
async def test_apollo_omits_filters_when_not_given(monkeypatch):
    captured = {}

    async def fake_post_json(url, **kwargs):
        captured.update(kwargs["json_body"])
        return {"people": [], "pagination": {"total_entries": 0}}

    monkeypatch.setattr(apollo_people, "post_json", fake_post_json)
    await apollo_people.search_people(["CISO"], "Austin, TX", 10)

    assert "person_seniorities" not in captured
    assert "organization_num_employees_ranges" not in captured
    assert "q_keywords" not in captured


@pytest.mark.asyncio
async def test_bettercontact_maps_all_new_filters(monkeypatch):
    captured = {}

    async def fake_post_json(url, **kwargs):
        captured.update(kwargs["json_body"])
        return {"success": True, "request_id": "r1"}

    async def fake_get_json(url, **kwargs):
        return {"status": "terminated", "leads": []}

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)
    monkeypatch.setattr(bettercontact, "get_json", fake_get_json)

    await bettercontact.search_people(
        ["CISO"],
        "Austin, TX",
        10,
        seniorities=["director"],
        headcount_min=50,
        headcount_max=500,
        industries=["fintech"],
        technologies=["salesforce"],
    )

    filters = captured["filters"]
    assert filters["lead_seniority"] == {"include": ["director"]}
    assert filters["company_headcount_min"] == 50
    assert filters["company_headcount_max"] == 500
    assert filters["company_industry"] == {"include": ["fintech"]}
    assert filters["company_technologies"] == {"include": ["salesforce"]}


@pytest.mark.asyncio
async def test_bettercontact_omits_filters_when_not_given(monkeypatch):
    captured = {}

    async def fake_post_json(url, **kwargs):
        captured.update(kwargs["json_body"])
        return {"success": True, "request_id": "r1"}

    async def fake_get_json(url, **kwargs):
        return {"status": "terminated", "leads": []}

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)
    monkeypatch.setattr(bettercontact, "get_json", fake_get_json)

    await bettercontact.search_people(["CISO"], "Austin, TX", 10)

    filters = captured["filters"]
    for key in ("lead_seniority", "company_headcount_min", "company_headcount_max", "company_industry", "company_technologies"):
        assert key not in filters
