import asyncio
import datetime
import random

import structlog
from agents import Runner, RunConfig, RunHooks, Agent, Tool

from vkbottle.bot import Message, BotLabeler

from src.bot import api, rdb
from src.handlers.checkin import ai_lock, scheduler, CHAT_PEER_ID
from src.handlers.creative.agent import creative_agent
from src.handlers.chat.memory import (
    get_rin_self_state, update_rin_self_state, record_message,
)
from src.handlers.chat.utils import resolve_user_name, parse_response

logger = structlog.get_logger("creative.handler")
labeler = BotLabeler()

ADMIN_ID = 326129427


class CreativeLoggingHooks(RunHooks):
    async def on_agent_start(self, context, agent: Agent, **kwargs):
        await logger.ainfo("Creative: агент запущен", agent=agent.name)

    async def on_tool_start(self, context, agent: Agent, tool: Tool, **kwargs):
        await logger.ainfo("Creative: вызов инструмента", agent=agent.name, tool=tool.name)

    async def on_tool_end(self, context, agent: Agent, tool: Tool, result: str, **kwargs):
        short = (result or "")[:200]
        await logger.ainfo("Creative: инструмент завершён", tool=tool.name, result=short)

    async def on_agent_end(self, context, agent: Agent, output, **kwargs):
        short = str(output or "")[:200]
        await logger.ainfo("Creative: агент завершён", agent=agent.name, output=short)


_hooks = CreativeLoggingHooks()
_run_config = RunConfig(tracing_disabled=True)
MAX_TURNS = 50

LAST_MSG_KEY = "rin:chat:{peer_id}:last_msg_ts"
CREATIVE_COOLDOWN_KEY = "rin:creative:last_run"
MIN_QUIET_MINUTES = 60       # минимум тишины в чате
CREATIVE_COOLDOWN_HOURS = 3  # минимум между сессиями


def _parse_valkey_ts(raw) -> datetime.datetime | None:
    """Распарсить timestamp из Valkey (bytes или str)."""
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw if isinstance(raw, str) else raw.decode())
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


async def _chat_is_quiet(peer_id: int) -> bool:
    """Проверить что чат тихий уже MIN_QUIET_MINUTES минут."""
    last = _parse_valkey_ts(await rdb.get(LAST_MSG_KEY.format(peer_id=peer_id)))
    if not last:
        return True
    return (datetime.datetime.now() - last).total_seconds() > MIN_QUIET_MINUTES * 60


async def _can_run() -> bool:
    """Проверить кулдаун между сессиями."""
    last = _parse_valkey_ts(await rdb.get(CREATIVE_COOLDOWN_KEY))
    if not last:
        return True
    return (datetime.datetime.now() - last).total_seconds() > CREATIVE_COOLDOWN_HOURS * 3600


async def _build_prompt(extra: str = "") -> str:
    """Собрать промпт для creative agent."""
    self_state = await get_rin_self_state()
    parts = [f"Дата: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}"]
    if self_state:
        parts.append("Твоё текущее состояние:\n" + "\n".join(f"- {s}" for s in self_state))
    parts.append(extra or "Начни рабочую сессию над Частотой.")
    return "\n\n".join(parts)


GROUP_ID = 204871130


async def _post_to_chat(raw_output: str):
    """Перефразировать результат через основного агента Рин и отправить в чат."""
    try:
        # Lazy import чтобы избежать circular import при старте
        from src.handlers.chat.agents import chat_agent
        from src.handlers.chat.tools import PENDING_FILE_KEY

        prompt = f"Ты только что поработала над Частотой. Расскажи в чат коротко что сделала (1-3 предложения, без списков). Вот технический отчёт:\n{raw_output[:500]}"
        async with ai_lock:
            await rdb.delete(PENDING_FILE_KEY)
            result = await asyncio.wait_for(
                Runner.run(chat_agent, prompt),
                timeout=60,
            )
            await rdb.delete(PENDING_FILE_KEY)  # Убираем если chat_agent что-то прикрепил
        r = parse_response(result.final_output)
        if not r.text:
            return
        await api.messages.send(
            peer_ids=[CHAT_PEER_ID],
            message=r.text,
            random_id=random.getrandbits(31),
        )
        await record_message(CHAT_PEER_ID, -GROUP_ID, r.text, resolve_user_name)
    except Exception as e:
        await logger.awarn("Creative: не удалось отправить в чат", error=str(e))


async def _run_and_save(prompt: str, label: str, post_result: bool = True):
    """Запустить creative agent и сохранить результат в self_state."""
    await logger.ainfo(f"Creative: {label}")
    try:
        async with ai_lock:
            result = await asyncio.wait_for(
                Runner.run(creative_agent, prompt, run_config=_run_config, hooks=_hooks, max_turns=MAX_TURNS),
                timeout=900,
            )

        output = result.final_output or ""
        if output:
            current = await get_rin_self_state()
            summary = output[:200].strip()
            if summary:
                updated = current + [f"[creative] {summary}"]
                if len(updated) > 15:
                    updated = updated[-15:]
                await update_rin_self_state(updated)

            # Отправляем итог в чат (полный output, chat_agent сам сократит)
            if post_result:
                await _post_to_chat(output)

        await logger.ainfo("Creative: сессия завершена", output=output[:500])
    except asyncio.TimeoutError:
        await logger.aerror("Creative: таймаут (900с)")
    except Exception as e:
        await logger.aerror("Creative: ошибка", error=str(e), exc_info=True)


async def run_creative_session():
    """Запустить creative сессию с проверкой тишины и кулдауна."""
    if not await _can_run():
        await logger.ainfo("Creative: пропуск — кулдаун не прошёл")
        return
    if not await _chat_is_quiet(CHAT_PEER_ID):
        await logger.ainfo("Creative: пропуск — чат активен")
        return

    prompt = await _build_prompt()
    # Ставим cooldown ДО запуска — иначе параллельный scheduled job может стартовать пока идёт _post_to_chat
    await rdb.set(CREATIVE_COOLDOWN_KEY, datetime.datetime.now().isoformat(), ex=CREATIVE_COOLDOWN_HOURS * 3600 + 60)
    await _run_and_save(prompt, "начинаю сессию")


TRIGGER_KEY = "rin:creative:trigger"


async def run_forced_session(task: str = ""):
    """Принудительный запуск — без проверки тишины и кулдауна."""
    extra = f"Задача из чата: {task}" if task and task != "1" else ""
    prompt = await _build_prompt(extra)
    await _run_and_save(prompt, f"принудительный запуск ({task[:50]})" if task else "принудительный запуск")


# Проверка триггера каждые 30 секунд (coalesce + misfire подавляют спам)
@scheduler.scheduled_job(
    trigger="interval", seconds=30,
    max_instances=1, coalesce=True, misfire_grace_time=60,
)
async def check_creative_trigger():
    raw = await rdb.getdel(TRIGGER_KEY)
    if raw:
        task = raw.decode() if isinstance(raw, bytes) else str(raw)
        await run_forced_session(task)


# Расписание: ночью в 1:00 и 3:00 (когда Рин по лору работает)
@scheduler.scheduled_job(trigger="cron", hour=1, minute=0)
async def creative_session_night_1():
    await run_creative_session()


@scheduler.scheduled_job(trigger="cron", hour=3, minute=0)
async def creative_session_night_2():
    await run_creative_session()


# ═══════════════════════════════════════════════════════════
#              ЛС КОМАНДЫ ДЛЯ ТЕСТИРОВАНИЯ (admin only)
# ═══════════════════════════════════════════════════════════

@labeler.private_message(text="/creative run")
async def dm_creative_run(message: Message):
    if message.from_id != ADMIN_ID:
        return
    await message.answer("Запускаю creative сессию...")
    await run_forced_session()
    await message.answer("Сессия завершена.")


@labeler.private_message(text="/creative run <task>")
async def dm_creative_run_task(message: Message, task: str):
    if message.from_id != ADMIN_ID:
        return
    await message.answer(f"Запускаю: {task}")
    await run_forced_session(task)
    await message.answer("Сессия завершена.")


@labeler.private_message(text="/creative web")
async def dm_creative_web(message: Message):
    if message.from_id != ADMIN_ID:
        return
    await message.answer("Собираю веб-билд...")
    from src.handlers.creative.tools import renpy_web_build
    result = await renpy_web_build()
    await message.answer(result)


@labeler.private_message(text="/creative lint")
async def dm_creative_lint(message: Message):
    if message.from_id != ADMIN_ID:
        return
    from src.handlers.creative.tools import renpy_lint
    result = await renpy_lint()
    await message.answer(result[:4000])


@labeler.private_message(text="/creative roadmap")
async def dm_creative_roadmap(message: Message):
    if message.from_id != ADMIN_ID:
        return
    from src.handlers.creative.tools import read_roadmap
    result = read_roadmap()
    await message.answer(result[:4000])


@labeler.private_message(text="/creative files")
async def dm_creative_files(message: Message):
    if message.from_id != ADMIN_ID:
        return
    from src.handlers.creative.tools import list_scripts
    result = list_scripts()
    await message.answer(result[:4000])
