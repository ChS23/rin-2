import datetime
import random
import re

import structlog
from vkbottle import BaseMiddleware
from vkbottle.bot import Message, BotLabeler
from vkbottle.dispatch.rules import ABCRule

from agents import Runner

from src.bot import api
from src.handlers.checkin import ai_lock, REACTIONS, scheduler, CHAT_PEER_ID
from src.handlers.chat.agents import chat_agent, initiative_agent
from src.handlers.chat.memory import (
    record_message, get_context,
    remember_facts, forget_facts, maybe_compress_memory,
    get_user_memory, get_all_memory_summary,
)
from src.handlers.chat.utils import (
    REPLY_LIMIT_PROMPT,
    get_remaining_replies, record_reply, resolve_user_name,
    parse_response, get_community_context,
)

logger = structlog.get_logger("chat.handler")
labeler = BotLabeler()

GROUP_ID = 204871130
PASSIVE_REACTION_CHANCE = 0.08
MAX_PASSIVE_PER_DAY = 3
_passive_reactions_today: int = 0
_passive_reactions_date: str = ""


# ═══════════════════════════════════════════════════════════
#                    ПАССИВНЫЕ РЕАКЦИИ
# ═══════════════════════════════════════════════════════════

def _check_passive_limit() -> bool:
    global _passive_reactions_today, _passive_reactions_date
    today = datetime.date.today().isoformat()
    if _passive_reactions_date != today:
        _passive_reactions_date = today
        _passive_reactions_today = 0
    return _passive_reactions_today < MAX_PASSIVE_PER_DAY


async def _do_passive_reaction(message: Message):
    global _passive_reactions_today

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
            _passive_reactions_today += 1
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
            if msg.text:
                await record_message(msg.peer_id, msg.from_id, msg.text, resolve_user_name)
                name = await resolve_user_name(msg.from_id) if msg.from_id > 0 else "бот"
                await logger.adebug("Сообщение в чате", user=name, text=msg.text[:50])
            if msg.from_id != -GROUP_ID and _check_passive_limit():
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
    remaining = get_remaining_replies(message.from_id)
    if remaining <= 0:
        return

    text = message.text or ""
    text = re.sub(r'\[club\d+\|[^\]]*\]', '', text).strip()
    text = re.sub(r'@rinchan_bot', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'@club\d+', '', text, flags=re.IGNORECASE).strip()

    if not text:
        text = "привет"

    user_name = await resolve_user_name(message.from_id)

    context = await get_context(message.peer_id)
    user_facts = get_user_memory(message.from_id)
    all_memory = get_all_memory_summary()
    community = get_community_context()

    prompt_parts = []
    if user_facts:
        prompt_parts.append(f"Что ты помнишь о {user_name}:\n" + "\n".join(f"- {f}" for f in user_facts))
    if all_memory:
        prompt_parts.append(f"Что ты помнишь о других участниках:\n{all_memory}")
    if community:
        prompt_parts.append(f"Инфо о сообществе:\n{community}")
    if context:
        prompt_parts.append(context)
    prompt_parts.append(REPLY_LIMIT_PROMPT.format(remaining=remaining))
    prompt_parts.append(f"{user_name} обращается к тебе: {text}")

    prompt = "\n\n".join(prompt_parts)

    try:
        async with ai_lock:
            result = await Runner.run(chat_agent, prompt)
    except Exception as e:
        await logger.aerror("Ошибка AI в чате", error=str(e))
        return

    record_reply(message.from_id)
    r = parse_response(result.final_output)

    await message.answer(r.text)
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

    await logger.ainfo("Рин ответила",
        user_id=message.from_id,
        user_name=user_name,
        input=text,
        reaction=r.reaction,
        remembered=r.remember or None,
    )


# ═══════════════════════════════════════════════════════════
#                    РИН ИНИЦИИРУЕТ
# ═══════════════════════════════════════════════════════════

@scheduler.scheduled_job(trigger="cron", hour=13, minute=30)
async def rin_initiative():
    context = await get_context(CHAT_PEER_ID)
    all_memory = get_all_memory_summary()

    prompt_parts = [f"Текущий день: {datetime.datetime.now().strftime('%d.%m.%Y %A')}"]
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
