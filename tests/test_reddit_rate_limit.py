import time

import pytest

from leadorbyt import config
from leadorbyt.sources import reddit


@pytest.mark.asyncio
async def test_paced_get_enforces_minimum_interval(monkeypatch):
    monkeypatch.setattr(config, "REDDIT_MAX_REQUESTS_PER_MINUTE", 600)  # 0.1s minimum interval
    monkeypatch.setattr(reddit, "_last_request_at", 0.0)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": {"children": [], "after": None}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *args, **kwargs):
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())

    start = time.monotonic()
    await reddit._paced_get("https://oauth.reddit.com/search", {}, "tok")
    await reddit._paced_get("https://oauth.reddit.com/search", {}, "tok")
    elapsed = time.monotonic() - start

    assert elapsed >= 0.1
