import asyncio
import datetime

import structlog
from agents import Runner

from src.bot import rdb
from src.handlers.checkin import ai_lock, scheduler, CHAT_PEER_ID
from src.handlers.creative.agent import creative_agent
from src.handlers.chat.memory import (
    get_rin_self_state, update_rin_self_state,
)

logger = structlog.get_logger("creative.handler")

LAST_MSG_KEY = "rin:chat:{peer_id}:last_msg_ts"
CREATIVE_COOLDOWN_KEY = "rin:creative:last_run"
MIN_QUIET_MINUTES = 60       # минимум тишины в чате
CREATIVE_COOLDOWN_HOURS = 3  # минимум между сессиями


async def _chat_is_quiet(peer_id: int) -> bool:
    """Проверить что чат тихий уже MIN_QUIET_MINUTES минут."""
    last_ts = await rdb.get(LAST_MSG_KEY.format(peer_id=peer_id))
    if not last_ts:
        return True
    try:
        last = datetime.datetime.fromisoformat(last_ts)
        delta = datetime.datetime.now() - last
        return delta.total_seconds() > MIN_QUIET_MINUTES * 60
    except (ValueError, TypeError):
        return True


async def _can_run() -> bool:
    """Проверить кулдаун между сессиями."""
    last_run = await rdb.get(CREATIVE_COOLDOWN_KEY)
    if not last_run:
        return True
    try:
        last = datetime.datetime.fromisoformat(last_run)
        delta = datetime.datetime.now() - last
        return delta.total_seconds() > CREATIVE_COOLDOWN_HOURS * 3600
    except (ValueError, TypeError):
        return True


async def run_creative_session():
    """Запустить одну creative сессию — Рин работает над Частотой."""
    if not await _can_run():
        await logger.ainfo("Creative: пропуск — кулдаун не прошёл")
        return

    if not await _chat_is_quiet(CHAT_PEER_ID):
        await logger.ainfo("Creative: пропуск — чат активен")
        return

    self_state = await get_rin_self_state()

    prompt_parts = [
        f"Дата: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}",
    ]
    if self_state:
        prompt_parts.append(
            "Твоё текущее состояние:\n" + "\n".join(f"- {s}" for s in self_state)
        )
    prompt_parts.append("Начни рабочую сессию над Частотой.")

    prompt = "\n\n".join(prompt_parts)

    await logger.ainfo("Creative: начинаю сессию")

    try:
        async with ai_lock:
            result = await asyncio.wait_for(
                Runner.run(creative_agent, prompt), timeout=600
            )

        await rdb.set(
            CREATIVE_COOLDOWN_KEY,
            datetime.datetime.now().isoformat(),
            ex=CREATIVE_COOLDOWN_HOURS * 3600 + 60,
        )

        output = result.final_output or ""

        # Логируем использованные инструменты
        tool_names = []
        for item in result.raw_responses:
            if hasattr(item, "output") and hasattr(item.output, "tool_calls"):
                for tc in item.output.tool_calls:
                    if hasattr(tc, "function"):
                        tool_names.append(tc.function.name)

        # Обновляем self_state если агент что-то сделал
        if output:
            current = await get_rin_self_state()
            summary = output[:200].strip()
            if summary:
                updated = current + [f"[creative] {summary}"]
                if len(updated) > 15:
                    updated = updated[-15:]
                await update_rin_self_state(updated)

        await logger.ainfo(
            "Creative: сессия завершена",
            output=output[:500],
            tools_used=tool_names[:20] if tool_names else None,
        )

    except asyncio.TimeoutError:
        await logger.aerror("Creative: таймаут (600с)")
    except Exception as e:
        await logger.aerror("Creative: ошибка", error=str(e), exc_info=True)


# Расписание: ночью в 1:00 и 3:00 (когда Рин по лору работает)
@scheduler.scheduled_job(trigger="cron", hour=1, minute=0)
async def creative_session_night_1():
    await run_creative_session()


@scheduler.scheduled_job(trigger="cron", hour=3, minute=0)
async def creative_session_night_2():
    await run_creative_session()
