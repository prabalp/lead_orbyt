import time

import pytest

from leadorbyt import config
from leadorbyt.sources import reddit


@pytest.fixture(autouse=True)
def reddit_keys(monkeypatch):
    monkeypatch.setattr(config, "REDDIT_CLIENT_ID", "fake-id")
    monkeypatch.setattr(config, "REDDIT_CLIENT_SECRET", "fake-secret")
    monkeypatch.setattr(config, "REDDIT_USER_AGENT", "leadorbyt-test/0.1 by u/test")
    monkeypatch.setattr(config, "REDDIT_MAX_REQUESTS_PER_MINUTE", 6000)  # effectively unpaced in tests
    # Reset module-level token cache between tests.
    monkeypatch.setattr(reddit, "_token", None)
    monkeypatch.setattr(reddit, "_token_expires_at", 0.0)
    monkeypatch.setattr(reddit, "_last_request_at", 0.0)


def test_enabled_requires_all_three_settings(monkeypatch):
    assert reddit.enabled() is True
    monkeypatch.setattr(config, "REDDIT_USER_AGENT", "")
    assert reddit.enabled() is False


def test_normalize_post_skips_deleted_author():
    assert reddit._normalize_post({"author": "[deleted]", "id": "p1"}, "query") is None
    assert reddit._normalize_post({"author": "", "id": "p1"}, "query") is None


def test_normalize_post_shape():
    post = {
        "id": "abc123",
        "author": "some_user",
        "subreddit": "smallbusiness",
        "title": "Looking for a CRM",
        "selftext": "x" * 1000,
        "permalink": "/r/smallbusiness/comments/abc123/looking_for_a_crm/",
        "created_utc": 1700000000,
    }
    normalized = reddit._normalize_post(post, "looking for a crm")
    assert normalized["reddit_post_id"] == "abc123"
    assert normalized["reddit_username"] == "some_user"
    assert normalized["subreddit"] == "smallbusiness"
    assert normalized["permalink"] == "https://reddit.com/r/smallbusiness/comments/abc123/looking_for_a_crm/"
    assert len(normalized["post_body"]) == 500  # truncated
    assert normalized["matched_query"] == "looking for a crm"


@pytest.mark.asyncio
async def test_get_token_caches_until_near_expiry(monkeypatch):
    calls = {"n": 0}

    async def fake_post(*args, **kwargs):
        calls["n"] += 1

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"access_token": f"token-{calls['n']}", "expires_in": 3600}

        return FakeResponse()

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        post = staticmethod(fake_post)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())

    token1 = await reddit._get_token()
    token2 = await reddit._get_token()

    assert token1 == token2 == "token-1"
    assert calls["n"] == 1  # second call served from cache


@pytest.mark.asyncio
async def test_get_token_refetches_after_expiry(monkeypatch):
    calls = {"n": 0}

    async def fake_post(*args, **kwargs):
        calls["n"] += 1

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"access_token": f"token-{calls['n']}", "expires_in": 3600}

        return FakeResponse()

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        post = staticmethod(fake_post)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())

    await reddit._get_token()
    # Simulate the cached token being 59 minutes old -- inside the 60s
    # early-refresh window, so a fresh token should be fetched.
    monkeypatch.setattr(reddit, "_token_expires_at", time.time() + 30)
    await reddit._get_token()

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_get_token_returns_none_on_error(monkeypatch):
    async def fake_post(*args, **kwargs):
        class FakeResponse:
            status_code = 401

            def json(self):
                return {}

        return FakeResponse()

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        post = staticmethod(fake_post)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())

    assert await reddit._get_token() is None


@pytest.mark.asyncio
async def test_search_signals_disabled_without_keys(monkeypatch):
    monkeypatch.setattr(config, "REDDIT_CLIENT_ID", "")
    assert await reddit.search_signals("query", None, max_results=10) == []


@pytest.mark.asyncio
async def test_search_signals_uses_user_access_token(reddit_keys, monkeypatch):
    captured = {}

    async def fake_get_token():
        raise AssertionError("should not mint an app token when a user token is supplied")

    async def fake_paced_get(url, params, token):
        captured["token"] = token
        return {"data": {"children": [], "after": None}}

    monkeypatch.setattr(reddit, "_get_token", fake_get_token)
    monkeypatch.setattr(reddit, "_paced_get", fake_paced_get)
    await reddit.search_signals("query", None, max_results=10, access_token="user-token")
    assert captured["token"] == "user-token"


@pytest.mark.asyncio
async def test_search_signals_returns_empty_without_token(monkeypatch):
    async def fake_get_token():
        return None

    monkeypatch.setattr(reddit, "_get_token", fake_get_token)
    assert await reddit.search_signals("query", None, max_results=10) == []


@pytest.mark.asyncio
async def test_search_signals_global_vs_subreddit_urls(monkeypatch):
    captured_urls = []

    async def fake_get_token():
        return "tok"

    async def fake_paced_get(url, params, token):
        captured_urls.append(url)
        return {"data": {"children": [], "after": None}}

    monkeypatch.setattr(reddit, "_get_token", fake_get_token)
    monkeypatch.setattr(reddit, "_paced_get", fake_paced_get)

    await reddit.search_signals("query", None, max_results=10)
    assert captured_urls == ["https://oauth.reddit.com/search"]

    captured_urls.clear()
    await reddit.search_signals("query", ["smallbusiness", "sales"], max_results=10)
    assert captured_urls == [
        "https://oauth.reddit.com/r/smallbusiness/search",
        "https://oauth.reddit.com/r/sales/search",
    ]


@pytest.mark.asyncio
async def test_search_signals_parses_and_paginates(monkeypatch):
    pages = [
        {
            "data": {
                "children": [
                    {"data": {"id": "p1", "author": "u1", "title": "t1", "permalink": "/r/x/1/"}},
                    {"data": {"id": "p2", "author": "[deleted]", "title": "t2", "permalink": "/r/x/2/"}},
                ],
                "after": "cursor1",
            }
        },
        {
            "data": {
                "children": [
                    {"data": {"id": "p3", "author": "u3", "title": "t3", "permalink": "/r/x/3/"}},
                ],
                "after": None,
            }
        },
    ]

    async def fake_get_token():
        return "tok"

    call_count = {"n": 0}

    async def fake_paced_get(url, params, token):
        page = pages[call_count["n"]]
        call_count["n"] += 1
        return page

    monkeypatch.setattr(reddit, "_get_token", fake_get_token)
    monkeypatch.setattr(reddit, "_paced_get", fake_paced_get)

    results = await reddit.search_signals("query", None, max_results=10)

    assert len(results) == 2  # p2's [deleted] author is skipped
    assert {r["reddit_post_id"] for r in results} == {"p1", "p3"}
    assert call_count["n"] == 2  # paginated via after


@pytest.mark.asyncio
async def test_search_signals_respects_max_results(monkeypatch):
    async def fake_get_token():
        return "tok"

    async def fake_paced_get(url, params, token):
        return {
            "data": {
                "children": [
                    {"data": {"id": f"p{i}", "author": f"u{i}", "permalink": f"/r/x/{i}/"}} for i in range(10)
                ],
                "after": "more",
            }
        }

    monkeypatch.setattr(reddit, "_get_token", fake_get_token)
    monkeypatch.setattr(reddit, "_paced_get", fake_paced_get)

    results = await reddit.search_signals("query", None, max_results=3)
    assert len(results) == 3
