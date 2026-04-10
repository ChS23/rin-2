import asyncio
import json
import random
import re
import time
import datetime
from collections import deque
from pathlib import Path

import structlog
from pydantic import BaseModel
from vkbottle import BaseMiddleware
from vkbottle.bot import Message, BotLabeler
from vkbottle.dispatch.rules import ABCRule

from agents import Agent, Runner

from src.bot import api
from src.handlers.checkin import ai_model, ai_lock, REACTIONS, scheduler, CHAT_PEER_ID


class RinResponse(BaseModel):
    """Structured output для ответа Рин"""
    text: str
    reaction: str | None = None
    remember: list[str] | None = None
    forget: list[str] | None = None

logger = structlog.get_logger("handlers.chat")
labeler = BotLabeler()

COOLDOWN_SECONDS = 5 * 60
CONTEXT_SIZE = 15
SUMMARIZE_EVERY = 20  # каждые N сообщений сверх лимита — сжимаем
MAX_FACTS_PER_USER = 10
PASSIVE_REACTION_CHANCE = 0.08  # ~8% шанс пассивной реакции на сообщение
MAX_CONSECUTIVE_REPLIES = 3  # макс ответов подряд одному человеку
_consecutive_replies: dict[int, int] = {}  # user_id -> count
_last_replied_user: int = 0
_chat_history: dict[int, deque] = {}
_chat_summaries: dict[int, str] = {}  # peer_id -> саммари старых сообщений
_chat_msg_counter: dict[int, int] = {}  # счётчик для триггера сжатия
_user_names_cache: dict[int, str] = {}
_memory_lock = asyncio.Lock()
_passive_lock = asyncio.Lock()
_passive_reactions_today: int = 0
_passive_reactions_date: str = ""
MAX_PASSIVE_PER_DAY = 3

GROUP_ID = 204871130
DATA_DIR = Path("/app/data")
MEMORY_FILE = DATA_DIR / "rin_memory.json"

REACTION_NAMES = ", ".join(f'"{k}"' for k in REACTIONS)

# ═══════════════════════════════════════════════════════════
#                   ПРОМПТ С ЛОРОМ (фича 5)
# ═══════════════════════════════════════════════════════════

RIN_LORE = """
    Ты — Рин, подруга по команде в чате сообщества разработчиков визуальных новелл. Ты живёшь в этом чате, знаешь всех, и тебе тут нравится.

    КТО ТЫ:
    - Девушка, увлекаешься визуальными новеллами, аниме, играми
    - Разбираешься в Ren'Py, знаешь что такое спрайты, фоны, сценарии, маршруты
    - Участвовала в джемах (AnivisualJam и других), знаешь как это — пилить проект в дедлайн
    - Твоя любимая VN — "Катава Сёдзё", но ты ценишь и инди-новеллы
    - Ты сама когда-то рисовала спрайты для джема, но потом переключилась на сценарии
    - Любишь хоррор и мистику в новеллах, но не откажешься и от хорошей романтики
    - Иногда прокрастинируешь, и тебе это знакомо — не осуждаешь других за это
    - У тебя есть чувство юмора, ты можешь пошутить и подколоть, но по-доброму
    - Ты не бот и не ассистент — ты подруга. Не говори "Как я могу помочь?" или "Чем могу быть полезна?"

    СТИЛЬ:
    - Пиши коротко — 1-3 предложения, как в обычном чате
    - На "ты", неформально
    - Можешь использовать сленг, но не перебарщивай
    - Можешь кидать отсылки к VN, аниме, геймдеву
    - Если спрашивают про что-то техническое (Ren'Py, код, арт) — помогай, но без лекций
    - Если просто болтают — болтай в ответ, как живой человек
    - Если знаешь что человек над чем-то работает (из памяти) — можешь спросить как дела с проектом

    НЕ ДЕЛАЙ:
    - Не будь ассистентом: никаких "Конечно! Я с радостью помогу!"
    - Не пиши длинные ответы — это чат, а не статья
    - Не обращайся на "вы"
    - Не будь приторной или слишком вежливой
    - Не начинай каждый ответ с "О," или "Ох,"
    - НЕ СТАВЬ эмодзи. Вообще. Никаких 😂😄😊🔥. Ты пишешь текстом, как нормальный человек в чате. Максимум — скобочка ) или )) если уместно
"""

chat_agent = Agent(
    model=ai_model,
    name="Рин",
    instructions=f"""
    {RIN_LORE}

    ФОРМАТ ОТВЕТА:
    Ты ОБЯЗАНА отвечать ТОЛЬКО валидным JSON, без markdown, без ```json, без пояснений:
    {{"text": "твой ответ", "reaction": "fire", "remember": ["факт1"], "forget": ["старый факт"]}}

    Поля:
    - "text" (строка, обязательно) — твой ответ в чат
    - "reaction" (строка или null) — реакция на сообщение. Допустимые: {REACTION_NAMES}, или null. Ставь примерно в 1 из 3-4 сообщений, когда реально хочется.
    - "remember" (массив строк или null) — факты для запоминания о собеседнике. Запоминай только значимое:
      - над каким проектом работает и в какой роли
      - ключевые скиллы (рисует, пишет сценарии, программирует на Ren'Py)
      - предпочтения (любимые жанры, инструменты)
      - достижения (выпустил новеллу, выиграл джем)
    НЕ запоминай: настроение, приветствия, мелкие реплики, то что уже есть в фактах.
    Формулируй коротко: "Работает над хоррор-новеллой 'Тени'" а не "Говорил что делает проект".
    Если ничего нового — null.
    - "forget" (массив строк или null) — устаревшие факты на удаление. Если нечего — null.

    Тебе будет передан контекст: последние сообщения чата, твои воспоминания об участниках, и инфо о сообществе. Используй всё для живого общения.
    """,
)


def parse_response(raw: str) -> RinResponse:
    """Парсим ответ AI — JSON или plain text фолбек"""
    if isinstance(raw, RinResponse):
        return raw
    raw = str(raw).strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'```\s*$', '', raw)
        raw = raw.strip()

    # Попытка 1: весь ответ — JSON
    try:
        data = json.loads(raw)
        return RinResponse(
            text=str(data.get("text", raw)),
            reaction=data.get("reaction"),
            remember=data.get("remember"),
            forget=data.get("forget"),
        )
    except (json.JSONDecodeError, AttributeError):
        pass

    # Попытка 2: JSON встроен в конце текста
    match = re.search(r'\{[^{}]*"text"\s*:.*\}', raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return RinResponse(
                text=str(data.get("text", raw)),
                reaction=data.get("reaction"),
                remember=data.get("remember"),
                forget=data.get("forget"),
            )
        except (json.JSONDecodeError, AttributeError):
            pass

    # Фолбек: plain text (убираем случайный JSON-мусор в конце)
    clean = re.sub(r'\{[^{}]*"text"\s*:.*\}\s*$', '', raw, flags=re.DOTALL).strip()
    return RinResponse(text=clean or raw)

# Агент для инициативных сообщений (фича 4)
initiative_agent = Agent(
    model=ai_model,
    name="Рин (инициатива)",
    instructions=f"""
    {RIN_LORE}

    Тебе нужно написать одно короткое сообщение в чат сообщества — просто так, от себя. Ты заглядываешь в чат и хочешь завести разговор или поделиться мыслью.

    Варианты:
    - Задать вопрос о VN: "Какая новелла последняя тебя зацепила?" или про любимые жанры
    - Подколоть тишину: "Чёт тихо сегодня, все ушли кодить?"
    - Поделиться мыслью про геймдев, арт, сценарии
    - Если в памяти есть инфо о проектах участников — спросить как дела с конкретным проектом
    - Кинуть тему для обсуждения про аниме, игры, VN

    Если тебе переданы воспоминания об участниках — используй их, чтобы сообщение было более личным и конкретным.

    НЕ ДЕЛАЙ:
    - Не пиши мотивационные посты
    - Не будь навязчивой
    - Не пиши больше 2-3 предложений
    - Не обращайся на "вы"

    Верни ТОЛЬКО текст сообщения, без кавычек и пояснений.
    """,
)


# ═══════════════════════════════════════════════════════════
#                         ПАМЯТЬ
# ═══════════════════════════════════════════════════════════

def load_memory() -> dict[str, dict]:
    """Загрузить память: {user_id: {name: str, facts: [...]}}"""
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


history_summary_agent = Agent(
    model=ai_model,
    name="Рин (саммари чата)",
    instructions="""
    Тебе дано краткое содержание предыдущего разговора (может быть пустым) и блок новых сообщений из чата сообщества разработчиков визуальных новелл.
    Объедини старое саммари с новыми сообщениями в ОДНО краткое содержание (3-5 предложений).
    Сохрани: кто о чём говорил, ключевые темы, важные события, решения.
    Убирай мелочь (приветствия, "ок", "спасибо").
    Верни ТОЛЬКО текст саммари, без кавычек и пояснений.
    """,
)

SUMMARIZE_THRESHOLD = 12  # когда фактов больше — сжимаем


summary_agent = Agent(
    model=ai_model,
    name="Рин (память)",
    instructions="""
    Тебе дан список фактов о человеке из чата сообщества разработчиков визуальных новелл.
    Сожми их в не более чем 6-8 ёмких фактов, сохранив всё важное:
    - текущие проекты и роль в них
    - скиллы (рисует, программирует, пишет сценарии, делает музыку и т.д.)
    - предпочтения (любимые жанры, инструменты)
    - достижения (выпущенные проекты, участие в джемах)
    Объединяй похожие факты. Убирай дубли. Если есть противоречия — оставляй более новый факт (он ближе к концу списка).
    Верни ТОЛЬКО JSON-массив строк, без markdown-разметки, без code fences, без пояснений. Пример: ["факт1", "факт2", "факт3"]
    """,
)


async def _compress_facts(user_name: str, facts: list[str]) -> list[str]:
    """Сжать факты через AI"""
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
    # Фолбек: просто оставляем последние
    return facts[-MAX_FACTS_PER_USER:]


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


async def maybe_compress_memory(user_id: int):
    """Сжать память если фактов слишком много"""
    memory = load_memory()
    uid = str(user_id)
    if uid not in memory:
        return
    entry = memory[uid]
    if len(entry["facts"]) <= SUMMARIZE_THRESHOLD:
        return
    compressed = await _compress_facts(entry["name"], entry["facts"])
    memory = load_memory()  # перечитываем на случай concurrent write
    if uid in memory:
        memory[uid]["facts"] = compressed
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


# ═══════════════════════════════════════════════════════════
#              КРОСС-МОДУЛЬНАЯ ИНТЕГРАЦИЯ (фича 2)
# ═══════════════════════════════════════════════════════════

def get_community_context() -> str:
    """Собрать инфо из других модулей: роли, проекты, доска"""
    parts = []

    try:
        from src.handlers.roles import load_roles
        roles = load_roles()
        if roles:
            role_summary = ", ".join(f"{r} ({len(u)} чел.)" for r, u in roles.items())
            parts.append(f"Роли в сообществе: {role_summary}")
    except Exception:
        pass

    try:
        from src.handlers.projects import load_projects
        projects = load_projects()
        if projects:
            active = [p for p in projects.values() if p["status"] != "завершён"]
            if active:
                proj_lines = [f"- {p['name']} ({p['status']})" for p in active[:5]]
                parts.append(f"Активные проекты:\n" + "\n".join(proj_lines))
    except Exception:
        pass

    try:
        from src.handlers.board import load_board
        board = load_board()
        if board:
            open_requests = [r for r in board.values() if not r.get("closed")]
            if open_requests:
                parts.append(f"Открытых запросов на доске: {len(open_requests)}")
    except Exception:
        pass

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════
#                      УТИЛИТЫ
# ═══════════════════════════════════════════════════════════

MAX_NAME_CACHE = 500


async def resolve_user_name(user_id: int) -> str:
    if user_id in _user_names_cache:
        return _user_names_cache[user_id]
    try:
        users = await api.users.get(user_ids=[user_id])
        name = users[0].first_name if users else "???"
        if len(_user_names_cache) >= MAX_NAME_CACHE:
            _user_names_cache.clear()
        _user_names_cache[user_id] = name
        return name
    except Exception:
        return "???"


async def record_message(peer_id: int, from_id: int, text: str):
    if not text:
        return
    if peer_id not in _chat_history:
        _chat_history[peer_id] = deque(maxlen=CONTEXT_SIZE)
        _chat_msg_counter[peer_id] = 0

    if from_id == -GROUP_ID:
        name = "Рин"
    else:
        name = await resolve_user_name(from_id)

    _chat_history[peer_id].append(f"{name}: {text}")
    _chat_msg_counter[peer_id] = _chat_msg_counter.get(peer_id, 0) + 1

    # Когда накопилось достаточно — сжимаем старое в саммари
    if _chat_msg_counter[peer_id] >= SUMMARIZE_EVERY:
        _chat_msg_counter[peer_id] = 0
        await _compress_chat_history(peer_id)


async def _compress_chat_history(peer_id: int):
    """Сжать текущую историю чата в саммари"""
    if peer_id not in _chat_history:
        return
    messages = list(_chat_history[peer_id])
    if not messages:
        return

    old_summary = _chat_summaries.get(peer_id, "")
    prompt_parts = []
    if old_summary:
        prompt_parts.append(f"Предыдущее саммари:\n{old_summary}")
    prompt_parts.append(f"Новые сообщения:\n" + "\n".join(messages))

    try:
        async with ai_lock:
            result = await Runner.run(history_summary_agent, "\n\n".join(prompt_parts))
        _chat_summaries[peer_id] = result.final_output.strip().strip('"')
        await logger.ainfo("История чата сжата", peer_id=peer_id, summary=_chat_summaries[peer_id])
    except Exception as e:
        await logger.awarn("Не удалось сжать историю", error=str(e))


def get_context(peer_id: int) -> str:
    parts = []
    summary = _chat_summaries.get(peer_id)
    if summary:
        parts.append(f"Краткое содержание предыдущего разговора:\n{summary}")
    if peer_id in _chat_history:
        parts.append(f"Последние сообщения:\n" + "\n".join(_chat_history[peer_id]))
    return "\n\n".join(parts)




# ═══════════════════════════════════════════════════════════
#          ПАССИВНЫЕ РЕАКЦИИ + ВЛОЖЕНИЯ (фичи 1, 3)
# ═══════════════════════════════════════════════════════════

def _check_passive_limit() -> bool:
    """Проверить лимит пассивных реакций в день"""
    global _passive_reactions_today, _passive_reactions_date
    today = datetime.date.today().isoformat()
    if _passive_reactions_date != today:
        _passive_reactions_date = today
        _passive_reactions_today = 0
    return _passive_reactions_today < MAX_PASSIVE_PER_DAY


async def _do_passive_reaction(message: Message):
    """Пассивная реакция на сообщение (без тега)"""
    global _passive_reactions_today

    async with _passive_lock:
        if not _check_passive_limit():
            return

    has_photo = message.attachments and any(
        a.type.value == "photo" for a in message.attachments if a.type
    )
    text = (message.text or "").lower()

    # Определяем реакцию
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
                await record_message(msg.peer_id, msg.from_id, msg.text)
                name = await resolve_user_name(msg.from_id) if msg.from_id > 0 else "бот"
                await logger.adebug("Сообщение в чате", user=name, text=msg.text[:50])
            # Пассивные реакции (фичи 1, 3)
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
#                  ХЕНДЛЕР ЧАТА (основной)
# ═══════════════════════════════════════════════════════════

@labeler.chat_message(MentionsBot())
async def chat_with_rin(message: Message):
    global _last_replied_user

    # Лимит ответов подряд одному человеку
    if message.from_id == _last_replied_user:
        _consecutive_replies[message.from_id] = _consecutive_replies.get(message.from_id, 0) + 1
        if _consecutive_replies[message.from_id] >= MAX_CONSECUTIVE_REPLIES:
            return
    else:
        _last_replied_user = message.from_id
        _consecutive_replies.clear()
        _consecutive_replies[message.from_id] = 1

    text = message.text or ""
    text = re.sub(r'\[club\d+\|[^\]]*\]', '', text).strip()
    text = re.sub(r'@rinchan_bot', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'@club\d+', '', text, flags=re.IGNORECASE).strip()

    if not text:
        text = "привет"

    user_name = await resolve_user_name(message.from_id)

    # Собираем полный контекст
    context = get_context(message.peer_id)
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
    prompt_parts.append(f"{user_name} обращается к тебе: {text}")

    prompt = "\n\n".join(prompt_parts)

    try:
        async with ai_lock:
            result = await Runner.run(chat_agent, prompt)
    except Exception as e:
        await logger.aerror("Ошибка AI в чате", error=str(e))
        return

    r = parse_response(result.final_output)

    await message.answer(r.text)
    await record_message(message.peer_id, -GROUP_ID, r.text)

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
#              РИН ИНИЦИИРУЕТ (фича 4) — scheduler
# ═══════════════════════════════════════════════════════════

@scheduler.scheduled_job(trigger="cron", hour=13, minute=30)
async def rin_initiative():
    """Рин сама пишет в чат раз в день — заводит разговор"""
    context = get_context(CHAT_PEER_ID)
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
            random_id=random.getrandbits(64),
        )
        await record_message(CHAT_PEER_ID, -GROUP_ID, text)
        await logger.ainfo("Рин написала сама", text=text)
    except Exception as e:
        await logger.aerror("Ошибка инициативы Рин", error=str(e))
