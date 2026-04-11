import asyncio
import os
from pathlib import Path

import structlog
from agents import function_tool
from firecrawl import FirecrawlApp

logger = structlog.get_logger("chat.tools")

SCRIPTS_DIR = Path("/app/data/scripts")
_file_registry: dict[int, str] = {}  # task id -> file path

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
        if not results.web:
            return "Ничего не найдено"

        parts = []
        for item in results.web[:3]:
            parts.append(f"{item.title}\n{item.url}\n{item.description or ''}")

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
        result = _fc.scrape(url, formats=["markdown"])
        md = result.markdown or ""
        if not md:
            return "Не удалось прочитать страницу"
        if len(md) > MAX_CONTENT:
            md = md[:MAX_CONTENT] + "\n\n[...обрезано]"
        return md
    except Exception as e:
        logger.warning("Ошибка скрейпинга", error=str(e), url=url)
        return f"Не удалось прочитать: {e}"


@function_tool
def write_script(filename: str, content: str) -> str:
    """Написать Python или Ren'Py файл и прикрепить его к ответу.
    filename — имя файла, например 'persistent_example.py' или 'scene_lab.rpy'.
    content — полное содержимое файла."""
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = filename.replace("/", "_").replace("..", "_").strip() or "script.py"
    if not any(safe_name.endswith(ext) for ext in (".py", ".rpy", ".txt")):
        safe_name += ".py"
    safe_name = safe_name[-64:]
    file_path = SCRIPTS_DIR / safe_name
    file_path.write_text(content, encoding="utf-8")
    task = asyncio.current_task()
    if task:
        _file_registry[id(task)] = str(file_path)
    return f"Файл {safe_name} готов ({len(content.splitlines())} строк)"


all_tools = [web_search, read_url, write_script]
