import pytest

from leadorbyt import backoff, config
from leadorbyt.errors import ErrorType, LeadOrbytError


@pytest.fixture(autouse=True)
def fast_backoff(isolated_db, monkeypatch):
    """Keep retry delays near-zero so these tests don't take seconds each."""
    monkeypatch.setattr(config, "AUTOTHROTTLE_START_DELAY", 0.001)
    monkeypatch.setattr(config, "AUTOTHROTTLE_MAX_DELAY", 0.002)


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body


def _ok_response():
    return _FakeResponse(200, b"x" * 300)


def _blocked_response():
    return _FakeResponse(403, b"")


def _is_blocked(response):
    return response is None or response.status >= 400 or len(response.body or b"") < 200


@pytest.mark.asyncio
async def test_success_on_first_attempt():
    async def fetch_fn():
        return _ok_response()

    response = await backoff.fetch_with_backoff("https://example.com", fetch_fn, _is_blocked)
    assert response.status == 200


@pytest.mark.asyncio
async def test_raises_blocked_after_exhausting_retries():
    async def fetch_fn():
        return _blocked_response()

    with pytest.raises(LeadOrbytError) as exc_info:
        await backoff.fetch_with_backoff("https://example.com", fetch_fn, _is_blocked)
    assert exc_info.value.type == ErrorType.BLOCKED


@pytest.mark.asyncio
async def test_raises_transport_error_after_exhausting_retries():
    async def fetch_fn():
        raise ConnectionError("boom")

    with pytest.raises(LeadOrbytError) as exc_info:
        await backoff.fetch_with_backoff("https://example.com", fetch_fn, _is_blocked)
    assert exc_info.value.type == ErrorType.TRANSPORT_ERROR


@pytest.mark.asyncio
async def test_recovers_after_transient_failure():
    attempts = {"n": 0}

    async def fetch_fn():
        attempts["n"] += 1
        if attempts["n"] < 2:
            return _blocked_response()
        return _ok_response()

    response = await backoff.fetch_with_backoff("https://example.com", fetch_fn, _is_blocked)
    assert response.status == 200
    assert attempts["n"] == 2
