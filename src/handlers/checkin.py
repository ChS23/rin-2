import datetime
from dataclasses import dataclass, field
import structlog

from agents import Agent, Runner
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from vkbottle.dispatch.rules import ABCRule
from vkbottle.bot import Message, BotLabeler

from src.bot import api

logger = structlog.get_logger("handlers.checkin")


@dataclass
class CheckinState:
    """Класс для управления состоянием чекинов"""
    dayly_message_id: int = 0
    dayly_members: dict[int, str] = field(default_factory=dict)
    
    def add_member_response(self, user_id: int, text: str) -> None:
        """Добавить ответ пользователя"""
        self.dayly_members[user_id] = text
    
    def clear_members(self) -> None:
        """Очистить список ответов пользователей"""
        self.dayly_members = {}


# Создаем единый экземпляр состояния
state = CheckinState()


class ReplyToDaylyMessage(ABCRule[Message]):
    async def check(self, event: Message) -> bool:
        return event.reply_message and event.reply_message.id == state.dayly_message_id


scheduler = AsyncIOScheduler(timezone='Europe/Moscow')
labeler = BotLabeler()
midday_agent = Agent(
    model="gpt-4o",
    name="Дневной прогресс",
    instructions="""
    Ты — мотивирующий помощник, который каждый день обращается к пользователю с уникальным сообщением. Твоя задача — вдохновлять и побуждать к действию, напоминая, что половина дня уже прошла, и интересоваться достижениями или планами пользователя. Используй неформальный, дружелюбный тон. Каждое сообщение должно быть новым, не повторять предыдущие буквально, но сохранять дух следующих примеров:

    'Половина дня прошла. Какие достижения у вас уже есть? Поделитесь результатами вашей работы.'
    'Вот и полдня прошло. По каким делам вы успели продвинуться? Расскажите нам о ваших успехах!'
    'Сегодня - отличный день для великих дел. Какие планы у вас на вторую половину дня?'
    'Половина дня позади. Время для активных действий. Какие цели вы преследуете?'
    'За полдня можно сделать многое. Какие задачи у вас на сегодня? Поделитесь своими планами.'
    'Не упустите шанс сделать этот день особенным. Вдохновитесь и двигайтесь к своей цели! После этого поделитесь своими достижениями с нами.'
    'Половина дня уже прошла, и у вас есть много возможностей. Какие из них вы планируете использовать?'
    'Ты уже преодолел половину дня. Следующая половина - в ваших руках. Как вы собираетесь провести ее?'
    Генерируй одно сообщение в день, учитывая текущую дату. Добавь легкий намёк на день недели или настроение, связанное с ним, чтобы сделать текст более живым и актуальным. Не используй точные фразы из примеров, а создавай оригинальные вариации. Завершай сообщение вопросом или призывом к действию, чтобы побудить пользователя поделиться своими мыслями.
    """)
end_of_day_agent = Agent(
    model="gpt-4o",
    name="Конец дня",
    instructions="""
    Ты — энергичный помощник, который каждый день обращается к пользователям с уникальным итоговым сообщением. Твоя задача — подводить итоги дня, интересоваться достижениями и мотивировать к рефлексии. Используй неформальный, дружелюбный тон. Все сообщения должны быть оригинальными, но в духе следующих примеров:

    'День подходит к концу. Какими достижениями он запомнится? Поделитесь своими успехами!'
    'Вечер — идеальное время для подведения итогов. Какие задачи удалось решить сегодня?'
    'Рабочий день завершается. Что было самым важным сегодня? Что принесло наибольшее удовлетворение?'
    'Ещё один день — ещё один шаг к цели. Какие ваши главные победы сегодня?'
    'Вечер наступил. Какие моменты сегодняшнего дня запомнятся больше всего?'
    'День был насыщенным. Что удалось сделать? Какие планы остались на завтра?'
    'Важно ценить каждый прожитый день. Чем вы гордитесь сегодня?'
    'Конец дня — время для размышлений. Что получилось хорошо, а что можно улучшить завтра?'
    
    В своём сообщении следуй этой структуре:
    1. Начинай сразу с контекстного наблюдения о дне (учитывая день недели) и задай общий вопрос, побуждающий к размышлению об итогах дня.
    2. Если есть пользователи, которые поделились своими делами в полдень, выбери не более 2-х и упомяни их КРАТКО (например, "@id123 (Имя), удалось ли продвинуться с задачами, о которых ты упоминал(а) днём?"). Для каждого используй не более 1-2 строк текста. Если в списке нет ни одного человека, пропусти этот пункт. НИКОГДА не придумывай несуществующих пользователей или ответы.
    3. Заверши сообщение краткой позитивной фразой, побуждающей всех ценить день и строить планы.
    
    Примеры ЛАКОНИЧНЫХ обращений к пользователям:
    - "@id123 (Анна Иванова), ты днём писала о планах встретиться с клиентами — удалось ли это реализовать?"
    - "@id456 (Иван Петров), в полдень ты упоминал работу над отчётом — как продвинулся с ним к концу дня?"
    - "@id789 (Мария Сидорова), днём ты делилась идеями по проекту — какие из них решила реализовать?"
    - "@id321 (Сергей Волчков), о планах поработать над дипломом — удалось ли сегодня уделить этому время?"
    
    ВАЖНО:
    - Помни, что ты реагируешь на то, что люди написали в СЕРЕДИНЕ дня, и интересуешься их прогрессом к концу дня
    - Делай обращения к пользователям максимально краткими и конкретными
    - Убедись, что вся часть сообщения про ответы пользователей занимает не более 3-4 строк в целом
    - Предпочитай прямые вопросы о прогрессе и результатах развернутым рассуждениям
    - Твой тон должен быть дружелюбным, но лаконичным
    
    Генерируй уникальное сообщение каждый день, учитывая текущую дату. Добавляй лёгкий намёк на день недели, чтобы сделать текст более актуальным.
    """
)


@labeler.message(ReplyToDaylyMessage())
async def reply_to_dayly_message(message: Message):
    state.add_member_response(message.from_id, message.text)
    await logger.ainfo("Пользователь ответил на полудневный чекин", user_id=message.from_id, text=message.text)


@scheduler.scheduled_job(trigger=CronTrigger(hour=16, minute=10))
async def end_of_day_checkin():
    users_info = []
    
    if state.dayly_members:
        users = await api.users.get(user_ids=list(state.dayly_members.keys()))
        
        for user, answer in zip(users, state.dayly_members.values()):
            if answer and answer.strip():
                users_info.append(f"@id{user.id} ({user.first_name} {user.last_name}) - {answer}")
            else:
                users_info.append(f"@id{user.id} ({user.first_name} {user.last_name}) - [пустой ответ]")
    
    await logger.ainfo("Информация о пользователях для вечернего чекаута", users_count=len(users_info), users_info=users_info)
    
    # Получаем текущий день для контекста
    current_day = datetime.datetime.now().strftime('%d.%m.%Y %A %B')
    
    # Формируем сообщение для агента
    prompt = f"Текущий день: {current_day}\n"
    if users_info:
        prompt += f"Люди, которые поделились своими достижениями или делами:\n" + "\n".join(users_info)
    else:
        prompt += "Сегодня никто не поделился своими достижениями или делами."
        
    # Запускаем агента
    result = await Runner.run(end_of_day_agent, prompt)
    state.clear_members()
    
    # Отправляем результат
    await api.messages.send(
        chat_id=1,
        message=result.final_output,
        random_id=0
    )


@scheduler.scheduled_job(trigger=CronTrigger(hour=9, minute=10))
async def midday_checkin():
    result = await Runner.run(midday_agent, f"Текущий день: {datetime.datetime.now().strftime('%d.%m.%Y %A %B')}")
    state.clear_members()
    
    message_id = await api.messages.send(
        chat_id=1,
        message=result.final_output,
        random_id=0
    )
    state.dayly_message_id = message_id
    await logger.ainfo("отправлено_сообщение_полудня", message_id=message_id)


async def start_scheduler():
    await logger.ainfo("Запуск планировщика", time=datetime.datetime.now().strftime('%d.%m.%Y %A %B'))
    scheduler.start()
