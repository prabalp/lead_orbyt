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

    `fetch_fn` takes no args and returns a response object (or None on
    exception). `is_blocked_fn(response)` decides whether the response looks
    like a block (rate-limit, CAPTCHA, empty body, etc.) and should trigger a
    backoff + retry rather than being treated as a real (if disappointing)
    result.
    """
    domain = _domain_of(url)
    response = None

    for attempt in range(MAX_RETRIES):
        await _wait_for_domain(domain)

        try:
            response = await fetch_fn()
        except Exception:
            response = None

        if not is_blocked_fn(response):
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

    return response
