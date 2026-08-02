import asyncio
import datetime
from pathlib import Path

import orjson

import structlog
from src.bot import rdb
from src.utils import run_agent_streamed
from src.handlers.checkin import ai_lock
from src.handlers.chat.agents import summary_agent, history_summary_agent
from src.utils import safe_json_write

logger = structlog.get_logger("chat.memory")

DATA_DIR = Path("/app/data")
MEMORY_FILE = DATA_DIR / "rin_memory.json"
HISTORY_SIZE = 100     # сколько хранить в Valkey
CONTEXT_SIZE = 30      # сколько отдавать в промпт чата
SUMMARIZE_EVERY = 40
SUMMARIZE_THRESHOLD = 12
MAX_FACTS_PER_USER = 10

_memory_lock = asyncio.Lock()


# ═══════════════════════════════════════════════════════════
#                    ФАКТЫ О ЛЮДЯХ (JSON)
# ═══════════════════════════════════════════════════════════

def load_memory() -> dict[str, dict]:
    if not MEMORY_FILE.exists():
        return {}
    try:
        return orjson.loads(MEMORY_FILE.read_bytes())
    except (orjson.JSONDecodeError, IOError):
        return {}


def save_memory(memory: dict[str, dict]):
    safe_json_write(MEMORY_FILE, memory)


async def remember_facts(user_id: int, user_name: str, facts: list[str]):
    async with _memory_lock:
        memory = load_memory()
        uid = str(user_id)
        if uid not in memory:
            memory[uid] = {"name": user_name, "facts": []}
        memory[uid]["name"] = user_name
        for fact in facts:
            if fact not in memory[uid]["facts"]:
                memory[uid]["facts"].append(fact)
        save_memory(memory)


async def forget_facts(user_id: int, facts: list[str]):
    async with _memory_lock:
        memory = load_memory()
        uid = str(user_id)
        if uid not in memory:
            return
        memory[uid]["facts"] = [f for f in memory[uid]["facts"] if f not in facts]
        if not memory[uid]["facts"]:
            del memory[uid]
        save_memory(memory)


def get_user_memory(user_id: int) -> list[str]:
    memory = load_memory()
    entry = memory.get(str(user_id))
    return entry["facts"] if entry else []


def get_all_memory_summary() -> str:
    memory = load_memory()
    if not memory:
        return ""
    lines = []
    for entry in memory.values():
        lines.append(f"{entry['name']}: {'; '.join(entry['facts'])}")
    return "\n".join(lines)


def _fmt_entry(entry: dict) -> str:
    """Строка о человеке для промпта: живая мысль-рефлексия (если есть) + факты."""
    facts = "; ".join(entry.get("facts", [])[:5])
    refl = entry.get("reflection")
    if refl:
        return f"{entry['name']} — {refl}\n    (факты: {facts})"
    return f"{entry['name']}: {facts}"


def get_memory_for_ids(user_ids: set[str], exclude_uid: int | None = None) -> str:
    """Вернуть факты только о людях из user_ids (участники чата)."""
    memory = load_memory()
    if not memory:
        return ""
    exclude = str(exclude_uid) if exclude_uid else None
    lines = []
    for uid, entry in memory.items():
        if uid == exclude:
            continue
        if uid in user_ids and entry.get("facts"):
            lines.append(_fmt_entry(entry))
    return "\n".join(lines)


def extract_participants_from_history(messages: list[str]) -> tuple[set[str], set[str]]:
    """Извлечь user_id и имена из истории.
    Новый формат: '@123 Имя: текст' → id.
    Старый формат: 'Имя: текст' → имя (fallback).
    Возвращает (user_ids, names_without_ids).
    """
    import re
    ids = set()
    names = set()
    for msg in messages:
        m = re.match(r"@(\d+) ", msg)
        if m:
            ids.add(m.group(1))
        elif ": " in msg:
            name = msg.split(": ", 1)[0]
            if name and name != "Рин":
                names.add(name)
    return ids, names


def get_memory_for_participants(user_ids: set[str], names: set[str], exclude_uid: int | None = None) -> str:
    """Вернуть факты о людях по id (приоритет) или по имени (fallback для старой истории)."""
    memory = load_memory()
    if not memory:
        return ""
    exclude = str(exclude_uid) if exclude_uid else None
    lines = []
    seen = set()
    # Сначала точное совпадение по id
    for uid, entry in memory.items():
        if uid == exclude:
            continue
        if uid in user_ids and entry.get("facts"):
            lines.append(_fmt_entry(entry))
            seen.add(entry["name"])
    # Потом fallback по имени (старые записи без id)
    if names:
        for uid, entry in memory.items():
            if uid == exclude or entry["name"] in seen:
                continue
            if entry["name"] in names and entry.get("facts"):
                lines.append(_fmt_entry(entry))
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#             РЕФЛЕКСИЯ — КАК РИН ВИДИТ ЧЕЛОВЕКА
# ═══════════════════════════════════════════════════════════
async def dump_state_snapshot(tag: str = "manual") -> int:
    """Слепок живого состояния из Valkey + файла памяти в /app/data/logs.

    Valkey-том в restic-бэкап НЕ входит, а bot_data входит — поэтому дамп кладём
    в файлы. Снимается дважды за ночь (до и после ночных джобов), чтобы было видно,
    что именно поменяли self_state-агент и рефлексии, а что — разговоры за день.
    """
    from src.handlers.chat.datalog import _append, code_version
    from src.handlers.checkin import CHAT_PEER_ID

    snap: dict = {"tag": tag, "code_version": code_version()}
    try:
        snap["self_state"] = await get_rin_self_state()
        snap["self_archive"] = await get_rin_self_archive()
        snap["episodes"] = await rdb.lrange(EPISODES_KEY, 0, -1)
        snap["chat_summary"] = await rdb.get(_summary_key(CHAT_PEER_ID))
        snap["chat_history"] = await rdb.lrange(_history_key(CHAT_PEER_ID), 0, -1)
        snap["proactive"] = {
            "enabled": await rdb.get("rin:proactive:enabled"),
            "decisions": await rdb.lrange("rin:proactive:shadow", 0, -1),
            "usage": await rdb.hgetall("rin:proactive:usage"),
        }
        snap["creative"] = {
            "paused": await rdb.get("rin:creative:paused"),
            "last_run": await rdb.get("rin:creative:last_run"),
        }
        snap["last_active"] = await rdb.get("rin:last_active")
        seen = {}
        async for k in rdb.scan_iter(match="rin:user:*:last_seen", count=200):
            seen[k.split(":")[2]] = await rdb.get(k)
        snap["last_seen"] = seen
        snap["people"] = load_memory()      # факты + рефлексии (история дрейфа по дням)
    except Exception as e:
        snap["error"] = f"{type(e).__name__}: {e}"

    _append("state", snap)
    await logger.ainfo("Слепок состояния сохранён", tag=tag,
                       self_state=len(snap.get("self_state") or []),
                       episodes=len(snap.get("episodes") or []))
    return len(snap.get("self_state") or [])


_NUMERAL_WORDS = (
    "тысяч", "сотн", "полторы", "полтора", "десятк", "сорок", "пятьдесят",
    "шестьдесят", "семьдесят", "восемьдесят", "девяносто", "двадцать", "тридцать",
    "сто ", "двести", "триста", "четыреста", "пятьсот", "миллион",
)


def _reflection_numbers_ok(text: str, facts: list[str]) -> str | None:
    """Проверить, что числа в рефлексии не выдуманы.

    Наблюдалось (02.08.2026): факт «1200+ спрайтов» превращался в рефлексии в
    «полторы тысячи», потом в «сто двадцать». Источник при этом оставался верным,
    так что расхождение незаметно — ловим его здесь.
    Возвращает причину отбраковки или None, если всё чисто.
    """
    import re
    joined = " ".join(facts)
    for num in re.findall(r"\d+", text or ""):
        if num not in joined:
            return f"число {num} отсутствует в фактах"
    low = (text or "").lower()
    for w in _NUMERAL_WORDS:
        if w in low:
            return f"число прописью ('{w.strip()}') — требуется цифрами и точно из фактов"
    return None


async def refresh_all_reflections():
    """Пересобрать 'как Рин видит человека' (одна живая мысль) для всех, у кого ≥3 фактов."""
    from src.handlers.chat.agents import reflection_agent
    memory = load_memory()
    updated = 0
    rejected = 0
    for uid, entry in list(memory.items()):
        facts = entry.get("facts") or []
        if len(facts) < 3:
            continue
        prompt = f"Человек: {entry['name']}\nФакты о нём:\n" + "\n".join(f"- {f}" for f in facts)
        try:
            async with ai_lock:
                result = await run_agent_streamed(reflection_agent, prompt)
            text = (result.final_output or "").strip().strip('"').strip()
            if text:
                bad = _reflection_numbers_ok(text, facts)
                if bad:
                    # искажённая цифра ушла бы в промпт как её собственная память
                    rejected += 1
                    await logger.awarn("Рефлексия отбракована", name=entry.get("name"),
                                       reason=bad, text=text[:120])
                    continue
                async with _memory_lock:
                    m = load_memory()
                    if uid in m:
                        m[uid]["reflection"] = text[:300]
                        save_memory(m)
                updated += 1
        except Exception as e:
            await logger.awarn("Не удалось сделать рефлексию", uid=uid, error=str(e))
    await logger.ainfo("Рефлексии обновлены", count=updated, rejected=rejected)


# ═══════════════════════════════════════════════════════════
#                 ЭПИЗОДЫ / ВНУТРЯКИ (Valkey)
# ═══════════════════════════════════════════════════════════
EPISODES_KEY = "rin:episodes"
EPISODES_MAX = 20


async def add_episode(text: str):
    """Запомнить памятный/смешной момент из чата (внутряк) — одной строкой от первого лица."""
    text = (text or "").strip()
    if not text:
        return
    existing = await rdb.lrange(EPISODES_KEY, 0, -1)
    if text in existing:  # не дублировать точь-в-точь
        return
    await rdb.rpush(EPISODES_KEY, text)
    await rdb.ltrim(EPISODES_KEY, -EPISODES_MAX, -1)


async def get_episodes(n: int = 6) -> list[str]:
    """Последние n внутряков для подмешивания в промпт."""
    return await rdb.lrange(EPISODES_KEY, -n, -1)


async def _compress_facts(user_name: str, facts: list[str]) -> list[str]:
    try:
        prompt = f"Факты о {user_name}:\n" + "\n".join(f"- {f}" for f in facts)
        async with ai_lock:
            result = await run_agent_streamed(summary_agent, prompt)
        raw = result.final_output.strip()
        compressed = orjson.loads(raw)
        if isinstance(compressed, list) and compressed:
            return [str(f) for f in compressed]
    except Exception:
        pass
    return facts[-MAX_FACTS_PER_USER:]


async def maybe_compress_memory(user_id: int):
    memory = load_memory()
    uid = str(user_id)
    if uid not in memory:
        return
    entry = memory[uid]
    if len(entry["facts"]) <= SUMMARIZE_THRESHOLD:
        return
    compressed = await _compress_facts(entry["name"], entry["facts"])
    async with _memory_lock:
        memory = load_memory()
        if uid in memory:
            memory[uid]["facts"] = compressed
            save_memory(memory)


# ═══════════════════════════════════════════════════════════
#                  ИСТОРИЯ ЧАТА (Valkey)
# ═══════════════════════════════════════════════════════════

def _history_key(peer_id: int) -> str:
    return f"rin:chat:{peer_id}:history"


def _summary_key(peer_id: int) -> str:
    return f"rin:chat:{peer_id}:summary"


def _counter_key(peer_id: int) -> str:
    return f"rin:chat:{peer_id}:counter"


GROUP_ID = 204871130


async def record_message(peer_id: int, from_id: int, text: str, resolve_name, extra: dict | None = None):
    if not text:
        return

    if from_id == -GROUP_ID:
        name = "Рин"
        uid_prefix = ""
        await touch_rin_active()
    else:
        name = await resolve_name(from_id)
        uid_prefix = f"@{from_id} "

    # даталог: полная история чата (в Valkey окно всего HISTORY_SIZE, а тут — навсегда)
    try:
        from src.handlers.chat.datalog import log_message
        log_message(peer_id, from_id, name, text, is_rin=(from_id == -GROUP_ID), extra=extra)
    except Exception:
        pass

    key = _history_key(peer_id)
    await rdb.rpush(key, f"{uid_prefix}{name}: {text}")
    await rdb.ltrim(key, -HISTORY_SIZE, -1)
    length = await rdb.llen(key)
    await logger.adebug("Valkey: записано сообщение", key=key, length=length)

    count = await rdb.incr(_counter_key(peer_id))
    if count >= SUMMARIZE_EVERY:
        await rdb.set(_counter_key(peer_id), 0)
        # Не сжимать чаще раза в час
        throttle_key = f"rin:chat:{peer_id}:compress_at"
        if not await rdb.exists(throttle_key):
            await rdb.set(throttle_key, "1", ex=3600)
            await _compress_chat_history(peer_id)


async def _compress_chat_history(peer_id: int):
    messages = await rdb.lrange(_history_key(peer_id), 0, -1)
    if not messages:
        return

    old_summary = await rdb.get(_summary_key(peer_id)) or ""
    prompt_parts = []
    if old_summary:
        prompt_parts.append(f"Предыдущее саммари:\n{old_summary}")
    prompt_parts.append("Новые сообщения:\n" + "\n".join(messages))

    try:
        async with ai_lock:
            result = await run_agent_streamed(history_summary_agent, "\n\n".join(prompt_parts))
        summary = result.final_output.strip().strip('"')
        await rdb.set(_summary_key(peer_id), summary)
        await logger.ainfo("История чата сжата", peer_id=peer_id, summary=summary)
    except Exception as e:
        await logger.awarn("Не удалось сжать историю", error=str(e))


async def get_context(peer_id: int) -> str:
    parts = []
    summary = await rdb.get(_summary_key(peer_id))
    if summary:
        parts.append(f"Краткое содержание предыдущего разговора:\n{summary}")
    messages = await rdb.lrange(_history_key(peer_id), -CONTEXT_SIZE, -1)
    if messages:
        parts.append("Последние сообщения:\n" + "\n".join(messages))
    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════
#                 СОБСТВЕННЫЙ ЛОР РИН (Valkey)
# ═══════════════════════════════════════════════════════════

RIN_SELF_KEY = "rin:self:state"
RIN_SELF_ARCHIVE_KEY = "rin:self:archive"
RIN_SELF_MAX = 15
RIN_ARCHIVE_MAX = 50


RIN_INITIAL_STATE = [
    "«Частота» доделана и выложена — полная история от первого лупа до финала, интерактивная крутилка приёмника, английский с русским. Проект закрыт, и мне с этим спокойно.",
    "Сейчас осознанно отдыхаю: не открываю проект, не переписываю сцены по пятому разу. Игра закончена — надо дать ей побыть законченной.",
    "Звуковой дизайн — то, чем горжусь. Статика, что нарастает почти незаметно, на плейтестах реально заставляла людей нервничать к концу.",
    "Идеи в столе есть (короткая «Статика»; новелла, где терапевт говорит только знаками препинания), но я за них не берусь — просто пусть лежат.",
    "Помогаю по мелочи, если просят — Ren'Py, звук, сценарий. Но свой большой проект держу на паузе, и мне так спокойно.",
]


async def init_rin_self_state():
    """Засеять начальное состояние Рин если Valkey пуст"""
    existing = await rdb.get(RIN_SELF_KEY)
    if not existing:
        await rdb.set(RIN_SELF_KEY, orjson.dumps(RIN_INITIAL_STATE).decode())
        await logger.ainfo("Начальное состояние Рин инициализировано")


async def get_rin_self_state() -> list[str]:
    """Получить текущее состояние/прогресс самой Рин"""
    raw = await rdb.get(RIN_SELF_KEY)
    if not raw:
        return []
    try:
        data = orjson.loads(raw)
        return data if isinstance(data, list) else []
    except (orjson.JSONDecodeError, TypeError):
        return []


async def update_rin_self_state(new_state: list[str]):
    """Перезаписать состояние Рин, вытесненные факты уходят в архив"""
    if len(new_state) > RIN_SELF_MAX:
        overflow = new_state[:-RIN_SELF_MAX]
        # Добавляем в архив
        raw_archive = await rdb.get(RIN_SELF_ARCHIVE_KEY)
        archive = orjson.loads(raw_archive) if raw_archive else []
        archive.extend(overflow)
        archive = archive[-RIN_ARCHIVE_MAX:]  # ротация архива
        await rdb.set(RIN_SELF_ARCHIVE_KEY, orjson.dumps(archive).decode())
        new_state = new_state[-RIN_SELF_MAX:]
    await rdb.set(RIN_SELF_KEY, orjson.dumps(new_state).decode())


async def get_rin_self_archive() -> list[str]:
    """Получить архив вытесненных состояний Рин"""
    raw = await rdb.get(RIN_SELF_ARCHIVE_KEY)
    if not raw:
        return []
    try:
        data = orjson.loads(raw)
        return data if isinstance(data, list) else []
    except (orjson.JSONDecodeError, TypeError):
        return []


async def refresh_rin_self_state(peer_id: int):
    """Обновить состояние Рин на основе последней истории чата"""
    from src.handlers.chat.agents import self_state_agent

    messages = await rdb.lrange(_history_key(peer_id), 0, -1)
    if not messages:
        return

    current = await get_rin_self_state()
    prompt_parts = []
    if current:
        prompt_parts.append("Твоё текущее состояние:\n" + "\n".join(f"- {s}" for s in current))
    else:
        prompt_parts.append("Твоё текущее состояние: пусто (только начинаешь вести записи).")
    prompt_parts.append("Последние сообщения из чата:\n" + "\n".join(messages))

    try:
        async with ai_lock:
            result = await run_agent_streamed(self_state_agent, "\n\n".join(prompt_parts))
        raw = result.final_output.strip()
        updated = orjson.loads(raw)
        if isinstance(updated, list) and updated:
            await update_rin_self_state([str(s) for s in updated])
            await logger.ainfo("Состояние Рин обновлено", count=len(updated))
    except Exception as e:
        await logger.awarn("Не удалось обновить состояние Рин", error=str(e))


# ═══════════════════════════════════════════════════════════
#                    LAST SEEN (Valkey)
# ═══════════════════════════════════════════════════════════

def _last_seen_key(user_id: int) -> str:
    return f"rin:user:{user_id}:last_seen"


async def update_last_seen(user_id: int):
    """Обновить дату последнего общения с пользователем"""
    today = datetime.date.today().isoformat()
    await rdb.set(_last_seen_key(user_id), today)


async def get_days_since(user_id: int) -> int | None:
    """Сколько дней прошло с последнего общения. None — если не видели раньше."""
    raw = await rdb.get(_last_seen_key(user_id))
    if not raw:
        return None
    try:
        last = datetime.date.fromisoformat(raw)
        return (datetime.date.today() - last).days
    except ValueError:
        return None


# ═══════════════════════════════════════════════════════════
#              АКТИВНОСТЬ РИН / РАЗРЫВ (Valkey)
# ═══════════════════════════════════════════════════════════

RIN_LAST_ACTIVE_KEY = "rin:last_active"


async def touch_rin_active():
    """Отметить, что Рин только что проявила активность в чате (написала сообщение)."""
    await rdb.set(RIN_LAST_ACTIVE_KEY, datetime.datetime.now().isoformat())


async def get_rin_gap_days() -> int | None:
    """Сколько дней Рин не появлялась в чате. None — если отметки ещё нет."""
    raw = await rdb.get(RIN_LAST_ACTIVE_KEY)
    if not raw:
        return None
    try:
        last = datetime.datetime.fromisoformat(raw)
        return (datetime.datetime.now() - last).days
    except ValueError:
        return None
