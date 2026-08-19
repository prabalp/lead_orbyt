"""robots.txt compliance for discovery, now that it runs outside CrawlSpider.

The original CrawlSpider handled `robots_txt_obey` itself via
`scrapling.spiders.robotstxt.RobotsTxtManager`. Since discovery.py now talks
to Google Maps directly through a pooled browser session (see
browser_pool.py) instead of through CrawlSpider, that manager is reused
directly here so the same "Google Maps' own robots.txt explicitly allows
/maps/search/ and /maps/place/" guarantee this project relies on still holds.
"""

import asyncio

from scrapling.fetchers import Fetcher
from scrapling.spiders.robotstxt import RobotsTxtManager

from . import config

_SID = "leadorbyt-discovery"


async def _fetch_robots(url: str, sid: str):
    return await asyncio.to_thread(
        Fetcher.get, url, timeout=config.REQUEST_TIMEOUT, headers={"User-Agent": config.USER_AGENT}
    )


_manager = RobotsTxtManager(_fetch_robots)


async def can_fetch(url: str) -> bool:
    if not config.ROBOTS_TXT_OBEY:
        return True
    return await _manager.can_fetch(url, _SID)
