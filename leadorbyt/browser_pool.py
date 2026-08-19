"""A small pool of long-lived, reusable stealth-browser sessions.

`StealthyFetcher.fetch()`/`AsyncStealthySession` used as a one-shot call
launches and tears down a whole Chromium browser per call (confirmed against
reference/scrapling: the fetcher wrapper does `async with
AsyncStealthySession(**kwargs) as engine: ...` for a single URL). At
thousands of searches/day, that cold-launch cost dominates. `AsyncStealthySession`
used directly, however, only opens/closes a *page* per `.fetch()` call and
keeps its browser/context alive across many calls -- so this pool launches a
fixed, small number of sessions once and hands them out for reuse.
"""

import asyncio
import logging

from scrapling.fetchers import AsyncStealthySession

from . import config

logger = logging.getLogger("leadorbyt.browser_pool")

_pool: asyncio.Queue[AsyncStealthySession] | None = None
_sessions: list[AsyncStealthySession] = []
_lock = asyncio.Lock()


async def start(size: int | None = None) -> None:
    """Launch `size` (default config.DISCOVERY_POOL_SIZE) warm browser sessions."""
    global _pool
    async with _lock:
        if _pool is not None:
            return
        size = size or config.DISCOVERY_POOL_SIZE
        _pool = asyncio.Queue()
        for _ in range(size):
            session = AsyncStealthySession(
                headless=True,
                network_idle=True,
                useragent=config.USER_AGENT,
            )
            await session.__aenter__()
            _sessions.append(session)
            _pool.put_nowait(session)
        logger.info(f"Browser pool started with {size} warm sessions")


async def stop() -> None:
    global _pool
    async with _lock:
        for session in _sessions:
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                logger.exception("Error closing pooled browser session")
        _sessions.clear()
        _pool = None


class _Checkout:
    """Async context manager: `async with browser_pool.checkout() as session:`."""

    def __init__(self):
        self._session: AsyncStealthySession | None = None

    async def __aenter__(self) -> AsyncStealthySession:
        if _pool is None:
            await start()
        self._session = await _pool.get()
        return self._session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        _pool.put_nowait(self._session)


def checkout() -> _Checkout:
    return _Checkout()
