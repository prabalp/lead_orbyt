"""Shared, persistent per-domain politeness/backoff wrapper for fetch calls.

Scrapling's own AutoThrottle resets fully on every spider `.start()` and
isn't persisted (confirmed against reference/scrapling's CrawlerEngine.crawl,
which unconditionally clears domain limiters and resets AutoThrottle at the
top of every run). At scale, with many concurrent workers and no paid
proxy/IP rotation, staying unbanned depends on every worker respecting one
shared cool-down per domain instead of each call reinventing its own delay
from zero -- that's what this module provides, backed by `store.py` so it
also survives process restarts.
"""

import asyncio
import logging
import random
from urllib.parse import urlparse

from . import config, store
from .errors import ErrorType, LeadOrbytError

logger = logging.getLogger("leadorbyt.backoff")

BLOCK_BACKOFF_FACTOR = 2.0
MAX_RETRIES = 3


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


async def _wait_for_domain(domain: str) -> None:
    wait = await asyncio.to_thread(store.get_backoff_wait, domain)
    if wait > 0:
        logger.info(f"Waiting {wait:.1f}s for {domain} backoff to clear")
        await asyncio.sleep(wait)


async def fetch_with_backoff(url: str, fetch_fn, is_blocked_fn):
    """Call `await fetch_fn()` for `url`, retrying with shared per-domain backoff.

    `fetch_fn` takes no args and returns a response object. `is_blocked_fn(response)`
    decides whether the response looks like a block (rate-limit, CAPTCHA, empty
    body, etc.) and should trigger a backoff + retry rather than being treated
    as a real (if disappointing) result.

    Raises `LeadOrbytError(ErrorType.BLOCKED, ...)` if every retry looked
    blocked, or `LeadOrbytError(ErrorType.TRANSPORT_ERROR, ...)` if every
    retry raised an exception (network/timeout/render failure) -- these are
    distinguishable failure modes callers can react to differently, rather
    than both silently collapsing into the same "no result" signal.
    """
    domain = _domain_of(url)
    response = None
    last_exception: Exception | None = None

    for attempt in range(MAX_RETRIES):
        await _wait_for_domain(domain)

        try:
            response = await fetch_fn()
            last_exception = None
        except Exception as exc:
            response = None
            last_exception = exc

        if response is not None and not is_blocked_fn(response):
            if domain:
                await asyncio.to_thread(store.record_success, domain)
            return response

        if domain:
            consecutive = await asyncio.to_thread(store.bump_block_count, domain)
            delay = min(
                config.AUTOTHROTTLE_START_DELAY * (BLOCK_BACKOFF_FACTOR ** consecutive),
                config.AUTOTHROTTLE_MAX_DELAY,
            )
            delay *= 1 + random.uniform(-0.2, 0.2)  # jitter so workers don't retry in lockstep
            await asyncio.to_thread(store.set_backoff_until, domain, delay)
            logger.warning(
                f"Blocked response from {domain} (attempt {attempt + 1}/{MAX_RETRIES}), "
                f"backing off {delay:.1f}s"
            )

    if last_exception is not None:
        raise LeadOrbytError(
            ErrorType.TRANSPORT_ERROR,
            f"failed to fetch {url} after {MAX_RETRIES} attempts: {last_exception}",
            retryable=True,
        ) from last_exception

    raise LeadOrbytError(
        ErrorType.BLOCKED,
        f"{url} looked blocked on every attempt ({MAX_RETRIES}/{MAX_RETRIES})",
        retryable=True,
    )
