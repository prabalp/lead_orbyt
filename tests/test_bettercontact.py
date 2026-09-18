import pytest

from leadorbyt import config
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
async def test_search_people_returns_empty_on_poll_timeout(monkeypatch):
    async def fake_post_json(url, **kwargs):
        return {"success": True, "request_id": "r1"}

    async def fake_get_json(url, **kwargs):
        return {"status": "processing"}

    monkeypatch.setattr(bettercontact, "post_json", fake_post_json)
    monkeypatch.setattr(bettercontact, "get_json", fake_get_json)

    people = await bettercontact.search_people(["CISO"], "Austin, TX", 10)
    assert people == []


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
