import asyncio
import os
import zipfile
from pathlib import Path

import aiofiles
import aiohttp
import mido
import structlog
from agents import function_tool
from firecrawl import FirecrawlApp
from pydantic import BaseModel

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


SOUNDFONT = os.getenv("SOUNDFONT", "/usr/share/sounds/sf2/FluidR3_GM.sf2")


class MidiNote(BaseModel):
    pitch: int = 60   # MIDI нота 0-127
    vel: int = 64     # громкость 0-127
    beat: float = 0.0 # начало в долях
    dur: float = 1.0  # длительность в долях


class MidiTrack(BaseModel):
    program: int = 0        # инструмент 0-127
    notes: list[MidiNote] = []


@function_tool
async def compose_midi(filename: str, bpm: int, tracks: list[MidiTrack]) -> str:
    """Сочинить музыку и прикрепить как .ogg файл.
    filename — имя без расширения, например 'drone_loop1'.
    bpm — темп (для эмбиента 40-70, для мелодии 80-120).
    tracks — список инструментов: [{"program": 48, "notes": [{"pitch": 48, "vel": 25, "beat": 0.0, "dur": 4.0}]}]
    Полезные program: 0=фортепиано, 40=скрипка, 48=струнные, 51=хор, 88=синт-пад, 92=атмосфера.
    pitch — нота MIDI (36=C2, 48=C3, 60=C4, 67=G4). vel — громкость 0-127. beat — начало в долях. dur — длительность в долях."""
    ticks = 480
    tempo = mido.bpm2tempo(max(10, min(300, bpm)))
    mid = mido.MidiFile(ticks_per_beat=ticks)
    meta = mido.MidiTrack()
    mid.tracks.append(meta)
    meta.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))

    for ch_idx, td in enumerate(tracks[:15]):
        track = mido.MidiTrack()
        mid.tracks.append(track)
        ch = ch_idx % 16
        prog = max(0, min(127, td.program))
        track.append(mido.Message("program_change", channel=ch, program=prog, time=0))

        events: list[tuple] = []
        for n in td.notes:
            pitch = max(0, min(127, n.pitch))
            vel   = max(0, min(127, n.vel))
            beat  = n.beat
            dur   = max(0.05, n.dur)
            events.append((int(beat * ticks),       "on",  ch, pitch, vel))
            events.append((int((beat + dur) * ticks), "off", ch, pitch))

        events.sort(key=lambda e: e[0])
        prev = 0
        for ev in events:
            delta, prev = ev[0] - prev, ev[0]
            if ev[1] == "on":
                track.append(mido.Message("note_on",  channel=ev[2], note=ev[3], velocity=ev[4], time=delta))
            else:
                track.append(mido.Message("note_off", channel=ev[2], note=ev[3], velocity=0,    time=delta))

    safe = filename.replace("/", "_").strip() or "music"
    mid_path = SCRIPTS_DIR / f"{safe}.mid"
    ogg_path = SCRIPTS_DIR / f"{safe}.ogg"
    mid_path.parent.mkdir(parents=True, exist_ok=True)
    mid.save(str(mid_path))

    proc = await asyncio.create_subprocess_exec(
        "fluidsynth", "-ni", SOUNDFONT, str(mid_path),
        "-F", str(ogg_path), "-r", "44100",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    mid_path.unlink(missing_ok=True)

    if not ogg_path.exists():
        return f"Ошибка рендера FluidSynth: {stderr.decode()[:300]}"

    size_kb = ogg_path.stat().st_size // 1024
    await rdb.set(PENDING_FILE_KEY, str(ogg_path), ex=300)
    await logger.ainfo("Музыка создана", filename=f"{safe}.ogg", size_kb=size_kb, bpm=bpm)
    return f"Файл {safe}.ogg готов ({size_kb} KB)"


all_tools = [web_search, read_url, write_script, edit_file, read_script, list_scripts, download_file, create_archive]
music_tools = [compose_midi]
