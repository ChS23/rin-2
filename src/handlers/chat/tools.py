import os
from pathlib import Path

import aiofiles
import structlog
from agents import function_tool
from firecrawl import FirecrawlApp

from src.bot import rdb

logger = structlog.get_logger("chat.tools")

SCRIPTS_DIR = Path("/app/data/scripts")
PENDING_FILE_KEY = "rin:pending_file"

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


def _safe_path(filename: str) -> Path:
    """Резолвить путь внутри SCRIPTS_DIR, не допуская выхода за пределы."""
    parts = [p for p in Path(filename).parts if p not in ("", ".", "..")]
    resolved = SCRIPTS_DIR.joinpath(*parts) if parts else SCRIPTS_DIR / "script.py"
    # гарантируем что путь внутри SCRIPTS_DIR
    resolved.relative_to(SCRIPTS_DIR)
    return resolved


@function_tool
async def write_script(filename: str, content: str) -> str:
    """Написать Python или Ren'Py файл и прикрепить его к ответу.
    filename — путь относительно папки скриптов, например 'scene_lab.rpy' или 'chastota/game/loop1.rpy'.
    content — полное содержимое файла."""
    try:
        file_path = _safe_path(filename)
    except ValueError:
        return "Недопустимый путь файла"
    if not any(file_path.suffix == ext for ext in (".py", ".rpy", ".txt")):
        file_path = file_path.with_suffix(file_path.suffix + ".py")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
        await f.write(content)
    lines = len(content.splitlines())
    rel = file_path.relative_to(SCRIPTS_DIR)
    await rdb.set(PENDING_FILE_KEY, str(file_path), ex=300)
    await logger.ainfo("Файл создан", filename=str(rel), lines=lines, path=str(file_path))
    return f"Файл {rel} готов ({lines} строк)"


@function_tool
async def read_script(filename: str) -> str:
    """Прочитать ранее написанный файл скрипта.
    filename — путь относительно папки скриптов, например 'chastota/game/loop1.rpy'."""
    try:
        file_path = _safe_path(filename)
    except ValueError:
        return "Недопустимый путь файла"
    if not file_path.exists():
        return f"Файл {filename} не найден"
    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            content = await f.read()
        if len(content) > 4000:
            content = content[:4000] + "\n\n[...обрезано]"
        return content
    except Exception as e:
        return f"Не удалось прочитать: {e}"


@function_tool
def list_scripts() -> str:
    """Показать список всех написанных файлов скриптов."""
    if not SCRIPTS_DIR.exists():
        return "Файлов нет"
    files = sorted(SCRIPTS_DIR.rglob("*"))
    files = [f for f in files if f.is_file()]
    if not files:
        return "Файлов нет"
    lines = []
    for f in files:
        rel = f.relative_to(SCRIPTS_DIR)
        size = f.stat().st_size
        lines.append(f"{rel} ({size} байт)")
    return "\n".join(lines)


all_tools = [web_search, read_url, write_script, read_script, list_scripts]
