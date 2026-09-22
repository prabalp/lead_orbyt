import pytest

from leadorbyt import config
from leadorbyt.errors import LeadOrbytError
from leadorbyt.sources import bettercontact


@pytest.fixture(autouse=True)
def bc_key(monkeypatch):
    monkeypatch.setattr(config, "BETTERCONTACT_API_KEY", "fake-key")
    monkeypatch.setattr(config, "BETTERCONTACT_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "BETTERCONTACT_MAX_POLL_ATTEMPTS", 3)


@pytest.mark.asyncio
async def test_search_people_parses_response(monkeypatch):
    async def fake_post_json(url, **kwargs):
        assert url.endswith("/lead_finder/async")
        assert kwargs["json_body"]["filters"]["lead_job_title"]["include"] == ["CISO"]
        return {"success": True, "request_id": "r1"}

    async def fake_get_json(url, **kwargs):
        assert url.endswith("/lead_finder/async/r1")
        return {
            "status": "terminated",
            "leads": [
                {
                    "id": "bc1",
                    "contact_full_name": "Jane Smith",
                    "contact_first_name": "Jane",
                    "contact_last_name": "Smith",
                    "contact_job_title": "CISO",
                    "company_name": "Acme Corp",
                    "company_domain": "acme.com",
                    "contact_linkedin_profile_url": "https://linkedin.com/in/janesmith",
                    "contact_email_address": "",
                }
            ],
        }

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)
    monkeypatch.setattr(bettercontact, "get_json", fake_get_json)

    people = await bettercontact.search_people(["CISO"], "Austin, TX", 10)

    assert len(people) == 1
    person = people[0]
    assert person["full_name"] == "Jane Smith"
    assert person["company_domain"] == "acme.com"
    assert person["linkedin_url"] == "https://linkedin.com/in/janesmith"
    assert person["has_email"] is False


@pytest.mark.asyncio
async def test_search_people_disabled_without_key(monkeypatch):
    monkeypatch.setattr(config, "BETTERCONTACT_API_KEY", "")
    assert await bettercontact.search_people(["CISO"], "Austin, TX", 10) == []


@pytest.mark.asyncio
async def test_search_people_raises_on_poll_timeout_instead_of_empty_list(monkeypatch):
    """A poll that never terminates is a real failure, not '0 leads found' --
    it must be distinguishable by the caller, not silently swallowed into []."""

    async def fake_post_json(url, **kwargs):
        return {"success": True, "request_id": "r1"}

    async def fake_get_json(url, **kwargs):
        return {"status": "processing"}

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)
    monkeypatch.setattr(bettercontact, "get_json", fake_get_json)

    with pytest.raises(LeadOrbytError, match="transport_error"):
        await bettercontact.search_people(["CISO"], "Austin, TX", 10)


@pytest.mark.asyncio
async def test_search_people_raises_when_submit_response_has_no_response(monkeypatch):
    async def fake_post_json(url, **kwargs):
        return None

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)

    with pytest.raises(LeadOrbytError, match="transport_error"):
        await bettercontact.search_people(["CISO"], "Austin, TX", 10)


@pytest.mark.asyncio
async def test_search_people_raises_when_api_rejects_the_request(monkeypatch):
    async def fake_post_json(url, **kwargs):
        return {"success": False, "message": "invalid filter"}

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)

    with pytest.raises(LeadOrbytError, match="invalid filter"):
        await bettercontact.search_people(["CISO"], "Austin, TX", 10)


@pytest.mark.asyncio
async def test_search_people_normalizes_state_abbreviation_in_location(monkeypatch):
    captured = {}

    async def fake_post_json(url, **kwargs):
        captured["location"] = kwargs["json_body"]["filters"]["lead_location"]["include"]
        return {"success": True, "request_id": "r1"}

    async def fake_get_json(url, **kwargs):
        return {"status": "terminated", "leads": []}

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)
    monkeypatch.setattr(bettercontact, "get_json", fake_get_json)

    await bettercontact.search_people(["CISO"], "San Francisco, CA", 10)

    assert captured["location"] == ["San Francisco, California"]


@pytest.mark.asyncio
async def test_search_people_backfills_country_onto_bare_state_name(monkeypatch):
    captured = {}

    async def fake_post_json(url, **kwargs):
        captured["location"] = kwargs["json_body"]["filters"]["lead_location"]["include"]
        return {"success": True, "request_id": "r1"}

    async def fake_get_json(url, **kwargs):
        return {"status": "terminated", "leads": []}

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)
    monkeypatch.setattr(bettercontact, "get_json", fake_get_json)

    await bettercontact.search_people(["CISO"], "Florida", 10)

    assert captured["location"] == ["Florida, United States"]


@pytest.mark.asyncio
async def test_search_people_leaves_bare_country_unchanged(monkeypatch):
    captured = {}

    async def fake_post_json(url, **kwargs):
        captured["location"] = kwargs["json_body"]["filters"]["lead_location"]["include"]
        return {"success": True, "request_id": "r1"}

    async def fake_get_json(url, **kwargs):
        return {"status": "terminated", "leads": []}

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)
    monkeypatch.setattr(bettercontact, "get_json", fake_get_json)

    await bettercontact.search_people(["CISO"], "United States", 10)

    assert captured["location"] == ["United States"]


@pytest.mark.asyncio
async def test_search_people_lowercases_technologies(monkeypatch):
    captured = {}

    async def fake_post_json(url, **kwargs):
        captured["technologies"] = kwargs["json_body"]["filters"]["company_technologies"]["include"]
        return {"success": True, "request_id": "r1"}

    async def fake_get_json(url, **kwargs):
        return {"status": "terminated", "leads": []}

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)
    monkeypatch.setattr(bettercontact, "get_json", fake_get_json)

    await bettercontact.search_people(["CISO"], "", 10, technologies=["HubSpot", "Shopify"])

    assert captured["technologies"] == ["hubspot", "shopify"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("San Francisco, CA", "San Francisco, California"),
        ("Miami, FL", "Miami, Florida"),
        ("Florida", "Florida, United States"),
        ("California", "California, United States"),
        ("Austin, TX", "Austin, Texas"),
        ("United States", "United States"),
        ("", ""),
        ("  Miami,  FL  ", "Miami, Florida"),
    ],
)
def test_normalize_location(raw, expected):
    assert bettercontact._normalize_location(raw) == expected


@pytest.mark.asyncio
async def test_reveal_emails_scopes_to_exact_linkedin_urls(monkeypatch):
    requested_urls = ["https://linkedin.com/in/jane", "https://linkedin.com/in/bob"]

    async def fake_post_json(url, **kwargs):
        assert kwargs["json_body"]["enrich_email_address"] is True
        assert kwargs["json_body"]["filters"]["lead_linkedin_url"]["include"] == requested_urls
        return {"success": True, "request_id": "r2"}

    async def fake_get_json(url, **kwargs):
        return {
            "status": "terminated",
            "leads": [
                {"contact_linkedin_profile_url": requested_urls[0], "contact_email_address": "jane@acme.com"},
                # bob is a miss -- simply absent from the results
            ],
        }

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)
    monkeypatch.setattr(bettercontact, "get_json", fake_get_json)

    result = await bettercontact.reveal_emails(requested_urls)
    assert result[requested_urls[0]]["email"] == "jane@acme.com"
    assert requested_urls[1] not in result


@pytest.mark.asyncio
async def test_reveal_emails_disabled_without_key(monkeypatch):
    monkeypatch.setattr(config, "BETTERCONTACT_API_KEY", "")
    assert await bettercontact.reveal_emails(["https://linkedin.com/in/jane"]) == {}


@pytest.mark.asyncio
async def test_reveal_emails_empty_list_short_circuits(monkeypatch):
    async def fake_post_json(url, **kwargs):
        raise AssertionError("should not be called with no linkedin urls")

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)
    assert await bettercontact.reveal_emails([]) == {}
