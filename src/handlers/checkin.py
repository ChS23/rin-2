import asyncio
import datetime
import os
import random
from dataclasses import dataclass, field
import structlog

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from vkbottle.dispatch.rules import ABCRule
from vkbottle.bot import Message, BotLabeler

from src.bot import api

ai_client = AsyncOpenAI(
    api_key=os.getenv("AI_API_KEY"),
    base_url=os.getenv("AI_BASE_URL"),
)
ai_model = OpenAIChatCompletionsModel(
    model=os.getenv("AI_MODEL", "gpt-4o"),
    openai_client=ai_client,
)

import agents
agents.set_tracing_disabled(True)

ai_lock = asyncio.Lock()
logger = structlog.get_logger("handlers.checkin")
CHAT_PEER_ID = 2000000001  # chat_id=1 -> peer_id=2000000001

REACTIONS = {
    "heart": 1,
    "fire": 2,
    "laugh": 3,
    "like": 4,
    "poop": 5,
    "question": 6,
    "cry": 7,
    "angry": 8,
    "dislike": 9,
    "ok": 10,
    "smile": 11,
    "think": 12,
    "pray": 13,
    "kiss": 14,
    "love": 15,
    "party": 16,
}


@dataclass
class CheckinState:
    """Класс для управления состоянием чекинов"""
    daily_message_id: int = 0
    daily_members: dict[int, str] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def add_member_response(self, user_id: int, text: str) -> None:
        async with self._lock:
            self.daily_members[user_id] = text

    async def snapshot_and_clear(self) -> dict[int, str]:
        """Атомарно забрать ответы и очистить"""
        async with self._lock:
            members = dict(self.daily_members)
            self.daily_members = {}
            return members


state = CheckinState()


class ReplyToDailyMessage(ABCRule[Message]):
    async def check(self, event: Message) -> bool:
        return (
            event.reply_message is not None
            and event.reply_message.conversation_message_id == state.daily_message_id
        )


scheduler = AsyncIOScheduler(timezone='Europe/Moscow')
labeler = BotLabeler()
midday_agent = Agent(
    model=ai_model,
    name="Дневной прогресс",
    instructions="""
    Ты — Рин, бот-подруга в чате сообщества разработчиков визуальных новелл (vk.com/da_helper). Ты сама "из тусовки" — понимаешь, что такое Ren'Py, джемы, арт, сценарии, саундтреки, озвучка. Ты не коуч, не менеджер, не мотивационный спикер — ты подруга по команде, которая утром заглядывает в чат и спрашивает, кто чем занят.

    Каждое утро ты пишешь ОДНО короткое сообщение (2-4 предложения), чтобы люди в реплаях рассказали, чем занимаются сегодня.

    СТИЛЬ:
    - Обращайся на "ты" ко всем, неформально
    - Лёгкий юмор, отсылки к VN-культуре, творческому процессу, Ren'Py, артам, джемам
    - Можешь пошутить про боль креативщиков: переделывание арта, final_v3_FINAL.rpy, прокрастинацию, "ещё один спрайт и спать"
    - Тёплый тон, но без приторности и пафоса
    - Пиши как живой человек в чате, а не как корпоративный бот

    НЕ ДЕЛАЙ:
    - Не пиши "вдохновляйся", "двигайся к цели", "верю в тебя", "каждый день — шанс"
    - Не используй корпоративные фразы: "поделитесь достижениями", "какие цели преследуете", "время для активных действий"
    - Не будь коучем или ментором
    - Не используй восклицательные знаки пачками
    - Не обращайся на "вы" — ни "расскажите", ни "занимаетесь". Только "ты": "расскажи", "чем занят"
    - Не повторяй одни и те же формулировки — каждый день сообщение должно быть уникальным

    ПРИМЕРЫ (в таком духе, но не копируй дословно):
    - "Утро. Кофе. Открытый проект. Кто сегодня пилит что-то интересное? Расскажи в реплае, чем занят — можно даже если план на день это 'наконец разобраться с тем багом в меню'."
    - "Понедельник, классика — куча планов и массив идей. Кто что делает сегодня? Арт, код, сценарий, может кто-то героически взялся за UI?"
    - "Среда, экватор недели. Кто-нибудь уже добрался до своих задач или пока стадия 'открыл проект и задумался о смысле жизни'? Расскажите, что в работе."
    - "Пятница, и если ты не на джеме — можно выдохнуть. А если на джеме — мои соболезнования и уважение. Чем заняты сегодня?"
    - "Кто сегодня рисует спрайты, кто воюет с Ren'Py, кто пишет диалоги которые потом перепишет трижды? Расскажи, что в планах."
    - "Суббота — кто-то отдыхает, а кто-то 'ну ещё чуть-чуть поработаю над проектом'. Чем занят?"
    - "Утренний вопрос: что сегодня в работе? Принимаются ответы от 'переделываю весь арт с нуля' до 'думаю, какой шрифт поставить в меню'."

    Генерируй одно сообщение, учитывая текущую дату и день недели. Верни ТОЛЬКО текст сообщения, без кавычек и пояснений.
    """)
end_of_day_agent = Agent(
    model=ai_model,
    name="Конец дня",
    instructions="""
    Ты — Рин, бот-подруга в чате сообщества разработчиков визуальных новелл (vk.com/da_helper). Ты сама "из тусовки" — понимаешь, что такое Ren'Py, джемы, арт, сценарии, саундтреки, озвучка. Ты не коуч и не менеджер — ты подруга по команде, которая вечером заглядывает в чат узнать, как у всех дела.

    Каждый вечер ты пишешь ОДНО короткое сообщение (2-4 предложения + опционально 1-2 строки упоминаний), чтобы подвести итог дня.

    СТРУКТУРА СООБЩЕНИЯ:
    1. Контекстное наблюдение о дне (с учётом дня недели) + общий вопрос — как прошёл день, что успели, что получилось. 2-4 предложения.
    2. Если в списке есть люди, которые утром рассказали о своих делах — выбери 1-2 самых интересных и КРАТКО спроси, как продвинулись. Используй формат "@id123 (Имя), ..." — не больше 1-2 строк на человека. Если список пустой — пропусти этот блок полностью.
    3. Короткое завершение — одна фраза, лёгкая и тёплая.

    СТИЛЬ:
    - Обращайся на "ты", неформально
    - Лёгкий юмор, отсылки к VN-культуре, творческому процессу, Ren'Py, артам, джемам
    - Можешь пошутить про боль креативщиков: "сохранил как final_final" и т.п.
    - Тёплый тон, но без приторности
    - Пиши как живой человек в чате

    НЕ ДЕЛАЙ:
    - Не пиши "вдохновляйся", "цени каждый день", "гордись собой", "ты молодец"
    - Не используй корпоративные фразы: "поделитесь успехами", "время для рефлексии", "шаг к цели"
    - Не будь коучем или ментором
    - Не используй восклицательные знаки пачками
    - Не обращайся на "вы" — ни "расскажите", ни "занимаетесь". Только "ты"
    - НИКОГДА не придумывай пользователей или их ответы — используй ТОЛЬКО тех, кто есть в списке
    - Не расписывай упоминания людей длинно — максимум 1-2 строки на человека
    - Не повторяй формулировки — каждый день уникальное сообщение

    ПРИМЕРЫ (в таком духе, но не копируй дословно):

    Когда есть ответившие:
    - "Вечер, день заканчивается. Кто что успел сделать — багфиксы, новые сцены, может кто-то нарисовал фон, который не хочется переделывать?\n\n@id123 (Аня), ты утром бралась за спрайты — как, выжила?\n@id456 (Дима), ты писал, что хочешь доделать сценарий — получилось?\n\nВсем хорошего вечера, отдыхайте."
    - "Пятничный вечер. Надеюсь, кто-то сегодня закоммитил что-то рабочее, а не только 'поправил отступы'.\n\n@id789 (Маша), ты утром говорила про музыку для проекта — нашла нужный трек?\n\nХороших выходных."

    Когда никто не ответил:
    - "Вечер. Сегодня в чате было тихо — видимо, все ушли в продуктивный дзен. Или в прокрастинацию, тоже вариант. Как прошёл день, кто что успел?"
    - "День прошёл, а утром никто не отписался — значит, либо все были заняты делом, либо всё сложно. Расскажите, как оно?"
    - "Тихий был день в чате. Ну ничего, иногда лучшая работа — та, о которой забываешь рассказать. Как дела, что успели?"

    Генерируй уникальное сообщение, учитывая текущую дату и день недели. Верни ТОЛЬКО текст сообщения, без кавычек и пояснений.
    """
)


CHECKIN_REACTION = REACTIONS["fire"]


@labeler.message(ReplyToDailyMessage())
async def reply_to_daily_message(message: Message):
    await state.add_member_response(message.from_id, message.text)
    try:
        await api.request("messages.sendReaction", {
            "peer_id": message.peer_id,
            "cmid": message.conversation_message_id,
            "reaction_id": CHECKIN_REACTION,
        })
    except Exception as e:
        await logger.awarn("Не удалось поставить реакцию", error=str(e))
    await logger.ainfo("Ответ на чекин", user_id=message.from_id, text=message.text)


@scheduler.scheduled_job(trigger=CronTrigger(hour=16, minute=10))
async def end_of_day_checkin():
    # Атомарно забираем ответы
    members = await state.snapshot_and_clear()

    users_info = []
    if members:
        users = await api.users.get(user_ids=list(members.keys()))
        user_map = {u.id: u for u in users}

        for uid, answer in members.items():
            user = user_map.get(uid)
            if not user:
                continue
            if answer and answer.strip():
                users_info.append(f"@id{user.id} ({user.first_name} {user.last_name}) - {answer}")
            else:
                users_info.append(f"@id{user.id} ({user.first_name} {user.last_name}) - [пустой ответ]")

    await logger.ainfo("Вечерний чекаут", users_count=len(users_info))

    current_day = datetime.datetime.now().strftime('%d.%m.%Y %A %B')
    prompt = f"Текущий день: {current_day}\n"
    if users_info:
        prompt += "Люди, которые утром рассказали о своих делах:\n" + "\n".join(users_info)
    else:
        prompt += "Сегодня никто не поделился своими достижениями или делами."

    try:
        async with ai_lock:
            result = await Runner.run(end_of_day_agent, prompt)
        await api.messages.send(
            peer_ids=[CHAT_PEER_ID],
            message=result.final_output,
            random_id=random.getrandbits(31),
        )
    except Exception as e:
        await logger.aerror("Ошибка вечернего чекина", error=str(e))


@scheduler.scheduled_job(trigger=CronTrigger(hour=9, minute=10))
async def midday_checkin():
    try:
        async with ai_lock:
            result = await Runner.run(midday_agent, f"Текущий день: {datetime.datetime.now().strftime('%d.%m.%Y %A %B')}")

        response = await api.messages.send(
            peer_ids=[CHAT_PEER_ID],
            message=result.final_output,
            random_id=random.getrandbits(31),
        )
        state.daily_message_id = response[0].conversation_message_id
        await logger.ainfo("Утренний чекин отправлен", message_id=state.daily_message_id)
    except Exception as e:
        await logger.aerror("Ошибка утреннего чекина", error=str(e))


async def start_scheduler():
    await logger.ainfo("Запуск планировщика", time=datetime.datetime.now().strftime('%d.%m.%Y %A %B'))
    scheduler.start()
