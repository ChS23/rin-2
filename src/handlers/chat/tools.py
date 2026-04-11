import os
import re

import structlog
from agents import function_tool
from firecrawl import FirecrawlApp

logger = structlog.get_logger("chat.tools")

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
_fc = FirecrawlApp(api_key=FIRECRAWL_API_KEY) if FIRECRAWL_API_KEY else None

MAX_CONTENT = 1500


@function_tool
def web_search(query: str) -> str:
    """Поиск в интернете. Используй когда нужно найти актуальную информацию: даты джемов, документацию, новости, ответы на фактические вопросы."""
    if not _fc:
        return "Поиск недоступен"
    try:
        results = _fc.search(query, limit=3)
        if not results or not results.get("data"):
            return "Ничего не найдено"

        parts = []
        for item in results["data"][:3]:
            title = item.get("title", "")
            url = item.get("url", "")
            content = item.get("markdown", item.get("description", ""))
            if content and len(content) > 500:
                content = content[:500] + "..."
            parts.append(f"{title}\n{url}\n{content}")

        return "\n\n".join(parts)
    except Exception as e:
        logger.warning("Ошибка веб-поиска", error=str(e))
        return f"Ошибка поиска: {e}"


@function_tool
def read_url(url: str) -> str:
    """Прочитать содержимое веб-страницы. Используй когда кто-то скинул ссылку и спрашивает что там, или нужно прочитать документацию."""
    if not _fc:
        return "Чтение страниц недоступно"
    try:
        result = _fc.scrape_url(url, formats=["markdown"])
        md = result.get("markdown", "")
        if not md:
            return "Не удалось прочитать страницу"
        if len(md) > MAX_CONTENT:
            md = md[:MAX_CONTENT] + "\n\n[...обрезано]"
        return md
    except Exception as e:
        logger.warning("Ошибка скрейпинга", error=str(e), url=url)
        return f"Не удалось прочитать: {e}"


all_tools = [web_search, read_url]
