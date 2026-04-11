import asyncio

import structlog
from agents import function_tool

from src.handlers.chat.tools import (
    SCRIPTS_DIR,
    write_script, edit_file, read_script, list_scripts,
    generate_image, compose_midi,
)

logger = structlog.get_logger("creative.tools")

RENPY_SH = "/opt/renpy/renpy.sh"
PROJECT_DIR = SCRIPTS_DIR / "chastota"
GAME_DIR = PROJECT_DIR / "game"
ROADMAP_PATH = PROJECT_DIR / "ROADMAP.md"


@function_tool
async def renpy_lint() -> str:
    """Запустить проверку (lint) Ren'Py проекта. Покажет ошибки синтаксиса, битые ссылки, недостающие файлы."""
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


@function_tool
async def renpy_compile() -> str:
    """Скомпилировать .rpy файлы в .rpyc. Проверяет синтаксис без запуска игры."""
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


@function_tool
def read_roadmap() -> str:
    """Прочитать текущий роадмап проекта — что сделано, что дальше."""
    if not ROADMAP_PATH.exists():
        return "Роадмап ещё не создан"
    content = ROADMAP_PATH.read_text(encoding="utf-8")
    if len(content) > 4000:
        content = content[:4000] + "\n\n[...обрезано]"
    return content


@function_tool
def update_roadmap(content: str) -> str:
    """Обновить роадмап проекта. content — полное содержимое файла ROADMAP.md."""
    ROADMAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    ROADMAP_PATH.write_text(content, encoding="utf-8")
    return "Роадмап обновлён"


# Все инструменты для creative agent
creative_tools = [
    write_script, edit_file, read_script, list_scripts,
    generate_image,
    read_roadmap, update_roadmap,
    renpy_lint, renpy_compile,
]

music_creative_tools = [compose_midi]
