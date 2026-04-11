import asyncio
import urllib.parse

import aiofiles
import aiohttp
import structlog
from agents import function_tool

from src.handlers.chat.tools import (
    SCRIPTS_DIR, _safe_path,
    edit_file, read_script, list_scripts,
    compose_midi, POLLINATIONS_URL,
)

logger = structlog.get_logger("creative.tools")

RENPY_SH = "/opt/renpy/renpy.sh"
PROJECT_DIR = SCRIPTS_DIR / "chastota"
GAME_DIR = PROJECT_DIR / "game"
ROADMAP_PATH = PROJECT_DIR / "ROADMAP.md"
WEB_BUILD_DIR = SCRIPTS_DIR / "chastota_web"


ALLOWED_WRITE_EXTS = {".rpy", ".py", ".txt", ".md", ".cfg"}


@function_tool
async def write_file(filename: str, content: str) -> str:
    """Записать файл в проект.
    filename — путь относительно папки скриптов, например 'chastota/game/script.rpy' или 'chastota/ROADMAP.md'.
    content — полное содержимое файла."""
    try:
        file_path = _safe_path(filename)
    except ValueError:
        return "Недопустимый путь файла"
    if file_path.suffix.lower() not in ALLOWED_WRITE_EXTS:
        return f"Недопустимое расширение {file_path.suffix}. Разрешены: {', '.join(sorted(ALLOWED_WRITE_EXTS))}"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
        await f.write(content)
    lines = len(content.splitlines())
    rel = file_path.relative_to(SCRIPTS_DIR)
    await logger.ainfo("Creative: файл записан", filename=str(rel), lines=lines)
    return f"Файл {rel} записан ({lines} строк)"


@function_tool
async def create_image(description: str, style: str = "digital art", filename: str = "") -> str:
    """Сгенерировать картинку для игры и сохранить в проект (НЕ прикрепляется к VK).
    description — описание НА АНГЛИЙСКОМ, 30-80 слов.
    style — стиль: 'digital art', 'anime', 'watercolor', 'photo', 'pixel art'.
    filename — путь, например 'chastota/game/images/bg_station_night.png'. Обязателен."""
    if not filename:
        return "Укажи filename — путь для сохранения"
    try:
        file_path = _safe_path(filename)
    except ValueError:
        return "Недопустимый путь файла"
    if not file_path.suffix:
        file_path = file_path.with_suffix(".png")

    raw_prompt = f"{style} style, {description}"
    encoded = urllib.parse.quote(raw_prompt)
    url = POLLINATIONS_URL.format(prompt=encoded)
    full_url = f"{url}?width=1024&height=1024&nologo=true&enhance=true&safe=true"

    file_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(full_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    return f"Ошибка генерации: HTTP {resp.status}"
                data = await resp.read()
                if len(data) < 1000:
                    return "Получена пустая картинка"
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(data)
        rel = file_path.relative_to(SCRIPTS_DIR)
        size_kb = len(data) // 1024
        await logger.ainfo("Creative: картинка создана", filename=str(rel), size_kb=size_kb)
        return f"Картинка {rel} сохранена ({size_kb} KB)"
    except asyncio.TimeoutError:
        return "Таймаут генерации (>60с)"
    except Exception as e:
        return f"Ошибка: {e}"


async def _renpy_lint_impl() -> str:
    if not GAME_DIR.exists():
        return "Папка game/ не существует — нечего проверять"
    try:
        proc = await asyncio.create_subprocess_exec(
            RENPY_SH, str(PROJECT_DIR), "lint",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"HOME": "/tmp", "PATH": "/usr/bin:/bin:/opt/renpy"},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = stdout.decode()
        if len(output) > 3000:
            output = output[:3000] + "\n\n[...обрезано]"
        return output or stderr.decode()[:1000] or "Lint завершён без вывода"
    except asyncio.TimeoutError:
        return "Таймаут lint (>60с)"
    except Exception as e:
        return f"Ошибка lint: {e}"


async def _renpy_compile_impl() -> str:
    if not GAME_DIR.exists():
        return "Папка game/ не существует"
    try:
        proc = await asyncio.create_subprocess_exec(
            RENPY_SH, str(PROJECT_DIR), "compile",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"HOME": "/tmp", "PATH": "/usr/bin:/bin:/opt/renpy"},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = stdout.decode()
        if proc.returncode == 0:
            return "Компиляция успешна"
        return output or stderr.decode()[:1000]
    except asyncio.TimeoutError:
        return "Таймаут компиляции (>60с)"
    except Exception as e:
        return f"Ошибка компиляции: {e}"


def _read_roadmap_impl() -> str:
    if not ROADMAP_PATH.exists():
        return "Роадмап ещё не создан"
    content = ROADMAP_PATH.read_text(encoding="utf-8")
    if len(content) > 4000:
        content = content[:4000] + "\n\n[...обрезано]"
    return content


def _update_roadmap_impl(content: str) -> str:
    ROADMAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    ROADMAP_PATH.write_text(content, encoding="utf-8")
    return "Роадмап обновлён"


async def _renpy_web_build_impl() -> str:
    if not GAME_DIR.exists():
        return "Папка game/ не существует"
    try:
        proc = await asyncio.create_subprocess_exec(
            RENPY_SH, "/opt/renpy/launcher", "web_build",
            str(PROJECT_DIR), "--destination", str(WEB_BUILD_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"HOME": "/tmp", "PATH": "/usr/bin:/bin:/opt/renpy"},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            return f"Веб-билд готов в {WEB_BUILD_DIR}"
        output = stdout.decode() + stderr.decode()
        return f"Ошибка web_build:\n{output[:1000]}"
    except asyncio.TimeoutError:
        return "Таймаут web_build (>120с)"
    except Exception as e:
        return f"Ошибка: {e}"


# Tool-обёртки для agent SDK
@function_tool
async def renpy_lint() -> str:
    """Запустить проверку (lint) Ren'Py проекта. Покажет ошибки синтаксиса, битые ссылки, недостающие файлы."""
    return await _renpy_lint_impl()


@function_tool
async def renpy_compile() -> str:
    """Скомпилировать .rpy файлы в .rpyc. Проверяет синтаксис без запуска игры."""
    return await _renpy_compile_impl()


@function_tool
def read_roadmap() -> str:
    """Прочитать текущий роадмап проекта — что сделано, что дальше."""
    return _read_roadmap_impl()


@function_tool
def update_roadmap(content: str) -> str:
    """Обновить роадмап проекта. content — полное содержимое файла ROADMAP.md."""
    return _update_roadmap_impl(content)


@function_tool
async def renpy_web_build() -> str:
    """Собрать веб-версию игры (HTML+WASM). Результат в папке chastota_web/."""
    return await _renpy_web_build_impl()


# Все инструменты для creative agent
creative_tools = [
    write_file, edit_file, read_script, list_scripts,
    create_image,
    read_roadmap, update_roadmap,
    renpy_lint, renpy_compile, renpy_web_build,
]

music_creative_tools = [compose_midi]
