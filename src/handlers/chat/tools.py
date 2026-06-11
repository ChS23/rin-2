import asyncio
import os
import re
import urllib.parse
import zipfile
from pathlib import Path

import aiofiles
import aiohttp
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


@function_tool
async def send_file(filename: str) -> str:
    """Прикрепить уже существующий файл к ответу (не создавая новый).
    filename — путь относительно папки скриптов, например 'arctic_drone.ogg'."""
    try:
        file_path = _safe_path(filename)
    except ValueError:
        return "Недопустимый путь файла"
    if not file_path.exists():
        return f"Файл {filename} не найден"
    size_kb = file_path.stat().st_size // 1024
    if size_kb > MAX_DOWNLOAD_BYTES // 1024:
        return f"Файл слишком большой ({size_kb} KB)"
    await rdb.set(PENDING_FILE_KEY, str(file_path), ex=300)
    rel = file_path.relative_to(SCRIPTS_DIR)
    await logger.ainfo("Файл прикреплён", filename=str(rel), size_kb=size_kb)
    return f"Файл {rel} будет прикреплён ({size_kb} KB)"


ALLOWED_EXTS = {
    ".ogg", ".mp3", ".wav", ".flac",           # аудио
    ".png", ".jpg", ".jpeg", ".gif", ".webp",  # картинки
    ".pdf", ".txt", ".md", ".csv",             # документы
    ".zip",                                     # архивы
}
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024  # 30 MB


@function_tool
async def download_file(url: str, filename: str) -> str:
    """Скачать файл по URL и прикрепить к ответу.
    url — прямая ссылка на файл.
    filename — имя файла для сохранения, например 'wind_ambient.ogg' или 'reference.png'."""
    try:
        file_path = _safe_path(filename)
    except ValueError:
        return "Недопустимый путь файла"
    if file_path.suffix.lower() not in ALLOWED_EXTS:
        return f"Недопустимое расширение. Разрешены: {', '.join(sorted(ALLOWED_EXTS))}"
    file_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    return f"Ошибка загрузки: HTTP {resp.status}"
                content_length = resp.content_length
                if content_length and content_length > MAX_DOWNLOAD_BYTES:
                    return f"Файл слишком большой ({content_length // 1024 // 1024} MB, лимит 30 MB)"
                data = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_DOWNLOAD_BYTES:
                        return "Файл слишком большой (лимит 30 MB)"

        async with aiofiles.open(file_path, "wb") as f:
            await f.write(data)

        size_kb = len(data) // 1024
        rel = file_path.relative_to(SCRIPTS_DIR)
        await rdb.set(PENDING_FILE_KEY, str(file_path), ex=300)
        await logger.ainfo("Файл скачан", filename=str(rel), size_kb=size_kb, url=url)
        return f"Файл {rel} скачан ({size_kb} KB)"
    except Exception as e:
        return f"Не удалось скачать: {e}"


@function_tool
async def edit_file(filename: str, old_string: str, new_string: str) -> str:
    """Заменить фрагмент в существующем файле.
    filename — путь относительно папки скриптов.
    old_string — точный текст для замены (должен встречаться ровно один раз).
    new_string — на что заменить."""
    try:
        file_path = _safe_path(filename)
    except ValueError:
        return "Недопустимый путь файла"
    if not file_path.exists():
        return f"Файл {filename} не найден"
    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            content = await f.read()
        count = content.count(old_string)
        if count == 0:
            return "Фрагмент не найден в файле"
        if count > 1:
            return f"Фрагмент встречается {count} раз — уточни контекст чтобы было однозначно"
        new_content = content.replace(old_string, new_string, 1)
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(new_content)
        # показываем контекст вокруг изменения
        lines = new_content.splitlines()
        insert_line = new_content[: new_content.index(new_string)].count("\n")
        start = max(0, insert_line - 2)
        end = min(len(lines), insert_line + new_string.count("\n") + 3)
        snippet = "\n".join(f"{start + i + 1}: {l}" for i, l in enumerate(lines[start:end]))
        await logger.ainfo("Файл отредактирован", filename=filename)
        return f"Готово:\n{snippet}"
    except Exception as e:
        return f"Ошибка: {e}"


@function_tool
async def create_archive(filenames: list[str], archive_name: str) -> str:
    """Упаковать несколько файлов в zip-архив и прикрепить к ответу.
    filenames — список путей относительно папки скриптов (используй list_scripts чтобы узнать что есть).
    archive_name — имя архива, например 'chastota_sounds.zip'."""
    if not archive_name.endswith(".zip"):
        archive_name += ".zip"
    archive_path = SCRIPTS_DIR / archive_name.replace("/", "_")

    missing = []
    resolved = []
    for name in filenames:
        try:
            p = _safe_path(name)
            if not p.exists():
                missing.append(name)
            else:
                resolved.append(p)
        except ValueError:
            missing.append(name)

    if not resolved:
        return "Ни один из файлов не найден: " + ", ".join(missing)

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in resolved:
            zf.write(p, p.relative_to(SCRIPTS_DIR))

    size_kb = archive_path.stat().st_size // 1024
    await rdb.set(PENDING_FILE_KEY, str(archive_path), ex=300)
    await logger.ainfo("Архив создан", archive=archive_name, files=len(resolved), size_kb=size_kb)

    result = f"Архив {archive_name} готов ({len(resolved)} файлов, {size_kb} KB)"
    if missing:
        result += f". Не найдено: {', '.join(missing)}"
    return result


POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"

# Фиксированная внешность Рин для консистентных "селфи"
RIN_APPEARANCE = (
    "young woman, early 20s, messy dark brown shoulder-length hair with side bangs, "
    "tired dark eyes with slight eye bags, pale skin, thin build, "
    "wearing an oversized dark hoodie, no makeup, "
    "small silver earring in left ear, chipped black nail polish"
)


@function_tool
async def generate_image(description: str, style: str = "digital art", selfie: bool = False, filename: str = "") -> str:
    """Сгенерировать картинку по описанию и прикрепить к ответу.
    description — описание на любом языке, например 'арктическая метеостанция ночью' или 'кот в скафандре'.
    style — стиль: 'digital art', 'anime', 'watercolor', 'photo', 'pixel art', 'oil painting' (по умолчанию digital art).
    selfie — если True, на картинке будешь ты (Рин). Используй когда просят фото/селфи/как ты выглядишь.
    filename — путь для сохранения, например 'chastota/game/images/bg_station_night.png'. Если пусто — автоимя.
    Промпт будет автоматически улучшен для лучшего результата."""
    # Если селфи — подмешиваем фиксированную внешность Рин
    if selfie:
        raw_prompt = f"{style} style, {RIN_APPEARANCE}, {description}"
    else:
        raw_prompt = f"{style} style, {description}"
    encoded = urllib.parse.quote(raw_prompt)

    url = POLLINATIONS_URL.format(prompt=encoded)
    params = "width=1024&height=1024&nologo=true&enhance=true&safe=true"
    full_url = f"{url}?{params}"

    if filename:
        try:
            file_path = _safe_path(filename)
        except ValueError:
            return "Недопустимый путь файла"
        if not file_path.suffix:
            file_path = file_path.with_suffix(".png")
    else:
        safe_name = re.sub(r'[^\w\s-]', '', description[:40]).strip().replace(' ', '_') or "image"
        file_path = SCRIPTS_DIR / f"{safe_name}.png"
    file_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(full_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    return f"Ошибка генерации: HTTP {resp.status}"
                data = await resp.read()
                if len(data) < 1000:
                    return "Получена пустая или слишком маленькая картинка"

        async with aiofiles.open(file_path, "wb") as f:
            await f.write(data)

        size_kb = len(data) // 1024
        rel = file_path.relative_to(SCRIPTS_DIR)
        await rdb.set(PENDING_FILE_KEY, str(file_path), ex=300)
        await logger.ainfo("Картинка сгенерирована", filename=str(rel), size_kb=size_kb, style=style)
        return f"Картинка {rel} готова ({size_kb} KB)"
    except asyncio.TimeoutError:
        return "Таймаут генерации картинки (>60с)"
    except Exception as e:
        return f"Ошибка: {e}"


CATBOX_API = "https://catbox.moe/user/api.php"


@function_tool
async def upload_to_catbox(filename: str) -> str:
    """Загрузить файл на catbox.moe и получить прямую ссылку. Используй когда ВК не принимает файл.
    filename — путь относительно папки скриптов."""
    try:
        file_path = _safe_path(filename)
    except ValueError:
        return "Недопустимый путь файла"
    if not file_path.exists():
        return f"Файл {filename} не найден"
    try:
        async with aiohttp.ClientSession() as session:
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("reqtype", "fileupload")
                data.add_field("fileToUpload", f, filename=file_path.name)
                async with session.post(CATBOX_API, data=data, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    url = await resp.text()
                    if resp.status == 200 and url.startswith("https://"):
                        return url.strip()
                    return f"Ошибка catbox: {resp.status} {url[:200]}"
    except Exception as e:
        return f"не удалось загрузить на catbox: {e}"


all_tools = [web_search, read_url, write_script, edit_file, read_script, list_scripts, send_file, download_file, create_archive, generate_image, upload_to_catbox]
