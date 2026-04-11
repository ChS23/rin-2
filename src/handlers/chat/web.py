import os
import re

import structlog
from firecrawl import FirecrawlApp

logger = structlog.get_logger("chat.web")

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
_fc = FirecrawlApp(api_key=FIRECRAWL_API_KEY) if FIRECRAWL_API_KEY else None

MAX_CONTENT_LENGTH = 2000


async def search_web(query: str) -> str | None:
    """Поиск через Firecrawl, вернуть markdown-результаты"""
    if not _fc:
        return None
    try:
        results = _fc.search(query, limit=3)
        if not results or not results.get("data"):
            return None

        parts = []
        for item in results["data"][:3]:
            title = item.get("title", "")
            url = item.get("url", "")
            content = item.get("markdown", item.get("description", ""))
            if content and len(content) > 500:
                content = content[:500] + "..."
            parts.append(f"**{title}**\n{url}\n{content}")

        return "\n\n".join(parts)
    except Exception as e:
        await logger.awarn("Ошибка веб-поиска", error=str(e))
        return None


async def scrape_url(url: str) -> str | None:
    """Скрейпить URL, вернуть markdown"""
    if not _fc:
        return None
    try:
        result = _fc.scrape_url(url, formats=["markdown"])
        md = result.get("markdown", "")
        if md and len(md) > MAX_CONTENT_LENGTH:
            md = md[:MAX_CONTENT_LENGTH] + "\n\n[...обрезано]"
        return md or None
    except Exception as e:
        await logger.awarn("Ошибка скрейпинга", error=str(e), url=url)
        return None


def extract_urls(text: str) -> list[str]:
    """Извлечь URL из текста"""
    return re.findall(r'https?://\S+', text)
