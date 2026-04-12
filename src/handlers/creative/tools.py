import asyncio
import re
import urllib.parse

import aiofiles
import aiohttp
import structlog
from agents import function_tool

from src.handlers.chat.tools import (
    SCRIPTS_DIR, _safe_path,
    compose_midi, POLLINATIONS_URL,
    web_search, read_url,
)

logger = structlog.get_logger("creative.tools")

RENPY_SH = "/opt/renpy/renpy.sh"
PROJECT_DIR = SCRIPTS_DIR / "chastota"
GAME_DIR = PROJECT_DIR / "game"
ROADMAP_PATH = PROJECT_DIR / "ROADMAP.md"
WEB_BUILD_DIR = SCRIPTS_DIR / "chastota_web"

ALLOWED_WRITE_EXTS = {".rpy", ".py", ".txt", ".md", ".cfg", ".json"}


# ═══════════════════════════════════════════════════════════
#                    ФАЙЛОВЫЕ ИНСТРУМЕНТЫ
# ═══════════════════════════════════════════════════════════

@function_tool
async def read_file(filename: str, offset: int = 0, limit: int = 200) -> str:
    """Прочитать файл проекта.
    filename — путь относительно папки скриптов, например 'chastota/game/script.rpy'.
    offset — с какой строки начать (0 = сначала).
    limit — сколько строк читать (по умолчанию 200). Для больших файлов читай частями."""
    try:
        file_path = _safe_path(filename)
    except ValueError:
        return "Недопустимый путь файла"
    if not file_path.exists():
        return f"Файл {filename} не найден"
    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            content = await f.read()
        lines = content.splitlines()
        total = len(lines)
        chunk = lines[offset:offset + limit]
        result = "\n".join(f"{offset + i + 1}: {l}" for i, l in enumerate(chunk))
        if offset + limit < total:
            result += f"\n\n[показано {len(chunk)} из {total} строк, offset={offset}]"
        return result
    except Exception as e:
        return f"Ошибка чтения: {e}"


@function_tool
async def write_file(filename: str, content: str) -> str:
    """Записать файл в проект (создаёт или перезаписывает).
    filename — путь относительно папки скриптов, например 'chastota/game/script.rpy'.
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
async def edit_file(filename: str, old_string: str, new_string: str) -> str:
    """Заменить фрагмент в файле. old_string должен встречаться ровно один раз.
    filename — путь относительно папки скриптов.
    old_string — точный текст для замены.
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
            return f"Фрагмент встречается {count} раз — уточни контекст"
        new_content = content.replace(old_string, new_string, 1)
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(new_content)
        lines = new_content.splitlines()
        insert_line = new_content[: new_content.index(new_string)].count("\n")
        start = max(0, insert_line - 2)
        end = min(len(lines), insert_line + new_string.count("\n") + 3)
        snippet = "\n".join(f"{start + i + 1}: {l}" for i, l in enumerate(lines[start:end]))
        await logger.ainfo("Creative: файл отредактирован", filename=filename)
        return f"Готово:\n{snippet}"
    except Exception as e:
        return f"Ошибка: {e}"


@function_tool
def find_files(pattern: str = "*.rpy", path: str = "chastota") -> str:
    """Найти файлы по glob-паттерну.
    pattern — паттерн (*.rpy, **/*.png, *.ogg). По умолчанию *.rpy.
    path — директория для поиска относительно скриптов. По умолчанию 'chastota'."""
    try:
        search_dir = _safe_path(path) if path else SCRIPTS_DIR
    except ValueError:
        return "Недопустимый путь"
    if not search_dir.exists():
        return f"Директория {path} не найдена"
    files = sorted(search_dir.rglob(pattern))
    files = [f for f in files if f.is_file()]
    if not files:
        return f"Файлы по паттерну '{pattern}' не найдены"
    lines = []
    for f in files:
        rel = f.relative_to(SCRIPTS_DIR)
        size = f.stat().st_size
        lines.append(f"{rel} ({size} б)")
    return "\n".join(lines)


@function_tool
def grep_files(pattern: str, path: str = "chastota", glob: str = "*.rpy", context: int = 0, ignore_case: bool = False) -> str:
    """Поиск текста по содержимому файлов (regex).
    pattern — что искать (regex), например 'label day1_' или 'define.*Character'.
    path — директория для поиска. По умолчанию 'chastota'.
    glob — фильтр файлов. По умолчанию '*.rpy'.
    context — сколько строк вокруг совпадения показать (0 = только совпавшую строку).
    ignore_case — игнорировать регистр."""
    try:
        search_dir = _safe_path(path) if path else SCRIPTS_DIR
    except ValueError:
        return "Недопустимый путь"
    if not search_dir.exists():
        return f"Директория {path} не найдена"
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Невалидный regex: {e}"
    results = []
    for f in sorted(search_dir.rglob(glob)):
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if regex.search(line):
                rel = f.relative_to(SCRIPTS_DIR)
                if context > 0:
                    start = max(0, i - context)
                    end = min(len(lines), i + context + 1)
                    block = "\n".join(f"  {start+j+1}: {lines[start+j]}" for j in range(end - start))
                    results.append(f"{rel}:{i+1}:\n{block}")
                else:
                    results.append(f"{rel}:{i+1}: {line.strip()}")
                if len(results) >= 50:
                    results.append("[...обрезано, >50 совпадений]")
                    return "\n".join(results)
    return "\n".join(results) if results else "Совпадений не найдено"


@function_tool
def delete_file(filename: str) -> str:
    """Удалить файл из проекта.
    filename — путь относительно папки скриптов."""
    try:
        file_path = _safe_path(filename)
    except ValueError:
        return "Недопустимый путь файла"
    if not file_path.exists():
        return f"Файл {filename} не найден"
    file_path.unlink()
    return f"Файл {filename} удалён"


@function_tool
def move_file(src: str, dst: str) -> str:
    """Переместить/переименовать файл.
    src — текущий путь. dst — новый путь. Оба относительно папки скриптов."""
    try:
        src_path = _safe_path(src)
        dst_path = _safe_path(dst)
    except ValueError:
        return "Недопустимый путь"
    if not src_path.exists():
        return f"Файл {src} не найден"
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.rename(dst_path)
    return f"Перемещён: {src} → {dst}"


@function_tool
async def bash(command: str, timeout: int = 30) -> str:
    """Выполнить shell-команду в контейнере. Рабочая директория: /app/data/scripts.
    command — команда для выполнения.
    timeout — таймаут в секундах (по умолчанию 30, максимум 120).
    Используй для: ls, du, wc, diff, head, tail, cp, chmod, tree, и любых команд которых нет в других инструментах.
    Не используй для: редактирования файлов (есть edit_file), поиска (есть grep_files/find_files)."""
    timeout = max(5, min(120, timeout))
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SCRIPTS_DIR),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode()
        if stderr:
            output += "\nSTDERR: " + stderr.decode()
        if len(output) > 3000:
            output = output[:3000] + "\n\n[...обрезано]"
        return output or "(пустой вывод)"
    except asyncio.TimeoutError:
        return "Таймаут (>30с)"
    except Exception as e:
        return f"Ошибка: {e}"


# ═══════════════════════════════════════════════════════════
#                    VISION (GLM-4.6V)
# ═══════════════════════════════════════════════════════════

import os
from openai import AsyncOpenAI

_vision_client = AsyncOpenAI(
    api_key=os.getenv("AI_API_KEY"),
    base_url=os.getenv("AI_BASE_URL"),
)
VISION_MODEL = "glm-4.6v"


@function_tool
async def analyze_image(image_url: str, question: str = "Опиши что на картинке") -> str:
    """Анализ изображения через vision-модель.
    image_url — URL картинки или путь к локальному файлу (будет прочитан как base64).
    question — что спросить про картинку. Примеры:
      'Опиши что на картинке' — общий анализ
      'Прочитай текст на скриншоте' — OCR
      'Что за ошибка на скриншоте?' — диагностика
      'Опиши архитектуру на диаграмме' — анализ схем
      'Сравни с оригинальным дизайном' — UI ревью"""
    import base64

    content = [{"type": "text", "text": question}]

    # Если это локальный файл — читаем как base64
    if not image_url.startswith("http"):
        try:
            file_path = _safe_path(image_url)
            if not file_path.exists():
                return f"Файл {image_url} не найден"
            with open(file_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            ext = file_path.suffix.lower().strip(".")
            if ext == "jpg":
                ext = "jpeg"
            data_url = f"data:image/{ext};base64,{b64}"
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        except Exception as e:
            return f"Ошибка чтения файла: {e}"
    else:
        content.append({"type": "image_url", "image_url": {"url": image_url}})

    try:
        resp = await _vision_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": content}],
            max_tokens=1000,
        )
        result = resp.choices[0].message.content or ""
        if len(result) > 3000:
            result = result[:3000] + "\n\n[...обрезано]"
        return result
    except Exception as e:
        return f"Ошибка vision: {e}"


# ═══════════════════════════════════════════════════════════
#                    ГЕНЕРАЦИЯ АССЕТОВ
# ═══════════════════════════════════════════════════════════

@function_tool
async def create_image(description: str, style: str = "digital art", filename: str = "") -> str:
    """Сгенерировать картинку для игры и сохранить в проект.
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


# ═══════════════════════════════════════════════════════════
#                    REN'PY ИНСТРУМЕНТЫ
# ═══════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════
#                    ЭКСПОРТ
# ═══════════════════════════════════════════════════════════

creative_tools = [
    # Файлы
    read_file, write_file, edit_file,
    find_files, grep_files,
    delete_file, move_file,
    bash,
    # Веб
    web_search, read_url,
    # Vision
    analyze_image,
    # Ассеты
    create_image,
    # Роадмап
    read_roadmap, update_roadmap,
    # Ren'Py
    renpy_lint, renpy_compile, renpy_web_build,
]

music_creative_tools = [compose_midi]
