"""NewsAPI.org -- recent news mentions of a company.

https://newsapi.org/docs/endpoints/everything
"""

from .. import config
from .base import get_json


def enabled() -> bool:
    return bool(config.NEWSAPI_ORG_API_KEY)


async def recent_mentions(company_name: str, page_size: int = 5) -> dict | None:
    if not enabled() or not company_name:
        return None

    data = await get_json(
        "https://newsapi.org/v2/everything",
        headers={"X-Api-Key": config.NEWSAPI_ORG_API_KEY},
        params={"q": f'"{company_name}"', "sortBy": "publishedAt", "pageSize": page_size, "language": "en"},
        source="newsapi",
    )
    articles = (data or {}).get("articles") or []
    if not articles:
        return None
    return {
        "news_mention_count": (data or {}).get("totalResults", len(articles)),
        "news_recent_headlines": [
            {"title": a.get("title", ""), "url": a.get("url", ""), "published_at": a.get("publishedAt", "")}
            for a in articles
        ],
    }
