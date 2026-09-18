import pytest

from leadorbyt import config
from leadorbyt.sources import apollo_people


@pytest.fixture(autouse=True)
def apollo_key(monkeypatch):
    monkeypatch.setattr(config, "APOLLO_API_KEY", "fake-key")


@pytest.mark.asyncio
async def test_search_people_parses_response(monkeypatch):
    async def fake_post_json(url, **kwargs):
        return {
            "people": [
                {
                    "id": "p1",
                    "first_name": "Jane",
                    "last_name_obfuscated": "S.",
                    "title": "CISO",
                    "has_email": True,
                    "linkedin_url": "https://linkedin.com/in/janes",
                    "organization": {"name": "Acme Corp", "primary_domain": "acme.com"},
                }
            ],
            "pagination": {"total_entries": 1},
        }

    monkeypatch.setattr(apollo_people, "post_json", fake_post_json)
    people = await apollo_people.search_people(["CISO"], "Austin, TX", 10)

    assert len(people) == 1
    person = people[0]
    assert person["apollo_person_id"] == "p1"
    assert person["title"] == "CISO"
    assert person["company_name"] == "Acme Corp"
    assert person["company_domain"] == "acme.com"
    assert person["has_email"] is True


@pytest.mark.asyncio
async def test_search_people_returns_empty_without_titles(monkeypatch):
    async def fake_post_json(url, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(apollo_people, "post_json", fake_post_json)
    assert await apollo_people.search_people([], "Austin, TX", 10) == []


@pytest.mark.asyncio
async def test_search_people_disabled_without_key(monkeypatch):
    monkeypatch.setattr(config, "APOLLO_API_KEY", "")
    people = await apollo_people.search_people(["CISO"], "Austin, TX", 10)
    assert people == []


@pytest.mark.asyncio
async def test_search_people_stops_at_max_results(monkeypatch):
    call_count = {"n": 0}

    async def fake_post_json(url, **kwargs):
        call_count["n"] += 1
        page = kwargs["json_body"]["page"]
        return {
            "people": [
                {"id": f"p{page}-{i}", "first_name": "A", "organization": {}} for i in range(100)
            ],
            "pagination": {"total_entries": 1000},
        }

    monkeypatch.setattr(apollo_people, "post_json", fake_post_json)
    people = await apollo_people.search_people(["CISO"], "Austin, TX", 5)
    assert len(people) == 5


@pytest.mark.asyncio
async def test_reveal_email_returns_none_on_miss(monkeypatch):
    async def fake_post_json(url, **kwargs):
        return {"person": {"email_status": "unavailable"}, "match_confidence": "none"}

    monkeypatch.setattr(apollo_people, "post_json", fake_post_json)
    result = await apollo_people.reveal_email("p1")
    assert result is None


@pytest.mark.asyncio
async def test_reveal_email_returns_email_on_hit(monkeypatch):
    async def fake_post_json(url, **kwargs):
        return {"person": {"email": "jane@acme.com", "last_name": "Smith"}, "match_confidence": "high"}

    monkeypatch.setattr(apollo_people, "post_json", fake_post_json)
    result = await apollo_people.reveal_email("p1")
    assert result["email"] == "jane@acme.com"
    assert result["last_name"] == "Smith"


@pytest.mark.asyncio
async def test_reveal_email_disabled_without_key(monkeypatch):
    monkeypatch.setattr(config, "APOLLO_API_KEY", "")
    assert await apollo_people.reveal_email("p1") is None
