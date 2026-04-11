import os

import structlog
from firecrawl import FirecrawlApp

logger = structlog.get_logger("chat.web")

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
_fc = FirecrawlApp(api_key=FIRECRAWL_API_KEY) if FIRECRAWL_API_KEY else None

MAX_CONTENT_LENGTH = 2000


async def search_web(query: str) -> str | None:
    if not _fc:
        return None
    try:
        results = _fc.search(query, limit=3)
        if not results.web:
            return None

        parts = []
        for item in results.web[:3]:
            parts.append(f"**{item.title}**\n{item.url}\n{item.description or ''}")

        return "\n\n".join(parts)
    except Exception as e:
        await logger.awarn("Ошибка веб-поиска", error=str(e))
        return None


async def scrape_url(url: str) -> str | None:
    if not _fc:
        return None
    try:
        result = _fc.scrape(url, formats=["markdown"])
        md = result.markdown or ""
        if md and len(md) > MAX_CONTENT_LENGTH:
            md = md[:MAX_CONTENT_LENGTH] + "\n\n[...обрезано]"
        return md or None
    except Exception as e:
        await logger.awarn("Ошибка скрейпинга", error=str(e), url=url)
        return None
