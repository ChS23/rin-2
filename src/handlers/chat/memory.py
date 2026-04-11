import asyncio
import json
import os
from pathlib import Path

import redis.asyncio as aioredis
import structlog
from agents import Runner

from src.handlers.checkin import ai_lock
from src.handlers.chat.agents import summary_agent, history_summary_agent

logger = structlog.get_logger("chat.memory")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
rdb = aioredis.from_url(REDIS_URL, decode_responses=True)

DATA_DIR = Path("/app/data")
MEMORY_FILE = DATA_DIR / "rin_memory.json"
CONTEXT_SIZE = 15
SUMMARIZE_EVERY = 20
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
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_memory(memory: dict[str, dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)


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


async def _compress_facts(user_name: str, facts: list[str]) -> list[str]:
    try:
        prompt = f"Факты о {user_name}:\n" + "\n".join(f"- {f}" for f in facts)
        async with ai_lock:
            result = await Runner.run(summary_agent, prompt)
        raw = result.final_output.strip()
        compressed = json.loads(raw)
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


async def record_message(peer_id: int, from_id: int, text: str, resolve_name):
    if not text:
        return

    if from_id == -GROUP_ID:
        name = "Рин"
    else:
        name = await resolve_name(from_id)

    key = _history_key(peer_id)
    await rdb.rpush(key, f"{name}: {text}")
    await rdb.ltrim(key, -CONTEXT_SIZE, -1)
    length = await rdb.llen(key)
    await logger.adebug("Valkey: записано сообщение", key=key, length=length)

    count = await rdb.incr(_counter_key(peer_id))
    if count >= SUMMARIZE_EVERY:
        await rdb.set(_counter_key(peer_id), 0)
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
            result = await Runner.run(history_summary_agent, "\n\n".join(prompt_parts))
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
    messages = await rdb.lrange(_history_key(peer_id), 0, -1)
    if messages:
        parts.append("Последние сообщения:\n" + "\n".join(messages))
    return "\n\n".join(parts)
