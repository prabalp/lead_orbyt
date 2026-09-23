import pytest

from leadorbyt import config, web_search


@pytest.mark.asyncio
async def test_search_web_empty_query_returns_nothing():
    assert await web_search.search_web("") == []
    assert await web_search.search_web("   ") == []


@pytest.mark.asyncio
async def test_search_web_uses_serper_when_configured(monkeypatch):
    monkeypatch.setattr(config, "SERPER_API_KEY", "fake-key")
    captured = {}

    async def fake_post_json(url, **kwargs):
        captured["url"] = url
        captured["json_body"] = kwargs["json_body"]
        return {
            "organic": [
                {"title": "Acme Corp", "link": "https://acme.com", "snippet": "Acme makes things"},
                {"title": "Acme Careers", "link": "https://acme.com/careers", "snippet": "Join us"},
            ]
        }

    monkeypatch.setattr(web_search, "post_json", fake_post_json)

    results = await web_search.search_web("acme corp", max_results=5)

    assert captured["url"] == web_search.SERPER_SEARCH_URL
    assert captured["json_body"]["q"] == "acme corp"
    assert captured["json_body"]["num"] == 5
    assert len(results) == 2
    assert results[0] == {
        "title": "Acme Corp",
        "url": "https://acme.com",
        "snippet": "Acme makes things",
        "discovered_by_query": "acme corp",
    }


@pytest.mark.asyncio
async def test_search_web_caps_serper_num_at_ten(monkeypatch):
    """Serper's free tier rejects num > 10 -- see web_signals.py's identical fix."""
    monkeypatch.setattr(config, "SERPER_API_KEY", "fake-key")
    captured = {}

    async def fake_post_json(url, **kwargs):
        captured["num"] = kwargs["json_body"]["num"]
        return {"organic": []}

    monkeypatch.setattr(web_search, "post_json", fake_post_json)
    await web_search.search_web("query", max_results=50)

    assert captured["num"] == 10


@pytest.mark.asyncio
async def test_search_web_dedups_by_url(monkeypatch):
    monkeypatch.setattr(config, "SERPER_API_KEY", "fake-key")

    async def fake_post_json(url, **kwargs):
        return {
            "organic": [
                {"title": "A", "link": "https://acme.com", "snippet": "s1"},
                {"title": "A dup", "link": "https://acme.com", "snippet": "s2"},
            ]
        }

    monkeypatch.setattr(web_search, "post_json", fake_post_json)
    results = await web_search.search_web("query")

    assert len(results) == 1


@pytest.mark.asyncio
async def test_search_web_falls_back_to_ddg_when_serper_fails(monkeypatch):
    monkeypatch.setattr(config, "SERPER_API_KEY", "fake-key")

    async def fake_post_json(url, **kwargs):
        return None  # transport failure

    async def fake_fetch_serp(query):
        return None  # DDG also fails -- should degrade to empty, not raise

    monkeypatch.setattr(web_search, "post_json", fake_post_json)
    monkeypatch.setattr(web_search, "_fetch_serp", fake_fetch_serp)

    results = await web_search.search_web("query")

    assert results == []


@pytest.mark.asyncio
async def test_search_web_falls_back_to_ddg_when_no_serper_key(monkeypatch):
    monkeypatch.setattr(config, "SERPER_API_KEY", "")
    called = {}

    async def fake_fetch_serp(query):
        called["query"] = query
        return None

    monkeypatch.setattr(web_search, "_fetch_serp", fake_fetch_serp)
    await web_search.search_web("some query")

    assert called["query"] == "some query"
