import asyncio
import datetime
import random
import re
from pathlib import Path

import aiohttp
import orjson

import structlog
from vkbottle import BaseMiddleware
from vkbottle.bot import Message, BotLabeler
from vkbottle.dispatch.rules import ABCRule

from agents import Runner

from src.bot import api, rdb
from src.handlers.checkin import ai_lock, REACTIONS, scheduler, CHAT_PEER_ID
from src.handlers.chat.agents import chat_agent, initiative_agent
from src.handlers.chat.tools import _file_registry
from src.handlers.chat.memory import (
    record_message, get_context,
    remember_facts, forget_facts, maybe_compress_memory,
    get_user_memory, get_all_memory_summary,
    get_rin_self_state, refresh_rin_self_state,
    update_last_seen, get_days_since,
)
from src.handlers.chat.utils import (
    REPLY_CONTEXT_PROMPT, HARD_REPLY_CAP,
    get_reply_count, record_reply, mark_done, resolve_user_name,
    parse_response, get_community_context,
)

logger = structlog.get_logger("chat.handler")
labeler = BotLabeler()

GROUP_ID = 204871130


async def _upload_doc(peer_id: int, file_path: str) -> str | None:
    """Загрузить файл как документ VK и вернуть attachment string."""
    try:
        upload_server = await api.docs.get_messages_upload_server(peer_id=peer_id, type="doc")
        async with aiohttp.ClientSession() as session:
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=Path(file_path).name, content_type="application/octet-stream")
                async with session.post(upload_server.upload_url, data=data) as resp:
                    result = await resp.json(content_type=None)
        saved = await api.docs.save(file=result["file"], title=Path(file_path).name)
        doc = saved.doc
        return f"doc{doc.owner_id}_{doc.id}"
    except Exception as e:
        await logger.awarn("Не удалось загрузить файл", error=str(e), path=file_path)
        return None
PASSIVE_REACTION_CHANCE = 0.08
MAX_PASSIVE_PER_DAY = 3
_seen_messages: set[int] = set()
_seen_max = 200

PASSIVE_COUNT_KEY = "rin:passive_reactions:{date}"


# ═══════════════════════════════════════════════════════════
#                    ПАССИВНЫЕ РЕАКЦИИ
# ═══════════════════════════════════════════════════════════

async def _check_passive_limit() -> bool:
    key = PASSIVE_COUNT_KEY.format(date=datetime.date.today().isoformat())
    count = await rdb.get(key)
    return int(count or 0) < MAX_PASSIVE_PER_DAY


async def _incr_passive_count():
    key = PASSIVE_COUNT_KEY.format(date=datetime.date.today().isoformat())
    await rdb.incr(key)
    await rdb.expire(key, 86400)


async def _do_passive_reaction(message: Message):

    has_photo = message.attachments and any(
        a.type.value == "photo" for a in message.attachments if a.type
    )
    text = (message.text or "").lower()

    reaction_id = None
    if has_photo:
        reaction_id = REACTIONS[random.choice(["fire", "heart", "like"])]
    elif any(w in text for w in ["готово", "сделал", "сделала", "закончил", "закончила", "дописал", "дорисовал"]):
        reaction_id = REACTIONS[random.choice(["fire", "party", "like"])]
    elif any(w in text for w in ["помогите", "не работает", "баг", "сломал"]):
        reaction_id = REACTIONS[random.choice(["cry", "pray"])]
    elif random.random() < PASSIVE_REACTION_CHANCE:
        reaction_id = REACTIONS[random.choice(["like", "fire", "smile"])]

    if reaction_id:
        try:
            await api.request("messages.sendReaction", {
                "peer_id": message.peer_id,
                "cmid": message.conversation_message_id,
                "reaction_id": reaction_id,
            })
            await _incr_passive_count()
            await logger.ainfo("Пассивная реакция",
                user_id=message.from_id,
                reaction_id=reaction_id,
                has_photo=has_photo,
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════
#                    MIDDLEWARE + RULES
# ═══════════════════════════════════════════════════════════

class ChatHistoryMiddleware(BaseMiddleware[Message]):
    async def pre(self):
        msg = self.event
        if msg.peer_id > 2000000000:
            # Дедупликация — vkbottle может обработать одно сообщение дважды
            cmid = msg.conversation_message_id
            if cmid in _seen_messages:
                return
            _seen_messages.add(cmid)
            if len(_seen_messages) > _seen_max:
                _seen_messages.clear()

            if msg.text:
                await record_message(msg.peer_id, msg.from_id, msg.text, resolve_user_name)
                name = await resolve_user_name(msg.from_id) if msg.from_id > 0 else "бот"
                await logger.adebug("Сообщение в чате", user=name, text=msg.text[:50])
            if msg.from_id != -GROUP_ID and await _check_passive_limit():
                await _do_passive_reaction(msg)


labeler.message_view.register_middleware(ChatHistoryMiddleware)


class MentionsBot(ABCRule[Message]):
    async def check(self, event: Message) -> bool:
        if not event.peer_id > 2000000000:
            return False
        if event.reply_message and event.reply_message.from_id == -GROUP_ID:
            return True
        if event.fwd_messages:
            for fwd in event.fwd_messages:
                if fwd.from_id == -GROUP_ID:
                    return True
        mention_patterns = [
            f"[club{GROUP_ID}|",
            "@rinchan_bot",
            f"@club{GROUP_ID}",
        ]
        text_lower = (event.text or "").lower()
        return any(p.lower() in text_lower for p in mention_patterns)


# ═══════════════════════════════════════════════════════════
#                       ХЕНДЛЕР ЧАТА
# ═══════════════════════════════════════════════════════════

@labeler.chat_message(MentionsBot())
async def chat_with_rin(message: Message):
    reply_count = await get_reply_count(message.from_id)
    if reply_count >= HARD_REPLY_CAP:
        return

    text = message.text or ""
    text = re.sub(r'\[club\d+\|[^\]]*\]', '', text).strip()
    text = re.sub(r'@rinchan_bot', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'@club\d+', '', text, flags=re.IGNORECASE).strip()

    if not text:
        text = "привет"

    user_name = await resolve_user_name(message.from_id)

    now = datetime.datetime.now()
    context = await get_context(message.peer_id)
    user_facts = get_user_memory(message.from_id)
    all_memory = get_all_memory_summary()
    community = get_community_context()
    self_state = await get_rin_self_state()
    days_since = await get_days_since(message.from_id)

    prompt_parts = [f"Сейчас: {now.strftime('%d.%m.%Y %H:%M, %A')}"]
    if self_state:
        prompt_parts.append("Твой текущий прогресс и состояние:\n" + "\n".join(f"- {s}" for s in self_state))
    if user_facts:
        user_ctx = f"Что ты помнишь о {user_name}:\n" + "\n".join(f"- {f}" for f in user_facts)
        if days_since is not None and days_since >= 7:
            user_ctx += f"\n(Последний раз общались {days_since} дней назад)"
        prompt_parts.append(user_ctx)
    elif days_since is None:
        # нет ни фактов, ни last_seen — точно первый раз
        prompt_parts.append(f"({user_name} впервые пишет тебе)")
    elif days_since >= 7:
        prompt_parts.append(f"({user_name} не заходил {days_since} дней)")
    if all_memory:
        prompt_parts.append(f"Что ты помнишь о других участниках:\n{all_memory}")
    if community:
        prompt_parts.append(f"Инфо о сообществе:\n{community}")
    if context:
        prompt_parts.append(context)
    prompt_parts.append(REPLY_CONTEXT_PROMPT.format(reply_num=reply_count + 1))
    prompt_parts.append(f"{user_name} обращается к тебе: {text}")

    prompt = "\n\n".join(prompt_parts)

    current_task = asyncio.current_task()
    task_id = id(current_task) if current_task else 0

    try:
        async with ai_lock:
            result = await asyncio.wait_for(Runner.run(chat_agent, prompt), timeout=120)
    except asyncio.TimeoutError:
        await logger.aerror("Таймаут AI в чате")
        return
    except Exception as e:
        await logger.aerror("Ошибка AI в чате", error=str(e))
        return

    file_path = _file_registry.pop(task_id, None)
    attachment = await _upload_doc(message.peer_id, file_path) if file_path else None

    await record_reply(message.from_id)
    r = parse_response(result.final_output)

    await api.messages.send(
        peer_id=message.peer_id,
        message=r.text,
        attachment=attachment,
        forward=orjson.dumps({
            "peer_id": message.peer_id,
            "conversation_message_ids": [message.conversation_message_id],
            "is_reply": 1,
        }).decode(),
        random_id=random.getrandbits(31),
    )
    await record_message(message.peer_id, -GROUP_ID, r.text, resolve_user_name)

    if r.reaction and r.reaction in REACTIONS:
        try:
            await api.request("messages.sendReaction", {
                "peer_id": message.peer_id,
                "cmid": message.conversation_message_id,
                "reaction_id": REACTIONS[r.reaction],
            })
        except Exception as e:
            await logger.awarn("Не удалось поставить реакцию", error=str(e))

    if r.forget:
        await forget_facts(message.from_id, r.forget)
    if r.remember:
        await remember_facts(message.from_id, user_name, r.remember)
        await maybe_compress_memory(message.from_id)

    if r.done:
        await mark_done(message.from_id)

    await update_last_seen(message.from_id)

    await logger.ainfo("Рин ответила",
        user_id=message.from_id,
        user_name=user_name,
        input=text,
        reaction=r.reaction,
        remembered=r.remember or None,
        done=r.done,
        reply_num=reply_count + 1,
        attachment=attachment,
    )


# ═══════════════════════════════════════════════════════════
#                    РИН ИНИЦИИРУЕТ
# ═══════════════════════════════════════════════════════════

@scheduler.scheduled_job(trigger="cron", hour=4, minute=0)
async def rin_self_state_update():
    """Обновляет собственное состояние Рин на основе истории чата за день"""
    try:
        await refresh_rin_self_state(CHAT_PEER_ID)
    except Exception as e:
        await logger.aerror("Ошибка обновления состояния Рин", error=str(e))


@scheduler.scheduled_job(trigger="cron", hour=13, minute=30)
async def rin_initiative():
    context = await get_context(CHAT_PEER_ID)
    all_memory = get_all_memory_summary()
    self_state = await get_rin_self_state()

    prompt_parts = [f"Текущий день: {datetime.datetime.now().strftime('%d.%m.%Y %A')}"]
    if self_state:
        prompt_parts.append("Твой текущий прогресс и состояние:\n" + "\n".join(f"- {s}" for s in self_state))
    if all_memory:
        prompt_parts.append(f"Что ты помнишь об участниках:\n{all_memory}")
    if context:
        prompt_parts.append(context)
    prompt_parts.append("Напиши что-нибудь в чат от себя.")

    prompt = "\n\n".join(prompt_parts)

    try:
        async with ai_lock:
            result = await Runner.run(initiative_agent, prompt)

        text = result.final_output.strip().strip('"')

        await api.messages.send(
            peer_ids=[CHAT_PEER_ID],
            message=text,
            random_id=random.getrandbits(31),
        )
        await record_message(CHAT_PEER_ID, -GROUP_ID, text, resolve_user_name)
        await logger.ainfo("Рин написала сама", text=text)
    except Exception as e:
        await logger.aerror("Ошибка инициативы Рин", error=str(e))
